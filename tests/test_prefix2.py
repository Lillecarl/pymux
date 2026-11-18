"""
`prefix2` is a second key into the prefix table, and `send-prefix -2`
sends it on, as in tmux. `None`, tmux's word, is no second key.
Lillecarl/pyterm#1035.
"""

from __future__ import annotations

from prompt_toolkit.application.current import set_app
from prompt_toolkit.keys import Keys
from session import create_session
from test_command_mode import press

from pymux.commands import handle_command


def run_as_client(pymux, state, command):
    with set_app(state.app):
        handle_command(pymux, command)


async def test_prefix2_enters_the_prefix_table():
    async with create_session() as (pymux, state):
        run_as_client(pymux, state, "set -g prefix2 C-a")

        press(state, Keys.ControlA)

        assert state.has_prefix


async def test_the_first_prefix_still_works():
    async with create_session() as (pymux, state):
        run_as_client(pymux, state, "set -g prefix2 C-a")

        press(state, Keys.ControlB)

        assert state.has_prefix


async def test_none_takes_it_away():
    async with create_session() as (pymux, state):
        run_as_client(pymux, state, "set -g prefix2 C-a")
        run_as_client(pymux, state, "set -g prefix2 None")

        press(state, Keys.ControlA)

        assert not state.has_prefix
        press(state, Keys.ControlB)
        assert state.has_prefix


async def test_send_prefix_2_sends_the_second_key():
    async with create_session() as (pymux, state):
        run_as_client(pymux, state, "set -g prefix2 C-a")
        pane = pymux.arrangement.get_active_pane()
        written = []
        pane.process.write_input = written.append

        run_as_client(pymux, state, "send-prefix -2")

        assert "".join(written) == "\x01", written
