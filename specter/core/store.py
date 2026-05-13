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
SPECTER Store — shared gevent-safe mutable state.

Gevent-safe shared mutable state. Wraps a dict behind a ``BoundedSemaphore``
and fans out updates to subscribers via the bus.
"""

import logging
from typing import Any, Callable, Dict, Iterable, Optional, Set, TypeVar, Union, overload

from gevent.lock import BoundedSemaphore

from .bus import bus
from ._typing import CleanupOwner, StateDict, StateSubscriber, Unsubscribe

logger = logging.getLogger(__name__)

R = TypeVar('R')


class Store:
    """Named shared state container with subscriptions."""

    def __init__(
        self,
        name: str,
        initial_state: Optional[Dict[str, Any]] = None,
        *,
        emit_events: bool = False,
        change_event: Optional[str] = None,
    ) -> None:
        self.name = name
        self._state: StateDict = dict(initial_state or {})
        self._lock = BoundedSemaphore(1)
        self._subscribers: Set[StateSubscriber] = set()
        self._emit_events = bool(emit_events)
        self._change_event = change_event or f'{name}:changed'

    @overload
    def get(self) -> StateDict:
        ...

    @overload
    def get(self, key: str, default: Any = None) -> Any:
        ...

    def get(self, key: Optional[str] = None, default: Any = None) -> Any:
        """
        Return a state value by key, or a full snapshot when ``key`` is omitted.
        """
        with self._lock:
            if key is None:
                return dict(self._state)
            return self._state.get(key, default)

    def snapshot(self) -> StateDict:
        """Return a shallow copy of the current state."""
        return self.get()

    def access(self, reader: Callable[[StateDict], R]) -> R:
        """
        Read state atomically without notifying subscribers.

        The callback receives the current state mapping and should treat it as
        read-only.
        """
        if not callable(reader):
            raise TypeError("[SPECTER:store] access() requires a callable reader")

        with self._lock:
            return reader(self._state)

    def set(self, partial: Dict[str, Any]) -> StateDict:
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

    def replace(self, state: Dict[str, Any]) -> StateDict:
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

    def delete(self, *keys: str) -> StateDict:
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

    def clear(self) -> StateDict:
        """Clear all state."""
        return self.replace({})

    def update(self, mutator: Callable[[StateDict], Optional[Dict[str, Any]]]) -> StateDict:
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

    def subscribe(
        self,
        fn: StateSubscriber,
        immediate: bool = False,
        owner: Optional[CleanupOwner] = None,
    ) -> Unsubscribe:
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

    def watch(self, fn: StateSubscriber, immediate: bool = True) -> Unsubscribe:
        """Alias for ``subscribe(fn, immediate=immediate)``."""
        return self.subscribe(fn, immediate=immediate)

    def destroy(self) -> None:
        """Reset store state and remove all subscribers."""
        with self._lock:
            self._state.clear()
        self._subscribers.clear()
        logger.debug(f"[SPECTER:store] '{self.name}' destroyed")

    def _notify(self, snapshot: StateDict) -> None:
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

    def __repr__(self) -> str:
        return f"<Store '{self.name}' keys={len(self._state)}>"


def create_store(
    name: str,
    initial_state: Optional[Dict[str, Any]] = None,
    **kwargs: Any,
) -> Store:
    """Factory for creating a :class:`Store`."""
    return Store(name, initial_state=initial_state, **kwargs)
