"""
The command line gets answers.

A command that runs from the command line runs on a client that draws
nothing, and the answers it asks for -- a listing, a value, an id --
have to come back on stdout. They used to go to a popup on that
client, which went nowhere. Lillecarl/pymux#288, #289, #292.
"""

from __future__ import annotations

import argparse

from prompt_toolkit.application.current import set_app
from session import create_session

from pymux.answer import Answer
from pymux.commands import _finish


async def test_list_windows_answers_command_line():
    async with create_session() as (pymux, state):
        pymux.answer = Answer()

        pymux.handle_command("list-windows")

        assert pymux.answer.output, "the command line heard nothing"


async def test_list_panes_answers_command_line():
    async with create_session() as (pymux, state):
        pymux.answer = Answer()

        pymux.handle_command("list-panes")

        assert pymux.answer.output, "the command line heard nothing"


async def test_listing_shows_in_pane_and_prints_on_command_line():
    """
    The two ways one listing reaches a person: a popup for a person in
    a pane, stdout for the command line.
    """
    async with create_session() as (pymux, state):
        shown = []
        state.layout_manager.display_popup = lambda title, text: shown.append((title, text))

        with set_app(state.app):
            pymux.handle_command("list-windows")
        assert shown and shown[0][0] == "list-windows"
        assert pymux.answer is None

        pymux.answer = Answer()
        pymux.handle_command("list-windows")
        assert pymux.answer.output
        assert len(shown) == 1, "the command line drew a popup"


async def test_capture_pane_p_answers_one_entry_per_call():
    """
    A multi-line answer is one entry, not one per line.

    The server joins the answer's output with a newline, so a command that
    answers once has to append once. This says that much and no more:
    the pane here has no program in it, so the text is empty.

    What `capture-pane -p` really printed one character per line is in
    `tests/drive_with_pty.py`, and it was never this: a detached pane
    parsed at zero columns. Lillecarl/pymux#321.
    """
    async with create_session() as (pymux, state):
        pymux.answer = Answer()

        pymux.handle_command("capture-pane -p")

        assert len(pymux.answer.output) == 1, "one call, one entry -- got %r" % (pymux.answer.output,)
        assert all(isinstance(entry, str) for entry in pymux.answer.output)


async def test_display_message_p_prints_answer():
    async with create_session() as (pymux, state):
        pymux.answer = Answer()

        pymux.handle_command("display-message -p hello")

        assert pymux.answer.output == ["hello"]


async def test_display_message_without_p_shows_message():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("display-message hello")

        assert state.message == "hello"
        assert pymux.answer is None


async def test_get_paneid_names_pane():
    async with create_session() as (pymux, state):
        pane = pymux.arrangement.get_active_pane()

        pymux.answer = Answer()
        pymux.handle_command("get paneid")
        assert pymux.answer.output == [str(pane.pane_id)]

        pymux.answer = Answer()
        pymux.handle_command("get -t %%%i paneid" % pane.pane_id)
        assert pymux.answer.output == [str(pane.pane_id)]


async def test_set_option_without_value_answers_what_it_holds():
    async with create_session() as (pymux, state):
        pymux.answer = Answer()

        pymux.handle_command("set-option status")
        assert pymux.answer.output == ["status on"]

        pymux.handle_command("set-option status off")
        pymux.answer = Answer()
        pymux.handle_command("set-option status")
        assert pymux.answer.output == ["status off"]


async def test_set_window_option_g_answers_default():
    async with create_session() as (pymux, state):
        pymux.answer = Answer()

        pymux.handle_command("set-window-option -g strip on")
        pymux.handle_command("set-window-option -g strip")

        assert pymux.answer.output == ["strip on"]


async def test_option_that_was_never_set_says_so():
    async with create_session() as (pymux, state):
        pymux.answer = Answer()

        pymux.handle_command("set-window-option -g strip")

        assert pymux.answer.output == ["strip not set"]


async def test_set_option_ignores_g_on_read():
    """
    A session option is already global, so `-g` says nothing for it,
    on the read as on the write.
    """
    async with create_session() as (pymux, state):
        pymux.answer = Answer()

        pymux.handle_command("set-option -g status")

        assert pymux.answer.output == ["status on"]


async def test_a_command_that_breaks_after_it_starts_answers_that_it_failed():
    "`upgrade-server` answered 0 to every upgrade a snapshot bug refused."

    async def broken():
        raise RuntimeError("no snapshot")

    async with create_session() as (pymux, state):
        errors = []
        pymux.add_command_error = errors.append
        await _finish(pymux, "upgrade-server x", argparse.Namespace(), broken())

    assert errors == ["pymux: upgrade-server x failed: RuntimeError('no snapshot')"]
