"""
What a question sees: virtual tables over the server's objects, built
as the question starts, and nothing stored. Lillecarl/pymux#722.
"""

from __future__ import annotations

import apsw
from session import create_session

import pymux.jobs as jobs_module
from pymux import snapshot, tables
from pymux.query import SCHEMA_VERSION, ask


async def _rows(pymux, query: str, **params) -> list[tuple]:
    return (await ask(pymux, query, params, 0, 0)).rows


def test_apsw_runs_on_the_sqlite_of_its_own_release():
    "Compiled in, and the same version to the patch: nothing else can answer for its symbols."
    assert apsw.using_amalgamation
    assert apsw.apsw_version().startswith(apsw.sqlite_lib_version() + ".")


async def test_a_question_sees_the_jobs_and_the_live_tables():
    async with create_session() as (pymux, _state):
        assert await _rows(pymux, "PRAGMA user_version") == [(SCHEMA_VERSION,)]
        main = {name for (name,) in await _rows(pymux, "SELECT name FROM main.sqlite_master WHERE type = 'table'")}
        live = {name for (name,) in await _rows(pymux, "SELECT name FROM live.sqlite_master WHERE type = 'table'")}

    assert main == {"jobs", "job_tags", "saved_queries"}
    assert live == set(tables.LIVE_TABLES)
    assert not live & {*tables.SCREEN_TABLES, *tables.JOB_TABLES, *tables.CLIENT_MODE_TABLES}


async def test_the_live_tables_answer_what_the_objects_hold():
    "One question counts every table. `ask` builds its rows before it first awaits, so this is one instant."
    counts = ", ".join("(SELECT count(*) FROM live.%s)" % table for table in tables.LIVE_TABLES)
    async with create_session() as (pymux, _state):
        held = dict.fromkeys(tables.LIVE_TABLES, 0)
        for row in snapshot.rows_of(pymux):
            held[row.TABLE] += 1
        (answered,) = await _rows(pymux, "SELECT %s" % counts)

    assert dict(zip(tables.LIVE_TABLES, answered, strict=True)) == held


async def test_an_empty_table_still_names_its_columns():
    async with create_session() as (pymux, _state):
        answer = await ask(pymux, "SELECT id, command FROM jobs", {}, 0, 0)

    assert answer.columns == ["id", "command"]
    assert answer.rows == []


async def test_a_job_reads_as_it_is_now(monkeypatch):
    monkeypatch.setattr(jobs_module, "FINISHED_KEEP", 1)
    async with create_session() as (pymux, _state):
        first = pymux.jobs.submit("exit 3", None)
        assert await _rows(pymux, "SELECT id, status FROM jobs") == [(first.job_id, "running")]

        await pymux.jobs.supervise(first)
        assert await _rows(pymux, "SELECT status, returncode FROM jobs") == [("done", 3)]

        second = pymux.jobs.submit("true", None)
        await pymux.jobs.supervise(second)
        # The cap forgot the older finished job, and the table with it.
        assert await _rows(pymux, "SELECT id FROM jobs") == [(second.job_id,)]


async def test_an_id_is_never_handed_out_twice():
    async with create_session() as (pymux, _state):
        first = pymux.jobs.submit("true", None)
        pymux.jobs._forget(first)
        second = pymux.jobs.submit("true", None)

    assert second.job_id == first.job_id + 1
