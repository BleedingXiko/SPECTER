# Copyright 2026 BleedingXiko
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
SPECTER Registry — Lifecycle-aware service locator.

Lifecycle-aware service locator. Use it to wire cross-service dependencies
at the composition root.

Ownership rules:
    - Pass an ``owner`` to ``provide()`` so the registry auto-unregisters
      when the owner stops.
    - Use ``wait_for()`` (gevent-friendly) when a service may not be
      registered yet.
    - Read via ``registry.resolve(key)`` or ``registry.require(key)``.
    - Write only via ``registry.provide()`` — never mutate internals.

Concurrency:
    All methods are gevent-safe via ``BoundedSemaphore``.
    ``wait_for()`` uses ``gevent.event.AsyncResult`` so it yields
    cooperatively instead of busy-waiting.
"""

import logging
from gevent.lock import BoundedSemaphore
from gevent.event import AsyncResult
from gevent.timeout import Timeout as GeventTimeout

logger = logging.getLogger(__name__)


class SPECTERRegistry:
    """Lifecycle-aware service registry.  Singleton exported as ``registry``."""

    def __init__(self):
        self._entries = {}       # key -> value
        self._owners = {}        # key -> owner (for lifecycle tracking)
        self._waiters = {}       # key -> list of AsyncResult
        self._lock = BoundedSemaphore(1)

    # ------------------------------------------------------------------
    # Provide / Unregister
    # ------------------------------------------------------------------

    def provide(self, key, value, owner=None, replace=False):
        """
        Register a value under a key.

        Args:
            key: Unique string key
            value: The service/cache/object to register
            owner: Optional ``Service`` instance.  When the owner calls
                   ``stop()``, this key is automatically unregistered.
            replace: If ``True``, overwrites an existing key silently.

        Returns:
            The registered value (for chaining convenience).

        Raises:
            KeyError: If key already exists and ``replace`` is ``False``.
            TypeError: If ``owner`` is given but has no ``add_cleanup()``.
        """
        key = self._validate_key(key, 'provide')

        if owner is not None:
            if not hasattr(owner, 'add_cleanup') or not callable(owner.add_cleanup):
                raise TypeError(
                    f"[SPECTER:registry] owner for '{key}' must expose "
                    f"add_cleanup(). Got {type(owner).__name__}."
                )

        with self._lock:
            if key in self._entries and not replace:
                raise KeyError(
                    f"[SPECTER:registry] '{key}' is already provided. "
                    f"Pass replace=True to overwrite."
                )

            self._entries[key] = value
            self._owners[key] = owner

            # Auto-unregister when owner stops
            if owner is not None:
                owner.add_cleanup(lambda k=key: self.unregister(k))

            # Resolve anyone waiting for this key
            waiters = self._waiters.pop(key, [])

        # Resolve outside the lock to avoid deadlock if a waiter
        # immediately calls back into the registry.
        for ar in waiters:
            ar.set(value)

        logger.debug(f"[SPECTER:registry] Provided '{key}'")
        return value

    def unregister(self, key):
        """
        Remove a registration.

        Args:
            key: The key to remove.

        Returns:
            ``True`` if the key was found and removed, ``False`` otherwise.
        """
        key = self._validate_key(key, 'unregister')
        with self._lock:
            if key not in self._entries:
                return False
            del self._entries[key]
            self._owners.pop(key, None)
        logger.debug(f"[SPECTER:registry] Unregistered '{key}'")
        return True

    # ------------------------------------------------------------------
    # Lookup
    # ------------------------------------------------------------------

    def resolve(self, key):
        """
        Look up a value.

        Returns:
            The registered value, or ``None`` if not found.
        """
        key = self._validate_key(key, 'resolve')
        with self._lock:
            return self._entries.get(key)

    def require(self, key):
        """
        Look up a value.  Raises if the key is not registered.

        Returns:
            The registered value.

        Raises:
            KeyError: If the key is not registered.
        """
        key = self._validate_key(key, 'require')
        with self._lock:
            value = self._entries.get(key)
        if value is None:
            raise KeyError(
                f"[SPECTER:registry] '{key}' is not provided. "
                f"Currently registered: {list(self._entries.keys())}"
            )
        return value

    def has(self, key):
        """Return ``True`` if the key is registered."""
        key = self._validate_key(key, 'has')
        with self._lock:
            return key in self._entries

    def list(self):
        """Return a list of all registered keys."""
        with self._lock:
            return list(self._entries.keys())

    # ------------------------------------------------------------------
    # Wait
    # ------------------------------------------------------------------

    def wait_for(self, key, timeout=None):
        """
        Block (gevent-friendly) until a key is provided.

        If the key is already registered, returns immediately.
        Uses ``gevent.event.AsyncResult`` so this yields cooperatively
        — it does NOT busy-wait.

        Args:
            key: The key to wait for.
            timeout: Optional timeout in seconds.  ``None`` = wait forever.

        Returns:
            The registered value.

        Raises:
            TimeoutError: If ``timeout`` expires before the key is provided.
        """
        key = self._validate_key(key, 'wait_for')

        with self._lock:
            if key in self._entries:
                return self._entries[key]

            # Create an AsyncResult for this waiter
            ar = AsyncResult()
            if key not in self._waiters:
                self._waiters[key] = []
            self._waiters[key].append(ar)

        # Wait outside the lock (gevent-cooperative)
        try:
            result = ar.get(timeout=timeout)
        except GeventTimeout as exc:
            raise TimeoutError(
                f"[SPECTER:registry] wait_for('{key}') timed out "
                f"after {timeout}s"
            ) from exc
        finally:
            self._remove_waiter(key, ar)

        if result is None and not self.has(key):
            raise TimeoutError(
                f"[SPECTER:registry] wait_for('{key}') timed out "
                f"after {timeout}s"
            )
        return result

    # ------------------------------------------------------------------
    # Housekeeping
    # ------------------------------------------------------------------

    def clear(self):
        """Clear all registrations.  Primarily for tests."""
        with self._lock:
            self._entries.clear()
            self._owners.clear()
            # Reject all pending waiters
            for key, waiters in self._waiters.items():
                for ar in waiters:
                    ar.set_exception(
                        RuntimeError(
                            f"[SPECTER:registry] Registry was cleared while "
                            f"waiting for '{key}'"
                        )
                    )
            self._waiters.clear()
        logger.debug("[SPECTER:registry] Cleared all entries")

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_key(key, method_name):
        if not isinstance(key, str) or not key.strip():
            raise TypeError(
                f"[SPECTER:registry] {method_name}() requires a "
                f"non-empty string key, got {key!r}"
            )
        return key.strip()

    def _remove_waiter(self, key, waiter):
        with self._lock:
            waiters = self._waiters.get(key)
            if not waiters:
                return

            try:
                waiters.remove(waiter)
            except ValueError:
                return

            if not waiters:
                self._waiters.pop(key, None)


# Singleton
registry = SPECTERRegistry()
