"""
Key-value tags on jobs.

The rules this judges: `run --tag` takes a bare key or a key with a
value, and a tag without a key refuses; the tags land in the database
with the row, a repeated key keeping its last value and a bare key
carrying NULL rather than an empty string; `list-jobs` names them;
and the `by_tag` seed finds what a tag names.
"""

from __future__ import annotations

import argparse
import json

import pytest
from prompt_toolkit.application.current import set_app
from session import create_session

from pymux.commands import CommandException, handle_command
from pymux.commands.run import run_job


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
        args = argparse.Namespace(shell_command=["true"], directory=None, tags=["=v"], w=False)
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
