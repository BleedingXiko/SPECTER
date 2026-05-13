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
SPECTER Model — structured shared state graph with selectors.

This is the next step above ``Store``: a nested state model for coordinated
application/runtime state, with path-based updates and selector subscriptions.
"""

import copy
import logging
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union, overload

from gevent.lock import BoundedSemaphore

from .bus import bus
from ._typing import CleanupOwner, StateDict, StateSubscriber, Unsubscribe

logger = logging.getLogger(__name__)

PathLike = Union[str, Sequence[str]]
Selector = Union[PathLike, Callable[[StateDict], Any]]


class Model:
    """Named nested state model with selector-aware subscriptions."""

    def __init__(
        self,
        name: str,
        initial_state: Optional[Dict[str, Any]] = None,
        *,
        emit_events: bool = False,
        change_event: Optional[str] = None,
    ) -> None:
        self.name = name
        self._state: StateDict = copy.deepcopy(initial_state or {})
        self._lock = BoundedSemaphore(1)
        self._subscribers: List[Dict[str, Any]] = []
        self._emit_events = bool(emit_events)
        self._change_event = change_event or f'{name}:changed'

    def snapshot(self) -> StateDict:
        """Return a deep copy of the entire model state."""
        with self._lock:
            return copy.deepcopy(self._state)

    @overload
    def get(self) -> StateDict:
        ...

    @overload
    def get(self, path: PathLike, default: Any = None) -> Any:
        ...

    def get(self, path: Optional[PathLike] = None, default: Any = None) -> Any:
        """
        Read the full state or a nested path.

        Paths can be:
        - ``"tv.playback.current_time"``
        - ``("tv", "playback", "current_time")``
        - ``["tv", "playback", "current_time"]``
        """
        with self._lock:
            if path is None:
                return copy.deepcopy(self._state)
            value = _get_path(self._state, _normalize_path(path), default=default)
            return copy.deepcopy(value)

    def select(self, selector: Selector, default: Any = None) -> Any:
        """
        Read derived state using a selector callable or a path expression.
        """
        if callable(selector):
            return selector(self.snapshot())
        return self.get(selector, default=default)

    def set(self, path: PathLike, value: Any) -> StateDict:
        """Set a nested path and notify subscribers."""
        normalized = _normalize_path(path)

        with self._lock:
            _set_path(self._state, normalized, copy.deepcopy(value))
            snapshot = copy.deepcopy(self._state)

        self._notify(snapshot)
        return snapshot

    def patch(self, partial: Dict[str, Any]) -> StateDict:
        """Shallow-merge at the model root."""
        if not isinstance(partial, dict):
            raise TypeError(
                f"[SPECTER:model] patch() requires a dict, got "
                f"{type(partial).__name__}"
            )

        with self._lock:
            self._state.update(copy.deepcopy(partial))
            snapshot = copy.deepcopy(self._state)

        self._notify(snapshot)
        return snapshot

    def update(self, mutator: Callable[[StateDict], Optional[Dict[str, Any]]]) -> StateDict:
        """
        Atomically update the full model using a draft mutator.
        """
        if not callable(mutator):
            raise TypeError("[SPECTER:model] update() requires a callable mutator")

        with self._lock:
            draft = copy.deepcopy(self._state)
            replacement = mutator(draft)
            if replacement is not None:
                if not isinstance(replacement, dict):
                    raise TypeError(
                        "[SPECTER:model] update() mutator must return a dict "
                        "or None"
                    )
                draft = copy.deepcopy(replacement)
            self._state = draft
            snapshot = copy.deepcopy(self._state)

        self._notify(snapshot)
        return snapshot

    def delete(self, path: PathLike) -> StateDict:
        """Delete a nested path if present."""
        normalized = _normalize_path(path)

        with self._lock:
            changed = _delete_path(self._state, normalized)
            snapshot = copy.deepcopy(self._state)

        if changed:
            self._notify(snapshot)
        return snapshot

    def clear(self) -> StateDict:
        """Reset the model to an empty dict."""
        with self._lock:
            self._state = {}
            snapshot: StateDict = {}

        self._notify(snapshot)
        return snapshot

    def subscribe(
        self,
        fn: StateSubscriber,
        *,
        selector: Optional[Selector] = None,
        immediate: bool = False,
        owner: Optional[CleanupOwner] = None,
    ) -> Unsubscribe:
        """
        Subscribe to full-state or selector-specific changes.

        Callbacks receive ``(value, model)`` where ``value`` is either the full
        snapshot or the selected slice.
        """
        initial_value = (
            self.select(selector) if selector is not None else self.snapshot()
        )
        record = {
            'fn': fn,
            'selector': selector,
            'last_value': copy.deepcopy(initial_value),
        }
        self._subscribers.append(record)

        def _unsubscribe() -> None:
            try:
                self._subscribers.remove(record)
            except ValueError:
                pass

        if owner is not None:
            if not hasattr(owner, 'add_cleanup') or not callable(owner.add_cleanup):
                raise TypeError(
                    f"[SPECTER:model] owner for '{self.name}' must expose "
                    f"add_cleanup(). Got {type(owner).__name__}."
                )
            owner.add_cleanup(_unsubscribe)

        if immediate:
            try:
                fn(copy.deepcopy(initial_value), self)
            except Exception as e:
                logger.warning(
                    f"[SPECTER:model] Error in immediate subscriber for "
                    f"'{self.name}': {e}"
                )

        return _unsubscribe

    def watch(
        self,
        fn: StateSubscriber,
        *,
        selector: Optional[Selector] = None,
        immediate: bool = True,
    ) -> Unsubscribe:
        """Alias for ``subscribe(..., immediate=True)``."""
        return self.subscribe(fn, selector=selector, immediate=immediate)

    def destroy(self) -> None:
        """Clear model state and subscribers."""
        with self._lock:
            self._state = {}
        self._subscribers.clear()
        logger.debug(f"[SPECTER:model] '{self.name}' destroyed")

    def _notify(self, snapshot: StateDict) -> None:
        for subscriber in list(self._subscribers):
            selector = subscriber['selector']
            try:
                if selector is None:
                    current_value = copy.deepcopy(snapshot)
                elif callable(selector):
                    current_value = selector(copy.deepcopy(snapshot))
                else:
                    current_value = copy.deepcopy(
                        _get_path(snapshot, _normalize_path(selector))
                    )

                if current_value == subscriber['last_value']:
                    continue

                subscriber['last_value'] = copy.deepcopy(current_value)
                subscriber['fn'](current_value, self)
            except Exception as e:
                logger.warning(
                    f"[SPECTER:model] Error in subscriber for "
                    f"'{self.name}': {e}"
                )

        if self._emit_events:
            bus.emit(self._change_event, copy.deepcopy(snapshot))

    def __repr__(self) -> str:
        return f"<Model '{self.name}'>"


def create_model(
    name: str,
    initial_state: Optional[Dict[str, Any]] = None,
    **kwargs: Any,
) -> Model:
    """Factory for creating a :class:`Model`."""
    return Model(name, initial_state=initial_state, **kwargs)


def _normalize_path(path: PathLike) -> Tuple[str, ...]:
    if isinstance(path, str):
        return tuple(part for part in path.split('.') if part)
    if isinstance(path, (list, tuple)):
        return tuple(path)
    raise TypeError(
        f"[SPECTER:model] Path must be str/list/tuple, got {type(path).__name__}"
    )


def _get_path(state: StateDict, path: Tuple[str, ...], default: Any = None) -> Any:
    current = state
    for part in path:
        if not isinstance(current, dict) or part not in current:
            return default
        current = current[part]
    return current


def _set_path(state: StateDict, path: Tuple[str, ...], value: Any) -> None:
    if not path:
        raise ValueError("[SPECTER:model] set() requires a non-empty path")

    current = state
    for part in path[:-1]:
        next_value = current.get(part)
        if not isinstance(next_value, dict):
            next_value = {}
            current[part] = next_value
        current = next_value

    current[path[-1]] = value


def _delete_path(state: StateDict, path: Tuple[str, ...]) -> bool:
    if not path:
        return False

    current = state
    for part in path[:-1]:
        if not isinstance(current, dict) or part not in current:
            return False
        current = current[part]

    if not isinstance(current, dict) or path[-1] not in current:
        return False

    del current[path[-1]]
    return True
