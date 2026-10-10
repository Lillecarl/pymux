"""
`sql`: free-form questions, kept questions, and the read-only gate.

The rules this judges: a SELECT answers with a table, `--json`
with objects; only questions that read run, and comments do not hide
a write; `:named` parameters fill from `--param`; `--save` keeps a
question for `--run`, `--list` shows the kept ones with the three
seeds, `--delete` forgets; `-n` caps the rows and says it truncated;
a question past `--timeout` fails instead of answering half its rows.
"""

from __future__ import annotations

import argparse
import json

import anyio
import pytest
from prompt_toolkit.application.current import set_app
from session import create_session

from pymux.commands import CommandException, handle_command
from pymux.commands.sql import DEFAULT_LIMIT, sql
from pymux.jobstore import is_read_only_query


async def _ask(pymux, state, command) -> list:
    "Ask `sql` the way the socket route does, and read what it answered."
    pymux.command_output = []
    try:
        with set_app(state.app):
            answer = handle_command(pymux, command)
            if answer is not None:
                await answer
            return list(pymux.command_output)
    finally:
        pymux.command_output = None


def _ns(**over) -> argparse.Namespace:
    "The flags `sql` reads, with only the test's own set."
    base = {
        "query": [],
        "save": None,
        "run": None,
        "list": False,
        "delete": None,
        "description": None,
        "param": [],
        "lines": DEFAULT_LIMIT,
        "timeout": 10.0,
        "json": False,
    }
    base.update(over)
    return argparse.Namespace(**base)


async def _run_handler(pymux, state, **over) -> list:
    "Run the handler the way a failure test does: raised, not messaged."
    pymux.command_output = []
    try:
        with set_app(state.app):
            await sql(pymux, _ns(**over))
            return list(pymux.command_output)
    finally:
        pymux.command_output = None


def test_reads_run_and_writes_do_not():
    assert is_read_only_query("SELECT id FROM jobs")
    assert is_read_only_query("  -- a comment\nWITH x AS (SELECT 1) SELECT * FROM x")
    assert is_read_only_query("/* a comment */ EXPLAIN SELECT 1")
    assert is_read_only_query("SELECT '-- not a comment'")
    assert is_read_only_query("PRAGMA table_info(jobs)")
    assert not is_read_only_query("DELETE FROM jobs")
    assert not is_read_only_query("DROP TABLE jobs")
    assert not is_read_only_query("INSERT INTO jobs(command) VALUES ('x')")
    assert not is_read_only_query("-- nothing but a comment\nDELETE FROM jobs")
    assert not is_read_only_query("/* nothing but a comment */ DELETE FROM jobs")
    assert not is_read_only_query("/* unterminated DELETE FROM jobs")
    assert not is_read_only_query("")
    assert not is_read_only_query("   ")


async def test_free_form_select_answers_a_table():
    async with create_session() as (pymux, state):
        await pymux.jobs.submit("echo one", None)
        await pymux.jobs.submit("echo two", None)
        said = await _ask(pymux, state, "sql SELECT id, command FROM jobs ORDER BY id")
        assert len(said) == 1
        assert "id" in said[0].splitlines()[0] and "command" in said[0].splitlines()[0]
        assert "echo one" in said[0] and "echo two" in said[0]


async def test_the_windows_are_there_beside_the_jobs():
    """
    The live tables answer in the same question as the jobs. Lillecarl/pymux#399.

    One window, the one attaching makes. The plain session's window
    runs a program that ends at once, and whether that window is still
    there when the question runs depends on the load.
    """
    async with create_session(window=None) as (pymux, state):
        await pymux.jobs.submit("echo one", None)
        # The rename and the question in one step: `sql` writes what the
        # step changed before it asks.
        with set_app(state.app):
            handle_command(pymux, "rename-window built")
        said = await _ask(
            pymux,
            state,
            "sql --json SELECT w.window_name, j.command FROM live.windows w, jobs j",
        )
        assert json.loads(said[0])["rows"] == [{"window_name": "built", "command": "echo one"}]
        said = await _ask(pymux, state, "sql --json SELECT count(*) AS n FROM panes")
        held = sum(len(window.panes) for s in pymux.sessions for window in s.arrangement.windows)
        assert json.loads(said[0])["rows"] == [{"n": held}]


async def test_the_live_store_writes_while_questions_read():
    """
    A writer on the loop and readers on their threads share one cache,
    which answers a lock with an error and not a wait. Readers take
    none, so neither side ever sees one.
    """
    async with create_session() as (pymux, state):
        failures: list[str] = []

        async def ask() -> None:
            for _ in range(20):
                try:
                    with set_app(state.app):
                        await sql(pymux, _ns(query=["SELECT * FROM windows, panes, sessions"]))
                except CommandException as e:
                    failures.append(e.message)

        async def write() -> None:
            for _ in range(200):
                pymux.live.mark()
                pymux.live.flush(pymux)
                await anyio.sleep(0)

        # The answers themselves are not the question here.
        pymux.command_output = []
        try:
            async with anyio.create_task_group() as tasks:
                for _ in range(4):
                    tasks.start_soon(ask)
                tasks.start_soon(write)
        finally:
            pymux.command_output = None
        assert failures == []


async def test_json_answers_objects():
    async with create_session() as (pymux, state):
        await pymux.jobs.submit("echo one", None)
        said = await _ask(pymux, state, "sql --json SELECT id, command FROM jobs ORDER BY id")
        payload = json.loads(said[0])
        assert payload["columns"] == ["id", "command"]
        assert payload["rows"] == [{"id": 1, "command": "echo one"}]
        assert payload["truncated"] is False


async def test_writes_are_refused_and_leave_the_table():
    async with create_session() as (pymux, state):
        await pymux.jobs.submit("echo one", None)
        queries = (
            "DELETE FROM jobs",
            "DROP TABLE jobs",
            "INSERT INTO jobs(command) VALUES ('x')",
        )
        for query in queries:
            with pytest.raises(CommandException):
                await _run_handler(pymux, state, query=query.split())
        said = await _ask(pymux, state, "sql SELECT COUNT(*) AS n FROM jobs")
        assert "1" in said[0]


async def test_params_fill_a_free_form_query():
    async with create_session() as (pymux, state):
        said = await _ask(pymux, state, "sql --param who=world SELECT :who AS who")
        assert "world" in said[0]
        # A missing fill fails; an extra one is ignored, the way
        # sqlite ignores a binding nothing names.
        with pytest.raises(CommandException):
            await _run_handler(pymux, state, query=["SELECT", ":who", "AS", "who"])
        said = await _run_handler(pymux, state, query=["SELECT", "1"], param=["broken=1"])
        assert "1" in said[0]
        with pytest.raises(CommandException):
            await _run_handler(pymux, state, query=["SELECT", "1"], param=["broken"])


async def test_save_run_list_and_delete_roundtrip():
    async with create_session() as (pymux, state):
        await pymux.jobs.submit("echo one", None)
        said = await _run_handler(
            pymux,
            state,
            save="mine",
            description="What I ask",
            query=["SELECT", "id", "FROM", "jobs", "WHERE", "id", "=", ":id"],
        )
        assert said == ["saved mine"]

        said = await _ask(pymux, state, "sql --list")
        assert "mine" in said[0] and "What I ask" in said[0]

        said = await _run_handler(pymux, state, run="mine", param=["id=1"])
        assert "1" in said[0]

        said = await _run_handler(pymux, state, delete="mine")
        assert said == ["deleted mine"]
        with pytest.raises(CommandException, match="no saved query mine"):
            await _run_handler(pymux, state, run="mine")
        with pytest.raises(CommandException, match="no saved query mine"):
            await _run_handler(pymux, state, delete="mine")


async def test_the_seeds_are_kept():
    async with create_session() as (pymux, state):
        said = await _ask(pymux, state, "sql --list")
        assert "running" in said[0] and "failed" in said[0] and "recent" in said[0]
        said = await _ask(pymux, state, "sql --run recent --param n=5 --json")
        assert json.loads(said[0])["columns"] == ["id", "command", "status", "returncode"]


async def test_the_cap_names_its_truncation():
    async with create_session() as (pymux, state):
        for index in range(3):
            await pymux.jobs.submit("echo %d" % index, None)
        said = await _ask(pymux, state, "sql -n 2 SELECT id FROM jobs ORDER BY id")
        assert "truncated at 2 rows" in said[0]
        said = await _run_handler(
            pymux, state, lines=0, json=True, query=["SELECT", "id", "FROM", "jobs", "ORDER", "BY", "id"]
        )
        payload = json.loads(said[0])
        assert [row["id"] for row in payload["rows"]] == [1, 2, 3]
        assert payload["truncated"] is False
        with pytest.raises(CommandException):
            await _run_handler(pymux, state, lines=-1, query=["SELECT", "1"])


async def test_question_flags_that_conflict_refuse():
    async with create_session() as (pymux, state):
        with pytest.raises(CommandException):
            await _run_handler(pymux, state, run="recent", query=["SELECT", "1"])
        with pytest.raises(CommandException):
            await _run_handler(pymux, state, save="mine", run="recent")
        with pytest.raises(CommandException):
            await _run_handler(pymux, state)
        with pytest.raises(CommandException, match="no saved query"):
            await _run_handler(pymux, state, run="no-such-question")


async def test_a_question_past_its_timeout_fails():
    async with create_session() as (pymux, state):
        query = "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c) SELECT COUNT(*) FROM c"
        with pytest.raises(CommandException, match="timed out"):
            await _run_handler(pymux, state, timeout=0.2, query=query.split())
        said = await _ask(pymux, state, "sql SELECT 1 AS one")
        assert "1" in said[0]
