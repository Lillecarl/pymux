"""
Passthrough: every key goes to the pane, the prefix too, until the key
that leaves. For a tmux or a pymux inside a pane, which wants the
prefix for itself. Lillecarl/pymux#530.

F12 enters and F12 leaves. The status line says so while it lasts, and
the client's own terminal gets a user variable it may release its own
shortcuts on: kitty's `map --when-focus-on var:pymux_passthrough`.
"""

from __future__ import annotations

from prompt_toolkit.application.current import set_app
from prompt_toolkit.keys import Keys
from session import create_session
from test_command_mode import press
from test_key_tables import written_keys

from pymux.notify.osc import PASSTHROUGH_VARIABLE, set_user_var, set_user_var_of


def status_right(state) -> str:
    with set_app(state.app):
        return state.layout_manager.status.right()


async def test_f12_enters_and_f12_leaves():
    async with create_session() as (pymux, state):
        press(state, Keys.F12)
        assert state.key_tables == ["passthrough"]

        press(state, Keys.F12)
        assert state.key_tables == []


async def test_the_prefix_reaches_the_pane():
    async with create_session() as (pymux, state):
        before = len(pymux.arrangement.windows)
        written = written_keys(pymux)
        press(state, Keys.F12)

        press(state, Keys.ControlB, "c")

        assert not state.has_prefix
        assert len(pymux.arrangement.windows) == before
        # `press` sends a key's name as its data, where a terminal
        # would send the byte.
        assert "".join(written) in ("\x02c", "c-bc")


async def test_the_prefix_answers_again_after_it():
    async with create_session() as (pymux, state):
        press(state, Keys.F12, Keys.F12)

        press(state, Keys.ControlB)

        assert state.has_prefix


async def test_the_status_line_says_so():
    async with create_session() as (pymux, state):
        assert "PASSTHROUGH" not in status_right(state)

        press(state, Keys.F12)

        assert "PASSTHROUGH" in status_right(state)


async def test_the_terminal_is_told_on_the_way_in_and_out(monkeypatch):
    async with create_session() as (pymux, state):
        sent = []
        monkeypatch.setattr(state.connection, "forward_osc", sent.append, raising=False)

        press(state, Keys.F12, Keys.F12)

        assert sent == [set_user_var(PASSTHROUGH_VARIABLE, "1"), set_user_var(PASSTHROUGH_VARIABLE, None)]


def test_the_variable_reads_back_as_set():
    "What a pane would send, so pymux's own reader agrees with the writer."
    sequence = set_user_var(PASSTHROUGH_VARIABLE, "1")
    rest = sequence.removeprefix("\x1b]1337;SetUserVar=").removesuffix("\x07")

    assert set_user_var_of(rest) == (PASSTHROUGH_VARIABLE, "1")
