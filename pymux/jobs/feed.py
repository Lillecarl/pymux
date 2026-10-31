"""
A pane that views a job: it replays the job's output onto its screen
and follows what arrives, with no program of its own behind it.
"""

from __future__ import annotations

import codecs
import re
from collections.abc import Callable
from contextlib import suppress
from typing import TYPE_CHECKING, ClassVar

import anyio
import anyio.abc
from anyio.streams.memory import MemoryObjectSendStream
from pyte.keep import Keep

from pymux.log import logger

if TYPE_CHECKING:
    pass

from .job import Job

__all__ = ["JobFeed"]


#: A newline with no return before it. What the viewer turns into a
#: full return, the way ONLCR would have on a pty.
_RETURNS = re.compile(r"(?<!\r)\n")


class _NoBackend:
    "No pty behind a viewer: `#{pane_pid}` reads blank."

    pid: int | None = None


class JobFeed:
    """
    A job where a pane expects a process: replay the tail, follow the
    rest, never fork.

    A viewer pane is a normal terminal whose process is this instead
    of a program on a pty: the screen, the scrollback and copy mode
    all work as usual, and the bytes come from the job. It is
    read-only by construction -- keys reach `write_input`, which
    drops them -- and closing the pane never touches the job: `kill`
    stops the follow, and the job runs on until it ends on its own.

    `feed_output` writes one piece to the screen and says it may have
    changed.

    An upgrade carries the job and the size, in `pane_viewers`, and the
    new server's viewer replays the tail again: what the screen showed
    is the job's, so nothing else needs to cross. Lillecarl/pymux#715.
    """

    KEEP: ClassVar[dict[str, Keep]] = {
        "job": Keep.SAVED,
        "sx": Keep.SAVED,
        "sy": Keep.SAVED,
        "_feed_output": Keep.REBUILT,
        "backend": Keep.REBUILT,
        "_alive": Keep.REBUILT,
        "_terminated": Keep.REBUILT,  # the replay says again how the job ended
        "suspended": Keep.DROPPED,  # the replay starts on a fresh screen, which no copy mode reads
        "_started": Keep.REBUILT,
        "_hint": Keep.DROPPED,
    }

    def __init__(self, job: Job, feed_output: Callable[[str], None]) -> None:
        self.job = job
        self._feed_output = feed_output
        #: The size the pane last had. No pty reads it; `info` and the
        #: resize math do.
        self.sx = 0
        self.sy = 0
        self.backend = _NoBackend()
        self._alive = True
        self._terminated = False
        #: Whether the follow holds while a person reads the history.
        #: Copy mode reads this, the way it reads it on a process.
        self.suspended = False
        self._started = False
        #: The send end of the hint queue this follow reads. `kill`
        #: pokes it from synchronous code, which an event cannot do
        #: twice and a condition cannot do at all.
        self._hint: MemoryObjectSendStream[None] | None = None

    async def start(self, task_group: anyio.abc.TaskGroup) -> None:
        "Replay the tail and follow the rest, watched by `task_group`."
        if self._started:
            return
        self._started = True
        # A viewer is a waiter: reaping never takes a job somebody
        # still watches.
        self.job.waiters += 1
        task_group.start_soon(self._follow)

    def set_size(self, width: int, height: int) -> None:
        self.sx = width
        self.sy = height

    def write_input(self, data: str) -> None:
        "Nowhere: a viewer is read-only, so keys fall on the floor."

    def suspend(self) -> None:
        "Hold the follow while a person reads the history."
        self.suspended = True

    def resume(self) -> None:
        self.suspended = False

    def get_cwd(self) -> None:
        return None

    def get_name(self) -> str:
        return "job %d" % self.job.job_id

    def kill(self) -> None:
        "Stop following. The job runs on; whoever kills removes the pane."
        self._alive = False
        self._terminated = True
        if self._hint is not None:
            with suppress(anyio.ClosedResourceError, anyio.WouldBlock):
                self._hint.send_nowait(None)

    def hang_up(self) -> None:
        "The same as `kill`: a viewer has nothing to hang up."
        self.kill()

    @property
    def is_terminated(self) -> bool:
        "Nothing more will ever be fed: the job ended, or this was killed."
        return self._terminated

    async def _follow(self) -> None:
        """
        Replay both tails, then everything new as it arrives.

        The replay shows stdout first and stderr under a separator;
        the order the two ran in is not recorded, so a replay cannot
        restore it. The follow needs no separator: from there every
        chunk is fed in the order it arrived. When the job ends under
        a pane still watching, the pane says how.
        """
        job = self.job
        out_offset = err_offset = 0
        out_decode = codecs.getincrementaldecoder("utf-8")("replace")
        err_decode = codecs.getincrementaldecoder("utf-8")("replace")
        send, recv = anyio.create_memory_object_stream(1)
        self._hint = send
        job.followers.add(send)
        try:
            out_offset = self._feed_new("stdout", out_offset, out_decode)
            if job.stderr_chunks:
                self._say("\r\n--- stderr ---\r\n")
                err_offset = self._feed_new("stderr", err_offset, err_decode)
            while self._alive:
                if not self.suspended:
                    new_out = self._feed_new("stdout", out_offset, out_decode)
                    new_err = self._feed_new("stderr", err_offset, err_decode)
                    fed = (new_out, new_err) != (out_offset, err_offset)
                    out_offset, err_offset = new_out, new_err
                else:
                    fed = False
                if not fed:
                    if job.is_done:
                        break
                    # A hint only wakes; what is new is read by offset
                    # above, so a hint that arrives anywhere around this
                    # sleep only causes another drain, never a miss.
                    await recv.receive()
            if self._alive:
                for text in (out_decode.decode(b"", True), err_decode.decode(b"", True)):
                    if text:
                        self._say(text)
                if job.error is not None:
                    self._say("\r\n[job %d never started: %s]\r\n" % (job.job_id, job.error))
                else:
                    self._say("\r\n[job %d exited %s]\r\n" % (job.job_id, job.returncode))
                self._terminated = True
        finally:
            job.followers.discard(send)
            self._hint = None
            with suppress(Exception):
                await send.aclose()
                await recv.aclose()
            job.waiters -= 1

    def _feed_new(self, stream: str, offset: int, decode: codecs.IncrementalDecoder) -> int:
        """
        Feed what one stream holds past an offset; answer the offset past it.

        One piece the screen cannot take must not stop the follow: the
        offset moves past it either way, the way a pane moves past a
        sequence its emulator cannot handle.

        Newlines go the way the pty would have taken them: a program
        on a terminal writes through ONLCR, which turns `\\n` into
        `\\r\\n`, and a pipe has no such discipline, so without it
        every line starts where the last one ended. A `\\r` already
        there stays alone; a split pair only doubles the return,
        which lands on the same column.
        """
        end, data = self.job.read(stream, offset)
        if data:
            try:
                text = decode.decode(data)
                if text:
                    self._feed_output(_RETURNS.sub("\r\n", text))
            except Exception:
                logger.exception("Feeding a job to its viewer failed.")
        return end

    def _say(self, text: str) -> None:
        "Feed a marker line of our own. One the screen cannot take is dropped."
        try:
            self._feed_output(text)
        except Exception:
            logger.exception("Feeding a job marker to its viewer failed.")
