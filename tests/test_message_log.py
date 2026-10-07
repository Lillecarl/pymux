"""
The commands of the batch Lillecarl/pymux#297 added with the message
log: `new-pane`, `show-messages`, `attach-session` and
`switch-client`.
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


async def test_new_pane_splits_window():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            await run(pymux, state, "new-pane 'sleep 30'")

        window = pymux.arrangement.get_active_window()
        assert len(window.panes) == 2


async def test_show_messages_reads_what_server_said():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            await run(pymux, state, "display hello")
            await run(pymux, state, "show-messages")

        lines = state.message.splitlines()
        assert "hello" in lines
        assert lines[-1] == "hello"


async def test_error_is_in_log_too():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            await run(pymux, state, "set-option nope")

        assert any("Invalid option" in line for line in pymux.message_log)


async def test_attach_and_switch_say_which_session_is_missing():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            await run(pymux, state, "attach-session -t nowhere")
            assert "can't find session: nowhere" in state.message

            await run(pymux, state, "switch-client -t nowhere")
            assert "can't find session: nowhere" in state.message
