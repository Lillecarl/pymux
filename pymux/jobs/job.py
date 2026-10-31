"""
One job: the command, its tags, the tail of each stream and how much
fell off it, and how it ended. What is said about a job, to a person or
a script, is here too.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, ClassVar, NewType

import anyio
import anyio.abc
from anyio.streams.memory import MemoryObjectSendStream
from ptyhost.backends.posix import PosixBackend
from pyte.keep import Keep

from pymux.commands import CommandException
from pymux.tags import Tags, spelled

if TYPE_CHECKING:
    from .pipes import JobPipes


__all__ = [
    "SESSION_TAG",
    "STREAM_KEEP",
    "Job",
    "JobId",
    "decode",
    "describe",
    "exit_of",
    "land",
    "outcome_text",
    "parse_env",
    "poke",
    "seconds",
]


#: What each stream of a job keeps, in bytes: the tail, and a count
#: of what fell off the front. A job that logs forever still answers.
STREAM_KEEP = 256 * 1024


#: A job's id. An int at run time -- sqlite stores it as one -- but a
#: distinct type to the checker, so a pane id, a window id, or a bare
#: number never passes where a job is expected without saying so.
JobId = NewType("JobId", int)


def parse_env(word: str) -> tuple[str, str]:
    """
    One `--env`: a key with a value.

    A bare key sets the empty string: there is no NULL in a process
    environment, so `KEY` and `KEY=` cannot stay two different things
    the way two tags can.
    """
    key, separator, value = word.partition("=")
    if not key:
        raise CommandException("a variable names a key: %r" % word)
    return key, value if separator else ""


#: The tag a job's session is stamped as. The client never writes it
#: into the command: the packet says who called, and `run` stamps what
#: it said. A hand-typed tag of this name filters like any other tag.
SESSION_TAG = "session"


class Job:
    "One command the server ran, running or remembered."

    #: What a hot upgrade does with each attribute; `pyte.keep` says.
    #: A running job crosses an upgrade's exec as a child of the same
    #: process, and the new image adopts its pty or its pipes.
    #: Lillecarl/pymux#399, Lillecarl/pymux#736.
    KEEP: ClassVar[dict[str, Keep]] = {
        "job_id": Keep.SAVED,
        "command": Keep.SAVED,
        "directory": Keep.SAVED,
        "pty": Keep.SAVED,
        "tags": Keep.SAVED,
        "env": Keep.SAVED,
        "status": Keep.SAVED,
        "returncode": Keep.SAVED,
        "error": Keep.SAVED,
        "started": Keep.SAVED,
        "finished": Keep.SAVED,
        "last_active": Keep.SAVED,
        "stdout_chunks": Keep.SAVED,
        "stderr_chunks": Keep.SAVED,
        "stdout_kept": Keep.SAVED,
        "stderr_kept": Keep.SAVED,
        "stdout_dropped": Keep.SAVED,
        "stderr_dropped": Keep.SAVED,
        "pid": Keep.SAVED,
        "backend": Keep.REBUILT,  # adopted from the fds the snapshot names
        "crlf": Keep.DROPPED,  # the pause lands what it held back
        "done": Keep.REBUILT,  # set: the job is finished
        "followers": Keep.DROPPED,  # the clients that followed went with the exec
        "waiters": Keep.DROPPED,  # and so did the commands that waited
        "kill_requested": Keep.DROPPED,
    }

    def __init__(
        self,
        job_id: JobId,
        command: str,
        directory: str | None,
        started: float | None = None,
        tags: Iterable[tuple[str, str | None]] = (),
        env: Mapping[str, str] | None = None,
        pty: bool = False,
    ) -> None:
        self.job_id = job_id
        self.command = command
        self.directory = directory
        #: Whether the process runs on a pty rather than pipes: `run
        #: --pty` sets it for the command that needs a terminal to
        #: behave -- colors, cursor motion, `test -t`.
        self.pty = pty
        #: What the job is called by, as key to value: a bare key
        #: carries no value, and a repeated key keeps its last one.
        #: The `session` key names the agent session that ran it, when
        #: one did: `run` stamps what the calling packet said.
        self.tags: Tags = dict(tags)
        #: What the process starts with: the caller's environment and
        #: the `--env` of the `run`, or the server's own where no
        #: caller said anything. In-memory only, like everything else
        #: here but the queryable columns.
        self.env: dict[str, str] = dict(env) if env else {}
        #: `running` until the process ends or fails to start.
        self.status = "running"
        #: What the process exited with, or the negative signal that
        #: ended it. None while it runs, and when it never started.
        self.returncode: int | None = None
        #: Why it never started, when starting it raised.
        self.error: str | None = None
        #: When the row was written, so the table and the database agree
        #: to the instant. `submit` passes it; a direct `Job` takes now.
        self.started = time.time() if started is None else started
        self.finished: float | None = None
        #: When the job last did anything: output landing counts, and
        #: so does finishing. The sweep measures idleness from here.
        self.last_active = self.started
        #: The tail of each stream, as (start offset, bytes): offsets
        #: never move, so a follower that holds one always knows what
        #: it has seen, even past what fell off the front.
        self.stdout_chunks: deque[tuple[int, bytes]] = deque()
        self.stderr_chunks: deque[tuple[int, bytes]] = deque()
        self.stdout_kept = 0
        self.stderr_kept = 0
        self.stdout_dropped = 0
        self.stderr_dropped = 0
        #: Who follows this job live, as send ends of hint queues: a
        #: pumped chunk and the end of the job poke every one. A hint
        #: only wakes; what is new is read by offset, so a hint that
        #: finds a full queue is dropped without loss.
        self.followers: set[MemoryObjectSendStream[None]] = set()
        #: Set when the job ends, whatever ended it.
        self.done = anyio.Event()
        #: Commands waiting on `done`, so reaping never takes a job
        #: somebody still waits for.
        self.waiters = 0
        #: The program's pid, once it runs.
        self.pid: int | None = None
        #: The terminal or the pipes the job runs on, once it runs.
        self.backend: PosixBackend | JobPipes | None = None
        self.crlf = _CrLf()
        #: Set by `kill-job` before the process exists: the supervise
        #: ends it as soon as it starts.
        self.kill_requested = False

    @property
    def is_done(self) -> bool:
        return self.status == "done"

    @property
    def session(self) -> str | None:
        "Whose job this is: the agent session stamped onto its tags, if any."
        return self.tags.get(SESSION_TAG)

    def kept(self, stream: str) -> bytes:
        "The tail of one stream, as one piece."
        chunks = self.stdout_chunks if stream == "stdout" else self.stderr_chunks
        return b"".join(data for _, data in chunks)

    def dropped(self, stream: str) -> int:
        "What fell off one stream past the cap."
        return self.stdout_dropped if stream == "stdout" else self.stderr_dropped

    def end(self, stream: str) -> int:
        "The offset past the last byte of one stream."
        if stream == "stdout":
            return self.stdout_kept + self.stdout_dropped
        return self.stderr_kept + self.stderr_dropped

    def read(self, stream: str, offset: int) -> tuple[int, bytes]:
        """
        What one stream holds past an offset, and the offset past it.

        An offset older than the tail reads from the tail: what fell
        off is gone, and waiting for it would wait forever. A viewer
        that falls that far behind skips the gap and follows from
        what is there.
        """
        chunks = self.stdout_chunks if stream == "stdout" else self.stderr_chunks
        if not chunks:
            return offset, b""
        at = max(offset, chunks[0][0])
        out = []
        for start, data in chunks:
            if start + len(data) <= at:
                continue
            out.append(data[at - start :] if at > start else data)
        return self.end(stream), b"".join(out)

    def age(self) -> float:
        "How long the job ran, or has run."
        return (self.finished or time.time()) - self.started


def poke(job: Job) -> None:
    """
    Wake every live follower: something arrived, or the job ended.

    Synchronous and safe from anywhere on the loop: a hint only
    wakes, and what is new is read by offset, so a full queue drops
    the hint without loss and a closed one leaves the set.
    """
    for follower in list(job.followers):
        try:
            follower.send_nowait(None)
        except anyio.ClosedResourceError:
            job.followers.discard(follower)
        except anyio.WouldBlock:
            pass


def exit_of(job: Job) -> int:
    """
    The exit code a follower reports: the job's own when it ended.

    A signal reads the way the shell reads it, 128 above the number;
    a job that never started has no code, so it reads 1.
    """
    if job.error is not None or job.returncode is None:
        return 1
    if job.returncode < 0:
        return 128 - job.returncode
    return job.returncode


def seconds(timeout: float) -> str:
    "A timeout the way it was said: `90`, not `90.0`."
    return "%d" % timeout if timeout == int(timeout) else "%s" % timeout


def describe(job: Job) -> str:
    "One line for `list-jobs`: id, state, age, command, and what tags it."
    if job.is_done:
        if job.error is not None:
            state = "error: %s" % job.error
        elif job.returncode is not None:
            state = "exit %d" % job.returncode
        else:
            state = "done"
        state = "done " + state
    else:
        state = "running"
    if job.pty:
        state += " pty"
    line = "%d %s %.1fs %s" % (job.job_id, state, job.age(), job.command)
    if job.tags:
        line += " [%s]" % spelled(job.tags)
    return line


def outcome_text(job: Job) -> str:
    "What a waiter reads: the tail of both streams, and what fell off."
    parts = []
    out = decode(job.kept("stdout"))
    if out:
        parts.append(out)
    err = decode(job.kept("stderr"))
    if err:
        parts.append("--- stderr ---\n%s" % err)
    for stream in ("stdout", "stderr"):
        if job.dropped(stream):
            parts.append("(%s dropped %d bytes past the cap)" % (stream, job.dropped(stream)))
    if not parts:
        return "(no output)"
    return "\n".join(parts).rstrip("\n")


def decode(data: bytes) -> str:
    "One stream's tail, decoded. What `show-job` prints."
    return data.decode("utf-8", "replace") if data else ""


def land(job: Job, name: str, data: bytes) -> None:
    "One chunk into the tail of a stream, at its offset, then the followers."
    chunks = job.stdout_chunks if name == "stdout" else job.stderr_chunks
    chunks.append((job.end(name), data))
    job.last_active = time.time()
    if name == "stdout":
        job.stdout_kept += len(data)
    else:
        job.stderr_kept += len(data)
    _trim(job, name)
    poke(job)


class _CrLf:
    """
    Fold the `\\r\\n` a terminal writes back to `\\n`, across reads.

    The terminal folds `\\n` to `\\r\\n` on the way out, so the tails
    of a pty job read the way piped ones do. A `\\r` at the end of a
    read waits for the next one rather than surviving as a stray.
    """

    def __init__(self) -> None:
        self.pending = b""

    def feed(self, data: bytes) -> bytes:
        data = self.pending + data
        if data.endswith(b"\r"):
            self.pending, data = b"\r", data[:-1]
        else:
            self.pending = b""
        return data.replace(b"\r\n", b"\n")

    def flush(self) -> bytes:
        "What waits, for the end of the stream."
        pending, self.pending = self.pending, b""
        return pending


def _trim(job: Job, name: str) -> None:
    "Forget the head of one stream past the cap, keeping the offsets."
    chunks = job.stdout_chunks if name == "stdout" else job.stderr_chunks
    kept = job.stdout_kept if name == "stdout" else job.stderr_kept
    overflow = kept - STREAM_KEEP
    while overflow > 0:
        start, oldest = chunks.popleft()
        if len(oldest) <= overflow:
            overflow -= len(oldest)
            kept -= len(oldest)
            if name == "stdout":
                job.stdout_dropped += len(oldest)
            else:
                job.stderr_dropped += len(oldest)
        else:
            chunks.appendleft((start + overflow, oldest[overflow:]))
            kept -= overflow
            if name == "stdout":
                job.stdout_dropped += overflow
            else:
                job.stderr_dropped += overflow
            overflow = 0
    if name == "stdout":
        job.stdout_kept = kept
    else:
        job.stderr_kept = kept
