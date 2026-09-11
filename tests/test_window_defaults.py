"""
What a new window starts as, and how a configuration file says it.

A window option belongs to one window. A configuration file is read
before there is a window, so `set-window-option strip on` in one had
nothing to set: it crashed the startup, and then it reported an error
and did nothing.

`set-window-option -g` is the answer, and it is tmux's: it says what
every new window starts with and changes no window that is open.

Lillecarl/pymux#199.
"""

import pytest

from pymux.arrangement import Arrangement, Pane, VSplit
from pymux.commands.commands import handle_command
from pymux.main import Pymux
from pymux.options import ALL_OPTIONS, SetOptionError


class _Fake:
    "Enough of a pane for the arrangement to hold it."


def create_pane():
    return Pane(terminal=_Fake())


def create_arrangement(windows=0):
    arrangement = Arrangement()
    for _ in range(windows):
        arrangement.create_window(create_pane(), set_active=False)
    return arrangement


# ----------------------------------------------------------------------
# The defaults themselves.


def test_a_window_starts_with_nothing_asked_for():
    arrangement = create_arrangement(1)

    assert arrangement.windows[0].synchronize_panes is False
    assert arrangement.windows[0].strip is False


def test_a_default_reaches_the_next_window():
    arrangement = create_arrangement()
    arrangement.window_defaults["synchronize_panes"] = True

    arrangement.create_window(create_pane(), set_active=False)

    assert arrangement.windows[0].synchronize_panes is True


def test_a_default_leaves_the_windows_that_are_open():
    "Which is what `-g` means in tmux."
    arrangement = create_arrangement(1)
    was = arrangement.windows[0]

    arrangement.window_defaults["synchronize_panes"] = True

    assert was.synchronize_panes is False


def test_a_default_that_reshapes_the_window_sees_the_pane():
    """
    `strip` makes the root the row of columns, and what is in the
    window becomes the first column. Applied before the pane it would
    have had nothing to wrap.
    """
    arrangement = create_arrangement()
    arrangement.window_defaults["strip"] = True

    arrangement.create_window(create_pane(), set_active=False)

    window = arrangement.windows[0]
    assert window.strip is True
    assert isinstance(window.root, VSplit)
    assert len(window.root) == 1
    assert len(window.panes) == 1


# ----------------------------------------------------------------------
# What a configuration file can say.


def run(pymux, command):
    """
    One command, and whatever it complained about.

    A command that fails does not raise. It records the message, which
    is how a line of a configuration file reaches the first client
    (Lillecarl/pymux#38), and `startup_errors` is where it lands
    before the startup is done.
    """
    was = len(pymux.startup_errors)
    handle_command(pymux, command)
    return pymux.startup_errors[was:]


def test_a_window_option_before_a_window_says_to_use_dash_g():
    """
    It used to crash the startup with an `IndexError`. The message has
    to name the way out, because a person who reads "there is no
    window" still has a window option they want set.
    """
    pymux = Pymux()

    complaints = run(pymux, "set-window-option strip on")

    assert complaints
    assert "-g" in complaints[0]


def test_dash_g_works_before_there_is_a_window():
    "The whole point: a configuration file is read before one exists."
    pymux = Pymux()

    assert run(pymux, "set-window-option -g strip on") == []
    assert pymux.arrangement.window_defaults == {"strip": True}


def test_a_session_option_has_no_global_form():
    "It is already one value for the whole session."
    pymux = Pymux()

    with pytest.raises(SetOptionError):
        ALL_OPTIONS["status"].set_default(pymux, "off")


def test_dash_g_on_a_session_option_just_sets_it():
    """
    `set -g` is the most common line in a tmux configuration, and
    `pymux -V` says pymux speaks tmux 3.4, so it has to be a line pymux
    takes. It changes nothing about what it means: pymux has one
    session, so a session option is already global.
    """
    pymux = Pymux()
    assert pymux.enable_status is True

    assert run(pymux, "set-option -g status off") == []

    assert pymux.enable_status is False


def test_the_tmux_spelling_of_it_works_too():
    "`set` and `setw` are what a person writes."
    pymux = Pymux()

    assert run(pymux, "set -g status off") == []
    assert run(pymux, "setw -g strip on") == []

    assert pymux.enable_status is False
    assert pymux.arrangement.window_defaults == {"strip": True}


def test_a_value_that_is_not_on_or_off_is_refused_with_dash_g_too():
    pymux = Pymux()

    assert run(pymux, "set-window-option -g strip maybe")
    assert pymux.arrangement.window_defaults == {}
