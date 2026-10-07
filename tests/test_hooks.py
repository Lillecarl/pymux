"""
The hooks: `set-hook` and `show-hooks`.

A hook runs its commands when the event it is named for happens; the
wake a command leaves when it ran is the `after-<name>` hook, and
the other events carry tmux's names. The tests keep to hooks that
answer on the message line, so what fired is what is read back.
Lillecarl/pymux#297.
"""

from __future__ import annotations

from prompt_toolkit.application.current import set_app
from session import create_session

from pymux.commands import handle_command


async def run(pymux, state, command):
    """
    Run a command as the person at this client, and wait for it.

    A key binding does not wait, but a test that did not would assert
    before the window exists. The end state is the same either way.
    """
    with set_app(state.app):
        answer = handle_command(pymux, command)
        if answer is not None:
            await answer


async def test_hook_runs_when_command_its_named_for_runs():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            await run(pymux, state, "set-hook after-select-pane 'display pane-was-selected'")
            await run(pymux, state, "select-pane -L")

        assert state.message == "pane-was-selected"


async def test_hook_runs_when_window_opens():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            await run(pymux, state, "set-hook after-new-window 'display a-window-opened'")
            await run(pymux, state, "new-window 'sleep 30'")

        assert state.message == "a-window-opened"


async def test_u_forgets_hook():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            await run(pymux, state, "set-hook after-select-pane 'display pane-was-selected'")
            await run(pymux, state, "set-hook -u after-select-pane")
            await run(pymux, state, "select-pane -L")

        assert state.message is None


async def test_hook_holds_its_commands_in_order():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            await run(pymux, state, "set-hook after-select-pane 'display first'")
            await run(pymux, state, "set-hook after-select-pane 'display second'")
            await run(pymux, state, "select-pane -L")

        assert state.message == "second"
        lines = pymux.hooks["after-select-pane"]
        assert lines == ["display first", "display second"]


async def test_show_hooks_lists_them():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            await run(pymux, state, "set-hook after-select-pane 'display pane-was-selected'")

            pymux.command_output = []
            await run(pymux, state, "show-hooks")

        assert any("after-select-pane" in line and "display pane-was-selected" in line for line in pymux.command_output)
        pymux.command_output = None
