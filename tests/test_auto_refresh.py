"""
The auto refresh draws a frame only when time can move something.

A background thread wakes every `status-interval` seconds and asks
every client for a frame. The clock in the status line is what it is
for. A session with `full-screen on` draws no status line and no pane
titlebar, so nothing on that screen changes on its own, and the frame
shows what the last one showed. Lillecarl/pymux#151.
"""
import time

from pymux.main import Pymux
from pymux.options import ALL_OPTIONS

#: Short enough that a test finishes, long enough that the thread sleeps.
INTERVAL = 0.01

#: How many intervals a test waits before it counts.
TICKS = 20


def a_pymux(**options):
    "A server whose clock runs fast, and which counts its own frames."
    pymux = Pymux()
    pymux.status_interval = INTERVAL
    for name, value in options.items():
        ALL_OPTIONS[name.replace("_", "-")].set_value(pymux, value)

    frames = []
    pymux.invalidate = lambda: frames.append(1)
    pymux._start_auto_refresh_thread()
    return pymux, frames


def test_a_session_with_a_status_line_asks_for_frames():
    "The clock is on the screen, so the refresh has work to do."
    pymux, frames = a_pymux()
    time.sleep(INTERVAL * TICKS)
    assert frames


def test_a_full_screen_session_asks_for_none():
    "One pane over every cell. Nothing there moves with time."
    pymux, frames = a_pymux(full_screen="on")
    time.sleep(INTERVAL * TICKS)
    assert frames == []


def test_turning_full_screen_off_starts_the_frames_again():
    "The thread reads the option each time it wakes. It never stops."
    pymux, frames = a_pymux(full_screen="on")
    time.sleep(INTERVAL * TICKS)
    assert frames == []

    ALL_OPTIONS["full-screen"].set_value(pymux, "off")
    time.sleep(INTERVAL * TICKS)
    assert frames


def test_a_session_with_no_decoration_at_all_asks_for_none():
    "Both parts off by hand is the same screen as full screen."
    pymux, frames = a_pymux(status="off", pane_border_status="off")
    assert not pymux.something_moves_with_time
    time.sleep(INTERVAL * TICKS)
    assert frames == []
