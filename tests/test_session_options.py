"""
A session's options: its own values over the server's defaults.

`SessionOptions` holds what one session set itself, and reads the
default on `Pymux` for the rest, the way tmux's `options_get` walks to
the parent table. Lillecarl/pymux#756.
"""

from __future__ import annotations

import pytest

from pymux.main import Pymux
from pymux.options import ALL_OPTIONS
from pymux.session import SessionOptions


def test_a_session_may_hold_every_session_option_and_nothing_else():
    "A name missing here reads the default for ever; one extra reads nothing."
    names = {option.attribute_name for option in ALL_OPTIONS.values() if option.attribute_name is not None}
    assert set(SessionOptions.__annotations__) == names


def test_every_name_has_a_default():
    pymux = Pymux()
    for name in SessionOptions.__annotations__:
        assert hasattr(pymux, name), name


def test_a_session_reads_the_default_until_it_sets_its_own():
    pymux = Pymux()
    options = pymux.sessions[0].options

    assert options.status_right == pymux.status_right
    pymux.status_right = "later"
    assert options.status_right == "later"

    options.status_right = "mine"
    assert options.status_right == "mine"
    assert pymux.status_right == "later"
    assert options.own() == {"status_right": "mine"}

    options.forget("status_right")
    assert options.status_right == "later"
    assert not options.holds("status_right")


def test_a_name_that_is_no_option_is_an_error():
    with pytest.raises(AttributeError):
        _ = Pymux().sessions[0].options.no_such_option


def test_the_window_order_reads_through_the_session():
    pymux = Pymux()
    arrangement = pymux.sessions[0].arrangement

    pymux.renumber_windows = True
    assert arrangement.renumber_windows is True

    arrangement.base_index = 0
    assert pymux.sessions[0].options.own() == {"base_index": 0}
    assert pymux.base_index == 1
