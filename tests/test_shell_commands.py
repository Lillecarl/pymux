"""
`run-shell` and `if-shell`: the server runs a shell for a command.

run-shell shows what the command said; if-shell runs one of two
commands by its answer. Lillecarl/pymux#297.

**Neither one holds the server any more.** Both are handlers that
answer later: the shell runs in a task, so the client that asked waits
and every pane keeps reading its pty meanwhile. `run-shell 'sleep 30'`
used to stop every pane and every client for thirty seconds.
Lillecarl/pymux#311, on the shape Lillecarl/pymux#87 settled.

So a test awaits what the command answered with, and the ones that do
not -- the `-F` form of if-shell, which asks a format and no shell --
say so by passing on the spot.
"""

import anyio
import pytest
from prompt_toolkit.application.current import set_app

from pymux.commands import handle_command
from session import create_session


async def _run(pymux, state, command) -> None:
    "Run a command and wait for it, the way the socket route does."
    with set_app(state.app):
        answer = handle_command(pymux, command)
        if answer is not None:
            await answer


async def test_run_shell_shows_what_command_said():
    async with create_session() as (pymux, state):
        await _run(pymux, state, "run-shell echo hello-from-the-shell")

        dialog = state.layout_manager.popup_dialog
        assert dialog is not None and dialog.title == "run-shell"


async def test_run_shell_from_command_line_answers_on_it():
    async with create_session() as (pymux, state):
        pymux.command_output = []
        try:
            await _run(pymux, state, "run-shell echo straight-back")
            said = pymux.command_output
        finally:
            pymux.command_output = None

        assert any("straight-back" in line for line in said)


async def test_run_shell_says_why_a_shell_that_will_not_start_failed(monkeypatch):
    """
    A shell that cannot start says so, on both routes.

    Starting a process raises OSError when there is no shell to run.
    The pane route ran it in a bare daemon thread, so what it raised
    went nowhere and the person who typed the command got an empty
    listing and no reason for it. Lillecarl/pymux#311.
    """

    async def refuse(*arguments, **named):
        raise OSError(2, "No such file or directory")

    monkeypatch.setattr(anyio, "run_process", refuse)

    async with create_session() as (pymux, state):
        pymux.command_output = []
        try:
            await _run(pymux, state, "run-shell echo never-runs")
            answer = "\n".join(pymux.command_output)
        finally:
            pymux.command_output = None

        assert "No such file or directory" in answer, answer
        assert "run-shell" in answer, answer


async def test_run_shell_with_b_does_not_make_the_caller_wait():
    """
    tmux's `-b`. The command answers with nothing, and the output
    arrives in the view of the client that asked.
    """
    async with create_session() as (pymux, state):
        with set_app(state.app):
            assert handle_command(pymux, "run-shell -b echo in-the-background") is None

        def arrived() -> bool:
            dialog = state.layout_manager.popup_dialog
            return dialog is not None and dialog.title == "run-shell"

        with anyio.fail_after(5):
            while not arrived():
                await anyio.sleep(0.005)


async def test_run_shell_lets_the_server_serve_while_it_runs():
    """
    The point of Lillecarl/pymux#311. The shell is in a task, so the
    loop turns while it runs.
    """
    turned = []

    async def keep_turning() -> None:
        while True:
            await anyio.sleep(0)
            turned.append(1)

    async with create_session() as (pymux, state):
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(keep_turning)
            await _run(pymux, state, "run-shell 'sleep 0.2'")
            tasks.cancel_scope.cancel()

    assert turned, "the server did not turn while the shell ran"


async def test_if_shell_runs_then_command_on_zero():
    async with create_session() as (pymux, state):
        await _run(pymux, state, "if-shell true 'display yes' 'display no'")

        assert state.message == "yes"


async def test_if_shell_runs_else_command_otherwise():
    async with create_session() as (pymux, state):
        await _run(pymux, state, "if-shell false 'display yes' 'display no'")

        assert state.message == "no"


async def test_if_shell_takes_format_for_question():
    "No shell runs, so there is nothing to wait for and it answers here."
    async with create_session() as (pymux, state):
        with set_app(state.app):
            answer = handle_command(
                pymux, "if-shell -F 'i-am-a-format' 'display format-said-yes' 'display no'"
            )

        assert answer is None, "asking a format waited for something"
        assert state.message == "format-said-yes"


async def test_if_shell_keeps_the_question_output_off_the_terminal(capfd):
    """
    Only the status is read. The output used to go to the server's own
    stdout, which is the person's terminal in the integrated and standalone
    routes, and drew over the frame. Lillecarl/pymux#312.
    """
    async with create_session() as (pymux, state):
        await _run(
            pymux, state, "if-shell 'echo scribble; echo noise >&2' 'display yes'"
        )

        assert state.message == "yes"

    written, errored = capfd.readouterr()
    assert "scribble" not in written
    assert "noise" not in errored


async def test_if_shell_with_b_does_not_make_the_caller_wait():
    "tmux's `-b` here too: `wait = !args_has(args, 'b')`."
    async with create_session() as (pymux, state):
        with set_app(state.app):
            assert handle_command(pymux, "if-shell -b true 'display later'") is None

        with anyio.fail_after(5):
            while state.message != "later":
                await anyio.sleep(0.005)


@pytest.mark.parametrize("command", ["run-shell true", "if-shell true 'display yes'"])
async def test_a_shell_command_answers_with_work_to_finish(command):
    "Both of them wait now, and the dispatch is what carries that."
    async with create_session() as (pymux, state):
        with set_app(state.app):
            answer = handle_command(pymux, command)
            assert answer is not None, "%s did not wait for its shell" % (command,)
            await answer
