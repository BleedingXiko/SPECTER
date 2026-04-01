"""
SPECTER Service — Lifecycle-managed background service.

A Service owns
background work: periodic tasks, bus subscriptions, child services,
and arbitrary cleanup callbacks.  When ``stop()`` is called, everything
tears down automatically in reverse order.

Lifecycle::

    Service(name, initial_state={})
      start()  →  on_start()   ← register intervals, bus listeners, adopt children
      set_state(partial)       ← shallow merge + notify subscribers
      stop()   →  on_stop()    ← pre-cleanup hook
                   teardown:
                     - cancel intervals and timeouts
                     - cancel owned greenlets
                     - unsubscribe from bus events
                     - stop adopted children (reverse order)
                     - run cleanup callbacks (reverse order)
                     - clear state subscribers

Concurrency:
    - ``interval()`` / ``timeout()`` use ``gevent.spawn_later()``
    - ``spawn()`` / ``spawn_later()`` track owned greenlets automatically
    - State updates are protected by a ``BoundedSemaphore``
    - ``stop()`` is safe to call from any greenlet
    - **Never** use ``threading.Lock`` — always ``gevent.lock``
"""

import logging
import uuid
from gevent.lock import BoundedSemaphore
import gevent

from .bus import bus
from .ownership import resolve_cleanup

logger = logging.getLogger(__name__)


class Service:
    """
    Base class for lifecycle-managed background services.

    Subclass this and override ``on_start()`` / ``on_stop()`` to define
    your service's behavior.

    Example::

        class ThumbnailService(Service):
            def __init__(self):
                super().__init__('thumbnails', {'queue_size': 0})

            def on_start(self):
                self.interval(self._process_queue, 5.0)
                self.listen('media:file_added', self._enqueue)

            def on_stop(self):
                self._flush_remaining()
    """

    def __init__(self, name, initial_state=None):
        """
        Args:
            name: Human-readable service name (used in logs and health).
            initial_state: Optional initial state dict.
        """
        self.name = name
        self.state = dict(initial_state) if initial_state else {}

        self._running = False
        self._state_lock = BoundedSemaphore(1)
        self.priority = 100  # Default boot priority (lower = earlier)

        # Managed resources
        self._intervals = {}      # id -> greenlet
        self._timeouts = {}       # id -> greenlet
        self._greenlets = set()   # managed ad-hoc greenlets
        self._bus_unsubs = []     # list of unsubscribe functions
        self._cleanups = []       # list of cleanup callbacks
        self._children = []       # list of adopted Service instances
        self._subscribers = set() # state change subscribers

    # ------------------------------------------------------------------
    # Lifecycle Hooks (Override These)
    # ------------------------------------------------------------------

    def on_start(self):
        """
        Called once when ``start()`` transitions to running.

        Override to register intervals, bus listeners, and adopt children.
        """

    def on_stop(self):
        """
        Called during ``stop()`` before automatic teardown.

        Override to perform pre-cleanup work (e.g. flushing a queue,
        persisting state to disk).
        """

    # ------------------------------------------------------------------
    # Lifecycle Control
    # ------------------------------------------------------------------

    def start(self):
        """
        Start the service.  Calls ``on_start()`` once.

        Idempotent — safe to call multiple times.

        Returns:
            ``self`` for chaining.
        """
        if self._running:
            return self
        self._running = True
        logger.info(f"[SPECTER] Service '{self.name}' starting")
        try:
            self.on_start()
        except Exception as e:
            logger.error(
                f"[SPECTER] Service '{self.name}' failed to start: {e}",
                exc_info=True,
            )
            self._running = False
            raise
        logger.info(f"[SPECTER] Service '{self.name}' started")
        return self

    def stop(self):
        """
        Stop the service and tear down all managed resources.

        Order of teardown:
        1. ``on_stop()`` hook
        2. Cancel all intervals and timeouts
        3. Cancel owned greenlets
        4. Unsubscribe from all bus events
        5. Stop adopted children (reverse order)
        6. Run cleanup callbacks (reverse order)
        7. Clear state subscribers

        Idempotent — safe to call multiple times.

        Returns:
            ``self`` for chaining.
        """
        if not self._running:
            return self

        logger.info(f"[SPECTER] Service '{self.name}' stopping")

        # 1. Pre-cleanup hook
        try:
            self.on_stop()
        except Exception as e:
            logger.error(
                f"[SPECTER] Error in Service '{self.name}' on_stop: {e}",
                exc_info=True,
            )

        # 2. Cancel intervals and timeouts
        for glet in list(self._intervals.values()):
            try:
                glet.kill(block=False)
            except Exception:
                pass
        self._intervals.clear()

        for glet in list(self._timeouts.values()):
            try:
                glet.kill(block=False)
            except Exception:
                pass
        self._timeouts.clear()

        for glet in list(self._greenlets):
            try:
                glet.kill(block=False)
            except Exception:
                pass
        self._greenlets.clear()

        # 3. Unsubscribe from bus events
        for unsub in self._bus_unsubs:
            try:
                unsub()
            except Exception as e:
                logger.warning(
                    f"[SPECTER] Failed to unsub bus listener for "
                    f"'{self.name}': {e}"
                )
        self._bus_unsubs.clear()

        # 4. Stop adopted children (reverse order)
        for child in reversed(self._children):
            try:
                child.stop()
            except Exception as e:
                logger.error(
                    f"[SPECTER] Error stopping child service "
                    f"'{child.name}' (owned by '{self.name}'): {e}",
                    exc_info=True,
                )
        self._children.clear()

        # 5. Run cleanup callbacks (reverse order — last added = first cleaned)
        while self._cleanups:
            fn = self._cleanups.pop()
            try:
                fn()
            except Exception as e:
                logger.warning(
                    f"[SPECTER] Failed to run cleanup for "
                    f"'{self.name}': {e}"
                )

        # 6. Clear subscribers
        self._subscribers.clear()

        self._running = False
        logger.info(f"[SPECTER] Service '{self.name}' stopped")
        return self

    @property
    def running(self):
        """``True`` if the service is currently running."""
        return self._running

    # ------------------------------------------------------------------
    # State Management
    # ------------------------------------------------------------------

    def set_state(self, partial):
        """
        Shallow merge into state and notify subscribers.

        Args:
            partial: Dict of keys to merge into the current state.
        """
        with self._state_lock:
            self.state.update(partial)
            state_copy = dict(self.state)

        # Notify outside the lock to avoid deadlock
        for sub in list(self._subscribers):
            try:
                sub(state_copy, self)
            except Exception as e:
                logger.warning(
                    f"[SPECTER] Error in state subscriber for "
                    f"'{self.name}': {e}"
                )

    def get_state(self):
        """Return a copy of the current state."""
        with self._state_lock:
            return dict(self.state)

    def subscribe(self, fn, immediate=False, owner=None):
        """
        Subscribe to state changes.

        Args:
            fn: Callback ``fn(state_dict, service)`` called on each
                ``set_state()``.
            immediate: If ``True``, fire immediately with current state.
            owner: If provided (a ``Service``), auto-unsubscribe when
                   the owner stops.

        Returns:
            An unsubscribe function.
        """
        self._subscribers.add(fn)
        unsub = lambda: self._subscribers.discard(fn)

        if owner is not None:
            owner.add_cleanup(unsub)

        if immediate:
            try:
                fn(self.get_state(), self)
            except Exception as e:
                logger.warning(
                    f"[SPECTER] Error in immediate subscriber for "
                    f"'{self.name}': {e}"
                )

        return unsub

    def watch(self, fn, immediate=True):
        """
        Self-subscribe: wires cleanup to this service's own lifecycle.

        Shorthand for ``subscribe(fn, immediate=immediate, owner=self)``.

        Args:
            fn: Callback ``fn(state_dict, service)``.
            immediate: Fire immediately with current state (default ``True``).

        Returns:
            An unsubscribe function.
        """
        return self.subscribe(fn, immediate=immediate, owner=self)

    # ------------------------------------------------------------------
    # Managed Timers
    # ------------------------------------------------------------------

    def spawn(self, callback, *args, label=None, **kwargs):
        """
        Spawn an owned greenlet and track it for teardown.

        Use this instead of bare ``gevent.spawn()`` from Service code.

        Args:
            callback: Callable to execute inside the greenlet.
            *args: Positional args for ``callback``.
            label: Optional log label for diagnostics.
            **kwargs: Keyword args for ``callback``.

        Returns:
            The spawned greenlet.
        """
        if not self._running:
            raise RuntimeError(
                f"[SPECTER] Cannot spawn task for '{self.name}' before start()"
            )

        task_label = label or getattr(callback, '__name__', 'greenlet')

        def _runner():
            try:
                return callback(*args, **kwargs)
            except gevent.GreenletExit:
                raise
            except Exception as e:
                logger.error(
                    f"[SPECTER] Greenlet error in '{self.name}' "
                    f"({task_label}): {e}",
                    exc_info=True,
                )

        glet = gevent.spawn(_runner)
        self._track_greenlet(glet)
        return glet

    def spawn_later(self, seconds, callback, *args, label=None, **kwargs):
        """
        Spawn an owned greenlet after a delay and track it for teardown.

        Args:
            seconds: Delay before execution.
            callback: Callable to execute.
            *args: Positional args for ``callback``.
            label: Optional log label for diagnostics.
            **kwargs: Keyword args for ``callback``.

        Returns:
            The scheduled greenlet.
        """
        if not self._running:
            raise RuntimeError(
                f"[SPECTER] Cannot schedule task for '{self.name}' before start()"
            )

        task_label = label or getattr(callback, '__name__', 'greenlet')

        def _runner():
            try:
                return callback(*args, **kwargs)
            except gevent.GreenletExit:
                raise
            except Exception as e:
                logger.error(
                    f"[SPECTER] Delayed greenlet error in '{self.name}' "
                    f"({task_label}): {e}",
                    exc_info=True,
                )

        glet = gevent.spawn_later(seconds, _runner)
        self._track_greenlet(glet)
        return glet

    def interval(self, callback, seconds):
        """
        Register a periodic task.  Auto-cancelled on ``stop()``.

        The callback is wrapped in try/except — unhandled exceptions
        are logged but do not kill the interval loop.

        Args:
            callback: Callable (no arguments) to run periodically.
            seconds: Interval duration in seconds.

        Returns:
            An interval ID string (pass to ``cancel_interval()``).
        """
        if not self._running:
            raise RuntimeError(
                f"[SPECTER] Cannot schedule interval for '{self.name}' before "
                "start()"
            )

        interval_id = f"interval_{uuid.uuid4().hex[:8]}"

        def _loop():
            while self._running and interval_id in self._intervals:
                gevent.sleep(seconds)
                if not self._running or interval_id not in self._intervals:
                    break
                try:
                    callback()
                except Exception as e:
                    logger.error(
                        f"[SPECTER] Interval error in '{self.name}' "
                        f"(id={interval_id}): {e}",
                        exc_info=True,
                    )

        glet = gevent.spawn(_loop)
        self._intervals[interval_id] = glet
        return interval_id

    def timeout(self, callback, seconds):
        """
        Register a one-shot delayed task.  Auto-cancelled on ``stop()``.

        Args:
            callback: Callable (no arguments) to run after delay.
            seconds: Delay in seconds.

        Returns:
            A timeout ID string (pass to ``cancel_timeout()``).
        """
        if not self._running:
            raise RuntimeError(
                f"[SPECTER] Cannot schedule timeout for '{self.name}' before "
                "start()"
            )

        timeout_id = f"timeout_{uuid.uuid4().hex[:8]}"

        def _delayed():
            gevent.sleep(seconds)
            if self._running and timeout_id in self._timeouts:
                self._timeouts.pop(timeout_id, None)
                try:
                    callback()
                except Exception as e:
                    logger.error(
                        f"[SPECTER] Timeout error in '{self.name}' "
                        f"(id={timeout_id}): {e}",
                        exc_info=True,
                    )

        glet = gevent.spawn(_delayed)
        self._timeouts[timeout_id] = glet
        return timeout_id

    def cancel_interval(self, interval_id):
        """Cancel a specific interval by ID."""
        glet = self._intervals.pop(interval_id, None)
        if glet:
            try:
                glet.kill(block=False)
            except Exception:
                pass

    def cancel_greenlet(self, glet):
        """Cancel a greenlet previously returned by ``spawn()``."""
        if glet is None:
            return self

        self._greenlets.discard(glet)
        try:
            glet.kill(block=False)
        except Exception:
            pass
        return self

    def cancel_timeout(self, timeout_id):
        """Cancel a specific timeout by ID."""
        glet = self._timeouts.pop(timeout_id, None)
        if glet:
            try:
                glet.kill(block=False)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Bus Integration
    # ------------------------------------------------------------------

    def listen(self, event, handler):
        """
        Subscribe to a bus event.  Auto-unsubscribed on ``stop()``.

        Args:
            event: Bus event name string.
            handler: Callback ``handler(data)`` invoked when event fires.

        Returns:
            ``self`` for chaining.
        """
        unsub = bus.on(event, handler)
        self._bus_unsubs.append(unsub)
        return self

    def emit(self, event, data=None):
        """
        Emit a bus event.

        Args:
            event: Bus event name.
            data: Optional payload.

        Returns:
            ``self`` for chaining.
        """
        bus.emit(event, data)
        return self

    # ------------------------------------------------------------------
    # Ownership
    # ------------------------------------------------------------------

    def adopt(self, child, start=True):
        """
        Own a child service's lifecycle.

        The child will be stopped when this service stops (in reverse
        adoption order).

        Args:
            child: A ``Service`` instance.
            start: If ``True`` (default), start the child immediately.

        Returns:
            ``self`` for chaining.
        """
        if child is None:
            return self
        if not isinstance(child, Service):
            raise TypeError(
                f"[SPECTER] '{self.name}' can only adopt Service instances, "
                f"got {type(child).__name__}"
            )
        self._children.append(child)
        if start and not child.running:
            child.start()
        return self

    def own(self, resource, stop_method=None):
        """
        Own a non-Service resource via its cleanup method.

        This is for external resources such as subprocess controllers,
        websocket clients, caches, stores, or other lifecycle-aware objects
        that are not ``Service`` subclasses.

        Args:
            resource: The object to own.
            stop_method: Optional explicit cleanup method name.

        Returns:
            The original resource.
        """
        if resource is None:
            return None

        self.add_cleanup(resolve_cleanup(resource, stop_method=stop_method))
        return resource

    def start_process(self, args, *, name=None, **popen_kwargs):
        """
        Start and own a managed subprocess.

        Args:
            args: ``subprocess.Popen`` argv sequence.
            name: Optional process name for diagnostics.
            **popen_kwargs: Forwarded to ``subprocess.Popen``.

        Returns:
            A started ``ManagedProcess`` instance.
        """
        from .process import ManagedProcess

        process = ManagedProcess(name or f'{self.name}:process')
        process.start(args, owner=self, **popen_kwargs)
        return process

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def add_cleanup(self, fn):
        """
        Register an arbitrary cleanup callback.

        Callbacks run during ``stop()`` in reverse order (last added
        runs first).

        Args:
            fn: Callable (no arguments).

        Returns:
            ``self`` for chaining.
        """
        if callable(fn):
            self._cleanups.append(fn)
        return self

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    def health(self):
        """
        Report service health.  Override for custom health data.

        Returns:
            A dict with at least a ``'status'`` key.
        """
        return {
            'status': 'ok' if self._running else 'stopped',
            'name': self.name,
        }

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _track_greenlet(self, glet):
        """Track an owned greenlet until it completes or is cancelled."""
        self._greenlets.add(glet)

        def _discard(completed):
            self._greenlets.discard(completed)

        glet.rawlink(_discard)
        return glet
