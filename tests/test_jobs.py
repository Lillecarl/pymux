"""
`run`, `wait-job`, `list-jobs`, `show-job` and `kill-job`: commands
that run without a pane, and remember what they did.

The rules this judges: `run` answers with an id and runs in the
background while every pane keeps reading its pty; `run -w` waits
here and answers with the output, failing when the exit is nonzero;
`wait-job` holds a waiting client until the job ends and reports the
same way; `list-jobs` shows every remembered job in one line each;
`show-job` reads either stream of a running job as well as a done
one; `kill-job` ends a running job and refuses one that ended; and
the table forgets the oldest finished past its cap, never a running
one and never one somebody waits for.
"""

from __future__ import annotations

import argparse
import sys

import anyio
import pytest
from prompt_toolkit.application.current import set_app
from session import create_session

import pymux.jobs as jobs_module
from pymux.commands import CommandException, handle_command
from pymux.commands.kill_job import kill_job
from pymux.commands.run import run_job
from pymux.commands.show_job import show_job
from pymux.commands.wait_job import wait_job


async def _run(pymux, state, command) -> None:
    "Run a command and wait for it, the way the socket route does."
    with set_app(state.app):
        answer = handle_command(pymux, command)
        if answer is not None:
            await answer


async def _run_answering(pymux, state, command) -> list:
    "Run a command a client waits for, and read what it answered."
    pymux.command_output = []
    try:
        with set_app(state.app):
            answer = handle_command(pymux, command)
            if answer is not None:
                await answer
            return list(pymux.command_output)
    finally:
        pymux.command_output = None


async def test_run_answers_with_an_id_and_runs_behind():
    async with create_session() as (pymux, state):
        said = await _run_answering(pymux, state, "run sleep 30")
        assert said and said[0].strip() == "1"

        job = pymux.jobs.get(1)
        assert job is not None and job.command == "sleep 30" and not job.is_done

        pymux.jobs.kill(job)
        with anyio.fail_after(5):
            await pymux.jobs.wait(job)
        assert job.is_done


async def test_run_wait_answers_with_the_output():
    async with create_session() as (pymux, state):
        said = await _run_answering(pymux, state, "run -w echo hello-from-a-job")
        assert any("hello-from-a-job" in line for line in said)

        job = pymux.jobs.get(1)
        assert job is not None and job.is_done and job.returncode == 0


async def test_run_takes_a_whole_command_line_as_one_word():
    # `run 'echo a; echo b'`: one word is already quoted for the
    # shell, and joining would quote it again into a program name.
    async with create_session() as (pymux, state):
        said = await _run_answering(pymux, state, "run -w 'echo first; echo second'")
        assert any("first" in line for line in said)
        assert any("second" in line for line in said)


async def test_run_wait_fails_on_a_nonzero_exit():
    # Through `handle_command` the failure would become a message;
    # the handler itself fails, so it is called directly.
    async with create_session() as (pymux, state):
        pymux.command_output = []
        try:
            with set_app(state.app):
                answer = run_job(
                    pymux,
                    argparse.Namespace(
                        w=True, directory=None, tags=[], session=None, env=[], shell_command=["sh", "-c", "exit 3"]
                    ),
                )
                assert answer is not None
                with pytest.raises(CommandException, match="job 1 exited 3"):
                    await answer
        finally:
            pymux.command_output = None


async def test_wait_job_reports_the_same_way():
    async with create_session() as (pymux, state):
        await _run(pymux, state, "run sh -c 'exit 3'")
        pymux.command_output = []
        try:
            with set_app(state.app):
                answer = wait_job(pymux, argparse.Namespace(job=1, tags=[], session=None))
                assert answer is not None
                with pytest.raises(CommandException, match="job 1 exited 3"):
                    await answer
        finally:
            pymux.command_output = None


async def test_wait_job_without_a_waiter_refuses():
    async with create_session() as (pymux, state):
        with pytest.raises(CommandException, match="not able to wait"):
            wait_job(pymux, argparse.Namespace(job=1, tags=[], session=None))


async def test_wait_job_of_no_job_says_so():
    async with create_session() as (pymux, state):
        pymux.command_output = []
        try:
            with pytest.raises(CommandException, match="no job 12"):
                wait_job(pymux, argparse.Namespace(job=12, tags=[], session=None))
        finally:
            pymux.command_output = None


async def test_list_jobs_shows_every_job_in_one_line_each():
    async with create_session() as (pymux, state):
        await _run_answering(pymux, state, "run -w echo first")
        await _run(pymux, state, "run sleep 30")
        said = await _run_answering(pymux, state, "list-jobs")
        lines = "\n".join(said).splitlines()

        assert any(line.startswith("1 done exit 0") and "echo first" in line for line in lines)
        assert any(line.startswith("2 running") and "sleep 30" in line for line in lines)

        pymux.jobs.kill(pymux.jobs.get(2))


async def test_show_job_reads_both_streams_of_a_running_job():
    async with create_session() as (pymux, state):
        await _run(
            pymux,
            state,
            "run sh -c 'echo out-line; echo err-line >&2; sleep 30'",
        )
        with anyio.fail_after(5):
            while not pymux.jobs.get(1).kept("stderr"):
                await anyio.sleep(0.01)

        said = await _run_answering(pymux, state, "show-job 1")
        assert any("out-line" in line for line in said)
        said = await _run_answering(pymux, state, "show-job -e 1")
        assert any("err-line" in line for line in said)

        pymux.jobs.kill(pymux.jobs.get(1))


async def test_show_job_of_no_job_says_so():
    async with create_session() as (pymux, state):
        pymux.command_output = []
        try:
            with set_app(state.app):
                with pytest.raises(CommandException, match="no job 12"):
                    show_job(pymux, argparse.Namespace(job=12, e=False, tags=[], session=None))
        finally:
            pymux.command_output = None


async def test_kill_job_ends_a_running_job_and_refuses_a_done_one():
    async with create_session() as (pymux, state):
        await _run(pymux, state, "run sleep 30")
        said = await _run_answering(pymux, state, "kill-job 1")
        assert any("job 1 stopping" in line for line in said)

        with anyio.fail_after(5):
            await pymux.jobs.wait(pymux.jobs.get(1))
        assert pymux.jobs.get(1).returncode == -15

        pymux.command_output = []
        try:
            with set_app(state.app):
                with pytest.raises(CommandException, match="already done"):
                    kill_job(pymux, argparse.Namespace(job=1, tags=[], session=None))
        finally:
            pymux.command_output = None


async def test_a_job_that_never_starts_says_why():
    async with create_session() as (pymux, state):
        pymux.command_output = []
        try:
            with set_app(state.app):
                answer = run_job(
                    pymux,
                    argparse.Namespace(
                        w=True,
                        directory="/no-such-directory",
                        tags=[],
                        session=None,
                        env=[],
                        shell_command=["echo", "hi"],
                    ),
                )
                assert answer is not None
                with pytest.raises(CommandException, match="job 1 never started"):
                    await answer
        finally:
            pymux.command_output = None

        job = pymux.jobs.get(1)
        assert job is not None and job.is_done and job.error is not None
        assert "No such file" in job.error


async def test_the_table_forgets_the_oldest_finished_past_its_cap(monkeypatch):
    async with create_session() as (pymux, state):
        monkeypatch.setattr(jobs_module, "FINISHED_KEEP", 2)
        await _run_answering(pymux, state, "run -w echo one")
        await _run_answering(pymux, state, "run -w echo two")
        await _run_answering(pymux, state, "run -w echo three")

        assert pymux.jobs.get(1) is None
        assert [job.job_id for job in pymux.jobs.listing()] == [2, 3]


async def test_the_table_never_forgets_a_running_job(monkeypatch):
    async with create_session() as (pymux, state):
        monkeypatch.setattr(jobs_module, "FINISHED_KEEP", 0)
        await _run(pymux, state, "run sleep 30")
        await _run_answering(pymux, state, "run -w echo quick")

        assert pymux.jobs.get(1) is not None and not pymux.jobs.get(1).is_done
        pymux.jobs.kill(pymux.jobs.get(1))


async def test_a_stream_keeps_its_tail_to_the_byte(monkeypatch):
    async with create_session() as (pymux, state):
        monkeypatch.setattr(jobs_module, "STREAM_KEEP", 1000)
        big = "%s -c \"import sys; sys.stdout.write('x' * 100000)\"" % sys.executable
        await _run_answering(pymux, state, "run -w " + big)

        job = pymux.jobs.get(1)
        assert job.stdout_kept == 1000
        assert job.stdout_dropped == 99000
        assert job.kept("stdout") == b"x" * 1000
