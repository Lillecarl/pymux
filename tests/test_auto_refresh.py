"""
The auto refresh draws a frame only when the text it draws changed.

The loop asks for a refresh every `status-interval` seconds. The clock
in the status line is what it is for. A whole frame is 279,645 bytecode
instructions and the text that time moves is 2,371 of them, so the
refresh reads the text and asks for a frame only when it differs from
what the last frame drew. A tick that finds it the same costs 2,461.
Lillecarl/pymux#154.

A session with `full-screen on` draws no status line and no pane
titlebar, so its text is empty and never differs. Lillecarl/pymux#151.

The tests call the refresh. What arms it is one `call_later` that arms
the next one, and it runs on the loop for the reason in
Lillecarl/pymux#155.
"""

import asyncio
import io
import sys

import pytest
from prompt_toolkit.application.current import set_app
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import ColorDepth
from prompt_toolkit.output.vt100 import Vt100_Output

from a_session import Connection
from pymux.main import Pymux
from pymux.options import ALL_OPTIONS

ROWS, COLUMNS = 24, 80

#: A status line with nothing in it that time moves, so that a test of
#: the window list does not race the minute of the real clock.
NO_CLOCK = "[#S]"


@pytest.fixture
def session():
    """
    A server with one client and one window.

    The client counts the frames its own application is asked for. That
    application never runs, so a frame is a request and no more.
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    pymux = Pymux()
    pymux.create_window("%s -c 'import time; time.sleep(30)'" % (sys.executable,))

    output = Vt100_Output(
        stdout=io.StringIO(), get_size=lambda: Size(rows=ROWS, columns=COLUMNS)
    )
    with create_pipe_input() as pipe:
        state = pymux.add_client(
            output=output,
            input=pipe,
            color_depth=ColorDepth.DEPTH_8_BIT,
            connection=Connection(),
        )

        frames = []
        state.app.invalidate = lambda: frames.append(1)

        try:
            yield pymux, state, frames
        finally:
            for window in list(pymux.arrangement.windows):
                for pane in list(window.panes):
                    if not pane.process.is_terminated:
                        pane.process.kill()
            loop.close()


def set_option(pymux, name, value):
    ALL_OPTIONS[name].set_value(pymux, value)


def the_text(state):
    "What the refresh reads, read the way the refresh reads it."
    with set_app(state.app):
        return state.layout_manager.what_time_moves()


def in_view(pymux, state):
    """
    The window this client looks at.

    Not `windows[0]`: `add_client` runs `startup()`, which makes a
    window of its own and makes it active.
    """
    with set_app(state.app):
        return pymux.arrangement.get_active_window()


def test_the_first_refresh_draws_what_no_frame_drew(session):
    "Nothing has drawn the status line yet, so it has something to say."
    pymux, state, frames = session

    pymux.refresh_what_time_moves()

    assert len(frames) == 1


def test_a_refresh_that_finds_the_same_text_asks_for_no_frame(session):
    "The clock says the same minute for fourteen of every fifteen ticks."
    pymux, state, frames = session
    set_option(pymux, "status-right", NO_CLOCK)
    pymux.refresh_what_time_moves()
    assert len(frames) == 1

    pymux.refresh_what_time_moves()
    pymux.refresh_what_time_moves()

    assert len(frames) == 1


def test_a_refresh_asks_for_a_frame_when_the_text_changed(session):
    "`#W` in the window list names a window, so a rename shows."
    pymux, state, frames = session
    set_option(pymux, "status-right", NO_CLOCK)
    pymux.refresh_what_time_moves()
    assert len(frames) == 1

    pymux.arrangement.windows[0].chosen_name = "renamed"
    pymux.refresh_what_time_moves()

    assert len(frames) == 2


def test_a_full_screen_session_asks_for_no_frame_at_all(session):
    "One pane over every cell. Nothing there moves with time."
    pymux, state, frames = session
    set_option(pymux, "full-screen", "on")

    assert the_text(state) == ()
    pymux.refresh_what_time_moves()
    pymux.refresh_what_time_moves()

    assert frames == []


def test_a_clock_inside_a_pane_asks_for_frames(session):
    """
    `clock-mode` draws a clock over the content of the pane, and
    `ctrl-b t` turns it on. With the status line hidden, that clock is
    the only thing on the screen that time moves.
    """
    pymux, state, frames = session
    set_option(pymux, "full-screen", "on")
    pymux.refresh_what_time_moves()
    assert frames == []

    # **The window this client looks at**, and not `windows[0]`.
    # `add_client` runs `startup()`, which makes a window of its own
    # and makes it active, so `windows[0]` is a window nobody is
    # looking at. This test used to set the clock there and pass,
    # because the refresh read every window. Lillecarl/pymux#251.
    in_view(pymux, state).panes[0].clock_mode = True
    pymux.refresh_what_time_moves()

    assert len(frames) == 1
    assert the_text(state) != ()


def test_the_text_holds_the_clock_and_the_window_list(session):
    "What the refresh compares, spelled out."
    pymux, state, frames = session

    text = the_text(state)

    assert any(":" in part for part in text), text  # The clock.
    assert any("python" in part or "bash" in part for part in text), text


# ----------------------------------------------------------------------
# How wide the question is. Lillecarl/pymux#251.
#
# A pane's write asks the other clients this now, so reading every pane
# of every window costs on every write and not once every four seconds.


def out_of_view(pymux, state):
    "A window this client is not looking at. There is always one."
    shown = in_view(pymux, state)
    others = [w for w in pymux.arrangement.windows if w is not shown]
    assert others, "the fixture is meant to leave a window out of view"
    return others[0]


def test_a_title_in_a_window_out_of_view_is_not_read(session):
    """
    The titlebars this client draws are its own window's. Another
    window reaches its screen through `window-status-format`, which
    `_get_status_tokens` already formats.
    """
    pymux, state, frames = session
    other = out_of_view(pymux, state)

    before = the_text(state)
    other.panes[0].screen.titles.window = "a name nobody can see"

    assert the_text(state) == before


def test_a_title_in_the_window_in_view_is_read(session):
    "The control: the same title, in the window this client looks at."
    pymux, state, frames = session
    set_option(pymux, "pane-border-status", "on")

    before = the_text(state)
    in_view(pymux, state).panes[0].screen.titles.window = "a name in view"

    assert the_text(state) != before


def test_the_window_list_still_carries_the_other_windows(session):
    """
    Narrowing the panes must not narrow the window list: a window that
    is renamed still changes what every client draws.
    """
    pymux, state, frames = session
    other = out_of_view(pymux, state)

    before = the_text(state)
    other.chosen_name = "renamed"

    assert the_text(state) != before


def test_a_clock_in_a_window_out_of_view_asks_for_nothing(session):
    "A clock that is not drawn is not a reason to draw."
    pymux, state, frames = session
    set_option(pymux, "full-screen", "on")
    other = out_of_view(pymux, state)

    pymux.refresh_what_time_moves()
    frames.clear()

    other.panes[0].clock_mode = True
    pymux.refresh_what_time_moves()

    assert frames == []
