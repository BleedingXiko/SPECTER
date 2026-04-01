"""
SPECTER SocketIngress — shared Socket.IO event ingress and fanout.

Flask-SocketIO effectively stores one callback per event name. That makes
feature-level ownership awkward when multiple domains need to react to the
same inbound socket event. SocketIngress registers exactly one dispatcher per
socket event with Flask-SocketIO, then fans that event out to subscribers in
priority order.
"""

import logging

from gevent.lock import BoundedSemaphore

logger = logging.getLogger(__name__)


class SocketIngress:
    """Shared inbound Socket.IO event dispatcher."""

    def __init__(self, socketio=None, *, name='socket_ingress'):
        self.name = name
        self._socketio = socketio
        self._subscribers = {}   # event -> list[{callback, priority, order}]
        self._dispatchers = {}   # event -> dispatcher fn
        self._default_error_subscribers = []
        self._default_error_dispatcher = None
        self._order = 0
        self._lock = BoundedSemaphore(1)

    def attach(self, socketio):
        """Attach a Flask-SocketIO instance and bind any pending dispatchers."""
        with self._lock:
            self._socketio = socketio
            events_to_bind = [
                event for event in self._subscribers
                if event not in self._dispatchers
            ]
            bind_default_error = (
                self._default_error_subscribers and
                self._default_error_dispatcher is None
            )

        for event in events_to_bind:
            self._bind_dispatcher(event)
        if bind_default_error:
            self._bind_default_error_dispatcher()
        return self

    def on(self, event_name, callback=None, *, owner=None, priority=100):
        """Register a subscriber, or return a decorator when callback is omitted."""
        if callback is None:
            def decorator(fn):
                self.subscribe(
                    event_name,
                    fn,
                    owner=owner,
                    priority=priority,
                )
                return fn

            return decorator

        self.subscribe(
            event_name,
            callback,
            owner=owner,
            priority=priority,
        )
        return callback

    def handler(self, event_name, *, owner=None, priority=100):
        """Decorator alias for ``on(..., callback=None)``."""
        return self.on(
            event_name,
            owner=owner,
            priority=priority,
        )

    def on_error_default(self, callback=None, *, owner=None, priority=100):
        """Register a subscriber for Socket.IO's default error hook."""
        if callback is None:
            def decorator(fn):
                self.subscribe_error_default(
                    fn,
                    owner=owner,
                    priority=priority,
                )
                return fn

            return decorator

        self.subscribe_error_default(
            callback,
            owner=owner,
            priority=priority,
        )
        return callback

    def subscribe(self, event_name, callback, *, owner=None, priority=100):
        """Subscribe a callback to an inbound socket event."""
        if not isinstance(event_name, str) or not event_name.strip():
            raise TypeError(
                f"[SPECTER:socket_ingress] event_name must be a non-empty string, got {event_name!r}"
            )
        if not callable(callback):
            raise TypeError(
                "[SPECTER:socket_ingress] callback must be callable, "
                f"got {type(callback).__name__}"
            )
        if owner is not None:
            if not hasattr(owner, 'add_cleanup') or not callable(owner.add_cleanup):
                raise TypeError(
                    "[SPECTER:socket_ingress] owner must expose add_cleanup(), "
                    f"got {type(owner).__name__}"
                )

        event_name = event_name.strip()
        should_bind = False
        with self._lock:
            self._order += 1
            records = self._subscribers.setdefault(event_name, [])
            records.append({
                'callback': callback,
                'priority': priority,
                'order': self._order,
            })
            records.sort(key=lambda record: (record['priority'], record['order']))
            should_bind = self._socketio is not None and event_name not in self._dispatchers

        unsubscribe = lambda: self.unsubscribe(event_name, callback)
        if owner is not None:
            owner.add_cleanup(unsubscribe)

        if should_bind:
            self._bind_dispatcher(event_name)

        logger.debug(
            "[SPECTER:socket_ingress] subscribed '%s' at priority %s",
            event_name,
            priority,
        )
        return unsubscribe

    def subscribe_error_default(self, callback, *, owner=None, priority=100):
        """Subscribe a callback to Socket.IO's default error hook."""
        if not callable(callback):
            raise TypeError(
                "[SPECTER:socket_ingress] callback must be callable, "
                f"got {type(callback).__name__}"
            )
        if owner is not None:
            if not hasattr(owner, 'add_cleanup') or not callable(owner.add_cleanup):
                raise TypeError(
                    "[SPECTER:socket_ingress] owner must expose add_cleanup(), "
                    f"got {type(owner).__name__}"
                )

        should_bind = False
        with self._lock:
            self._order += 1
            self._default_error_subscribers.append({
                'callback': callback,
                'priority': priority,
                'order': self._order,
            })
            self._default_error_subscribers.sort(
                key=lambda record: (record['priority'], record['order'])
            )
            should_bind = (
                self._socketio is not None and
                self._default_error_dispatcher is None
            )

        unsubscribe = lambda: self.unsubscribe_error_default(callback)
        if owner is not None:
            owner.add_cleanup(unsubscribe)

        if should_bind:
            self._bind_default_error_dispatcher()

        logger.debug(
            "[SPECTER:socket_ingress] subscribed default error at priority %s",
            priority,
        )
        return unsubscribe

    def unsubscribe(self, event_name, callback):
        """Remove a previously subscribed callback from an event."""
        removed = False
        with self._lock:
            records = self._subscribers.get(event_name)
            if not records:
                return False

            remaining = [
                record for record in records
                if record['callback'] is not callback
            ]
            removed = len(remaining) != len(records)

            if remaining:
                self._subscribers[event_name] = remaining
            else:
                self._subscribers.pop(event_name, None)

        if removed:
            logger.debug(
                "[SPECTER:socket_ingress] unsubscribed '%s'",
                event_name,
            )
        return removed

    def unsubscribe_error_default(self, callback):
        """Remove a previously subscribed default error callback."""
        removed = False
        with self._lock:
            remaining = [
                record for record in self._default_error_subscribers
                if record['callback'] is not callback
            ]
            removed = len(remaining) != len(self._default_error_subscribers)
            self._default_error_subscribers = remaining

        if removed:
            logger.debug(
                "[SPECTER:socket_ingress] unsubscribed default error handler"
            )
        return removed

    def dispatch(self, event_name, *args, **kwargs):
        """Fan out an inbound Socket.IO event to all subscribers."""
        with self._lock:
            subscribers = list(self._subscribers.get(event_name, ()))

        last_result = None
        for record in subscribers:
            callback = record['callback']
            try:
                result = callback(*args, **kwargs)
                if result is not None:
                    last_result = result
            except Exception as exc:
                logger.error(
                    "[SPECTER:socket_ingress] subscriber error for '%s': %s",
                    event_name,
                    exc,
                    exc_info=True,
                )

        return last_result

    def dispatch_default_error(self, *args, **kwargs):
        """Fan out Socket.IO default errors to all subscribers."""
        with self._lock:
            subscribers = list(self._default_error_subscribers)

        last_result = None
        for record in subscribers:
            callback = record['callback']
            try:
                result = callback(*args, **kwargs)
                if result is not None:
                    last_result = result
            except Exception as exc:
                logger.error(
                    "[SPECTER:socket_ingress] default error subscriber failed: %s",
                    exc,
                    exc_info=True,
                )

        return last_result

    def clear(self):
        """Remove all subscribers. Primarily for shutdown/tests."""
        with self._lock:
            self._socketio = None
            self._subscribers.clear()
            self._dispatchers.clear()
            self._default_error_subscribers.clear()
            self._default_error_dispatcher = None

    @property
    def socketio(self):
        """The attached Flask-SocketIO instance."""
        return self._socketio

    def _bind_dispatcher(self, event_name):
        with self._lock:
            if self._socketio is None or event_name in self._dispatchers:
                return

            def dispatcher(*args, _event_name=event_name, **kwargs):
                return self.dispatch(_event_name, *args, **kwargs)

            dispatcher.__name__ = f"specter_socket_dispatch_{event_name}"
            self._dispatchers[event_name] = dispatcher
            socketio = self._socketio

        socketio.on_event(event_name, dispatcher)
        logger.info(
            "[SPECTER:socket_ingress] bound dispatcher for '%s'",
            event_name,
        )

    def _bind_default_error_dispatcher(self):
        with self._lock:
            if self._socketio is None or self._default_error_dispatcher is not None:
                return

            def dispatcher(*args, **kwargs):
                return self.dispatch_default_error(*args, **kwargs)

            dispatcher.__name__ = 'specter_socket_dispatch_default_error'
            self._default_error_dispatcher = dispatcher
            socketio = self._socketio

        socketio.on_error_default(dispatcher)
        logger.info(
            "[SPECTER:socket_ingress] bound default error dispatcher"
        )
