"""
`run-shell` and `if-shell`: the server runs a shell for a command.

run-shell shows what the command said; if-shell runs one of two
commands by its answer. Both block the server while the shell runs,
the way tmux's do, so the tests keep to `true` and `false`, which
answer in no time. Lillecarl/pymux#297.
"""

import asyncio

import pytest
from prompt_toolkit.application.current import set_app

from session import create_session


async def test_run_shell_shows_what_command_said():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("run-shell echo hello-from-the-shell")

        for _ in range(100):
            dialog = state.layout_manager.popup_dialog
            if dialog is not None and dialog.title == "run-shell":
                break
            await asyncio.sleep(0.05)
        else:
            pytest.fail("the output never arrived")


async def test_run_shell_from_command_line_answers_on_it():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.command_output = []
            pymux.handle_command("run-shell echo straight-back")

        assert any("straight-back" in line for line in pymux.command_output)
        pymux.command_output = None


async def test_run_shell_says_why_a_shell_that_will_not_start_failed(monkeypatch):
    """
    A shell that cannot start says so, on both routes.

    `subprocess.run` raises OSError when there is no shell to run. The
    pane route ran it in a bare daemon thread, so what it raised went
    nowhere and the person who typed the command got an empty listing
    and no reason for it. Lillecarl/pymux#311.
    """
    import subprocess as subprocess_module

    def refuse(*arguments, **named):
        raise OSError(2, "No such file or directory")

    monkeypatch.setattr(subprocess_module, "run", refuse)

    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.command_output = []
            pymux.handle_command("run-shell echo never-runs")

        answer = "\n".join(pymux.command_output)
        pymux.command_output = None
        assert "No such file or directory" in answer, answer
        assert "run-shell" in answer, answer


async def test_if_shell_runs_then_command_on_zero():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("if-shell true 'display yes' 'display no'")

        assert state.message == "yes"


async def test_if_shell_runs_else_command_otherwise():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("if-shell false 'display yes' 'display no'")

        assert state.message == "no"


async def test_if_shell_takes_format_for_question():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("if-shell -F 'i-am-a-format' 'display format-said-yes' 'display no'")

        assert state.message == "format-said-yes"


async def test_if_shell_keeps_the_question_output_off_the_terminal(capfd):
    """
    Only the status is read. The output used to go to the server's own
    stdout, which is the person's terminal in the integrated and standalone
    routes, and drew over the frame. Lillecarl/pymux#312.
    """
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("if-shell 'echo scribble; echo noise >&2' 'display yes'")

        assert state.message == "yes"

    written, errored = capfd.readouterr()
    assert "scribble" not in written
    assert "noise" not in errored
