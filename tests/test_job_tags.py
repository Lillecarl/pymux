"""
Key-value tags on jobs.

The rules this judges: `run --tag` takes a bare key or a key with a
value, and a tag without a key refuses; the tags land in the database
with the row, a repeated key keeping its last value and a bare key
carrying NULL rather than an empty string; `list-jobs` names them;
and the `by_tag` seed finds what a tag names.

The session is one tag among them: `run` stamps what the calling
packet said, an explicit `--session` stamps instead, and lookups
prefer the caller's session without ever hiding another's.
"""

from __future__ import annotations

import argparse
import json
import os

import pytest
from prompt_toolkit.application.current import set_app
from session import create_session

from pymux.agentic import CallerContext
from pymux.commands import CommandException, handle_command
from pymux.commands.run import run_job
from pymux.commands.show_job import show_job
from pymux.jobs import find_job, parse_env


def test_env_parses_a_value_and_a_bare_key_sets_nothing():
    assert parse_env("EXTRA=yes") == ("EXTRA", "yes")
    assert parse_env("EMPTY=") == ("EMPTY", "")
    assert parse_env("BARE") == ("BARE", "")
    with pytest.raises(CommandException, match="names a key"):
        parse_env("=v")


async def _tags_of(store, job_id) -> dict:
    async with store.reader() as conn:
        cursor = await conn.execute("SELECT key, value FROM job_tags WHERE job_id = ?", (job_id,))
        return dict(await cursor.fetchall())


async def test_submit_stores_tags_with_the_row():
    from pymux.jobs import JobTable

    table = JobTable()
    await table.open()
    try:
        job = await table.submit("true", None, [("deploy", None), ("branch", "main")])
        assert job.tags == {"deploy": None, "branch": "main"}
        assert await _tags_of(table.store, job.job_id) == {"deploy": None, "branch": "main"}
        async with table.store.reader() as conn:
            cursor = await conn.execute("SELECT value FROM job_tags WHERE key = 'deploy'")
            assert (await cursor.fetchone()) == (None,)
    finally:
        await table.close()


async def test_a_repeated_key_keeps_its_last_value():
    from pymux.jobs import JobTable

    table = JobTable()
    await table.open()
    try:
        job = await table.submit("true", None, [("k", "first"), ("k", "second")])
        assert job.tags == {"k": "second"}
        assert await _tags_of(table.store, job.job_id) == {"k": "second"}
    finally:
        await table.close()


async def test_run_tags_a_job_end_to_end():
    async with create_session() as (pymux, state):
        pymux.command_output = []
        try:
            with set_app(state.app):
                answer = handle_command(pymux, "run --tag deploy --tag branch=main -w true")
                if answer is not None:
                    await answer
        finally:
            pymux.command_output = None
        (job,) = pymux.jobs.listing()
        assert job.tags == {"deploy": None, "branch": "main"}

        pymux.command_output = []
        try:
            with set_app(state.app):
                answer = handle_command(pymux, "list-jobs")
                if answer is not None:
                    await answer
                said = list(pymux.command_output)
        finally:
            pymux.command_output = None
        assert "[deploy,branch=main]" in said[0]


async def test_a_tag_without_a_key_refuses():
    async with create_session() as (pymux, state):
        args = argparse.Namespace(
            shell_command=["true"], directory=None, tags=["=v"], w=False, session=None, env=[], pty=False
        )
        pymux.command_output = []
        try:
            with set_app(state.app):
                with pytest.raises(CommandException, match="names a key"):
                    await run_job(pymux, args)
        finally:
            pymux.command_output = None


async def test_by_tag_finds_what_a_key_names():
    async with create_session() as (pymux, state):
        await pymux.jobs.submit("echo tagged", None, [("deploy", None)])
        await pymux.jobs.submit("echo plain", None)
        pymux.command_output = []
        try:
            with set_app(state.app):
                answer = handle_command(pymux, "sql --json --run by_tag --param tag=deploy")
                if answer is not None:
                    await answer
                said = list(pymux.command_output)
        finally:
            pymux.command_output = None
        assert json.loads(said[0])["rows"] == [{"id": 1, "command": "echo tagged"}]


async def test_reaping_forgets_the_tags_with_the_job(monkeypatch):
    import pymux.jobs as jobs_module

    monkeypatch.setattr(jobs_module, "FINISHED_KEEP", 1)
    async with create_session() as (pymux, state):
        first = await pymux.jobs.submit("true", None, [("gone", None)])
        await pymux.jobs.supervise(first)
        second = await pymux.jobs.submit("true", None, [("stays", None)])
        await pymux.jobs.supervise(second)
        assert pymux.jobs.get(first.job_id) is None
        assert await _tags_of(pymux.jobs.store, first.job_id) == {}
        assert await _tags_of(pymux.jobs.store, second.job_id) == {"stays": None}


async def test_jobs_default_to_no_session():
    async with create_session() as (pymux, state):
        first = await pymux.jobs.submit("true", None)
        second = await pymux.jobs.submit("true", None, [("session", "someone-else")])
        assert first.session is None
        assert second.session == "someone-else"


async def test_run_stamps_the_callers_session():
    async with create_session() as (pymux, state):
        pymux.caller_context = CallerContext(session_id="s1", environment={}, cwd=None)
        pymux.command_output = []
        try:
            with set_app(state.app):
                for command in ("run --tag a -w true", "run --tag b --session s2 -w true"):
                    answer = handle_command(pymux, command)
                    if answer is not None:
                        await answer
        finally:
            pymux.command_output = None
            pymux.caller_context = None
        by_tag = {tag: job.session for job in pymux.jobs.listing() for tag in job.tags}
        assert by_tag == {"a": "s1", "b": "s2", "session": "s2"}


async def test_a_hand_typed_session_tag_is_left_alone():
    async with create_session() as (pymux, state):
        pymux.caller_context = CallerContext(session_id="s1", environment={}, cwd=None)
        pymux.command_output = []
        try:
            with set_app(state.app):
                answer = handle_command(pymux, "run --tag session=s9 -w true")
                if answer is not None:
                    await answer
        finally:
            pymux.command_output = None
            pymux.caller_context = None
        (job,) = pymux.jobs.listing()
        assert job.session == "s9"


async def test_the_newest_tagged_job_wins():
    async with create_session() as (pymux, state):
        first = await pymux.jobs.submit("echo first", None, [("deploy", None), ("session", "s1")])
        await pymux.jobs.supervise(first)
        second = await pymux.jobs.submit("echo second", None, [("deploy", None), ("session", "s1")])
        await pymux.jobs.supervise(second)
        assert pymux.jobs.resolve([("deploy", None)], scope="s1") is second

        pymux.command_output = []
        try:
            with set_app(state.app):
                show_job(pymux, argparse.Namespace(job=None, e=False, tags=["deploy"], session="s1"))
                said = list(pymux.command_output)
        finally:
            pymux.command_output = None
        assert "second" in said[0] and "first" not in said[0]


async def test_a_bare_key_matches_any_value():
    async with create_session() as (pymux, state):
        job = await pymux.jobs.submit("true", None, [("branch", "main"), ("session", "s1")])
        assert pymux.jobs.resolve([("branch", None)], scope="s1") is job
        assert pymux.jobs.resolve([("branch", "main")], scope="s1") is job
        assert pymux.jobs.resolve([("branch", "other")], scope="s1") is None
        assert pymux.jobs.resolve([("missing", None)], scope="s1") is None


async def test_the_hint_prioritizes_without_filtering():
    async with create_session() as (pymux, state):
        own = await pymux.jobs.submit("echo mine", None, [("deploy", None), ("session", "s1")])
        await pymux.jobs.supervise(own)
        other = await pymux.jobs.submit("echo theirs", None, [("deploy", None), ("session", "s2")])
        await pymux.jobs.supervise(other)
        # The caller's own job wins over a newer job of another
        # session; without a caller the newest wins; and only an
        # explicit scope ever hides a session.
        assert pymux.jobs.resolve([("deploy", None)], hint="s1") is own
        assert pymux.jobs.resolve([("deploy", None)], hint=None) is other
        assert pymux.jobs.resolve([("deploy", None)], scope="s2") is other
        assert pymux.jobs.resolve([("deploy", None)], scope="s3") is None


async def test_find_job_prefers_the_caller_and_scopes_the_flag():
    async with create_session() as (pymux, state):
        await pymux.jobs.submit("echo mine", None, [("deploy", None), ("session", "s1")])
        await pymux.jobs.submit("echo theirs", None, [("deploy", None), ("session", "s2")])
        pymux.caller_context = CallerContext(session_id="s1", environment={}, cwd=None)
        try:
            assert find_job(pymux, None, ["deploy"], None).command == "echo mine"
            assert find_job(pymux, None, ["deploy"], "s2").command == "echo theirs"
            with pytest.raises(CommandException, match="in session s3"):
                find_job(pymux, None, ["deploy"], "s3")
        finally:
            pymux.caller_context = None
        with pytest.raises(CommandException, match="no job tagged missing"):
            find_job(pymux, None, ["missing"], None)


async def test_run_inherits_the_callers_directory_and_environment():
    async with create_session() as (pymux, state):
        pymux.caller_context = CallerContext(
            session_id=None, environment={**dict(os.environ), "AGENT_MARKER": "here"}, cwd="/tmp"
        )
        pymux.command_output = []
        try:
            with set_app(state.app):
                answer = handle_command(pymux, "run --tag x --env EXTRA=yes -w sh -c 'echo $AGENT_MARKER:$EXTRA; pwd'")
                if answer is not None:
                    await answer
                said = list(pymux.command_output)
        finally:
            pymux.command_output = None
            pymux.caller_context = None
        (job,) = pymux.jobs.listing()
        assert job.directory == "/tmp"
        assert job.env["AGENT_MARKER"] == "here"
        assert job.env["EXTRA"] == "yes"
        assert "here:yes" in said[0] and "/tmp" in said[0]


async def test_run_without_a_caller_runs_where_the_server_stands():
    import os

    async with create_session() as (pymux, state):
        pymux.command_output = []
        try:
            with set_app(state.app):
                answer = handle_command(pymux, "run --tag x -w true")
                if answer is not None:
                    await answer
        finally:
            pymux.command_output = None
        (job,) = pymux.jobs.listing()
        assert job.directory is None
        assert job.session is None
        assert job.env["PWD"] == os.environ["PWD"]


async def test_an_id_and_tags_together_refuse():
    async with create_session() as (pymux, state):
        with pytest.raises(CommandException, match="not both"):
            find_job(pymux, 1, ["deploy"], None)
        with pytest.raises(CommandException, match="or --tag"):
            find_job(pymux, None, [], None)
        with pytest.raises(CommandException, match="no job 12"):
            find_job(pymux, 12, [], None)
