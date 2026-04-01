"""
SPECTER QueueService — generic queue-backed background worker.

Keeps bounded async work queues with worker pools inside Service ownership
so startup, teardown, and worker cleanup are deterministic.
"""

import logging

from gevent.queue import Empty, Full, JoinableQueue

from .lifecycle import Service

logger = logging.getLogger(__name__)


class QueueService(Service):
    """
    Base class for queue-backed worker services.

    Subclass and override ``handle_item()``. Workers are started automatically
    when the service starts and are torn down with the owning lifecycle.
    """

    def __init__(
        self,
        name,
        *,
        worker_count=1,
        maxsize=0,
        poll_interval=0.5,
        initial_state=None,
    ):
        state = {
            'queue_size': 0,
            'worker_count': worker_count,
        }
        if initial_state:
            state.update(initial_state)

        super().__init__(name, state)
        self.worker_count = max(1, int(worker_count or 1))
        self.poll_interval = max(0.05, float(poll_interval or 0.5))
        self.queue = JoinableQueue(maxsize=maxsize)

    def on_start(self):
        for index in range(self.worker_count):
            self.spawn(
                self._worker_loop,
                label=f'worker-{index + 1}',
            )
        self.on_workers_started()

    def on_stop(self):
        self.on_workers_stopping()

    def enqueue(self, item, *, block=False, timeout=None):
        """
        Enqueue an item for worker processing.

        Returns ``True`` when accepted, ``False`` when the queue is full.
        """
        try:
            self.queue.put(item, block=block, timeout=timeout)
        except Full:
            return False

        self.set_state({'queue_size': self.queue.qsize()})
        return True

    def pending_count(self):
        """Return the current queue depth."""
        return self.queue.qsize()

    def handle_item(self, item):
        """Process a single queue item. Subclasses must override this."""
        raise NotImplementedError(
            f"{type(self).__name__}.handle_item() must be implemented"
        )

    def on_workers_started(self):
        """Hook called after workers are spawned."""

    def on_workers_stopping(self):
        """Hook called before worker greenlets are torn down."""

    def _worker_loop(self):
        while self.running:
            try:
                item = self.queue.get(timeout=self.poll_interval)
            except Empty:
                continue

            self.set_state({'queue_size': self.queue.qsize()})

            try:
                self.handle_item(item)
            except Exception as e:
                logger.error(
                    f"[SPECTER] Queue worker error in '{self.name}': {e}",
                    exc_info=True,
                )
            finally:
                try:
                    self.queue.task_done()
                except Exception:
                    pass
                self.set_state({'queue_size': self.queue.qsize()})
