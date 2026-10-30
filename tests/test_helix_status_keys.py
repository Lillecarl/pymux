"""
`set-option status-keys helix`: Helix's keys on the ":" line and on a
`command-prompt`, through ptkhx. Lillecarl/pymux#749.

Escape works as it does with vi status keys: the first leaves insert
mode and the line stays open, and Escape in normal mode closes it.
"""

from __future__ import annotations

from prompt_toolkit.application.current import set_app
from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text import fragment_list_to_text
from prompt_toolkit.keys import Keys
from ptkhx import Mode
from session import create_session
from test_command_mode import leaves_command_mode, press

from pymux.options.options import ALL_OPTIONS


def _helix(pymux, state, text: str = "new-window") -> None:
    "Helix status keys, and the line open with `text` typed into it."
    ALL_OPTIONS["status-keys"].set_value(pymux, "helix")
    with set_app(state.app):
        state.app.layout.focus(state.command_buffer)
        state.command_buffer.document = Document(text, len(text))


def mode(pymux, state) -> Mode:
    return pymux.key_bindings_manager.helix.mode_of(state.command_buffer)


def tag(state) -> str:
    with set_app(state.app):
        return fragment_list_to_text(state.layout_manager.command_line.before_command_tokens())


async def test_the_first_escape_goes_to_normal_mode_and_keeps_the_line():
    async with create_session() as (pymux, state):
        _helix(pymux, state)
        assert mode(pymux, state) is Mode.INSERT

        assert not leaves_command_mode(pymux, state, Keys.Escape)
        assert mode(pymux, state) is Mode.NORMAL


async def test_escape_in_normal_mode_closes():
    async with create_session() as (pymux, state):
        _helix(pymux, state)
        press(state, Keys.Escape)

        assert leaves_command_mode(pymux, state, Keys.Escape)


async def test_escape_with_a_count_half_typed_drops_the_count_and_stays():
    async with create_session() as (pymux, state):
        _helix(pymux, state)
        press(state, Keys.Escape, "2")

        assert not leaves_command_mode(pymux, state, Keys.Escape)
        assert pymux.key_bindings_manager.helix.pending_of(state.command_buffer) == ""


async def test_normal_mode_edits_the_line():
    async with create_session() as (pymux, state):
        _helix(pymux, state, "kill-window -a")
        press(state, Keys.Escape, "b", "d")
        assert state.command_buffer.text == "kill-window -"

        press(state, "u")
        assert state.command_buffer.text == "kill-window -a"


async def test_enter_in_normal_mode_runs_the_line():
    async with create_session() as (pymux, state):
        _helix(pymux, state, "set-option status-left helixed")
        press(state, Keys.Escape)
        press(state, Keys.ControlM)
        assert pymux.current_session.options.status_left == "helixed"


async def test_the_line_says_which_helix_mode_it_is_in():
    async with create_session() as (pymux, state):
        _helix(pymux, state)
        assert "INS" in tag(state)
        press(state, Keys.Escape)
        assert "NOR" in tag(state)
        assert tag(state).endswith(":")


async def test_the_regex_of_s_shows_on_the_line():
    "It shows nowhere else, and typing a regex blind is no use."
    async with create_session() as (pymux, state):
        _helix(pymux, state)
        press(state, Keys.Escape, "%", "s", "w")
        assert "select:w" in tag(state)


async def test_the_next_line_starts_in_insert_mode():
    async with create_session() as (pymux, state):
        _helix(pymux, state)
        press(state, Keys.Escape)
        with set_app(state.app):
            pymux.leave_command_mode(append_to_history=False)
        assert mode(pymux, state) is Mode.INSERT


async def test_emacs_status_keys_leave_the_line_alone():
    async with create_session() as (pymux, state):
        _helix(pymux, state)
        ALL_OPTIONS["status-keys"].set_value(pymux, "emacs")
        assert leaves_command_mode(pymux, state, Keys.Escape)
