"""
`run --pty`: a job on a terminal instead of pipes.

The rules this judges: `--pty` runs the command with a terminal on
every stream, so `test -t` passes where a piped job fails; the
waiting and the reading stay the same -- `run -w` answers with the
output, `wait-job` holds and reports, `show-job` reads it -- with
the whole terminal output in the stdout tail and nothing in stderr;
and `list-jobs` names which jobs hold a terminal.
"""

from __future__ import annotations

import argparse

import anyio
import pytest
from prompt_toolkit.application.current import set_app
from session import create_session

from pymux.commands import CommandException, handle_command
from pymux.commands.run import run_job
from pymux.jobs import JobTable


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


async def test_a_pty_job_holds_a_terminal_on_every_stream():
    async with create_session() as (pymux, state):
        # `-w` refuses without a waiter, so this answers.
        await _run_answering(pymux, state, "run --pty -w 'test -t 0 && test -t 1 && test -t 2'")
        job = pymux.jobs.get(1)
        assert job is not None and job.pty and job.is_done and job.returncode == 0


async def test_a_piped_job_holds_no_terminal():
    async with create_session() as (pymux, state):
        pymux.command_output = []
        try:
            with set_app(state.app):
                args = argparse.Namespace(
                    w=True, directory=None, env=[], tags=[], session=None, pty=False, shell_command=["test -t 1"]
                )
                with pytest.raises(CommandException, match="exited 1"):
                    await run_job(pymux, args)
        finally:
            pymux.command_output = None
        job = pymux.jobs.get(1)
        assert job is not None and not job.pty and job.is_done and job.returncode == 1


async def test_a_pty_job_collects_into_stdout_with_stderr_empty():
    async with create_session() as (pymux, state):
        said = await _run_answering(pymux, state, "run --pty -w 'echo out; echo err >&2'")
        assert any("out" in line for line in said)
        assert any("err" in line for line in said)

        job = pymux.jobs.get(1)
        assert job is not None and job.is_done and job.returncode == 0
        assert b"err" in job.kept("stdout")
        assert job.kept("stderr") == b""
        assert b"\r" not in job.kept("stdout")


async def test_wait_and_show_read_a_pty_job_the_same_way():
    async with create_session() as (pymux, state):
        said = await _run_answering(pymux, state, "run --pty --tag term 'echo waited-for'")
        assert said and said[0].strip() == "1"

        said = await _run_answering(pymux, state, "wait-job --tag term")
        assert any("waited-for" in line for line in said)

        said = await _run_answering(pymux, state, "show-job --tag term")
        assert any("waited-for" in line for line in said)


async def test_list_jobs_names_the_terminal():
    async with create_session() as (pymux, state):
        await _run_answering(pymux, state, "run --pty -w true")
        await _run_answering(pymux, state, "run -w true")
        said = await _run_answering(pymux, state, "list-jobs")
        assert len([line for line in said if " pty " in line]) == 1


async def test_kill_ends_a_pty_job():
    async with create_session() as (pymux, state):
        said = await _run_answering(pymux, state, "run --pty sleep 30")
        assert said and said[0].strip() == "1"

        job = pymux.jobs.get(1)
        assert job is not None and job.pty and not job.is_done
        pymux.jobs.kill(job)
        with anyio.fail_after(5):
            await pymux.jobs.wait(job)
        assert job.is_done


async def test_submit_stores_the_terminal_with_the_row():
    table = JobTable()
    await table.open()
    try:
        job = await table.submit("true", None, [], None, True)
        assert job.pty
        async with table.store.reader() as conn:
            cursor = await conn.execute("SELECT pty FROM jobs WHERE id = ?", (job.job_id,))
            assert (await cursor.fetchone()) == (1,)
    finally:
        await table.close()
