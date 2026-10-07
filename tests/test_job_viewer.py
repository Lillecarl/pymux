"""
`view-job`: replace a pane with a viewer for a job.

The rules this judges: the new pane keeps the place and the size of
the old one, and its screen shows the tail of the job -- stdout first
and stderr under a separator -- then follows everything new as it
arrives, until the job ends and the pane says how. Nothing forks: the
feed stands where the process would, the pane is read-only because
keys fall on the floor, and closing the pane never touches the job.
Replacing a pane whose program still runs refuses without `-k`, the
same rule `respawn-pane` holds.
"""

from __future__ import annotations

import argparse

import anyio
import pytest
from prompt_toolkit.application.current import set_app
from session import create_session

from pymux.commands import CommandException, handle_command
from pymux.commands.view_job import view_job
from pymux.jobs import JobFeed


async def _run(pymux, state, command) -> None:
    "Run a command and wait for it, the way the socket route does."
    with set_app(state.app):
        answer = handle_command(pymux, command)
        if answer is not None:
            await answer


async def _run_answering(pymux, state, command) -> None:
    "Run a command a client waits for: `run -w` refuses without one."
    pymux.command_output = []
    try:
        await _run(pymux, state, command)
    finally:
        pymux.command_output = None


async def _shows(pane, text: str, seconds: float = 5) -> None:
    "Hold until the screen of a pane shows some text."
    with anyio.fail_after(seconds):
        while text not in pane.screen.page.text(0, 23):
            await anyio.sleep(0.01)


def _viewer(pymux):
    "The pane `view-job` just made: the active one, showing a job."
    pane = pymux.arrangement.get_active_pane()
    assert isinstance(pane.process, JobFeed)
    return pane


async def test_view_job_replays_the_tail_and_says_how_it_ended():
    async with create_session() as (pymux, state):
        await _run_answering(pymux, state, "run -w echo hello-from-the-job")
        await _run(pymux, state, "view-job -k 1")
        pane = _viewer(pymux)

        await _shows(pane, "hello-from-the-job")
        await _shows(pane, "[job 1 exited 0]")

        assert pane.process.backend.pid is None
        assert pane.process.get_name() == "job 1"
        assert pane.process.is_terminated


async def test_view_job_follows_a_running_job():
    async with create_session() as (pymux, state):
        await _run(pymux, state, "run sh -c 'sleep 1; echo late-line; sleep 30'")
        await _run(pymux, state, "view-job -k 1")
        pane = _viewer(pymux)

        # Written after the viewing started, so only a follow shows it.
        await _shows(pane, "late-line", seconds=10)
        assert not pane.process.is_terminated

        pymux.jobs.kill(pymux.jobs.get(1))
        await _shows(pane, "[job 1 exited -15]")
        assert pane.process.is_terminated


async def test_view_job_shows_stderr_under_a_separator():
    async with create_session() as (pymux, state):
        await _run_answering(pymux, state, "run -w sh -c 'echo out-line; echo err-line >&2'")
        await _run(pymux, state, "view-job -k 1")
        pane = _viewer(pymux)

        await _shows(pane, "out-line")
        await _shows(pane, "--- stderr ---")
        await _shows(pane, "err-line")


async def test_view_job_starts_every_line_at_the_first_column():
    # A pipe has no ONLCR: without the viewer translating newlines,
    # every line would start where the last one ended.
    async with create_session() as (pymux, state):
        await _run_answering(pymux, state, "run -w printf 'aaa\\nbbbb\\n'")
        await _run(pymux, state, "view-job -k 1")
        pane = _viewer(pymux)

        await _shows(pane, "bbbb")
        rows = [line.text for line in pane.screen.page.text_lines(0, 23)]
        assert rows[0].startswith("aaa")
        assert rows[1].startswith("bbbb")


async def test_closing_the_viewer_leaves_the_job():
    async with create_session() as (pymux, state):
        await _run(pymux, state, "run sh -c 'sleep 1; echo done-line'")
        await _run(pymux, state, "view-job -k 1")
        pane = _viewer(pymux)
        await _shows(pane, "done-line")

        await _run(pymux, state, "kill-pane")

        panes = [p for w in pymux.arrangement.windows for p in w.panes]
        assert pane not in panes
        job = pymux.jobs.get(1)
        with anyio.fail_after(5):
            await pymux.jobs.wait(job)
        assert job.is_done and job.returncode == 0


async def test_keys_into_a_viewer_go_nowhere():
    async with create_session() as (pymux, state):
        await _run_answering(pymux, state, "run -w echo hello-from-the-job")
        await _run(pymux, state, "view-job -k 1")
        pane = _viewer(pymux)
        await _shows(pane, "hello-from-the-job")

        before = pane.screen.page.text(0, 23)
        pane.process.write_input("hello")
        await anyio.sleep(0.1)

        assert pane.screen.page.text(0, 23) == before


async def test_view_job_refuses_a_busy_pane_without_k():
    async with create_session() as (pymux, state):
        await _run_answering(pymux, state, "run -w echo hello-from-the-job")
        await _run(pymux, state, "new-window sleep 30")

        with pytest.raises(CommandException, match="Pane is busy"):
            view_job(pymux, argparse.Namespace(k=False, target_pane=None, job=1))

        await _run(pymux, state, "view-job -k 1")
        pane = _viewer(pymux)
        await _shows(pane, "hello-from-the-job")


async def test_view_job_of_no_job_says_so():
    async with create_session() as (pymux, state):
        with pytest.raises(CommandException, match="no job 12"):
            view_job(pymux, argparse.Namespace(k=False, target_pane=None, job=12))
