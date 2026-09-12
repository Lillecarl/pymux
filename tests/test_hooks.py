"""
The hooks: `set-hook` and `show-hooks`.

A hook runs its commands when the event it is named for happens; the
wake a command leaves when it ran is the `after-<name>` hook, and
the other events carry tmux's names. The tests keep to hooks that
answer on the message line, so what fired is what is read back.
Lillecarl/pymux#297.
"""

from prompt_toolkit.application.current import set_app

from session import create_session, in_a_loop


@in_a_loop
async def test_a_hook_runs_when_the_command_its_named_for_runs():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("set-hook after-select-pane 'display pane-was-selected'")
            pymux.handle_command("select-pane -L")

        assert state.message == "pane-was-selected"


@in_a_loop
async def test_a_hook_runs_when_a_window_opens():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("set-hook after-new-window 'display a-window-opened'")
            pymux.handle_command("new-window 'sleep 30'")

        assert state.message == "a-window-opened"


@in_a_loop
async def test_u_forgets_the_hook():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("set-hook after-select-pane 'display pane-was-selected'")
            pymux.handle_command("set-hook -u after-select-pane")
            pymux.handle_command("select-pane -L")

        assert state.message is None


@in_a_loop
async def test_a_hook_holds_its_commands_in_order():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("set-hook after-select-pane 'display first'")
            pymux.handle_command("set-hook after-select-pane 'display second'")
            pymux.handle_command("select-pane -L")

        assert state.message == "second"
        lines = pymux.hooks["after-select-pane"]
        assert lines == ["display first", "display second"]


@in_a_loop
async def test_show_hooks_lists_them():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("set-hook after-select-pane 'display pane-was-selected'")

            pymux.command_output = []
            pymux.handle_command("show-hooks")

        assert any(
            "after-select-pane" in line and "display pane-was-selected" in line
            for line in pymux.command_output
        )
        pymux.command_output = None
