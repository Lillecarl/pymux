"""
Jobs: commands the server runs without a pane, and remembers.

A pane is a view onto a program on a pty, and it dies with it. A job
is the other shape: a command with pipes instead of a terminal, owned
by the server rather than by any view, so an agent can submit it, go
away, and ask for the exit code and the output later. `run-shell`
already runs a command this way, but it shows the output once and
forgets it; a job keeps the tail of both streams, the exit, and the
times, until the table forgets it. `run --pty` is the exception to
the pipes: the command runs on a terminal of its own, for the program
that needs one to behave, and everything else stays the same.

The streams are pumped from the start rather than collected at the
end, so `show-job` reads a job that still runs and a pane attached
later can tail it live. Each stream keeps its tail past a cap and
counts what fell off, so a job that logs forever still answers, and
says how much of its answer is missing.
"""

from __future__ import annotations

import codecs
import fcntl
import os
import pty
import re
import struct
import subprocess
import termios
import time
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from contextlib import suppress
from typing import ClassVar, NewType

import anyio
import anyio.abc
from anyio.streams.memory import MemoryObjectSendStream
from pyte.keep import Keep

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

#: How often the idle sweep looks: a minute. The time to live is an
#: hour by default, so a minute's lateness never matters, and a test
#: in a hurry calls `sweep` itself rather than waiting for the clock.
JOB_SWEEP_EVERY = 60

#: The size a pty job's terminal reports: no viewer sizes it, so it
#: starts at the common default and stays there.
PTY_ROWS = 24
PTY_COLUMNS = 80

#: The terminal a pty job claims when the caller sent no TERM: enough
#: for colors and cursor motion, without promising anything rarer.
PTY_TERM = "xterm-256color"


#: A job's id. An int at run time -- sqlite stores it as one -- but a
#: distinct type to the checker, so a pane id, a window id, or a bare
#: number never passes where a job is expected without saying so.
JobId = NewType("JobId", int)


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
    #: A snapshot holds finished jobs only: a running one refuses it.
    #: Lillecarl/pymux#399.
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
        "done": Keep.REBUILT,  # set: the job is finished
        "followers": Keep.DROPPED,  # the clients that followed went with the exec
        "waiters": Keep.DROPPED,  # and so did the commands that waited
        "process": Keep.DROPPED,
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
        self.tags: dict[str, str | None] = dict(tags)
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


class JobTable:
    """
    Every job, by id. Ids never repeat, so `wait-job 3` means one job.

    The live jobs live in a dict; what a question could filter on lives
    in sqlite beside it, written at every transition. The dict answers
    the live operations -- get, wait, kill, follow -- and the database
    answers the questions; neither mirrors the other's shape, and both
    change in the same step, so they cannot disagree.
    """

    #: What a hot upgrade does with each attribute; `pyte.keep` says.
    KEEP: ClassVar[dict[str, Keep]] = {
        "_jobs": Keep.SAVED,
        "last_id": Keep.SAVED,
        "store": Keep.REBUILT,  # in memory: `restore` writes the rows again
    }

    def __init__(self) -> None:
        self._jobs: dict[JobId, Job] = {}
        self.store = JobStore()
        #: The newest id the database handed out. It outlives the jobs
        #: it named, so a restored server never hands one out again.
        self.last_id = 0

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
        env: Mapping[str, str] | None = None,
        pty: bool = False,
    ) -> Job:
        """
        Record a job. Starting it is `supervise`, in the caller's task.

        The id comes from the database, so it is the row and the job
        or neither: nothing hands out an id the table cannot answer.
        The tags go down with the row, in the same step. The
        environment falls back to the server's own: `run` passes what
        the calling packet said, and anything else -- a key binding,
        an old client -- runs where the server stands.
        """
        now = time.time()
        cursor = await self.store.write(
            "INSERT INTO jobs(command, directory, status, started, pty) VALUES (?, ?, 'running', ?, ?)",
            (command, directory, now, 1 if pty else 0),
        )
        job = Job(JobId(cursor.lastrowid), command, directory, now, tags, env or dict(os.environ), pty)
        self.last_id = max(self.last_id, job.job_id)
        self._jobs[job.job_id] = job
        for key, value in job.tags.items():
            await self.store.write(
                "INSERT OR REPLACE INTO job_tags(job_id, key, value) VALUES (?, ?, ?)",
                (job.job_id, key, value),
            )
        return job

    async def restore(self) -> None:
        """
        Write the rows of the finished jobs a snapshot's load put here.

        The database is in memory and the exec emptied it. The sequence
        goes back too, so the next id is past every id ever given.
        """
        for job in self.listing():
            job.done.set()
            await self.store.write(
                "INSERT OR REPLACE INTO jobs(id, command, directory, status, returncode, error, started, finished, pty)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    job.job_id,
                    job.command,
                    job.directory,
                    job.status,
                    job.returncode,
                    job.error,
                    job.started,
                    job.finished,
                    1 if job.pty else 0,
                ),
            )
            for key, value in job.tags.items():
                await self.store.write(
                    "INSERT OR REPLACE INTO job_tags(job_id, key, value) VALUES (?, ?, ?)",
                    (job.job_id, key, value),
                )
        # `sqlite_sequence` has no key, so a second row for the table
        # would be added, not replaced.
        await self.store.write("DELETE FROM sqlite_sequence WHERE name = 'jobs'")
        await self.store.write("INSERT INTO sqlite_sequence(name, seq) VALUES ('jobs', ?)", (self.last_id,))

    def get(self, job_id: JobId) -> Job | None:
        return self._jobs.get(job_id)

    def listing(self) -> list[Job]:
        "Every job, oldest first, which is id order."
        return [self._jobs[key] for key in sorted(self._jobs)]

    def resolve(
        self,
        tags: list[tuple[str, str | None]],
        hint: str | None = None,
        scope: str | None = None,
    ) -> Job | None:
        """
        The job a set of tags names: the newest carrying every tag.

        Tags are not unique -- the same key names a series of jobs --
        so the newest wins: later submits start later, and the id
        breaks a tie inside one instant. A bare key matches whatever
        value it carries; a valued tag matches that value exactly.

        A session only prioritizes, never filters. The hint is the
        calling packet's session: its jobs sort first, and anything
        else is the fallback rather than a miss, so an agent finds its
        own job and still reaches another's. The scope is an explicit
        `--session`: only that session is searched at all. Neither
        given is pure newness, the way lookups always read.
        """
        found = [job for job in self._jobs.values() if (scope is None or job.session == scope) and _matches(job, tags)]
        if not found:
            return None
        return max(found, key=lambda job: (hint is not None and job.session == hint, job.started, job.job_id))

    async def supervise(self, job: Job) -> None:
        """
        Run the job to its end: pump both streams, record the exit,
        wake the waiters, forget the oldest finished past the cap.

        The pumps run in a group of their own, so both streams drain
        together and the exit is recorded only once both hit EOF: the
        output is complete before `done` is set, and a waiter never
        reads half of it.
        """
        if job.pty:
            await self._supervise_pty(job)
            return
        try:
            process = await anyio.open_process(
                job.command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=job.directory,
                # The caller's environment, as the submit stamped it,
                # with the id last: nothing a caller sent overrides
                # which job this is.
                env={**job.env, "PYMUX_JOB": str(job.job_id)},
            )
        except OSError as e:
            job.error = "%s: %s" % (job.command, e)
            await self._finish(job, None)
            return

        job.process = process
        if job.kill_requested:
            process.terminate()

        stdout, stderr = process.stdout, process.stderr
        if stdout is None or stderr is None:
            raise RuntimeError("A job opened with two pipes came back without one.")
        try:
            async with anyio.create_task_group() as pumps:
                pumps.start_soon(_pump, stdout, job, "stdout")
                pumps.start_soon(_pump, stderr, job, "stderr")
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

    async def _supervise_pty(self, job: Job) -> None:
        """
        Run the job on a terminal: one pty for all three streams.

        The child is its own session without a controlling terminal,
        so the server's terminal signals never reach it, but `isatty`
        answers yes on every stream. Everything the pty says lands in
        the stdout tail with `\\r\\n` folded back to `\\n`, so the
        viewer, `show-job` and the waiters read a pty job exactly the
        way they read a piped one; the stderr tail stays empty.

        The pump drains the primary in a thread and closes it when it
        ends, so a cancelled supervise releases the reader rather
        than leaving it on the fd. The exit is recorded only once the
        drain and the wait both finish, the way the two pumps gate a
        piped job.
        """
        try:
            primary, replica = pty.openpty()
        except OSError as e:
            job.error = "%s: %s" % (job.command, e)
            await self._finish(job, None)
            return
        with suppress(OSError):
            fcntl.ioctl(primary, termios.TIOCSWINSZ, struct.pack("HHHH", PTY_ROWS, PTY_COLUMNS, 0, 0))
        env = dict(job.env)
        env.setdefault("TERM", PTY_TERM)
        try:
            process = await anyio.open_process(
                job.command,
                stdin=replica,
                stdout=replica,
                stderr=replica,
                cwd=job.directory,
                env={**env, "PYMUX_JOB": str(job.job_id)},
                start_new_session=True,
            )
        except OSError as e:
            job.error = "%s: %s" % (job.command, e)
            await self._finish(job, None)
            return
        finally:
            # The child holds its own end; the parent's copy would
            # hold the primary open past every exit.
            with suppress(OSError):
                os.close(replica)

        job.process = process
        if job.kill_requested:
            process.terminate()

        try:
            async with anyio.create_task_group() as pumps:
                pumps.start_soon(_pump_pty, primary, job)
                job.returncode = await process.wait()
        except BaseException as e:
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
        job.last_active = job.finished
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
            await self._forget(oldest)
            finished.pop(0)

    async def _forget(self, job: Job) -> None:
        """
        Forget one job: out of the table and out of the database, tags
        with it through the cascade.

        A supervise that still runs does not mind: it finishes onto
        the job, whose update then matches no row, and the reap finds
        no listing. A waiter that arrives after the forgetting finds
        no job, the way one arriving after the cap does.
        """
        self._jobs.pop(job.job_id, None)
        await self.store.write("DELETE FROM jobs WHERE id = ?", (job.job_id,))

    async def sweep(self, ttl_seconds: float) -> None:
        """
        Forget every job idle past its time, ending the running ones.

        Idleness is output or completion, whichever came last, so a
        job that logs stays while a job that hangs silent goes. Zero
        keeps everything: it is how long nothing has lasted. The
        waited stay -- somebody watches, so nothing here is garbage.
        """
        if not ttl_seconds > 0:
            return
        now = time.time()
        for job in self.listing():
            if job.waiters or now - job.last_active < ttl_seconds:
                continue
            if not job.is_done:
                self.kill(job)
            await self._forget(job)

    async def wait(self, job: Job) -> Job:
        "Hold until the job ends, then answer it. Reaping waits too."
        job.waiters += 1
        try:
            await job.done.wait()
        finally:
            job.waiters -= 1
        return job

    async def follow(self, job: Job, lines: int, timeout: float | None, emit) -> int:
        """
        Replay the tail and follow the job live, and answer its exit.

        The replay is the last `lines` of stdout, nothing when zero;
        the follow feeds both streams in the order they arrive. The
        end answers the exit code: the job's own when it ends, 124
        past the timeout with the job still running, 1 when it never
        started. A follower watches, so the sweep and the cap leave
        the job alone until this returns.
        """
        replay = decode(job.kept("stdout")).splitlines()
        if lines and replay:
            emit("\n".join(replay[-lines:]))
        job.waiters += 1
        send, recv = anyio.create_memory_object_stream(1)
        job.followers.add(send)
        try:
            out_decode = codecs.getincrementaldecoder("utf-8")("replace")
            err_decode = codecs.getincrementaldecoder("utf-8")("replace")
            if timeout is None or timeout > 0:
                if timeout is None:
                    await self._drain(job, recv, out_decode, err_decode, emit)
                else:
                    with anyio.move_on_after(timeout) as scope:
                        await self._drain(job, recv, out_decode, err_decode, emit)
                    if scope.cancel_called:
                        emit("[tail: job %d still running after %ss]" % (job.job_id, _seconds(timeout)))
                        return 124
            if job.error is not None:
                emit("[job %d never started: %s]" % (job.job_id, job.error))
            elif not job.is_done:
                # No waiting asked: the replay above is the answer,
                # and 124 says the job is still running.
                return 124
            return _exit_of(job)
        finally:
            job.followers.discard(send)
            with suppress(Exception):
                await send.aclose()
                await recv.aclose()
            job.waiters -= 1

    async def _drain(self, job, recv, out_decode, err_decode, emit) -> None:
        "Feed everything new until the job ends with nothing left."
        out_offset, err_offset = job.end("stdout"), job.end("stderr")
        while True:
            fed = False
            for stream, offset, decoder in (("stdout", out_offset, out_decode), ("stderr", err_offset, err_decode)):
                end, data = job.read(stream, offset)
                if data:
                    text = decoder.decode(data)
                    if text:
                        emit(text)
                    fed = True
                if stream == "stdout":
                    out_offset = end
                else:
                    err_offset = end
            if not fed:
                if job.is_done:
                    for text in (out_decode.decode(b"", True), err_decode.decode(b"", True)):
                        if text:
                            emit(text)
                    return
                await recv.receive()

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
    """

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
    The job an id or a set of tags names: the id as given, or what the
    tags resolve to.

    An id and tags together refuse, and so does neither: guessing
    which of the two was meant is how the wrong job gets waited on. A
    tag that names nothing names nothing anywhere: the session never
    hides a job, it only sorts the caller's own first, unless an
    explicit `--session` scoped the search to that session alone.
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
    if session is not None:
        found = table.resolve(tags, scope=session)
        if found is None:
            raise CommandException("no job tagged %s in session %s" % (",".join(words), session))
        return found
    caller = pymux.caller_context
    found = table.resolve(tags, hint=caller.session_id if caller else None)
    if found is None:
        raise CommandException("no job tagged %s" % ",".join(words))
    return found


def _exit_of(job: Job) -> int:
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


def _seconds(timeout: float) -> str:
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


async def _pump(stream: anyio.abc.ByteReceiveStream, job: Job, name: str) -> None:
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
            job.last_active = time.time()
            if name == "stdout":
                job.stdout_kept += len(data)
            else:
                job.stderr_kept += len(data)
            _trim(job, name)
            _poke(job)
    finally:
        with suppress(Exception):
            await stream.aclose()


async def _pump_pty(primary: int, job: Job) -> None:
    """
    Drain a pty job's primary into the stdout tail the job keeps.

    The read runs in a thread: the fd blocks, and nothing else here
    may wait on it. The terminal folds `\\n` to `\\r\\n` on the way
    out, so the fold comes off before storing and the tails read the
    way piped ones do; a `\\r` split across two reads rides along to
    the next one rather than surviving as a stray. The primary closes
    here at the end, which also releases a reader a cancelled
    supervise left on the fd.
    """
    pending = b""
    try:
        while True:
            try:
                data = await anyio.to_thread.run_sync(os.read, primary, 65536)
            except OSError:
                # The last write end closed: on a pty that reads as
                # EIO rather than EOF.
                break
            if not data:
                break
            data = pending + data
            if data.endswith(b"\r"):
                pending, data = b"\r", data[:-1]
            else:
                pending = b""
            data = data.replace(b"\r\n", b"\n")
            if data:
                job.stdout_chunks.append((job.end("stdout"), data))
                job.last_active = time.time()
                job.stdout_kept += len(data)
                _trim(job, "stdout")
                _poke(job)
        if pending:
            job.stdout_chunks.append((job.end("stdout"), pending))
            job.last_active = time.time()
            job.stdout_kept += len(pending)
            _trim(job, "stdout")
            _poke(job)
    finally:
        with suppress(OSError):
            os.close(primary)


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
