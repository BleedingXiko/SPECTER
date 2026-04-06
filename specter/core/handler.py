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
SPECTER Handler — Lifecycle-managed socket event handler.

Lifecycle-managed socket event handler for Flask-SocketIO.  A Handler
declares which socket events it handles and registers them through
Flask-SocketIO.

A Handler:
    - Declares socket events in the ``events`` dict
    - Has ``setup()`` / ``teardown()`` lifecycle
    - Can resolve services from the registry
    - Can be adopted by a Service or ServiceManager

Lifecycle::

    Handler(name)
      setup(socketio)  →  register socket events from `events` dict
                       →  on_setup()
      teardown()       →  on_teardown()
                       →  unregister socket events

The ``events`` dict maps socket event names to method names::

    class TVHandler(Handler):
        name = 'tv'
        events = {
            'tv_connected': 'handle_tv_connected',
            'cast_media_to_tv': 'handle_cast_media',
        }

        def handle_tv_connected(self, data=None):
            hdmi = self.require('hdmi_runtime_service')
            # ... business logic ...
"""

import logging

from .registry import registry
from .ownership import resolve_cleanup

logger = logging.getLogger(__name__)


class Handler:
    """
    Base class for lifecycle-managed socket event handlers.

    Subclass this and define:
        - ``name``: Human-readable handler name
        - ``events``: Dict mapping socket event names to method names

    Then override ``on_setup()`` / ``on_teardown()`` for additional
    lifecycle behavior.
    """

    name = 'unnamed_handler'
    events = {}

    def __init__(self):
        self._socketio = None
        self._registered = False
        self._registered_handlers = []  # [(event, handler_fn)]
        self._cleanups = []

    # ------------------------------------------------------------------
    # Lifecycle Hooks (Override These)
    # ------------------------------------------------------------------

    def on_setup(self):
        """
        Called after all socket events from ``events`` are registered.

        Override to perform additional setup (e.g. bus subscriptions,
        one-time initialization).
        """

    def on_teardown(self):
        """
        Called before socket events are unregistered.

        Override to perform pre-teardown work.
        """

    # ------------------------------------------------------------------
    # Lifecycle Control
    # ------------------------------------------------------------------

    def setup(self, socketio):
        """
        Register all declared socket events with Flask-SocketIO.

        Idempotent — safe to call multiple times.

        Args:
            socketio: The Flask-SocketIO instance.

        Returns:
            ``self`` for chaining.
        """
        if self._registered:
            return self

        self._socketio = socketio

        ingress = registry.resolve('socket_ingress')
        for event_name, method_name in self.events.items():
            method = getattr(self, method_name, None)
            if method is None:
                logger.error(
                    f"[SPECTER:handler] Handler '{self.name}' declares event "
                    f"'{event_name}' → method '{method_name}', but the method "
                    f"does not exist."
                )
                continue

            if not callable(method):
                logger.error(
                    f"[SPECTER:handler] Handler '{self.name}' attribute "
                    f"'{method_name}' is not callable."
                )
                continue

            priority = getattr(self, 'event_priorities', {}).get(event_name, 100)
            if ingress is not None:
                ingress.subscribe(
                    event_name,
                    method,
                    owner=self,
                    priority=priority,
                )
            else:
                socketio.on(event_name)(method)
            self._registered_handlers.append((event_name, method))

        self._registered = True
        logger.info(
            f"[SPECTER] Handler '{self.name}' set up "
            f"({len(self._registered_handlers)} events)"
        )

        try:
            self.on_setup()
        except Exception as e:
            logger.error(
                f"[SPECTER] Handler '{self.name}' on_setup failed: {e}",
                exc_info=True,
            )

        return self

    def teardown(self):
        """
        Unregister all socket events.

        Idempotent — safe to call multiple times.

        Returns:
            ``self`` for chaining.
        """
        if not self._registered:
            return self

        try:
            self.on_teardown()
        except Exception as e:
            logger.error(
                f"[SPECTER] Handler '{self.name}' on_teardown failed: {e}",
                exc_info=True,
            )

        while self._cleanups:
            fn = self._cleanups.pop()
            try:
                fn()
            except Exception as e:
                logger.warning(
                    f"[SPECTER] Failed to run cleanup for handler "
                    f"'{self.name}': {e}"
                )

        # Cleanup callbacks remove ingress subscriptions when available.
        # If we registered directly against Flask-SocketIO, there is still
        # no clean unregister API, so those direct handlers remain
        # process-lifetime registrations.
        self._registered_handlers.clear()
        self._registered = False
        self._socketio = None

        logger.info(f"[SPECTER] Handler '{self.name}' torn down")
        return self

    def stop(self):
        """Alias for teardown(), enables Service.own(handler)."""
        return self.teardown()

    # ------------------------------------------------------------------
    # Registry Access (convenience)
    # ------------------------------------------------------------------

    def add_cleanup(self, fn):
        """Register a cleanup callback for handler teardown."""
        if callable(fn):
            self._cleanups.append(fn)
        return self

    def own(self, resource, stop_method=None):
        """Own a non-Handler resource for teardown cleanup."""
        if resource is None:
            return None
        self.add_cleanup(resolve_cleanup(resource, stop_method=stop_method))
        return resource

    def require(self, key):
        """
        Resolve a service from the registry.  Throws if not found.

        Args:
            key: Registry key to look up.

        Returns:
            The registered value.

        Raises:
            KeyError: If the key is not in the registry.
        """
        return registry.require(key)

    def resolve(self, key):
        """
        Resolve a service from the registry.  Returns ``None`` if not found.

        Args:
            key: Registry key to look up.
        """
        return registry.resolve(key)

    @property
    def registered(self):
        """``True`` if the handler has been set up."""
        return self._registered

    @property
    def socketio(self):
        """The Flask-SocketIO instance (available after ``setup()``)."""
        return self._socketio
