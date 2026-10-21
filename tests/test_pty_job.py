"""
`run --pty`: a job on a terminal instead of pipes.

The rules this judges: `--pty` runs the command with a terminal on
every stream, so `test -t` passes where a piped job fails; the
waiting and the reading stay the same -- `run -w` answers with the
output, `wait-job` holds and reports, `show-job` reads it -- with
the whole terminal output in the stdout tail and nothing in stderr;
and `list-jobs` names which jobs hold a terminal.

Each rule holds twice: for a job this server forks, and for one its
holder forks, which outlives the server. Lillecarl/pymux#556.
"""

from __future__ import annotations

import argparse

import anyio
import pytest
from prompt_toolkit.application.current import set_app
from session import create_session

from pymux.commands import CommandException, handle_command
from pymux.commands.run import run_job
from pymux.query import ask


@pytest.fixture(params=["forked here", "forked by the holder"])
async def session(request, tmp_path):
    "A server and its client, with a holder beside it or none."
    async with create_session() as (pymux, state):
        if request.param == "forked by the holder":
            pymux.socket_name = str(tmp_path / "server.sock")
            await pymux.hold()
            assert pymux.jobs.holding is not None
        try:
            yield pymux, state
        finally:
            await pymux.unhold()


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


async def test_a_pty_job_holds_a_terminal_on_every_stream(session):
    pymux, state = session
    # `-w` refuses without a waiter, so this answers.
    await _run_answering(pymux, state, "run --pty -w 'test -t 0 && test -t 1 && test -t 2'")
    job = pymux.jobs.get(1)
    assert job is not None and job.pty and job.is_done and job.returncode == 0
    assert (job.program_id is not None) == (pymux.holding is not None)


async def test_a_piped_job_holds_no_terminal(session):
    pymux, state = session
    pymux.command_output = []
    try:
        with set_app(state.app):
            args = argparse.Namespace(
                w=True,
                directory=None,
                env=[],
                tags=[],
                session=None,
                pty=False,
                timeout=None,
                tail=10,
                shell_command=["test -t 1"],
            )
            with pytest.raises(CommandException, match="exited 1"):
                await run_job(pymux, args)
    finally:
        pymux.command_output = None
    job = pymux.jobs.get(1)
    assert job is not None and not job.pty and job.is_done and job.returncode == 1


async def test_a_holder_with_no_pipes_leaves_a_piped_job_to_this_server(tmp_path):
    "A holder from before the op is the one a running server has. Lillecarl/pymux#556."
    async with create_session() as (pymux, state):
        pymux.socket_name = str(tmp_path / "server.sock")
        await pymux.hold()
        assert pymux.holding is not None
        pymux.holding.ops = frozenset()
        try:
            await _run_answering(pymux, state, "run -w 'echo here'")
            job = pymux.jobs.get(1)
            assert job is not None and job.returncode == 0 and job.program_id is None
            assert job.kept("stdout") == b"here\n"
        finally:
            await pymux.unhold()


async def test_a_pty_job_collects_into_stdout_with_stderr_empty(session):
    pymux, state = session
    said = await _run_answering(pymux, state, "run --pty -w 'echo out; echo err >&2'")
    assert any("out" in line for line in said)
    assert any("err" in line for line in said)

    job = pymux.jobs.get(1)
    assert job is not None and job.is_done and job.returncode == 0
    assert b"err" in job.kept("stdout")
    assert job.kept("stderr") == b""
    assert b"\r" not in job.kept("stdout")


async def test_a_pty_job_in_a_directory_that_is_gone_never_starts(session):
    "The holder's child would start where the holder is, so it is refused first."
    pymux, state = session
    pymux.command_output = []
    try:
        with set_app(state.app):
            args = argparse.Namespace(
                w=True,
                directory="/no-such-directory",
                env=[],
                tags=[],
                session=None,
                pty=True,
                timeout=None,
                tail=10,
                shell_command=["true"],
            )
            with pytest.raises(CommandException, match="job 1 never started"):
                await run_job(pymux, args)
    finally:
        pymux.command_output = None
    job = pymux.jobs.get(1)
    assert job is not None and job.is_done and "No such file" in (job.error or "")


async def test_wait_and_show_read_a_pty_job_the_same_way(session):
    pymux, state = session
    said = await _run_answering(pymux, state, "run --pty --tag term 'echo waited-for'")
    assert said and said[0].strip() == "1"

    said = await _run_answering(pymux, state, "wait-job --tag term")
    assert any("waited-for" in line for line in said)

    said = await _run_answering(pymux, state, "show-job --tag term")
    assert any("waited-for" in line for line in said)


async def test_list_jobs_names_the_terminal(session):
    pymux, state = session
    await _run_answering(pymux, state, "run --pty -w true")
    await _run_answering(pymux, state, "run -w true")
    said = await _run_answering(pymux, state, "list-jobs")
    assert len([line for line in said if " pty " in line]) == 1


async def test_kill_ends_a_pty_job(session):
    pymux, state = session
    said = await _run_answering(pymux, state, "run --pty sleep 30")
    assert said and said[0].strip() == "1"

    job = pymux.jobs.get(1)
    assert job is not None and job.pty and not job.is_done
    pymux.jobs.kill(job)
    with anyio.fail_after(5):
        await pymux.jobs.wait(job)
    assert job.is_done


async def test_a_question_reads_a_job_on_a_terminal():
    async with create_session() as (pymux, _state):
        job = pymux.jobs.submit("true", None, [], None, True)
        assert job.pty
        answer = await ask(pymux, "SELECT pty FROM jobs WHERE id = :id", {"id": job.job_id}, 0, 0)
        assert answer.rows == [(1,)]
