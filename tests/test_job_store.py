"""
The jobs database: schema, transitions, and the reader pool.

The rules this judges: the store builds versioned schema on open; a
submitted job is a row and a live job or neither, with the id from the
database; finishing a job updates its row in the same step; reaping
forgets the row with the job; a reader sees what the writer wrote but
cannot write itself; and an idle reader past its time is closed, with
only the configured minimum kept warm.
"""

from __future__ import annotations

import anyio
import pytest

import pymux.jobs as jobs_module
from pymux.jobs import JobId, JobTable
from pymux.jobstore import SCHEMA_VERSION, JobStore


async def _row(store: JobStore, job_id: JobId):
    async with store.reader() as conn:
        cursor = await conn.execute("SELECT command, status, returncode, error FROM jobs WHERE id = ?", (job_id,))
        return await cursor.fetchone()


async def test_open_builds_a_versioned_schema():
    store = JobStore()
    await store.open()
    try:
        async with store.reader() as conn:
            cursor = await conn.execute("PRAGMA user_version")
            assert (await cursor.fetchone())[0] == SCHEMA_VERSION
            cursor = await conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            assert {row[0] for row in await cursor.fetchall()} == {"jobs", "sqlite_sequence"}
    finally:
        await store.close()


async def test_submit_is_a_row_and_a_job_or_neither():
    table = JobTable()
    await table.open()
    try:
        first = await table.submit("echo one", None)
        second = await table.submit("echo two", "/tmp")
        assert isinstance(first.job_id, int) and isinstance(second.job_id, int)
        assert second.job_id == first.job_id + 1

        assert table.get(first.job_id) is first
        assert (await _row(table.store, first.job_id))[:2] == ("echo one", "running")
        assert (await _row(table.store, second.job_id))[:2] == ("echo two", "running")
    finally:
        await table.close()


async def test_submit_opens_a_store_nobody_opened():
    table = JobTable()
    job = await table.submit("true", None)
    try:
        assert table.get(job.job_id) is job
        assert (await _row(table.store, job.job_id))[1] == "running"
    finally:
        await table.close()


async def test_finishing_updates_the_row_in_the_same_step():
    table = JobTable()
    await table.open()
    try:
        job = await table.submit("echo hello-from-a-job", None)
        await table.supervise(job)
        assert job.is_done and job.returncode == 0
        assert (await _row(table.store, job.job_id))[1:] == ("done", 0, None)
    finally:
        await table.close()


async def test_reaping_forgets_the_row_with_the_job(monkeypatch):
    monkeypatch.setattr(jobs_module, "FINISHED_KEEP", 2)
    table = JobTable()
    await table.open()
    try:
        for _ in range(3):
            await table.supervise(await table.submit("true", None))
        assert [job.job_id for job in table.listing()] == [2, 3]
        assert table.get(JobId(1)) is None
        async with table.store.reader() as conn:
            cursor = await conn.execute("SELECT id FROM jobs ORDER BY id")
            assert [row[0] for row in await cursor.fetchall()] == [2, 3]
    finally:
        await table.close()


async def test_a_reader_reads_but_cannot_write():
    store = JobStore()
    await store.open()
    try:
        await store.write("INSERT INTO jobs(command, status, started) VALUES ('echo x', 'running', 0.0)")
        async with store.reader() as conn:
            cursor = await conn.execute("SELECT command FROM jobs")
            assert (await cursor.fetchall()) == [("echo x",)]
            with pytest.raises(Exception):
                await conn.execute("DELETE FROM jobs")
    finally:
        await store.close()


async def test_idle_readers_past_their_time_are_closed():
    store = JobStore(min_idle_readers=0, reader_idle_ttl=-1.0)
    await store.open()
    try:
        async with store.reader():
            pass
        assert await store.idle_readers() == 0
    finally:
        await store.close()


async def test_the_minimum_stays_warm():
    store = JobStore(min_idle_readers=1, reader_idle_ttl=-1.0)
    await store.open()
    try:
        first = await store._checkout()
        await store._checkin(first)
        assert await store.idle_readers() == 1
        async with store.reader() as conn:
            cursor = await conn.execute("SELECT 1")
            assert (await cursor.fetchone()) == (1,)
        assert await store.idle_readers() == 1
    finally:
        await store.close()


async def test_stores_do_not_share_a_database():
    first, second = JobStore(), JobStore()
    await first.open()
    await second.open()
    try:
        await first.write("INSERT INTO jobs(command, status, started) VALUES ('a', 'running', 0.0)")
        async with second.reader() as conn:
            cursor = await conn.execute("SELECT COUNT(*) FROM jobs")
            assert (await cursor.fetchone())[0] == 0
    finally:
        await first.close()
        await second.close()


async def test_close_survives_cancellation():
    store = JobStore()
    await store.open()
    with anyio.CancelScope() as scope:
        scope.cancel()
        await store.close()
    # The shield held: nothing cancelled, and nothing leaked outward.
    assert not scope.cancelled_caught
    await store.open()
    try:
        await store.write("INSERT INTO jobs(command, status, started) VALUES ('a', 'running', 0.0)")
        async with store.reader() as conn:
            cursor = await conn.execute("SELECT COUNT(*) FROM jobs")
            assert (await cursor.fetchone())[0] == 1
    finally:
        await store.close()
