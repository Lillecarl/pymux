"""
The commands of the batch Lillecarl/pymux#297 added with the message
log: `new-pane`, `show-messages`, `attach-session` and
`switch-client`.
"""

import pytest
from prompt_toolkit.application.current import set_app

from session import create_session


async def test_new_pane_splits_window():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("new-pane 'sleep 30'")

        window = pymux.arrangement.get_active_window()
        assert len(window.panes) == 2


async def test_show_messages_reads_what_server_said():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("display hello")
            pymux.handle_command("show-messages")

        lines = state.message.splitlines()
        assert "hello" in lines
        assert lines[-1] == "hello"


async def test_error_is_in_log_too():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("set-option nope")

        assert any("Invalid option" in line for line in pymux.message_log)


async def test_attach_and_switch_have_nowhere_to_go():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("attach-session")
            assert "attached" in state.message

            pymux.handle_command("switch-client")
            assert "switch" in state.message
