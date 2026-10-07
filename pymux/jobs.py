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

import os
import subprocess
import time
from collections import deque
from contextlib import suppress

import anyio
import anyio.abc

#: What each stream of a job keeps, in bytes: the tail, and a count
#: of what fell off the front. A job that logs forever still answers.
STREAM_KEEP = 256 * 1024

#: Finished jobs the table keeps: past this the oldest goes, so a
#: server that runs one job a minute for a year holds fifty finished
#: ones, not half a million. Running jobs are never forgotten, and
#: neither is a finished one somebody still waits for.
FINISHED_KEEP = 50


class Job:
    "One command the server ran, running or remembered."

    def __init__(self, job_id: int, command: str, directory: str | None) -> None:
        self.job_id = job_id
        self.command = command
        self.directory = directory
        #: `running` until the process ends or fails to start.
        self.status = "running"
        #: What the process exited with, or the negative signal that
        #: ended it. None while it runs, and when it never started.
        self.returncode: int | None = None
        #: Why it never started, when starting it raised.
        self.error: str | None = None
        self.started = time.time()
        self.finished: float | None = None
        self.stdout: deque[bytes] = deque()
        self.stderr: deque[bytes] = deque()
        self.stdout_kept = 0
        self.stderr_kept = 0
        self.stdout_dropped = 0
        self.stderr_dropped = 0
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
        chunks = self.stdout if stream == "stdout" else self.stderr
        return b"".join(chunks)

    def dropped(self, stream: str) -> int:
        "What fell off one stream past the cap."
        return self.stdout_dropped if stream == "stdout" else self.stderr_dropped

    def age(self) -> float:
        "How long the job ran, or has run."
        return (self.finished or time.time()) - self.started


class JobTable:
    "Every job, by id. Ids never repeat, so `wait-job 3` means one job."

    def __init__(self) -> None:
        self._next_id = 1
        self._jobs: dict[int, Job] = {}

    def submit(self, command: str, directory: str | None = None) -> Job:
        "Record a job. Starting it is `supervise`, in the caller's task."
        job = Job(self._next_id, command, directory)
        self._next_id += 1
        self._jobs[job.job_id] = job
        return job

    def get(self, job_id: int) -> Job | None:
        return self._jobs.get(job_id)

    def listing(self) -> list[Job]:
        "Every job, oldest first, which is id order."
        return [self._jobs[key] for key in sorted(self._jobs)]

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
            self._finish(job, None)
            return

        job.process = process
        if job.kill_requested:
            process.terminate()

        try:
            async with anyio.create_task_group() as pumps:
                pumps.start_soon(_pump, process.stdout, job.stdout, job, "stdout")
                pumps.start_soon(_pump, process.stderr, job.stderr, job, "stderr")
                job.returncode = await process.wait()
        except BaseException:
            # The server is going down, or the pumps failed: no
            # orphan. The waiters hear that the job ended, with why.
            with suppress(Exception):
                process.kill()
            if job.returncode is None:
                job.error = "stopped with the server"
            raise
        finally:
            if job.status == "running":
                self._finish(job, job.returncode)

    def _finish(self, job: Job, returncode: int | None) -> None:
        job.returncode = returncode
        job.status = "done"
        job.finished = time.time()
        job.done.set()
        self._reap()

    def _reap(self) -> None:
        "Forget the oldest finished past the cap. The waited stay."
        finished = [job for job in self.listing() if job.is_done]
        while len(finished) > FINISHED_KEEP:
            oldest = finished[0]
            if oldest.waiters:
                return
            del self._jobs[oldest.job_id]
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


def describe(job: Job) -> str:
    "One line for `list-jobs`: id, state, age, command."
    if job.is_done:
        state = "error: %s" % job.error if job.error is not None else "exit %d" % job.returncode
        state = "done " + state
    else:
        state = "running"
    return "%d %s %.1fs %s" % (job.job_id, state, job.age(), job.command)


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


async def _pump(stream: anyio.abc.ByteStream, kept: deque[bytes], job: Job, name: str) -> None:
    """
    Drain one stream into the tail the job keeps, counting the rest.

    A chunk that straddles the cap is split: its head falls off and
    its tail stays, so the tail is the cap to the byte no matter how
    the pipe read. The loop holds no await, so a cancellation never
    lands with the counters half written.
    """
    if name == "stdout":
        fell, kept_now = job.stdout_dropped, job.stdout_kept
    else:
        fell, kept_now = job.stderr_dropped, job.stderr_kept
    try:
        async for chunk in stream:
            kept.append(chunk)
            kept_now += len(chunk)
            overflow = kept_now - STREAM_KEEP
            while overflow > 0:
                oldest = kept.popleft()
                if len(oldest) <= overflow:
                    overflow -= len(oldest)
                    kept_now -= len(oldest)
                    fell += len(oldest)
                else:
                    kept.appendleft(oldest[overflow:])
                    fell += overflow
                    kept_now -= overflow
                    overflow = 0
    finally:
        if name == "stdout":
            job.stdout_dropped, job.stdout_kept = fell, kept_now
        else:
            job.stderr_dropped, job.stderr_kept = fell, kept_now
        with suppress(Exception):
            await stream.aclose()
