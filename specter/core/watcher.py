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
SPECTER Watcher — lifecycle-managed observation primitive.

Encapsulates the "follow something over time" pattern: subprocess output
readers, device monitors, websocket client loops, filesystem pollers, and
log tailers.

The Watcher provides a single, composable primitive that:
- Owns a background greenlet to run a polling/reading loop.
- Handles automatic recovery with exponential back-off.
- Integrates with Service lifecycle (can be adopted/owned for cleanup).
- Publishes observed values to subscribers.
- Is domain-agnostic.

Every such loop shares the same skeletal shape:
  1. Start a greenlet/thread.
  2. Loop while running.
  3. Do work or wait for input.
  4. On error, log and optionally sleep before retrying.
  5. On stop, set a flag and join.

The Watcher encodes that shape as a first-class primitive.

Usage::

    from specter import Watcher

    # Poll-style watcher
    watcher = Watcher(
        'hdmi-status',
        poll=lambda: read_sysfs_status(),
        interval=2.0,
    )
    watcher.subscribe(lambda value, w: print(f"HDMI: {value}"))
    watcher.start()

    # Stream-style watcher (e.g., reading from a subprocess pipe)
    watcher = Watcher(
        'tunnel-url',
        stream=lambda: iter_process_stdout(proc),
    )
    watcher.subscribe(handle_line)
    watcher.start()

    # Lifecycle-owned watcher
    class TunnelService(Service):
        def on_start(self):
            self.url_watcher = Watcher('tunnel-url', stream=self._read_output)
            self.own(self.url_watcher)
            self.url_watcher.start()
"""

import logging
import time

import gevent
from gevent.event import Event as GeventEvent

logger = logging.getLogger(__name__)


class WatcherError(Exception):
    """Raised when a Watcher encounters an unrecoverable error."""


class Watcher:
    """
    Lifecycle-managed observation loop.

    A Watcher encapsulates the "follow state over time" pattern that every
    Most backend modules implement ad-hoc.

    There are two modes:

    **Poll mode** — provide a ``poll`` callable and an ``interval``.  The
    watcher calls ``poll()`` on each tick, delivers the returned value to
    subscribers, and sleeps for ``interval`` seconds.

    **Stream mode** — provide a ``stream`` callable that returns an iterable.
    The watcher iterates the iterable, delivering each yielded item to
    subscribers.  When the iterable is exhausted, the watcher can optionally
    retry.

    Exactly one of ``poll`` or ``stream`` must be provided.

    Parameters
    ----------
    name : str
        Human-readable identifier (for logs and health).
    poll : callable, optional
        Zero-argument function that returns the current observation.
    stream : callable, optional
        Zero-argument function that returns an iterable of observations.
    interval : float
        Seconds between poll ticks (poll mode only).  Default: ``5.0``.
    retry : bool
        Whether to auto-restart on error.  Default: ``True``.
    max_backoff : float
        Maximum back-off delay between retries.  Default: ``30.0``.
    dedupe : bool
        If True, only notify subscribers when the value changes.
        Default: ``False``.
    transform : callable, optional
        Optional function applied to each raw value before delivery.
    """

    def __init__(
        self,
        name,
        *,
        poll=None,
        stream=None,
        interval=5.0,
        retry=True,
        max_backoff=30.0,
        dedupe=False,
        transform=None,
    ):
        if poll and stream:
            raise ValueError(
                f"[SPECTER:watcher] '{name}': provide poll OR stream, not both"
            )
        if not poll and not stream:
            raise ValueError(
                f"[SPECTER:watcher] '{name}': one of poll or stream is required"
            )

        self.name = name
        self._poll_fn = poll
        self._stream_fn = stream
        self._interval = max(0.1, float(interval))
        self._retry = bool(retry)
        self._max_backoff = float(max_backoff)
        self._dedupe = bool(dedupe)
        self._transform = transform

        self._subscribers = []
        self._greenlet = None
        self._stop_event = GeventEvent()
        self._running = False
        self._last_value = _SENTINEL
        self._backoff = 1.0
        self._error_count = 0
        self._started_at = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self):
        """Start the observation loop in a background greenlet."""
        if self._running:
            return self
        self._running = True
        self._stop_event.clear()
        self._started_at = time.monotonic()
        self._greenlet = gevent.spawn(self._run)
        logger.debug(f"[SPECTER:watcher] '{self.name}' started")
        return self

    def stop(self):
        """Stop the observation loop and join the greenlet."""
        if not self._running:
            return self
        self._running = False
        self._stop_event.set()
        if self._greenlet and not self._greenlet.dead:
            self._greenlet.join(timeout=5.0)
            if not self._greenlet.dead:
                self._greenlet.kill()
        self._greenlet = None
        logger.debug(f"[SPECTER:watcher] '{self.name}' stopped")
        return self

    def subscribe(self, fn):
        """
        Subscribe to observed values.

        Callback signature: ``fn(value, watcher)``.

        Returns an unsubscribe function.
        """
        self._subscribers.append(fn)
        return lambda: (
            self._subscribers.remove(fn) if fn in self._subscribers else None
        )

    @property
    def running(self):
        return self._running

    @property
    def last_value(self):
        """The most recently observed value, or ``None`` if nothing yet."""
        return None if self._last_value is _SENTINEL else self._last_value

    def health(self):
        """Return a health snapshot for diagnostics."""
        uptime = (
            time.monotonic() - self._started_at
            if self._started_at
            else 0
        )
        return {
            'name': self.name,
            'running': self._running,
            'mode': 'poll' if self._poll_fn else 'stream',
            'error_count': self._error_count,
            'uptime_seconds': round(uptime, 1),
            'subscribers': len(self._subscribers),
        }

    # ------------------------------------------------------------------
    # Lifecycle integration (Service.own() compatibility)
    # ------------------------------------------------------------------

    def teardown(self):
        """Alias for stop(), enables ``Service.own(watcher)``."""
        self.stop()

    # ------------------------------------------------------------------
    # Internal loop
    # ------------------------------------------------------------------

    def _run(self):
        """Main observation loop with retry support."""
        while self._running:
            try:
                if self._poll_fn:
                    self._poll_loop()
                else:
                    self._stream_loop()

                # If we exit normally (no error), reset back-off
                self._backoff = 1.0

            except Exception as exc:
                self._error_count += 1
                if not self._running:
                    break

                logger.warning(
                    f"[SPECTER:watcher] '{self.name}' error "
                    f"(attempt {self._error_count}): {exc}"
                )

                if not self._retry:
                    logger.error(
                        f"[SPECTER:watcher] '{self.name}' stopped "
                        f"(retry=False)"
                    )
                    break

                # Exponential back-off
                delay = min(self._backoff, self._max_backoff)
                logger.info(
                    f"[SPECTER:watcher] '{self.name}' retrying in "
                    f"{delay:.1f}s"
                )
                self._stop_event.wait(timeout=delay)
                self._backoff = min(self._backoff * 2, self._max_backoff)

        self._running = False

    def _poll_loop(self):
        """Execute poll function on a timed interval."""
        while self._running:
            raw = self._poll_fn()
            value = self._transform(raw) if self._transform else raw

            if self._dedupe and value == self._last_value:
                pass  # Skip notification
            else:
                self._last_value = value
                self._notify(value)

            # Wait for interval or stop signal
            if self._stop_event.wait(timeout=self._interval):
                break  # Stop was requested

    def _stream_loop(self):
        """Iterate stream source and deliver items."""
        iterable = self._stream_fn()
        for raw in iterable:
            if not self._running:
                break

            value = self._transform(raw) if self._transform else raw

            if self._dedupe and value == self._last_value:
                continue

            self._last_value = value
            self._notify(value)

    def _notify(self, value):
        """Deliver a value to all subscribers."""
        for fn in list(self._subscribers):
            try:
                fn(value, self)
            except Exception as exc:
                logger.warning(
                    f"[SPECTER:watcher] '{self.name}' subscriber error: {exc}"
                )

    def __repr__(self):
        mode = 'poll' if self._poll_fn else 'stream'
        state = 'running' if self._running else 'stopped'
        return f"<Watcher '{self.name}' {mode} {state}>"


# Sentinel for "no value observed yet"
_SENTINEL = object()
