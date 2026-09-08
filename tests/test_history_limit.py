"""
The `history-limit` option says how many rows a pane keeps.

It reached the bounds of copy mode and the format variable, and never
the screen that holds the rows. So a person who asked for ten thousand
rows of scrollback got two thousand, which is what `Screen` keeps when
nobody tells it otherwise.

The option is read on every prune, not once when the pane starts, so a
change reaches a pane that is already running. tmux works the same way.
"""

import sys

import pytest

from pymux.main import Pymux
from pymux.options import ALL_OPTIONS


@pytest.fixture
def pymux():
    "A server with one window, whose program ends at once."
    mux = Pymux()
    mux.create_window("%s -c pass" % (sys.executable,))
    try:
        yield mux
    finally:
        for window in list(mux.arrangement.windows):
            for pane in list(window.panes):
                process = getattr(pane, "process", None)
                if process is not None and not process.is_terminated:
                    process.kill()


def the_pane(mux):
    return mux.arrangement.get_active_window().active_pane


def test_a_pane_keeps_what_the_option_says(pymux):
    ALL_OPTIONS["history-limit"].set_value(pymux, "10000")
    assert the_pane(pymux).screen.get_history_limit() == 10000


def test_the_default_is_the_one_tmux_keeps(pymux):
    assert the_pane(pymux).screen.get_history_limit() == 2000


def test_the_option_reaches_a_pane_that_is_already_running(pymux):
    pane = the_pane(pymux)
    assert pane.screen.get_history_limit() == 2000
    ALL_OPTIONS["history-limit"].set_value(pymux, "5000")
    assert pane.screen.get_history_limit() == 5000
