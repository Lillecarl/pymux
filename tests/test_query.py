"""
What a question sees: virtual tables over the server's objects, built
as the question reads them, and nothing stored. Lillecarl/pymux#722.
"""

from __future__ import annotations

import apsw
from session import create_session

import pymux.jobs.table as job_table
from pymux.core.snapshot.write import rows_of
from pymux.options import tables
from pymux.options.options import Scope
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

    assert main == {
        "jobs",
        "job_tags",
        "saved_queries",
        "known_options",
        "option_choices",
        "layouts",
        "key_names",
        "forwards",
    }
    assert live == set(tables.LIVE_TABLES)
    assert not live & {*tables.SCREEN_TABLES, *tables.JOB_TABLES, *tables.CLIENT_MODE_TABLES}


async def test_the_live_tables_answer_what_the_objects_hold():
    """
    One question counts every table. A table is read when the question
    reaches it, so the server must hold still: no window here ends.
    """
    counts = ", ".join("(SELECT count(*) FROM live.%s)" % table for table in tables.LIVE_TABLES)
    async with create_session(window=None) as (pymux, _state):
        held = dict.fromkeys(tables.LIVE_TABLES, 0)
        for row in rows_of(pymux):
            held[row.TABLE] += 1
        (answered,) = await _rows(pymux, "SELECT %s" % counts)

    assert dict(zip(tables.LIVE_TABLES, answered, strict=True)) == held


async def test_a_question_reads_what_a_person_sees():
    "The name the status line shows, and what the program set and runs, beside the stored columns."
    async with create_session(window=None) as (pymux, _state):
        window = pymux.sessions[0].arrangement.windows[0]
        pane = window.panes[0]
        pane.screen.titles.window = "a title"
        windows = await _rows(
            pymux, "SELECT shown_name FROM live.windows WHERE window_id = :id", id=str(window.window_id)
        )
        panes = await _rows(pymux, "SELECT shown_name, title FROM live.panes WHERE pane_id = :id", id=str(pane.pane_id))

        assert windows == [(window.name,)]
        assert panes == [(pane.name, "a title")]


async def test_an_equal_narrows_what_a_table_builds(monkeypatch):
    "`option_choices` lists the values of only the option a question names; listing every one is slow."
    listed = []
    real = {}
    async with create_session(window=None) as (pymux, _state):
        for name, option in pymux.option_tables[Scope.SESSION].items():
            real[name] = option.get_all_values

            def spy(pymux, name=name):
                listed.append(name)
                return real[name](pymux)

            monkeypatch.setattr(option, "get_all_values", spy)
        rows = await _rows(
            pymux, "SELECT value FROM option_choices WHERE scope = 'session' AND name = :name", name="status"
        )

    assert ("on",) in rows
    assert listed == ["status"]


async def test_an_empty_table_still_names_its_columns():
    async with create_session() as (pymux, _state):
        answer = await ask(pymux, "SELECT id, command FROM jobs", {}, 0, 0)

    assert answer.columns == ["id", "command"]
    assert answer.rows == []


async def test_a_job_reads_as_it_is_now(monkeypatch):
    monkeypatch.setattr(job_table, "FINISHED_KEEP", 1)
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
