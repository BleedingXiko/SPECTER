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
SPECTER EventBus — Internal pub/sub for cross-service communication.

Decoupled broadcast semantics so services can react to events without
importing each other directly.

NOTE: This bus is for **internal server-side** events only.
Socket events go through ``socketio.emit()``.

Concurrency model:
    Subscribers run synchronously in the emitter's greenlet.
    If a subscriber needs to do heavy work, it should spawn its own
    greenlet via ``gevent.spawn()``.
"""

import logging

logger = logging.getLogger(__name__)


class EventBus:
    """Global event bus for decoupled service communication."""

    def __init__(self):
        self._events = {}  # event_name -> set of callbacks

    # ------------------------------------------------------------------
    # Subscribe
    # ------------------------------------------------------------------

    def on(self, event, callback):
        """
        Subscribe to an event.

        Args:
            event: Event name string.
            callback: Function to call when event is emitted. Receives
                      a single ``data`` argument (may be ``None``).

        Returns:
            An unsubscribe function.
        """
        if not callable(callback):
            raise TypeError(
                f"[SPECTER:bus] on('{event}'): callback must be callable, "
                f"got {type(callback).__name__}"
            )
        if event not in self._events:
            self._events[event] = set()
        self._events[event].add(callback)
        return lambda: self.off(event, callback)

    def off(self, event, callback):
        """
        Unsubscribe from an event.

        Args:
            event: Event name
            callback: The exact callback reference passed to ``on()``
        """
        listeners = self._events.get(event)
        if listeners:
            listeners.discard(callback)
            if not listeners:
                del self._events[event]

    def once(self, event, callback):
        """
        Subscribe to an event exactly once — auto-unsubscribes after
        the first call.

        Args:
            event: Event name
            callback: Function to call once

        Returns:
            An unsubscribe function (no-op after first call).
        """
        def wrapper(data=None):
            self.off(event, wrapper)
            callback(data)

        return self.on(event, wrapper)

    # ------------------------------------------------------------------
    # Emit
    # ------------------------------------------------------------------

    def emit(self, event, data=None):
        """
        Emit an event to all subscribers.

        Subscribers run synchronously in the caller's greenlet.  If a
        subscriber raises, the error is logged and remaining subscribers
        still execute.

        Args:
            event: Event name
            data: Optional payload passed to every listener
        """
        listeners = self._events.get(event)
        if not listeners:
            return
        # Iterate over a snapshot so subscribers can unsub during iteration
        for callback in list(listeners):
            try:
                callback(data)
            except Exception as e:
                logger.error(
                    f"[SPECTER:bus] Error in listener for '{event}': {e}",
                    exc_info=True,
                )

    # ------------------------------------------------------------------
    # Housekeeping
    # ------------------------------------------------------------------

    def clear(self, event=None):
        """
        Clear listeners.

        Args:
            event: If provided, clear only listeners for this event.
                   If ``None``, clear **all** events.
        """
        if event:
            self._events.pop(event, None)
        else:
            self._events.clear()

    def has_listeners(self, event):
        """Return ``True`` if the event has at least one subscriber."""
        return bool(self._events.get(event))

    def listener_count(self, event=None):
        """
        Return listener count.

        Args:
            event: If provided, count for that event only.
                   If ``None``, count all listeners across all events.
        """
        if event:
            return len(self._events.get(event, set()))
        return sum(len(s) for s in self._events.values())


# Singleton — the one bus to rule them all.
bus = EventBus()
