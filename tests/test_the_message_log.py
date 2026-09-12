"""
The commands of the batch Lillecarl/pymux#297 added with the message
log: `new-pane`, `show-messages`, `attach-session` and
`switch-client`.
"""

import pytest
from prompt_toolkit.application.current import set_app

from session import create_session, in_a_loop


@in_a_loop
async def test_a_new_pane_splits_the_window():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("new-pane 'sleep 30'")

        window = pymux.arrangement.get_active_window()
        assert len(window.panes) == 2


@in_a_loop
async def test_show_messages_reads_what_the_server_said():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("display hello")
            pymux.handle_command("show-messages")

        lines = state.message.splitlines()
        assert "hello" in lines
        assert lines[-1] == "hello"


@in_a_loop
async def test_an_error_is_in_the_log_too():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("set-option nope")

        assert any("Invalid option" in line for line in pymux.message_log)


@in_a_loop
async def test_attach_and_switch_have_nowhere_to_go():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("attach-session")
            assert "attached" in state.message

            pymux.handle_command("switch-client")
            assert "switch" in state.message
