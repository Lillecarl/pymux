"""
The auto refresh draws a frame only when the text it draws changed.

A background thread wakes every `status-interval` seconds. The clock in
the status line is what it is for. A whole frame is 279,645 bytecode
instructions and the text that time moves is 2,371 of them, so the
thread reads the text and asks for a frame only when it differs from
what the last frame drew. Lillecarl/pymux#154.

A session with `full-screen on` draws no status line and no pane
titlebar, so its text is empty and never differs. Lillecarl/pymux#151.
"""
import asyncio
import io
import sys
import time

import pytest
from prompt_toolkit.application.current import set_app
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import ColorDepth
from prompt_toolkit.output.vt100 import Vt100_Output

from pymux.main import Pymux
from pymux.options import ALL_OPTIONS

#: Short enough that a test finishes, long enough that the thread sleeps.
INTERVAL = 0.01

#: How many intervals a test waits before it counts.
TICKS = 20

ROWS, COLUMNS = 24, 80


class _Connection:
    "What `Pymux` asks a connection for, and nothing else."

    kitty_source_flags = 0
    pointer_shape = None
    graphics = None


@pytest.fixture
def session():
    """
    A server with one client and one window, whose clock runs fast.

    The client counts the frames its own application is asked for. Its
    application never runs, so a frame is a request and no more.
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    pymux = Pymux()
    pymux.status_interval = INTERVAL
    pymux.create_window("%s -c 'import time; time.sleep(30)'" % (sys.executable,))

    output = Vt100_Output(
        stdout=io.StringIO(), get_size=lambda: Size(rows=ROWS, columns=COLUMNS)
    )
    with create_pipe_input() as pipe:
        state = pymux.add_client(
            output=output,
            input=pipe,
            color_depth=ColorDepth.DEPTH_8_BIT,
            connection=_Connection(),
        )

        frames = []
        state.app.invalidate = lambda: frames.append(1)
        pymux._start_auto_refresh_thread()

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


def the_text(pymux, state):
    "What the thread reads, read the way the thread reads it."
    with set_app(state.app):
        return state.layout_manager.what_time_moves()


def test_a_tick_that_finds_the_same_text_asks_for_no_frame(session):
    "The clock says the same minute for fourteen of every fifteen ticks."
    pymux, state, frames = session

    # The first tick draws what no frame drew before it.
    time.sleep(INTERVAL * TICKS)
    assert len(frames) == 1


def test_a_tick_asks_for_a_frame_when_the_text_changed(session):
    "`#W` in the status line names the window, so a rename shows."
    pymux, state, frames = session
    time.sleep(INTERVAL * TICKS)
    assert len(frames) == 1

    pymux.arrangement.windows[0].chosen_name = "renamed"
    time.sleep(INTERVAL * TICKS)
    assert len(frames) == 2


def test_a_full_screen_session_asks_for_none(session):
    "One pane over every cell. Nothing there moves with time."
    pymux, state, frames = session
    set_option(pymux, "full-screen", "on")
    state.last_time_text = ()
    frames.clear()

    assert the_text(pymux, state) == ()
    time.sleep(INTERVAL * TICKS)
    assert frames == []


def test_a_clock_inside_a_pane_asks_for_frames(session):
    """
    `clock-mode` draws a clock over the content of the pane, and
    `ctrl-b t` turns it on. With the status line hidden, that clock is
    the only thing on the screen that time moves.
    """
    pymux, state, frames = session
    set_option(pymux, "full-screen", "on")
    state.last_time_text = ()
    frames.clear()
    time.sleep(INTERVAL * TICKS)
    assert frames == []

    pymux.arrangement.windows[0].panes[0].clock_mode = True
    time.sleep(INTERVAL * TICKS)
    assert len(frames) == 1
    assert the_text(pymux, state) != ()


def test_the_text_holds_the_clock_and_the_window_list(session):
    "What the thread compares, spelled out."
    pymux, state, frames = session
    text = the_text(pymux, state)

    assert any(":" in part for part in text), text  # The clock.
    assert any("python" in part or "bash" in part for part in text), text
