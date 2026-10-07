"""
Jobs: commands the server runs without a pane, and remembers.

A pane is a view onto a program on a pty, and it dies with it. A job
is the other shape: a command with pipes instead of a terminal, owned
by the server rather than by any view, so an agent can submit it, go
away, and ask for the exit code and the output later. `run-shell`
already runs a command this way, but it shows the output once and
forgets it; a job keeps the tail of both streams, the exit, and the
times, until the table forgets it.

The streams are pumped from the start rather than collected at the
end, so `show-job` reads a job that still runs and a pane attached
later can tail it live. Each stream keeps its tail past a cap and
counts what fell off, so a job that logs forever still answers, and
says how much of its answer is missing.
"""

from __future__ import annotations

import codecs
import os
import re
import secrets
import subprocess
import time
from collections import deque
from collections.abc import Awaitable, Callable, Iterable
from contextlib import suppress
from typing import NewType

import anyio
import anyio.abc

from pymux.commands import CommandException
from pymux.jobstore import JobStore
from pymux.log import logger

#: A newline with no return before it. What the viewer turns into a
#: full return, the way ONLCR would have on a pty.
_RETURNS = re.compile(r"(?<!\r)\n")

#: What each stream of a job keeps, in bytes: the tail, and a count
#: of what fell off the front. A job that logs forever still answers.
STREAM_KEEP = 256 * 1024

#: Finished jobs the table keeps: past this the oldest goes, so a
#: server that runs one job a minute for a year holds fifty finished
#: ones, not half a million. Running jobs are never forgotten, and
#: neither is a finished one somebody still waits for.
FINISHED_KEEP = 50


#: A job's id. An int at run time -- sqlite stores it as one -- but a
#: distinct type to the checker, so a pane id, a window id, or a bare
#: number never passes where a job is expected without saying so.
JobId = NewType("JobId", int)


#: Where the agent session id comes from, in order. First-party names
#: first -- their values are session ids -- then the harness names
#: other agents document, newest thread and shell ids before a trace
#: id, which correlates but never names a session. The list follows
#: the harness survey in nixidae's pytest-agent (`_harness_detect`).
SESSION_ENV_VARS = (
    "OPENCODE_SESSION_ID",
    "OCAHUB_SESSION",
    "OC_SESSION",
    "CODEX_THREAD_ID",
    "TRAE_AI_SHELL_ID",
    "CURSOR_TRACE_ID",
)


def session_id() -> str:
    "The agent session this server runs in, or a fresh one."
    for name in SESSION_ENV_VARS:
        value = os.environ.get(name)
        if value:
            return value
    return secrets.token_hex(6)


def parse_tag(word: str) -> tuple[str, str | None]:
    """
    One `--tag`: a bare key, or a key with a value.

    A bare key carries no value -- NULL in the database, not an empty
    string -- so `key` and `key=` stay two different tags. A repeated
    key keeps its last value.
    """
    key, separator, value = word.partition("=")
    if not key:
        raise CommandException("a tag names a key: %r" % word)
    return key, value if separator else None


class Job:
    "One command the server ran, running or remembered."

    def __init__(
        self,
        job_id: JobId,
        command: str,
        directory: str | None,
        started: float | None = None,
        tags: Iterable[tuple[str, str | None]] = (),
        session: str | None = None,
    ) -> None:
        self.job_id = job_id
        self.command = command
        self.directory = directory
        #: Whose job this is: the agent session that ran it. Lookups
        #: stay inside it, so one agent never waits on another's job
        #: by mistake.
        self.session = session
        #: What the job is called by, as key to value: a bare key
        #: carries no value, and a repeated key keeps its last one.
        self.tags: dict[str, str | None] = dict(tags)
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
        self.followers: set = set()
        #: Set when the job ends, whatever ended it.
        self.done = anyio.Event()
        #: Commands waiting on `done`, so reaping never takes a job
        #: somebody still waits for.
        self.waiters = 0
        self.process: anyio.abc.Process | None = None
        #: Set by `kill-job` before the process exists: the supervise
        #: ends it as soon as it starts.
        self.kill_requested = False

    @property
    def is_done(self) -> bool:
        return self.status == "done"

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


class JobTable:
    """
    Every job, by id. Ids never repeat, so `wait-job 3` means one job.

    The live jobs live in a dict; what a question could filter on lives
    in sqlite beside it, written at every transition. The dict answers
    the live operations -- get, wait, kill, follow -- and the database
    answers the questions; neither mirrors the other's shape, and both
    change in the same step, so they cannot disagree.
    """

    def __init__(self) -> None:
        self._jobs: dict[JobId, Job] = {}
        self.store = JobStore()
        #: The session every job is stamped with unless told
        #: otherwise. Read once: the server's environment does not
        #: change under it, so reading it per job would only invent
        #: a new random one per job when nothing is set.
        self.session = session_id()

    async def open(self) -> None:
        "Build the database. The server calls this; the rest is lazy."
        await self.store.open()

    async def close(self) -> None:
        "Forget the database. In-memory, so this is also forgetting."
        await self.store.close()

    async def submit(
        self,
        command: str,
        directory: str | None = None,
        tags: Iterable[tuple[str, str | None]] = (),
        session: str | None = None,
    ) -> Job:
        """
        Record a job. Starting it is `supervise`, in the caller's task.

        The id comes from the database, so it is the row and the job
        or neither: nothing hands out an id the table cannot answer.
        The tags and the session go down with the row, in the same step.
        """
        now = time.time()
        stamped = session if session is not None else self.session
        cursor = await self.store.write(
            "INSERT INTO jobs(command, directory, status, started, session) VALUES (?, ?, 'running', ?, ?)",
            (command, directory, now, stamped),
        )
        job = Job(JobId(cursor.lastrowid), command, directory, now, tags, stamped)
        self._jobs[job.job_id] = job
        for key, value in job.tags.items():
            await self.store.write(
                "INSERT OR REPLACE INTO job_tags(job_id, key, value) VALUES (?, ?, ?)",
                (job.job_id, key, value),
            )
        return job

    def get(self, job_id: JobId) -> Job | None:
        return self._jobs.get(job_id)

    def listing(self) -> list[Job]:
        "Every job, oldest first, which is id order."
        return [self._jobs[key] for key in sorted(self._jobs)]

    def resolve(self, tags: list[tuple[str, str | None]], session: str) -> Job | None:
        """
        The newest job in this session carrying every tag, or nothing.

        Tags are not unique -- the same key names a series of jobs --
        so the newest wins: later submits start later, and the id
        breaks a tie inside one instant. A bare key matches whatever
        value it carries; a valued tag matches that value exactly.
        Nothing crosses sessions: another session's job is not a
        surprising match, it is no match at all.
        """
        found = [job for job in self._jobs.values() if job.session == session and _matches(job, tags)]
        if not found:
            return None
        return max(found, key=lambda job: (job.started, job.job_id))

    async def supervise(self, job: Job) -> None:
        """
        Run the job to its end: pump both streams, record the exit,
        wake the waiters, forget the oldest finished past the cap.

        The pumps run in a group of their own, so both streams drain
        together and the exit is recorded only once both hit EOF: the
        output is complete before `done` is set, and a waiter never
        reads half of it.
        """
        try:
            process = await anyio.open_process(
                job.command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=job.directory,
                env={**os.environ, "PYMUX_JOB": str(job.job_id)},
            )
        except OSError as e:
            job.error = "%s: %s" % (job.command, e)
            await self._finish(job, None)
            return

        job.process = process
        if job.kill_requested:
            process.terminate()

        try:
            async with anyio.create_task_group() as pumps:
                pumps.start_soon(_pump, process.stdout, job, "stdout")
                pumps.start_soon(_pump, process.stderr, job, "stderr")
                job.returncode = await process.wait()
        except BaseException as e:
            # The server is going down, or the pumps failed: no
            # orphan. A failed pump fails the job, never the server.
            with suppress(Exception):
                process.kill()
            if isinstance(e, anyio.get_cancelled_exc_class()):
                if job.returncode is None:
                    job.error = "stopped with the server"
                raise
            logger.exception("Running a job failed.")
            if job.returncode is None and job.error is None:
                job.error = "%s: %s" % (job.command, e)
        finally:
            if job.status == "running":
                await self._finish(job, job.returncode)

    async def _finish(self, job: Job, returncode: int | None) -> None:
        job.returncode = returncode
        job.status = "done"
        job.finished = time.time()
        await self.store.write(
            "UPDATE jobs SET status = 'done', returncode = ?, error = ?, finished = ? WHERE id = ?",
            (returncode, job.error, job.finished, job.job_id),
        )
        job.done.set()
        _poke(job)
        await self._reap()

    async def _reap(self) -> None:
        "Forget the oldest finished past the cap. The waited stay."
        finished = [job for job in self.listing() if job.is_done]
        while len(finished) > FINISHED_KEEP:
            oldest = finished[0]
            if oldest.waiters:
                return
            del self._jobs[oldest.job_id]
            await self.store.write("DELETE FROM jobs WHERE id = ?", (oldest.job_id,))
            finished.pop(0)

    async def wait(self, job: Job) -> Job:
        "Hold until the job ends, then answer it. Reaping waits too."
        job.waiters += 1
        try:
            await job.done.wait()
        finally:
            job.waiters -= 1
        return job

    def kill(self, job: Job) -> bool:
        """
        End a running job with SIGTERM. True when there was a job to
        end; a job that already ended answers that instead. No
        escalation: a program that ignores SIGTERM keeps running, and
        that is what `show-job` then says.
        """
        if job.is_done:
            return False
        job.kill_requested = True
        if job.process is not None:
            job.process.terminate()
        return True


def _poke(job: Job) -> None:
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


class _NoBackend:
    "No pty behind a viewer: `#{pane_pid}` reads blank."

    pid = None


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
    changed; `start_soon` puts the follow in the server's task group.
    """

    def __init__(
        self,
        job: Job,
        feed_output: Callable[[str], None],
        start_soon: Callable[[Awaitable[None]], None] | None,
    ) -> None:
        self.job = job
        self._feed_output = feed_output
        self._start_soon = start_soon
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
        self._hint = None

    def start(self) -> None:
        "Replay the tail and follow the rest, in the server's task group."
        if self._started:
            return
        self._started = True
        if self._start_soon is None:
            raise RuntimeError("A viewer was made outside `Pymux.running`, so nothing would follow the job.")
        # A viewer is a waiter: reaping never takes a job somebody
        # still watches.
        self.job.waiters += 1
        self._start_soon(self._follow)

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

    def _feed_new(self, stream: str, offset: int, decode) -> int:
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


def _matches(job: Job, tags: list[tuple[str, str | None]]) -> bool:
    "Whether the job carries every tag: a bare key takes any value."
    for key, value in tags:
        if key not in job.tags:
            return False
        if value is not None and job.tags[key] != value:
            return False
    return True


def find_job(pymux, job: int | None, words: list[str], session: str | None) -> Job:
    """
    The job an id or a set of tags names: the id as given, or the
    newest job carrying every tag in this session.

    An id and tags together refuse, and so does neither: guessing
    which of the two was meant is how the wrong job gets waited on.
    A tag that names nothing here names nothing anywhere else either
    -- no session is ever searched but this one.
    """
    table: JobTable = pymux.jobs
    if job is not None and words:
        raise CommandException("give a job id or --tag, not both")
    if job is not None:
        found = table.get(JobId(job))
        if found is None:
            raise CommandException("no job %d" % job)
        return found
    if not words:
        raise CommandException("give a job id or --tag")
    tags = [parse_tag(word) for word in words]
    current = session if session is not None else table.session
    found = table.resolve(tags, current)
    if found is None:
        raise CommandException("no job tagged %s in this session" % ",".join(words))
    return found


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
    line = "%d %s %.1fs %s" % (job.job_id, state, job.age(), job.command)
    if job.tags:
        tagged = ",".join(key if value is None else "%s=%s" % (key, value) for key, value in job.tags.items())
        line += " [%s]" % tagged
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


async def _pump(stream: anyio.abc.ByteStream, job: Job, name: str) -> None:
    """
    Drain one stream into the tail the job keeps, counting the rest.

    A chunk that straddles the cap is split: its head falls off and
    its tail stays, so the tail is the cap to the byte no matter how
    the pipe read. Every chunk lands with its offset in one
    synchronous step, then pokes the followers, so a follower that
    reads the offsets sees the chunk they count. The loop holds no
    other await, so a cancellation never lands with the tail half
    written.
    """
    chunks = job.stdout_chunks if name == "stdout" else job.stderr_chunks
    try:
        async for data in stream:
            chunks.append((job.end(name), data))
            if name == "stdout":
                job.stdout_kept += len(data)
            else:
                job.stderr_kept += len(data)
            _trim(job, name)
            _poke(job)
    finally:
        with suppress(Exception):
            await stream.aclose()


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
