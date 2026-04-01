"""
SPECTER Store — shared gevent-safe mutable state.

Gevent-safe shared mutable state. Wraps a dict behind a ``BoundedSemaphore``
and fans out updates to subscribers via the bus.
"""

import logging

from gevent.lock import BoundedSemaphore

from .bus import bus

logger = logging.getLogger(__name__)


class Store:
    """Named shared state container with subscriptions."""

    def __init__(
        self,
        name,
        initial_state=None,
        *,
        emit_events=False,
        change_event=None,
    ):
        self.name = name
        self._state = dict(initial_state or {})
        self._lock = BoundedSemaphore(1)
        self._subscribers = set()
        self._emit_events = bool(emit_events)
        self._change_event = change_event or f'{name}:changed'

    def get(self, key=None, default=None):
        """
        Return a state value by key, or a full snapshot when ``key`` is omitted.
        """
        with self._lock:
            if key is None:
                return dict(self._state)
            return self._state.get(key, default)

    def snapshot(self):
        """Return a shallow copy of the current state."""
        return self.get()

    def access(self, reader):
        """
        Read state atomically without notifying subscribers.

        The callback receives the current state mapping and should treat it as
        read-only.
        """
        if not callable(reader):
            raise TypeError("[SPECTER:store] access() requires a callable reader")

        with self._lock:
            return reader(self._state)

    def set(self, partial):
        """Shallow-merge a dict into the current state."""
        if not isinstance(partial, dict):
            raise TypeError(
                f"[SPECTER:store] set() requires a dict, got "
                f"{type(partial).__name__}"
            )

        with self._lock:
            self._state.update(partial)
            snapshot = dict(self._state)

        self._notify(snapshot)
        return snapshot

    def replace(self, state):
        """Replace the entire state with a new mapping."""
        if not isinstance(state, dict):
            raise TypeError(
                f"[SPECTER:store] replace() requires a dict, got "
                f"{type(state).__name__}"
            )

        with self._lock:
            self._state = dict(state)
            snapshot = dict(self._state)

        self._notify(snapshot)
        return snapshot

    def delete(self, *keys):
        """Delete one or more keys from the state if present."""
        changed = False
        with self._lock:
            for key in keys:
                if key in self._state:
                    del self._state[key]
                    changed = True
            snapshot = dict(self._state)

        if changed:
            self._notify(snapshot)
        return snapshot

    def clear(self):
        """Clear all state."""
        return self.replace({})

    def update(self, mutator):
        """
        Atomically update state using a mutator callback.

        The callback receives a mutable draft dict. If it returns a dict, that
        dict becomes the new state. Otherwise the mutated draft is committed.
        """
        if not callable(mutator):
            raise TypeError("[SPECTER:store] update() requires a callable mutator")

        with self._lock:
            draft = dict(self._state)
            replacement = mutator(draft)
            if replacement is not None:
                if not isinstance(replacement, dict):
                    raise TypeError(
                        "[SPECTER:store] update() mutator must return a dict "
                        "or None"
                    )
                draft = dict(replacement)
            self._state = draft
            snapshot = dict(self._state)

        self._notify(snapshot)
        return snapshot

    def subscribe(self, fn, immediate=False, owner=None):
        """
        Subscribe to state changes.

        Callbacks receive ``(snapshot, store)``.
        """
        self._subscribers.add(fn)
        unsub = lambda: self._subscribers.discard(fn)

        if owner is not None:
            if not hasattr(owner, 'add_cleanup') or not callable(owner.add_cleanup):
                raise TypeError(
                    f"[SPECTER:store] owner for '{self.name}' must expose "
                    f"add_cleanup(). Got {type(owner).__name__}."
                )
            owner.add_cleanup(unsub)

        if immediate:
            try:
                fn(self.snapshot(), self)
            except Exception as e:
                logger.warning(
                    f"[SPECTER:store] Error in immediate subscriber for "
                    f"'{self.name}': {e}"
                )

        return unsub

    def watch(self, fn, immediate=True):
        """Alias for ``subscribe(fn, immediate=immediate)``."""
        return self.subscribe(fn, immediate=immediate)

    def destroy(self):
        """Reset store state and remove all subscribers."""
        with self._lock:
            self._state.clear()
        self._subscribers.clear()
        logger.debug(f"[SPECTER:store] '{self.name}' destroyed")

    def _notify(self, snapshot):
        for subscriber in list(self._subscribers):
            try:
                subscriber(dict(snapshot), self)
            except Exception as e:
                logger.warning(
                    f"[SPECTER:store] Error in subscriber for "
                    f"'{self.name}': {e}"
                )

        if self._emit_events:
            bus.emit(self._change_event, dict(snapshot))

    def __repr__(self):
        return f"<Store '{self.name}' keys={len(self._state)}>"


def create_store(name, initial_state=None, **kwargs):
    """Factory for creating a :class:`Store`."""
    return Store(name, initial_state=initial_state, **kwargs)
