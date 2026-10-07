"""
The idle sweep: jobs that do nothing are forgotten.

The rules this judges: a finished job idle past the time is
forgotten from the table and the database, tags with it; a fresh
one stays, and so does one whose output is recent however old its
start; a running job idle past the time is ended first and then
forgotten; a waited job stays however idle, and zero keeps
everything. The time to live is `job-ttl`, an hour unless set.
"""

from __future__ import annotations

import anyio
from prompt_toolkit.application.current import set_app
from session import create_session

from pymux.commands import handle_command
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


async def _ids_of(store) -> list:
    async with store.reader() as conn:
        cursor = await conn.execute("SELECT id FROM jobs ORDER BY id")
        return [row[0] for row in await cursor.fetchall()]


async def _tags_of(store, job_id) -> dict:
    async with store.reader() as conn:
        cursor = await conn.execute("SELECT key, value FROM job_tags WHERE job_id = ?", (job_id,))
        return dict(await cursor.fetchall())


async def test_a_finished_job_idle_past_its_time_is_forgotten():
    async with create_session() as (pymux, state):
        await _run_answering(pymux, state, "run -w --tag gone true")
        job = pymux.jobs.get(1)
        assert job is not None and job.is_done
        job.last_active -= 7200

        await pymux.jobs.sweep(3600)

        assert pymux.jobs.get(1) is None
        assert await _ids_of(pymux.jobs.store) == []
        assert await _tags_of(pymux.jobs.store, 1) == {}


async def test_a_fresh_finished_job_stays():
    async with create_session() as (pymux, state):
        await _run_answering(pymux, state, "run -w true")
        job = pymux.jobs.get(1)
        assert job is not None and job.is_done

        await pymux.jobs.sweep(3600)

        assert pymux.jobs.get(1) is job
        assert await _ids_of(pymux.jobs.store) == [1]


async def test_output_counts_as_activity():
    async with create_session() as (pymux, state):
        await _run_answering(pymux, state, "run -w echo hi")
        job = pymux.jobs.get(1)
        assert job is not None and job.is_done
        # Old start, but the output just landed.
        assert job.last_active > job.started
        job.started -= 7200

        await pymux.jobs.sweep(3600)

        assert pymux.jobs.get(1) is job


async def test_a_running_job_idle_past_its_time_is_ended():
    async with create_session() as (pymux, state):
        await _run(pymux, state, "run sleep 30")
        job = pymux.jobs.get(1)
        assert job is not None and not job.is_done
        job.last_active -= 7200

        await pymux.jobs.sweep(3600)

        assert pymux.jobs.get(1) is None
        assert await _ids_of(pymux.jobs.store) == []
        with anyio.fail_after(5):
            await pymux.jobs.wait(job)
        assert job.is_done


async def test_a_waited_job_stays_however_idle():
    async with create_session() as (pymux, state):
        await _run(pymux, state, "run sleep 30")
        job = pymux.jobs.get(1)
        assert job is not None and not job.is_done
        job.last_active -= 7200
        try:
            async with anyio.create_task_group() as waiters:
                waiters.start_soon(pymux.jobs.wait, job)
                while not job.waiters:
                    await anyio.sleep(0)

                await pymux.jobs.sweep(3600)

                assert pymux.jobs.get(1) is job
                waiters.cancel_scope.cancel()
        finally:
            pymux.jobs.kill(job)
            with anyio.fail_after(5):
                await pymux.jobs.wait(job)


async def test_zero_keeps_everything():
    async with create_session() as (pymux, state):
        await _run_answering(pymux, state, "run -w true")
        job = pymux.jobs.get(1)
        assert job is not None and job.is_done
        job.last_active -= 7200

        await pymux.jobs.sweep(0)

        assert pymux.jobs.get(1) is job
        assert await _ids_of(pymux.jobs.store) == [1]


async def test_the_time_to_live_is_an_option():
    async with create_session() as (pymux, state):
        assert pymux.job_ttl == 3600
        await _run_answering(pymux, state, "set-option job-ttl 60")
        assert pymux.job_ttl == 60
        said = await _run_answering(pymux, state, "show-options job-ttl")
        assert said and "60" in said[0]


async def test_submit_starts_the_clock_at_the_start():
    table = JobTable()
    await table.open()
    try:
        job = await table.submit("true", None)
        assert job.last_active == job.started
    finally:
        await table.close()
