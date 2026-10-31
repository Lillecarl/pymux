"""
The two pipes of a job that runs without a terminal: pumped into the
job's tails while it runs, and reaped when it ends.
"""

from __future__ import annotations

import os
from contextlib import suppress

import anyio
import anyio.abc
from ptyhost.backends.posix import reaped_now, stranger_ended

from pymux.log import logger

from .job import Job, land

__all__ = ["JobPipes"]


class JobPipes:
    """
    The pipes of a job on pipes: the read ends of its stdout and stderr,
    and its pid. `parent` says whether this process forked it, and so
    can wait for it: an upgrade's exec keeps the pid, and a job another
    process forked never is its child. Lillecarl/pymux#736.

    A read parks while paused, as `PosixBackend._pump` does, so that an
    upgrade reads nothing after the snapshot that describes the job.
    """

    def __init__(self, pid: int, stdout: int, stderr: int, parent: bool) -> None:
        self.pid = pid
        self.stdout = stdout
        self.stderr = stderr
        self.parent = parent
        self.returncode: int | None = None
        self._ended = False
        self._reading = anyio.Event()
        self._reading.set()
        self._open = 2
        #: Both pipes read their end.
        self._drained = anyio.Event()

    @property
    def fds(self) -> list[int]:
        "What crosses an exec."
        return [self.stdout, self.stderr]

    def pause_reading(self) -> None:
        self._reading = anyio.Event()

    def resume_reading(self) -> None:
        self._reading.set()

    @property
    def reaped(self) -> bool:
        "It was waited for, so its pid may be another program's now."
        return self._ended

    def send_signal(self, number: int) -> None:
        if not self.reaped:
            with suppress(OSError):
                os.kill(self.pid, number)

    async def pump(self, fd: int, job: Job, name: str) -> None:
        "Drain one pipe into the tail of its stream, until the end."
        os.set_blocking(fd, False)
        try:
            while True:
                await self._reading.wait()
                try:
                    await anyio.wait_readable(fd)
                except anyio.ClosedResourceError, OSError, ValueError:
                    break
                # A pause that came while it waited holds too.
                if not self._reading.is_set():
                    continue
                try:
                    data = os.read(fd, 65536)
                except BlockingIOError:
                    continue
                except OSError:
                    break
                if not data:
                    break
                land(job, name, data)
        finally:
            with suppress(OSError):
                os.close(fd)
            self._open -= 1
            if not self._open:
                self._drained.set()

    async def reap(self) -> int | None:
        """
        Wait for the program to end, and say how. A child that ended
        already -- during an exec -- is reaped at once; a program this
        process did not fork ends with no code.
        """
        if not self.parent:
            await stranger_ended(self.pid, self._drained)
        else:
            ended, self.returncode = reaped_now(self.pid)
            if ended:
                logger.info("Job program %d ended before it was watched, with %s.", self.pid, self.returncode)
            else:
                # Abandoned on a cancel: the caller kills the child, and the
                # thread's wait then returns.
                self.returncode = await anyio.to_thread.run_sync(_wait_for, self.pid, abandon_on_cancel=True)
        self._ended = True
        return self.returncode


def _wait_for(pid: int) -> int | None:
    try:
        return os.waitstatus_to_exitcode(os.waitpid(pid, 0)[1])
    except ChildProcessError:
        return None
