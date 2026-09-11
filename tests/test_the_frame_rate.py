"""
Capping how many frames a second a client draws.

A frame is expensive and an eye is not fast. A pane that animates asks
for a frame on every write, and three of them together asked a real
server for seventeen a second. `frame-rate` refuses the ones nobody
sees. Lillecarl/pymux#254.

**It belongs to a window**, because a window is what a client looks at
and what holds the programs that animate: the one with cmatrix in it
can be told to draw ten times a second while the one being read stays
sharp.
"""

import sys

import pytest
from prompt_toolkit.application.current import set_app
from pymux.arrangement import DEFAULT_FRAME_RATE
from pymux.main import Pymux
from pymux.options import ALL_WINDOW_OPTIONS, SetOptionError
from session import create_session, in_a_loop

PANE_COMMAND = "%s -c pass" % (sys.executable,)


@pytest.fixture
def pymux():
    mux = Pymux()
    mux.create_window(PANE_COMMAND)
    return mux


def window(pymux):
    return pymux.arrangement.windows[0]


def test_a_window_starts_at_thirty(pymux):
    assert DEFAULT_FRAME_RATE == 30
    assert window(pymux).frame_rate == 30


def test_the_option_writes_the_active_window(pymux):
    ALL_WINDOW_OPTIONS["frame-rate"].set_value(pymux, "10")
    assert window(pymux).frame_rate == 10


def test_the_command_writes_it(pymux):
    pymux.handle_command("set-window-option frame-rate 12")
    assert window(pymux).frame_rate == 12


def test_zero_means_as_fast_as_it_can(pymux):
    ALL_WINDOW_OPTIONS["frame-rate"].set_value(pymux, "0")
    assert window(pymux).frame_rate == 0


def test_a_number_that_is_not_one_is_refused(pymux):
    with pytest.raises(SetOptionError):
        ALL_WINDOW_OPTIONS["frame-rate"].set_value(pymux, "smooth")


def test_a_negative_rate_is_refused(pymux):
    with pytest.raises(SetOptionError):
        ALL_WINDOW_OPTIONS["frame-rate"].set_value(pymux, "-1")


def test_the_global_form_says_what_a_new_window_starts_with(pymux):
    """
    `-g` on a window option is the default for the next window, and
    changes none that is open. Lillecarl/pymux#199.
    """
    was = window(pymux).frame_rate
    pymux.handle_command("set-window-option -g frame-rate 15")

    assert window(pymux).frame_rate == was, "it changed a window that was open"

    pymux.create_window(PANE_COMMAND)
    assert pymux.arrangement.windows[-1].frame_rate == 15


def test_the_option_offers_the_rates_worth_naming(pymux):
    assert "30" in ALL_WINDOW_OPTIONS["frame-rate"].get_all_values(pymux)


# ----------------------------------------------------------------------
# What it does to the client.
#
# **These need a running loop**, so they use `test_command_mode`'s
# session rather than making a client by hand. A `Pymux` with a client
# but no loop leaves the reap of a pane's child waiting in an executor
# thread, and the interpreter waits for that thread at exit: the run
# hangs with nothing to say. `what_leaks.py` names the same trap.


@in_a_loop
async def test_the_cap_reaches_the_application():
    """
    `min_redraw_interval` is prompt_toolkit's own knob, and it holds a
    redraw that arrives too soon rather than dropping it. So a cap
    loses no frame.
    """
    async with create_session() as (mux, state):
        with set_app(state.app):
            mux.handle_command("set-window-option frame-rate 10")

        assert state.app.min_redraw_interval == pytest.approx(0.1)


@in_a_loop
async def test_no_cap_leaves_the_application_uncapped():
    async with create_session() as (mux, state):
        with set_app(state.app):
            mux.handle_command("set-window-option frame-rate 0")

        assert state.app.min_redraw_interval is None


@in_a_loop
async def test_a_client_follows_the_window_it_looks_at():
    "The whole reason it is a window option and not a session one."
    async with create_session() as (mux, state):
        with set_app(state.app):
            first = mux.arrangement.get_active_window()
            mux.handle_command("set-window-option frame-rate 10")

            mux.create_window(PANE_COMMAND)
            mux.handle_command("set-window-option frame-rate 60")

        assert state.app.min_redraw_interval == pytest.approx(1 / 60)

        with set_app(state.app):
            mux.arrangement.set_active_window(first)
            mux.sync_the_frame_rate()

        assert state.app.min_redraw_interval == pytest.approx(0.1)
