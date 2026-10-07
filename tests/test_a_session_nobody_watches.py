"""
How big a window is while no client is looking at it.

A window exists before a client attaches, and the program in a pane
needs a size from the first byte it writes. So there is an answer for
nobody watching, and it used to be a number nothing could change:
`_create_pane` wrote eighty by twenty-four into the terminal control,
and every automated caller worked on that pane whatever it asked for.

**A program lays itself out at the width it is told and keeps no record
of what it meant.** A resize makes it redraw, so the view is right from
then on and wrong for everything already scrolled past. That is why the
size has to be right before the first pane starts, and not fixed
afterwards.

`new-session -x -y` names it, and it is the session's: tmux reads the
same two flags the same way and calls the result `default-size`
(`cmd-new-session.c`). It is not `resize-window`, which says a person
means a size to stay -- this one gives way to a client that attaches.
Lillecarl/pymux#459.
"""

from __future__ import annotations

import os
import sys

import pytest
from prompt_toolkit.data_structures import Size

from pymux.commands import call_command_handler
from pymux.entry_points.run_pymux import _axis_of
from pymux.main import Pymux
from pymux.session import DEFAULT_SIZE


@pytest.fixture
async def pymux():
    """
    A server with no window, so each test makes its own session.

    It holds one session already: `Pymux.__init__` makes one, so the
    session each test names is the last of them. Inside `running()`,
    which is the scope a pane's program runs in.
    """
    mux = Pymux()
    async with mux.running():
        try:
            yield mux
        finally:
            for session in list(mux.sessions):
                for window in list(session.arrangement.windows):
                    for pane in list(window.panes):
                        process = getattr(pane, "process", None)
                        if process is not None and not process.is_terminated:
                            process.kill()


ENDS_AT_ONCE = "%s -c pass" % (sys.executable,)


async def new_session(mux, *arguments) -> None:
    mux.command_output = []
    try:
        answer = call_command_handler("new-session", mux, ["-d", *arguments, ENDS_AT_ONCE])
        if answer is not None:
            await answer
    finally:
        mux.command_output = None


def one_pane(mux):
    session = mux.sessions[-1]
    return session, session.arrangement.windows[0].panes[0]


def size_of(pane):
    "What the program in the pane was told, as (columns, rows)."
    return (pane.process.sx, pane.process.sy)


async def test_a_session_nobody_named_a_size_for_is_the_standing_answer(pymux):
    await new_session(pymux)
    session, pane = one_pane(pymux)

    assert session.default_size == DEFAULT_SIZE
    assert size_of(pane) == (DEFAULT_SIZE.columns, DEFAULT_SIZE.rows)


async def test_the_size_reaches_the_program_and_not_only_the_session(pymux):
    """
    The whole point. The size used to be written after the pane was
    made, so the session knew one number and the pty another.
    """
    await new_session(pymux, "-x", "200", "-y", "50")
    session, pane = one_pane(pymux)

    assert session.default_size.columns == 200
    assert session.default_size.rows == 50
    assert size_of(pane) == (200, 50)


async def test_the_screen_is_sized_and_not_only_the_pty(pymux):
    """
    `TerminalControl.set_size` tells both. Sizing the pty alone left the
    screen at nought columns, and every character wrapped onto a row of
    its own. Lillecarl/pymux#321.
    """
    await new_session(pymux, "-x", "120", "-y", "40")
    _session, pane = one_pane(pymux)

    assert pane.screen.columns == 120
    assert pane.screen.lines == 40


async def test_one_axis_alone_keeps_the_other(pymux):
    await new_session(pymux, "-x", "200")
    session, _pane = one_pane(pymux)

    assert session.default_size.columns == 200
    assert session.default_size.rows == DEFAULT_SIZE.rows


async def test_the_plane_of_a_window_nobody_watches_is_that_size(pymux):
    "So a layout agrees with the pty, rather than holding its own number."
    await new_session(pymux, "-x", "200", "-y", "50")
    session, _pane = one_pane(pymux)
    window = session.arrangement.windows[0]

    assert pymux.plane_size(window) == session.default_size


async def test_a_pane_split_off_later_is_the_same_size(pymux):
    "The session says it, so a second pane reads the same answer."
    await new_session(pymux, "-x", "200", "-y", "50")
    session, _pane = one_pane(pymux)
    window = session.arrangement.windows[0]

    await pymux.add_process(ENDS_AT_ONCE, window=window)

    assert size_of(window.panes[-1]) == (200, 50)


async def test_each_session_has_its_own(pymux):
    await new_session(pymux, "-s", "wide", "-x", "200", "-y", "50")
    await new_session(pymux, "-s", "narrow")

    sizes = {session.name: session.default_size for session in pymux.sessions}
    assert sizes["wide"].columns == 200
    assert sizes["narrow"].columns == DEFAULT_SIZE.columns


# ----------------------------------------------------------------------
# What a number may be.


@pytest.mark.parametrize("given", ["nope", "12x40", ""])
async def test_a_size_that_is_not_a_number_is_an_error(pymux, given):
    errors = []
    pymux.add_command_error = errors.append
    pymux.show_message = lambda message: None

    call_command_handler("new-session", pymux, ["-d", "-x", given, ENDS_AT_ONCE])

    assert errors == ["pymux: Expecting an integer: %s" % (given,)]


@pytest.mark.parametrize("given", ["0", "-1"])
async def test_a_size_below_one_cell_is_an_error(pymux, given):
    errors = []
    pymux.add_command_error = errors.append
    pymux.show_message = lambda message: None

    call_command_handler("new-session", pymux, ["-d", "-y", given, ENDS_AT_ONCE])

    assert errors == ["pymux: A window is at least one cell."]


async def test_a_size_that_cannot_be_read_leaves_no_session_behind(pymux):
    """
    The numbers are read before anything is made, so a caller that
    mistyped one has no half-made session to clean up.
    """
    pymux.add_command_error = lambda message: None
    pymux.show_message = lambda message: None
    # A server starts with one session of its own, so the count is what
    # says whether the command made another.
    before = len(pymux.sessions)

    call_command_handler("new-session", pymux, ["-d", "-x", "nope", ENDS_AT_ONCE])

    assert len(pymux.sessions) == before


# ----------------------------------------------------------------------
# The route that starts a server.
#
# There is nothing to send a command to yet, so the client reads the two
# flags itself and hands them to `Pymux`. That route has to agree with
# the command handler above, and it is the one an automated caller uses:
# `pymux -S <path> new-session -d` is how a server comes to exist.


async def test_the_startup_window_is_the_size_the_client_asked_for():
    "The window `startup` makes, before any client has ever rendered."
    mux = Pymux(size_with_no_client=Size(rows=50, columns=200))
    async with mux.running():
        try:
            await mux.startup()
            _session, pane = one_pane(mux)
            assert size_of(pane) == (200, 50)
        finally:
            for session in list(mux.sessions):
                for window in list(session.arrangement.windows):
                    for pane in list(window.panes):
                        process = getattr(pane, "process", None)
                        if process is not None and not process.is_terminated:
                            process.kill()


@pytest.mark.parametrize(
    ("given", "expected"),
    [(None, 80), ("200", 200), ("1", 1)],
)
def test_the_client_reads_a_number_the_same_way(given, expected):
    values = {} if given is None else {"x": given}
    assert _axis_of(values, "x", 80) == expected


@pytest.mark.parametrize("given", ["nope", "12x40", ""])
def test_the_client_refuses_what_is_not_a_number(given):
    "Before a server is forked, so a mistyped number leaves nothing behind."
    with pytest.raises(ValueError, match="Expecting an integer"):
        _axis_of({"y": given}, "y", 24)


@pytest.mark.parametrize("given", ["0", "-1"])
def test_the_client_refuses_less_than_one_cell(given):
    with pytest.raises(ValueError, match="at least one cell"):
        _axis_of({"y": given}, "y", 24)


def test_a_dash_reads_the_terminal_of_whoever_typed_it(monkeypatch):
    """
    tmux reads "-" as the size of the client that asked. This route is
    that client, so its own terminal is the answer, and with no terminal
    `get_terminal_size` gives the standing numbers back.
    """
    import shutil

    monkeypatch.setattr(shutil, "get_terminal_size", lambda fallback=None: os.terminal_size((123, 45)))

    assert _axis_of({"x": "-"}, "x", 80) == 123
    assert _axis_of({"y": "-"}, "y", 24) == 45


async def test_a_dash_with_no_client_watching_is_the_standing_answer(pymux):
    """
    tmux reads "-" as the size of the client that asked, and answers 80
    or 24 when none did. A command over the socket has no client that
    draws.
    """
    await new_session(pymux, "-x", "-", "-y", "-")
    session, _pane = one_pane(pymux)

    assert session.default_size == DEFAULT_SIZE


# ----------------------------------------------------------------------
# Resizing a window no client owns.
#
# `new-session -x -y` names a size a session carries, and it gives way
# to a client that attaches. This is the other half: a size a person
# means to stay, on a window nobody is looking at. It needs a target,
# because there is no client to take the window from.
#
# Lillecarl/pymux#459.


def resize_window(pymux, *arguments) -> list:
    "Run the command, and give back what it complained about."
    errors: list = []
    pymux.add_command_error = errors.append
    pymux.show_message = lambda message: None
    call_command_handler("resize-window", pymux, list(arguments))
    return errors


def window_of(pymux):
    return pymux.sessions[-1].arrangement.windows[0]


def named(window) -> list:
    "The `-t` that names that one window on the whole server."
    return ["-t", "@%d" % (window.window_id,)]


async def test_a_window_of_a_detached_session_can_be_resized(pymux):
    await new_session(pymux)
    window = window_of(pymux)

    assert resize_window(pymux, *named(window), "-x", "200", "-y", "50") == []
    assert pymux.plane_size(window) == Size(rows=50, columns=200)


async def test_the_new_size_reaches_the_program(pymux):
    """
    **The whole point.** A frame is what tells a pane its size, and a
    window nobody watches gets no frame: the plane answered the new
    number and the pty kept the old one, so the program wrapped its
    output at eighty columns whatever anybody asked for.
    """
    await new_session(pymux)
    window = window_of(pymux)
    pane = window.panes[0]
    assert size_of(pane) == (DEFAULT_SIZE.columns, DEFAULT_SIZE.rows)

    resize_window(pymux, *named(window), "-x", "200", "-y", "50")

    # Forty-nine rows, because the titlebar of a pane draws in one of
    # the fifty the window was given. The test below holds that alone.
    assert size_of(pane) == (200, 49)
    assert (pane.screen.columns, pane.screen.lines) == (200, 49)


async def test_every_pane_of_the_window_hears_it(pymux):
    "Two panes divide the new plane between them, and both are told."
    await new_session(pymux)
    window = window_of(pymux)
    await pymux.add_process(ENDS_AT_ONCE, window=window)

    resize_window(pymux, *named(window), "-x", "200", "-y", "50")

    widths = {size_of(pane)[0] for pane in window.panes}
    assert widths == {200}, widths


async def test_a_target_nothing_holds_says_so(pymux):
    "A silent no-op is what this command used to be. Lillecarl/pymux#458."
    await new_session(pymux)

    assert resize_window(pymux, "-t", "@9999", "-x", "200") == ["pymux: can't find window: @9999"]


async def test_the_titlebar_row_comes_out_of_the_size_that_was_named(pymux):
    """
    `-y 50` is the window's fifty rows, and the titlebar of a pane
    draws in one of them, so the program gets forty-nine. That is what
    the attached case does too, and `resize-window` says the size is
    the window's own.

    **The row is reserved although nobody draws it.** A window no
    client watches has no titlebar on any screen, so the plan keeps a
    row that stays empty. Lillecarl/pymux#474.
    """
    await new_session(pymux)
    window = window_of(pymux)

    resize_window(pymux, *named(window), "-x", "200", "-y", "50")

    assert pymux.plane_size(window).rows == 50
    assert size_of(window.panes[0]) == (200, 49)


async def test_a_nudge_counts_from_the_size_that_window_has(pymux):
    "Not from the active window's, which is the target's whole purpose."
    await new_session(pymux, "-x", "100", "-y", "30")
    window = window_of(pymux)

    resize_window(pymux, *named(window), "-R", "10", "-D", "5")

    assert pymux.plane_size(window) == Size(rows=35, columns=110)
