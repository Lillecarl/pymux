"""
The server's jobs: submitting one, running it on pipes or on a terminal
of its own, following and waiting for it, and forgetting the finished
ones past `FINISHED_KEEP`.
"""

from __future__ import annotations

import codecs
import errno
import os
import signal
import time
from collections.abc import Callable, Iterable, Mapping
from contextlib import suppress
from typing import TYPE_CHECKING, ClassVar

import anyio
import anyio.abc
from anyio.streams.memory import MemoryObjectReceiveStream
from ptyhost.backends.posix import PosixBackend
from ptyhost.spawn import Spawn, fork_on_pipes
from pyte.keep import Keep

from pymux.commands import CommandException
from pymux.query import SEEDS
from pymux.tags import carries, parse_tag

if TYPE_CHECKING:
    from pymux.core.main import Pymux

from .job import Job, JobId, decode, exit_of, land, poke, seconds
from .pipes import JobPipes

__all__ = ["FINISHED_KEEP", "JOB_SWEEP_EVERY", "PTY_COLUMNS", "PTY_ROWS", "PTY_TERM", "JobTable", "find_job"]


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
        "saved_queries": Keep.SAVED,
        "forgotten_seeds": Keep.SAVED,
    }

    def __init__(self) -> None:
        self._jobs: dict[JobId, Job] = {}
        #: The newest id handed out. It outlives the jobs it named, so
        #: a restored server never hands one out again.
        self.last_id = 0
        #: The questions a person or an agent saved, by name: their SQL
        #: and description. Only these reach a snapshot. Lillecarl/pymux#547.
        self.saved_queries: dict[str, tuple[str, str | None]] = {}
        #: The seeds somebody took away. An upgrade keeps them away, and
        #: brings every seed it adds.
        self.forgotten_seeds: set[str] = set()

    @property
    def queries(self) -> dict[str, tuple[str, str | None]]:
        """
        Every kept question, as `sql` reads the table `saved_queries`:
        this build's seeds, and what was saved over or beside them.

        **The seeds are never saved.** A snapshot that kept them would
        give an upgraded server the seeds of the build it came from, and
        a seed this build adds -- `complete-option` -- would be missing.
        """
        seeded = {name: (sql, description) for name, sql, description in SEEDS if name not in self.forgotten_seeds}
        return seeded | self.saved_queries

    def save_query(self, name: str, sql: str, description: str | None) -> None:
        "Keep a question, or replace the one of that name."
        self.saved_queries[name] = (sql, description)

    def forget_query(self, name: str) -> bool:
        "Forget a kept question. False when none had that name."
        known = name in self.queries
        self.saved_queries.pop(name, None)
        if any(seed == name for seed, _sql, _description in SEEDS):
            self.forgotten_seeds.add(name)
        return known

    def submit(
        self,
        command: str,
        directory: str | None = None,
        tags: Iterable[tuple[str, str | None]] = (),
        env: Mapping[str, str] | None = None,
        pty: bool = False,
    ) -> Job:
        """
        Record a job. Starting it is `supervise`, in the caller's task.

        The environment falls back to the server's own: `run` passes what
        the calling packet said, and anything else -- a key binding,
        an old client -- runs where the server stands.
        """
        self.last_id += 1
        job = Job(JobId(self.last_id), command, directory, time.time(), tags, env or dict(os.environ), pty)
        self._jobs[job.job_id] = job
        return job

    def restore(self) -> None:
        "Set the done event of each finished job a snapshot's load put here."
        for job in self.listing():
            if job.is_done:
                job.done.set()

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
        found = [
            job for job in self._jobs.values() if (scope is None or job.session == scope) and carries(job.tags, tags)
        ]
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

        This server forks every job itself, so an upgrade's exec keeps
        it: the new image adopts its pty or its pipes, which `backend`
        holds then.
        """
        if isinstance(job.backend, PosixBackend):
            await self._supervise_on_a_terminal(job, job.backend)
            return
        if isinstance(job.backend, JobPipes):
            await self._supervise_on_pipes(job, job.backend)
            return
        if job.pty:
            backend = self._terminal_for(job)
            if backend is None:
                self._finish(job, None)
            else:
                await self._supervise_on_a_terminal(job, backend)
            return
        await self._supervise_on_pipes(job, None)

    def _spawn_of(self, job: Job, env: Mapping[str, str]) -> Spawn:
        """
        What starts the job: the caller's environment, as the submit
        stamped it, with the id last, so nothing a caller sent overrides
        which job this is. The child gives back a raised priority itself.
        """
        return Spawn(["/bin/sh", "-c", job.command], {**env, "PYMUX_JOB": str(job.job_id)}, job.directory)

    def _terminal_for(self, job: Job) -> PosixBackend | None:
        """
        A terminal for a pty job, or None when the job cannot start, with
        the reason on it. The child would start where the server is when
        the directory is gone, so a gone one is refused here.
        """
        env = dict(job.env)
        env.setdefault("TERM", PTY_TERM)
        spawn = self._spawn_of(job, env)
        try:
            if job.directory is not None and not os.path.isdir(job.directory):
                raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), job.directory)
            backend = PosixBackend(lambda: spawn)
        except OSError as e:
            job.error = "%s: %s" % (job.command, e)
            return None
        backend.set_size(PTY_COLUMNS, PTY_ROWS)
        return backend

    async def _supervise_on_a_terminal(self, job: Job, backend: PosixBackend) -> None:
        """
        Run the job on a terminal: one pty for all three streams.

        The child is its own session, so the server's terminal signals
        never reach it, but `isatty` answers yes on every stream.
        Everything the pty says lands in the stdout tail with `\\r\\n`
        folded back to `\\n`, so the viewer, `show-job` and the waiters
        read a pty job exactly the way they read a piped one; the stderr
        tail stays empty.

        A job a snapshot loaded comes with its terminal adopted, and goes
        on where the image before the exec stopped. The exit is recorded
        once the pump read the end and the reap said how it ended.
        """

        async def read_some() -> None:
            data = job.crlf.feed(backend.read_bytes(65536))
            if data:
                land(job, "stdout", data)

        backend.add_input_ready_callback(read_some)
        try:
            async with anyio.create_task_group() as pumps:
                await backend.start(pumps)
                job.backend = backend
                job.pid = backend.pid
                if job.kill_requested:
                    backend.send_signal(signal.SIGTERM)
            if job.crlf.pending:
                land(job, "stdout", job.crlf.flush())
            job.returncode = backend.returncode
        except anyio.get_cancelled_exc_class():
            # The reap killed it: a job does not outlive its server.
            if job.returncode is None:
                job.error = "stopped with the server"
            raise
        finally:
            if job.status == "running":
                self._finish(job, job.returncode)

    async def _supervise_on_pipes(self, job: Job, pipes: JobPipes | None) -> None:
        """
        Run a piped job: nothing on stdin, a pipe each for stdout and
        stderr, and both pumped into the job's tails. A job a snapshot
        loaded comes with its pipes adopted.
        """
        if pipes is None:
            try:
                pid, stdout, stderr = fork_on_pipes(self._spawn_of(job, job.env))
            except OSError as e:
                job.error = "%s: %s" % (job.command, e)
                self._finish(job, None)
                return
            pipes = JobPipes(pid, stdout, stderr, parent=True)
            job.backend, job.pid = pipes, pid
            if job.kill_requested:
                pipes.send_signal(signal.SIGTERM)
        try:
            async with anyio.create_task_group() as pumps:
                pumps.start_soon(pipes.pump, pipes.stdout, job, "stdout")
                pumps.start_soon(pipes.pump, pipes.stderr, job, "stderr")
                job.returncode = await pipes.reap()
        except anyio.get_cancelled_exc_class():
            # The server is going down: no orphan.
            pipes.send_signal(signal.SIGKILL)
            if job.returncode is None:
                job.error = "stopped with the server"
            raise
        finally:
            if job.status == "running":
                self._finish(job, job.returncode)

    def pause_reading(self) -> None:
        "Read no running job, so that what it writes waits for the next image."
        for job in self.listing():
            if job.backend is not None and not job.is_done:
                job.backend.pause_reading()
                if job.crlf.pending:
                    land(job, "stdout", job.crlf.flush())

    def resume_reading(self) -> None:
        "Read the running jobs again, as before the pause."
        for job in self.listing():
            if job.backend is not None and not job.is_done:
                job.backend.resume_reading()

    def _finish(self, job: Job, returncode: int | None) -> None:
        job.returncode = returncode
        job.status = "done"
        job.finished = time.time()
        job.last_active = job.finished
        job.done.set()
        poke(job)
        self._reap()

    def _reap(self) -> None:
        "Forget the oldest finished past the cap. The waited stay."
        finished = [job for job in self.listing() if job.is_done]
        while len(finished) > FINISHED_KEEP:
            oldest = finished[0]
            if oldest.waiters:
                return
            self._forget(oldest)
            finished.pop(0)

    def _forget(self, job: Job) -> None:
        """
        Forget one job, and its tags with it.

        A supervise that still runs does not mind: it finishes onto
        the job, whose update then matches no row, and the reap finds
        no listing. A waiter that arrives after the forgetting finds
        no job, the way one arriving after the cap does.
        """
        self._jobs.pop(job.job_id, None)

    def sweep(self, ttl_seconds: float) -> None:
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
            self._forget(job)

    async def wait(self, job: Job) -> Job:
        "Hold until the job ends, then answer it. Reaping waits too."
        job.waiters += 1
        try:
            await job.done.wait()
        finally:
            job.waiters -= 1
        return job

    async def follow(self, job: Job, lines: int, timeout: float | None, emit: Callable[[str], None]) -> int:
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
                        emit("[tail: job %d still running after %ss]" % (job.job_id, seconds(timeout)))
                        return 124
            if job.error is not None:
                emit("[job %d never started: %s]" % (job.job_id, job.error))
            elif not job.is_done:
                # No waiting asked: the replay above is the answer,
                # and 124 says the job is still running.
                return 124
            return exit_of(job)
        finally:
            job.followers.discard(send)
            with suppress(Exception):
                await send.aclose()
                await recv.aclose()
            job.waiters -= 1

    async def _drain(
        self,
        job: Job,
        recv: MemoryObjectReceiveStream[None],
        out_decode: codecs.IncrementalDecoder,
        err_decode: codecs.IncrementalDecoder,
        emit: Callable[[str], None],
    ) -> None:
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
        if job.backend is not None:
            job.backend.send_signal(signal.SIGTERM)
        return True


def find_job(pymux: Pymux, job: int | None, words: list[str], session: str | None) -> Job:
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
    caller = pymux.caller
    found = table.resolve(tags, hint=caller.session_id if caller else None)
    if found is None:
        raise CommandException("no job tagged %s" % ",".join(words))
    return found
