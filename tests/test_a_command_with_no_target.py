"""
What a command means by "the window here" when nobody is looking.

A command with no `-t` means the window the caller is looking at. Over
the socket the caller draws nothing and has never looked at one, so
there is no such window, and the answer used to be invented:
`windows[0]` of whichever session the temporary client landed on, or an
`IndexError` out of the handler where the session held no window at
all. An `IndexError` is not a `CommandException`, so nothing turned it
into a message. Lillecarl/pymux#473.
"""

from __future__ import annotations

import sys

import pytest

from pymux.commands import call_command_handler
from pymux.main import Pymux

ENDS_AT_ONCE = "%s -c pass" % (sys.executable,)


@pytest.fixture
def pymux():
    "A server whose one session holds no window yet."
    mux = Pymux()
    try:
        yield mux
    finally:
        for session in list(mux.sessions):
            for window in list(session.arrangement.windows):
                for pane in list(window.panes):
                    process = getattr(pane, "process", None)
                    if process is not None and not process.is_terminated:
                        process.kill()


def run(mux, command, *arguments):
    "Run a command the way the socket does, and give back what it said."
    errors = []
    mux.add_command_error = errors.append
    mux.show_message = lambda message: None
    mux.command_output = []
    try:
        call_command_handler(command, mux, list(arguments))
    finally:
        mux.command_output = None
    return errors


def new_session(mux, *arguments) -> None:
    mux.command_output = []
    try:
        call_command_handler("new-session", mux, ["-d", *arguments, ENDS_AT_ONCE])
    finally:
        mux.command_output = None


def test_a_session_with_no_window_has_no_current_window(pymux):
    assert pymux.arrangement.get_active_window() is None


def test_a_server_that_holds_a_window_somewhere_finds_it(pymux):
    """
    **"No current window" has to mean the server and not the first
    session.** The session `Pymux.__init__` makes holds no window, and
    a caller over the socket has never looked at one, so the answer for
    a bare target comes from `last_used_session`. A server with a
    window must not answer that it has none.
    """
    new_session(pymux, "-s", "work", "-x", "111")
    assert pymux.arrangement.get_active_window() is not None

    assert run(pymux, "resize-window", "-x", "200") == []
    assert pymux.sessions[-1].arrangement.windows[0].manual_size.columns == 200


def test_a_window_command_with_no_target_says_there_is_none(pymux):
    "An IndexError out of the handler reached nobody."
    assert run(pymux, "resize-window", "-x", "200") == ["pymux: no current window"]
