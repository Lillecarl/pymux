"""
`choose-job`: the jobs of the server in a centered picker.

Enter shows the pointed job in this session's overlay, read-only;
`o` opens it as a pane in a new window, and `t` runs its command
again, interactively, in a new window. `/` searches the commands
and the tags. None of the three touches the recorded job: closing
the viewer stops the follow, and detaching never kills.
Lillecarl/pymux#528.
"""

from __future__ import annotations

import anyio
import pytest
from prompt_toolkit.application.current import set_app
from session import create_session, once

from pymux.commands import call_command_handler, handle_command
from pymux.jobs import JobFeed
from pymux.main import Pymux


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


def _windows(pymux):
    return pymux.current_session.arrangement.windows


async def test_the_picker_lists_newest_first(pymux_state):
    pymux, state = pymux_state
    await _run_answering(pymux, state, "run -w echo first-job")
    await _run_answering(pymux, state, "run -w echo second-job")
    await _run(pymux, state, "choose-job")

    assert state.choose_job
    matches = state.layout_manager.chooser_matches()
    assert [job.command for job in matches] == ["echo second-job", "echo first-job"]


async def test_the_search_narrows_on_command_and_tags(pymux_state):
    pymux, state = pymux_state
    await _run_answering(pymux, state, "run -w echo first-job")
    await _run_answering(pymux, state, "run --tag nightly -w echo second-job")
    await _run(pymux, state, "choose-job")

    state.choose_window_filter.text = "nightly"
    assert [job.command for job in state.layout_manager.chooser_matches()] == ["echo second-job"]

    state.choose_window_filter.text = "first"
    assert [job.command for job in state.layout_manager.chooser_matches()] == ["echo first-job"]


async def test_enter_shows_the_job_in_the_overlay(pymux_state):
    pymux, state = pymux_state
    await _run_answering(pymux, state, "run -w echo hello-from-the-job")
    await _run(pymux, state, "choose-job")

    with set_app(state.app):
        state.layout_manager.choose_pointed_job()

    # The key handler hands the work to the server's task group, so
    # the overlay arrives a turn later.
    await once(
        lambda: pymux.current_session.overlay_pane is not None,
        5.0,
        "the overlay the key opened never arrived",
    )
    assert not state.choose_job
    overlay = pymux.current_session.overlay_pane
    assert overlay is not None
    assert isinstance(overlay.process, JobFeed)
    await _shows(overlay, "hello-from-the-job")
    await _shows(overlay, "[job 1 exited 0]")


async def test_closing_the_overlay_leaves_the_job_running(pymux_state):
    pymux, state = pymux_state
    await _run(pymux, state, "run sh -c 'sleep 30'")
    await _run(pymux, state, "choose-job")

    with set_app(state.app):
        state.layout_manager.choose_pointed_job()
    await once(
        lambda: pymux.current_session.overlay_pane is not None,
        5.0,
        "the overlay the key opened never arrived",
    )
    overlay = pymux.current_session.overlay_pane
    assert overlay is not None and not overlay.process.is_terminated

    pymux.close_overlay()
    assert pymux.current_session.overlay_pane is None
    assert overlay.process.is_terminated

    job = pymux.jobs.get(1)
    assert job is not None and job.status == "running"
    pymux.jobs.kill(job)


async def test_o_opens_the_job_as_a_pane(pymux_state):
    pymux, state = pymux_state
    await _run_answering(pymux, state, "run -w echo hello-from-the-pane")
    await _run(pymux, state, "choose-job")
    before = set(_windows(pymux))

    with set_app(state.app):
        state.layout_manager.open_pointed_job_in_pane()

    await once(
        lambda: len([window for window in _windows(pymux) if window not in before]) == 1,
        5.0,
        "the window the key opened never arrived",
    )
    assert not state.choose_job
    opened = [window for window in _windows(pymux) if window not in before]
    assert len(opened) == 1
    viewer = opened[0].active_pane
    assert isinstance(viewer.process, JobFeed)
    await _shows(viewer, "hello-from-the-pane")


async def test_t_runs_the_command_again(pymux_state):
    "Taking over respawns: a job holds no stdin to attach to."
    pymux, state = pymux_state
    await _run(pymux, state, "run sh -c 'sleep 30'")
    await _run(pymux, state, "choose-job")
    before = set(_windows(pymux))

    with set_app(state.app):
        state.layout_manager.take_over_pointed_job()

    await once(
        lambda: len([window for window in _windows(pymux) if window not in before]) == 1,
        5.0,
        "the window the key opened never arrived",
    )
    assert not state.choose_job
    opened = [window for window in _windows(pymux) if window not in before]
    assert len(opened) == 1
    taken = opened[0].active_pane
    assert not isinstance(taken.process, JobFeed)
    assert not taken.process.is_terminated

    # The recorded job runs on beside it: detach never kills.
    job = pymux.jobs.get(1)
    assert job is not None and job.status == "running"
    taken.process.kill()
    pymux.jobs.kill(job)


@pytest.fixture
async def pymux_state():
    async with create_session() as (pymux, state):
        yield pymux, state


def test_choose_job_over_the_socket_says_nothing():
    "The command line has no view to open a chooser on."
    mux = Pymux()
    errors = []
    mux.add_command_error = errors.append
    mux.show_message = lambda message: None
    mux.command_output = []
    try:
        call_command_handler("choose-job", mux, [])
    finally:
        mux.command_output = None
    assert errors == []
