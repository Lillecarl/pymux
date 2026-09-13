"""
`set-option status-keys vi`, and the way out of the command line.

tmux gives Escape to vi when the status keys are vi's: it leaves insert
mode and the line stays open, which Lillecarl/pymux#157 decided. Nothing
took the press after that, so with vi status keys **no number of
Escapes closed the line at all**. Lillecarl/pymux#255.

Carl: "if we're in vi mode we should display current editing mode, if
we're in normal mode esc should close (so in insert mode esc goes to
normal)."

So two Escapes: the first leaves insert mode, the second closes. And
the line says which mode it is in, because the mode is what decides
which of the two a press is.
"""

import pytest
from prompt_toolkit.application.current import set_app
from prompt_toolkit.enums import EditingMode
from prompt_toolkit.formatted_text import fragment_list_to_text
from prompt_toolkit.key_binding.vi_state import InputMode
from prompt_toolkit.keys import Keys

from session import create_session

# The pair that presses a key and says whether it left command mode.
# `test_command_mode.py` says why it watches the call and not the
# focus: an application that never ran has nothing to focus back to.
from test_command_mode import in_command_mode, leaves_command_mode


def mode_tag(state) -> str:
    "What the line says about the mode it is in."
    with set_app(state.app):
        return fragment_list_to_text(state.layout_manager._vi_mode_tokens())


def _vi(pymux, state) -> None:
    "Put this client's line on vi status keys, the way the option does."
    with set_app(state.app):
        pymux.handle_command("set-option status-keys vi")
    state.app.editing_mode = EditingMode.VI
    state.app.vi_state.reset()


# ----------------------------------------------------------------------
# The way out.


async def test_one_escape_closes_with_emacs_status_keys():
    "Unchanged, and the reason the hole went unseen. tmux does this."
    async with create_session() as (pymux, state):
        in_command_mode(state)

        assert leaves_command_mode(pymux, state, Keys.Escape)


async def test_the_first_escape_goes_to_normal_mode_and_keeps_the_line():
    """
    Escape belongs to vi in insert mode: it leaves insert mode and the
    line stays open. Lillecarl/pymux#157 decided that, and it is what
    the second press was missing.
    """
    async with create_session() as (pymux, state):
        _vi(pymux, state)
        in_command_mode(state)
        state.app.vi_state.input_mode = InputMode.INSERT

        assert not leaves_command_mode(pymux, state, Keys.Escape)
        assert state.app.vi_state.input_mode is InputMode.NAVIGATION


async def test_the_second_escape_closes():
    "The whole of Lillecarl/pymux#255: normal mode, Escape, gone."
    async with create_session() as (pymux, state):
        _vi(pymux, state)
        in_command_mode(state)
        state.app.vi_state.input_mode = InputMode.INSERT

        leaves_command_mode(pymux, state, Keys.Escape)

        assert leaves_command_mode(pymux, state, Keys.Escape)


async def test_ctrl_c_still_closes_from_either_mode():
    "It was the only way out, and it stays one."
    async with create_session() as (pymux, state):
        _vi(pymux, state)

        for mode in (InputMode.INSERT, InputMode.NAVIGATION):
            in_command_mode(state)
            state.app.vi_state.input_mode = mode

            assert leaves_command_mode(pymux, state, Keys.ControlC), mode


# ----------------------------------------------------------------------
# What the line says it is.


async def test_emacs_status_keys_say_nothing():
    "One mode, so a tag that never changes would say nothing."
    async with create_session() as (pymux, state):
        in_command_mode(state)
        assert mode_tag(state) == ""


@pytest.mark.parametrize(
    "mode,says",
    [
        (InputMode.INSERT, "INSERT"),
        (InputMode.INSERT_MULTIPLE, "INSERT"),
        (InputMode.NAVIGATION, "NORMAL"),
        (InputMode.REPLACE, "REPLACE"),
        (InputMode.REPLACE_SINGLE, "REPLACE"),
    ],
)
async def test_the_line_says_which_vi_mode_it_is_in(mode, says):
    async with create_session() as (pymux, state):
        _vi(pymux, state)
        in_command_mode(state)
        state.app.vi_state.input_mode = mode

        assert says in mode_tag(state)


async def test_the_tag_is_one_width_whatever_it_says():
    """
    The line a person is typing on must not move under them when the
    mode changes, and the tag stands before it.
    """
    async with create_session() as (pymux, state):
        _vi(pymux, state)
        in_command_mode(state)

        widths = set()
        for mode in (InputMode.INSERT, InputMode.NAVIGATION, InputMode.REPLACE):
            state.app.vi_state.input_mode = mode
            widths.add(len(mode_tag(state)))

        assert len(widths) == 1


async def test_the_tag_stands_before_the_colon():
    async with create_session() as (pymux, state):
        _vi(pymux, state)
        in_command_mode(state)
        state.app.vi_state.input_mode = InputMode.NAVIGATION

        with set_app(state.app):
            before = fragment_list_to_text(
                state.layout_manager._before_command_tokens()
            )

        assert before.endswith(":")
        assert "NORMAL" in before


async def test_the_prompt_says_it_too():
    "`command-prompt` reads the same option and takes the same keys."
    async with create_session() as (pymux, state):
        _vi(pymux, state)
        state.prompt_text = "Rename to"
        state.app.vi_state.input_mode = InputMode.NAVIGATION

        with set_app(state.app):
            before = fragment_list_to_text(
                state.layout_manager._before_prompt_command_tokens()
            )

        assert "NORMAL" in before
        assert "Rename to" in before
