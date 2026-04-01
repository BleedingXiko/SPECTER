"""
SPECTER ManagedProcess — lifecycle-friendly subprocess ownership.

Lifecycle-friendly subprocess ownership: start a process, attach output
readers, and tear the whole bundle down deterministically.
"""

import logging
import subprocess

import gevent
from gevent.lock import BoundedSemaphore

logger = logging.getLogger(__name__)


class ManagedProcess:
    """Own a subprocess and any greenlets used to read its streams."""

    def __init__(self, name, *, terminate_timeout=5.0, kill_timeout=1.0):
        self.name = name
        self.terminate_timeout = terminate_timeout
        self.kill_timeout = kill_timeout
        self.process = None
        self._lock = BoundedSemaphore(1)
        self._readers = set()

    def start(self, args, *, owner=None, **popen_kwargs):
        """Start the subprocess."""
        self._validate_owner(owner)

        with self._lock:
            if self.process and self.process.poll() is None:
                raise RuntimeError(
                    f"[SPECTER:process] '{self.name}' is already running"
                )
            self.process = subprocess.Popen(args, **popen_kwargs)

        if owner is not None:
            self.attach(owner)

        return self

    def attach(self, owner):
        """Attach cleanup to an owning object that exposes ``add_cleanup()``."""
        if owner is None:
            return self
        self._validate_owner(owner)
        owner.add_cleanup(self.stop)
        return self

    def watch_stream(
        self,
        stream_name,
        handler,
        *,
        chunk_size=None,
        strip=True,
        label=None,
        close_on_exit=False,
    ):
        """
        Read a process stream in a managed greenlet.

        Args:
            stream_name: ``stdout`` or ``stderr``.
            handler: Callable receiving each line/chunk.
            chunk_size: If set, read fixed-size chunks instead of lines.
            strip: Strip line endings in line mode.
            label: Optional label for diagnostics.
            close_on_exit: Close the stream when the reader exits.
        """
        process = self._require_process()
        stream = getattr(process, stream_name, None)
        if stream is None:
            raise ValueError(
                f"[SPECTER:process] '{self.name}' has no stream '{stream_name}'"
            )

        reader_label = label or f'{stream_name}_reader'

        def _reader():
            try:
                if chunk_size:
                    while True:
                        chunk = stream.read(chunk_size)
                        if not chunk:
                            break
                        handler(chunk)
                else:
                    sentinel = '' if getattr(stream, 'encoding', None) else b''
                    for item in iter(stream.readline, sentinel):
                        if not item:
                            break
                        if strip and isinstance(item, str):
                            item = item.rstrip('\r\n')
                        handler(item)
            except gevent.GreenletExit:
                raise
            except Exception as e:
                logger.error(
                    f"[SPECTER:process] Reader error in '{self.name}' "
                    f"({reader_label}): {e}",
                    exc_info=True,
                )
            finally:
                if close_on_exit:
                    try:
                        stream.close()
                    except Exception:
                        pass

        glet = gevent.spawn(_reader)
        self._track_reader(glet)
        return glet

    def follow_output(
        self,
        *,
        stdout_handler=None,
        stderr_handler=None,
        stdout_label='stdout',
        stderr_label='stderr',
    ):
        """Convenience helper to watch stdout/stderr with handlers."""
        watchers = []
        if stdout_handler is not None:
            watchers.append(
                self.watch_stream(
                    'stdout',
                    stdout_handler,
                    label=stdout_label,
                )
            )
        if stderr_handler is not None:
            watchers.append(
                self.watch_stream(
                    'stderr',
                    stderr_handler,
                    label=stderr_label,
                )
            )
        return watchers

    def wait(self, timeout=None):
        """Wait for process completion."""
        process = self._require_process()
        return process.wait(timeout=timeout)

    def poll(self):
        """Return the process return code or ``None`` while running."""
        if not self.process:
            return None
        return self.process.poll()

    def stop(self, timeout=None):
        """Terminate the process and stop any reader greenlets."""
        self._stop_readers()

        with self._lock:
            process = self.process
            self.process = None

        if process is None:
            return self

        if process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=timeout or self.terminate_timeout)
            except subprocess.TimeoutExpired:
                logger.warning(
                    f"[SPECTER:process] '{self.name}' did not terminate "
                    f"within {timeout or self.terminate_timeout}s; killing"
                )
                process.kill()
                try:
                    process.wait(timeout=self.kill_timeout)
                except Exception:
                    pass
            except Exception as e:
                logger.warning(
                    f"[SPECTER:process] Error terminating '{self.name}': {e}"
                )
                try:
                    process.kill()
                except Exception:
                    pass

        self._close_streams(process)
        return self

    @property
    def running(self):
        """``True`` if the process is alive."""
        return bool(self.process and self.process.poll() is None)

    @property
    def pid(self):
        """Process ID, or ``None`` when not started."""
        return self.process.pid if self.process else None

    @property
    def returncode(self):
        """Return code, or ``None`` while still running."""
        return self.process.returncode if self.process else None

    def _require_process(self):
        if self.process is None:
            raise RuntimeError(
                f"[SPECTER:process] '{self.name}' has not been started"
            )
        return self.process

    def _validate_owner(self, owner):
        if owner is None:
            return
        if not hasattr(owner, 'add_cleanup') or not callable(owner.add_cleanup):
            raise TypeError(
                f"[SPECTER:process] owner for '{self.name}' must expose "
                f"add_cleanup(). Got {type(owner).__name__}."
            )

    def _track_reader(self, glet):
        self._readers.add(glet)

        def _discard(completed):
            self._readers.discard(completed)

        glet.rawlink(_discard)
        return glet

    def _stop_readers(self):
        for glet in list(self._readers):
            try:
                glet.kill(block=False)
            except Exception:
                pass
        self._readers.clear()

    @staticmethod
    def _close_streams(process):
        for stream_name in ('stdout', 'stderr', 'stdin'):
            stream = getattr(process, stream_name, None)
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    pass


def start_process(name, args, *, owner=None, **popen_kwargs):
    """Factory helper for starting a managed subprocess."""
    process = ManagedProcess(name)
    process.start(args, owner=owner, **popen_kwargs)
    return process
