"""
`tail` and `run --timeout`: one call that waits, reads, and reports.

The rules this judges: `tail` replays the last lines and follows to
the end, and the exit code is the job's own; `--lines 0` follows
only, so a finished job answers nothing new; past `--timeout` it
stops with 124 and the job still running, ready to tail again; zero
replays without waiting; no job is a 4. `run --timeout` streams a
fresh job the same way, and a zero timeout answers with the id.
"""

from __future__ import annotations

import anyio
import pytest
from prompt_toolkit.application.current import set_app
from session import create_session

from pymux.agentic import CallerContext
from pymux.commands import CommandException, handle_command


async def _run(pymux, state, command) -> None:
    "Run a command and wait for it, the way the socket route does."
    with set_app(state.app):
        answer = handle_command(pymux, command)
        if answer is not None:
            await answer


async def _tail(pymux, state, command) -> tuple[list, list, int | None]:
    "Tail a job the way a waiting client does, and read the errors and the exit code."
    pymux.command_output = []
    pymux.command_error = []
    pymux.command_exit_code = None
    try:
        with set_app(state.app):
            answer = handle_command(pymux, command)
            if answer is not None:
                await answer
            return list(pymux.command_output), list(pymux.command_error), pymux.command_exit_code
    finally:
        pymux.command_output = None
        pymux.command_error = None
        pymux.command_exit_code = None


async def test_tail_replays_and_follows_to_the_job_s_exit():
    async with create_session() as (pymux, state):
        await _run(pymux, state, "run --tag lines -- sh -c 'echo one; echo two; echo three'")
        job = pymux.jobs.get(1)
        with anyio.fail_after(5):
            await pymux.jobs.wait(job)
        said, errors, code = await _tail(pymux, state, "tail --tag lines --lines 2")
        assert any("two" in line for line in said)
        assert any("three" in line for line in said)
        assert not any("one" in line for line in said)
        assert code == 0


async def test_tail_reports_a_nonzero_exit():
    async with create_session() as (pymux, state):
        await _run(pymux, state, "run --tag oops -- sh -c 'exit 3'")
        said, errors, code = await _tail(pymux, state, "tail --tag oops")
        assert said == []
        assert code == 3


async def test_tail_with_zero_lines_follows_only():
    async with create_session() as (pymux, state):
        await _run(pymux, state, "run --tag quiet -- echo said-long-ago")
        job = pymux.jobs.get(1)
        with anyio.fail_after(5):
            await pymux.jobs.wait(job)
        said, errors, code = await _tail(pymux, state, "tail --tag quiet --lines 0")
        assert said == []
        assert code == 0


async def test_tail_replays_and_follows_without_gap_or_overlap():
    # A job still producing when the tail starts: the replay and the
    # follow meet exactly, so every line arrives once, in order.
    async with create_session() as (pymux, state):
        await _run(pymux, state, "run --tag counting -- sh -c 'for i in 1 2 3 4 5; do echo line-$i; sleep 0.2; done'")
        said, errors, code = await _tail(pymux, state, "tail --tag counting --lines 2 --timeout 10")
        assert code == 0
        text = "\n".join(said)
        found = [text.find("line-%d" % i) for i in (1, 2, 3, 4, 5)]
        assert all(position >= 0 for position in found)
        assert found == sorted(found)
        for i in (1, 2, 3, 4, 5):
            assert text.count("line-%d" % i) == 1


async def test_tail_past_timeout_stops_with_124_and_the_job_runs_on():
    async with create_session() as (pymux, state):
        await _run(pymux, state, "run --tag slow -- sleep 30")
        said, errors, code = await _tail(pymux, state, "tail --tag slow --timeout 0.2")
        try:
            assert any("still running" in line for line in said)
            assert code == 124
            job = pymux.jobs.get(1)
            assert job is not None and not job.is_done
        finally:
            pymux.jobs.kill(pymux.jobs.get(1))
            with anyio.fail_after(5):
                await pymux.jobs.wait(pymux.jobs.get(1))


async def test_tail_with_zero_timeout_replays_without_waiting():
    async with create_session() as (pymux, state):
        await _run(pymux, state, "run --tag quick -- echo done-already")
        job = pymux.jobs.get(1)
        with anyio.fail_after(5):
            await pymux.jobs.wait(job)
        said, errors, code = await _tail(pymux, state, "tail --tag quick --timeout 0")
        assert any("done-already" in line for line in said)
        assert code == 0

        await _run(pymux, state, "run --tag slow -- sleep 30")
        try:
            said, errors, code = await _tail(pymux, state, "tail --tag slow --timeout 0")
            assert said == []
            assert code == 124
        finally:
            pymux.jobs.kill(pymux.jobs.get(2))
            with anyio.fail_after(5):
                await pymux.jobs.wait(pymux.jobs.get(2))


async def test_tail_of_no_job_is_a_4():
    async with create_session() as (pymux, state):
        said, errors, code = await _tail(pymux, state, "tail --tag nobody-home")
        assert said == []
        assert any("no job" in line for line in errors)
        assert code == 4


async def test_tail_without_a_timeout_refuses_for_an_agent():
    async with create_session() as (pymux, state):
        await _run(pymux, state, "run --tag boundless -- sleep 30")
        pymux.caller_context = CallerContext(session_id="s1", environment={}, cwd=None)
        try:
            said, errors, code = await _tail(pymux, state, "tail --tag boundless")
            assert said == []
            assert any("without --timeout" in line for line in errors)
            assert code is None
        finally:
            pymux.caller_context = None
            pymux.jobs.kill(pymux.jobs.get(1))
            with anyio.fail_after(5):
                await pymux.jobs.wait(pymux.jobs.get(1))


async def test_tail_without_a_timeout_waits_for_a_person():
    async with create_session() as (pymux, state):
        await _run(pymux, state, "run --tag patient -- echo hi")
        job = pymux.jobs.get(1)
        with anyio.fail_after(5):
            await pymux.jobs.wait(job)
        said, errors, code = await _tail(pymux, state, "tail --tag patient")
        assert any("hi" in line for line in said)
        assert code == 0


async def test_tail_with_a_negative_timeout_refuses():
    async with create_session() as (pymux, state):
        await _run(pymux, state, "run --tag neg -- echo hi")
        said, errors, code = await _tail(pymux, state, "tail --tag neg --timeout -5")
        assert said == []
        assert any("cannot be negative" in line for line in errors)
        assert code is None


async def test_wait_job_refuses_for_an_agent():
    from pymux.commands.wait_job import wait_job

    async with create_session() as (pymux, state):
        await _run(pymux, state, "run --tag unwatched -- echo hi")
        pymux.caller_context = CallerContext(session_id="s1", environment={}, cwd=None)
        pymux.command_output = []
        try:
            with set_app(state.app):
                import argparse

                with pytest.raises(CommandException, match="tail --timeout"):
                    wait_job(pymux, argparse.Namespace(job=None, tags=[("unwatched", None)], session=None))
        finally:
            pymux.caller_context = None
            pymux.command_output = None


async def test_run_wait_refuses_for_an_agent():
    from pymux.commands.run import run_job

    async with create_session() as (pymux, state):
        pymux.caller_context = CallerContext(session_id="s1", environment={}, cwd=None)
        pymux.command_output = []
        try:
            with set_app(state.app):
                import argparse

                with pytest.raises(CommandException, match="run --timeout"):
                    await run_job(
                        pymux,
                        argparse.Namespace(
                            w=True,
                            directory=None,
                            env=[],
                            tags=[],
                            session=None,
                            pty=False,
                            timeout=None,
                            tail=10,
                            shell_command=["echo", "hi"],
                        ),
                    )
        finally:
            pymux.caller_context = None
            pymux.command_output = None


async def test_run_with_timeout_streams_and_reports():
    async with create_session() as (pymux, state):
        said, errors, code = await _tail(pymux, state, "run --tag fresh --timeout 10 -- sh -c 'echo live; exit 7'")
        assert any("live" in line for line in said)
        assert code == 7
        job = pymux.jobs.get(1)
        assert job is not None and job.is_done


async def test_run_with_timeout_leaves_a_slow_job_running():
    async with create_session() as (pymux, state):
        said, errors, code = await _tail(pymux, state, "run --tag slow --timeout 0.2 -- sleep 30")
        try:
            assert any("still running" in line for line in said)
            assert code == 124
            job = pymux.jobs.get(1)
            assert job is not None and not job.is_done
            # The tag re-tails: the follow left the job where it was.
            said, errors, code = await _tail(pymux, state, "tail --tag slow --timeout 0.2")
            assert code == 124
        finally:
            pymux.jobs.kill(pymux.jobs.get(1))
            with anyio.fail_after(5):
                await pymux.jobs.wait(pymux.jobs.get(1))


async def test_run_with_zero_timeout_answers_with_the_id():
    async with create_session() as (pymux, state):
        said, errors, code = await _tail(pymux, state, "run --tag bg --timeout 0 -- sleep 30")
        try:
            assert said and said[0].strip() == "1"
            assert code is None
            job = pymux.jobs.get(1)
            assert job is not None and not job.is_done
        finally:
            pymux.jobs.kill(pymux.jobs.get(1))
            with anyio.fail_after(5):
                await pymux.jobs.wait(pymux.jobs.get(1))
