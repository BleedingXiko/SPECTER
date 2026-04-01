"""
SPECTER Cache — Shared state with TTL and dependency-based invalidation.

Shared state with TTL expiry, gevent-safe locking, and automatic cache
cascade through the bus.

Dependency cascade:
    When a cache with ``depends_on=['storage:mounts']`` is created, it
    auto-subscribes to that bus event.  When that event fires, the cache
    invalidates itself and emits ``'{name}:invalidated'`` on the bus —
    which in turn triggers any caches that depend on *it*.

    Example chain:
        storage:mounts  →  drives_cache invalidates
                            →  emits 'drives:invalidated'
                                →  categories_cache invalidates
                                    →  emits 'categories:invalidated'
                                        →  hidden_files_cache invalidates

Concurrency:
    All read/write operations are protected by a ``BoundedSemaphore``
    (gevent-aware).  Never use ``threading.Lock``.
"""

import time
import logging
from gevent.lock import BoundedSemaphore

from .bus import bus

logger = logging.getLogger(__name__)


class Cache:
    """
    Named cache with TTL and dependency-based cascade invalidation.

    Do not instantiate directly — use :func:`create_cache`.
    """

    def __init__(self, name, ttl=0, depends_on=None):
        """
        Args:
            name: Unique cache name (used in bus events and logging).
            ttl: Time-to-live in seconds.  0 means no auto-expiry.
            depends_on: List of bus event names that trigger invalidation.
        """
        self.name = name
        self._ttl = max(0.0, float(ttl or 0))
        self._entry_ttl = self._ttl
        self._value = None
        self._updated_at = 0
        self._lock = BoundedSemaphore(1)
        self._invalidate_callbacks = []
        self._bus_unsubs = []

        # Subscribe to dependency events
        if depends_on:
            for event in depends_on:
                unsub = bus.on(event, self._on_dependency_invalidated)
                self._bus_unsubs.append(unsub)

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def get(self, default=None):
        """
        Return cached value, or ``default`` if expired/empty.

        This is a non-blocking read protected by a gevent lock.
        """
        with self._lock:
            if self._value is None:
                return default
            if (
                self._entry_ttl > 0 and
                (time.time() - self._updated_at) >= self._entry_ttl
            ):
                # Expired — clear in place
                self._value = None
                self._updated_at = 0
                self._entry_ttl = self._ttl
                return default
            return self._value

    def is_valid(self):
        """Return ``True`` if the cache has a non-expired value."""
        with self._lock:
            if self._value is None:
                return False
            if (
                self._entry_ttl > 0 and
                (time.time() - self._updated_at) >= self._entry_ttl
            ):
                return False
            return True

    @property
    def updated_at(self):
        """Timestamp of the last ``set()`` call (epoch seconds)."""
        return self._updated_at

    @property
    def ttl(self):
        """Configured TTL in seconds.  0 means no auto-expiry."""
        return self._ttl

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def set(self, value, ttl=None):
        """
        Set the cached value.

        Args:
            value: The value to cache.
            ttl: Optional TTL override for this specific write.
                 Does not change the cache's default TTL.
        """
        with self._lock:
            self._value = value
            self._updated_at = time.time()
            self._entry_ttl = self._ttl if ttl is None else max(0.0, float(ttl))

    def get_or_compute(self, factory, ttl=None):
        """
        Return cached value if valid, otherwise compute it using
        ``factory()`` and cache the result.

        This is atomic — only one greenlet runs the factory at a time.

        Args:
            factory: Callable that returns the value to cache.
            ttl: Optional TTL override.

        Returns:
            The cached or freshly computed value.
        """
        # Fast path: check without lock contention
        value = self.get()
        if value is not None:
            return value

        # Slow path: compute under lock
        with self._lock:
            # Double-check after acquiring lock
            if self._value is not None:
                if (
                    self._entry_ttl == 0 or
                    (time.time() - self._updated_at) < self._entry_ttl
                ):
                    return self._value

            computed = factory()
            self._value = computed
            self._updated_at = time.time()
            self._entry_ttl = self._ttl if ttl is None else max(0.0, float(ttl))
            return computed

    # ------------------------------------------------------------------
    # Invalidation
    # ------------------------------------------------------------------

    def invalidate(self):
        """
        Clear the cache and emit ``'{name}:invalidated'`` on the bus.

        Any caches that ``depend_on`` this event will cascade-invalidate.
        Also fires registered ``on_invalidate`` callbacks.
        """
        with self._lock:
            was_valid = self._value is not None
            self._value = None
            self._updated_at = 0
            self._entry_ttl = self._ttl

        if was_valid:
            logger.info(f"[SPECTER:cache] '{self.name}' invalidated")

        # Fire invalidation callbacks
        for cb in list(self._invalidate_callbacks):
            try:
                cb()
            except Exception as e:
                logger.error(
                    f"[SPECTER:cache] Error in invalidation callback "
                    f"for '{self.name}': {e}",
                    exc_info=True,
                )

        # Cascade: emit so dependent caches can react
        bus.emit(f'{self.name}:invalidated')

    def on_invalidate(self, callback):
        """
        Subscribe to invalidation events for this cache.

        Args:
            callback: Callable (no arguments) invoked when the cache
                      is invalidated.

        Returns:
            An unsubscribe function.
        """
        self._invalidate_callbacks.append(callback)
        return lambda: (
            self._invalidate_callbacks.remove(callback)
            if callback in self._invalidate_callbacks else None
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def destroy(self):
        """
        Tear down the cache: unsubscribe from all bus events,
        clear callbacks, and reset state.  Called by the framework
        when the owning service stops.
        """
        for unsub in self._bus_unsubs:
            try:
                unsub()
            except Exception:
                pass
        self._bus_unsubs.clear()
        self._invalidate_callbacks.clear()
        with self._lock:
            self._value = None
            self._updated_at = 0
            self._entry_ttl = self._ttl
        logger.debug(f"[SPECTER:cache] '{self.name}' destroyed")

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _on_dependency_invalidated(self, data=None):
        """Bus callback: a dependency was invalidated, so we invalidate too."""
        logger.debug(
            f"[SPECTER:cache] '{self.name}' dependency triggered, invalidating"
        )
        self.invalidate()

    def __repr__(self):
        valid = self.is_valid()
        return (
            f"<Cache '{self.name}' valid={valid} "
            f"ttl={self._ttl}s>"
        )


def create_cache(name, ttl=0, depends_on=None):
    """
    Factory for creating a named cache.

    Args:
        name: Unique cache name.
        ttl: Time-to-live in seconds.  ``0`` = no auto-expiry.
        depends_on: List of bus event names that trigger invalidation.
                    Convention: ``'{cache_name}:invalidated'``
                    or domain events like ``'storage:mounts'``.

    Returns:
        A :class:`Cache` instance.

    Example::

        category_cache = create_cache(
            'categories',
            ttl=86400,
            depends_on=['storage:mounts'],
        )

        # Set
        category_cache.set(categories_list)

        # Get (returns None if expired)
        cached = category_cache.get()

        # Auto-invalidates when 'storage:mounts' fires on the bus
    """
    if depends_on is None:
        depends_on = []
    return Cache(name=name, ttl=ttl, depends_on=depends_on)
