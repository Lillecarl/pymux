"""
Changing how much a server logs, while it runs.

`--log-level` is read once, before the server starts. That is the wrong
moment: a person turns debug logging on because a server is already
misbehaving, and a restart takes the thing they wanted to look at with
it. Lillecarl/pymux#252.
"""

import logging

import pytest
from pymux import log
from pymux.main import Pymux
from pymux.options import ALL_OPTIONS, SetOptionError


@pytest.fixture(autouse=True)
def put_the_level_back():
    "A test that changes the level of the shared logger has to undo it."
    was = log.logger.level
    yield
    log.logger.setLevel(was)


def test_the_option_is_there():
    assert "log-level" in ALL_OPTIONS


def test_it_offers_the_levels_a_person_may_name():
    assert ALL_OPTIONS["log-level"].get_all_values(Pymux()) == [
        "debug",
        "error",
        "info",
        "warning",
    ]


def test_setting_it_reaches_the_logger():
    pymux = Pymux()
    ALL_OPTIONS["log-level"].set_value(pymux, "debug")
    assert log.logger.level == logging.DEBUG

    ALL_OPTIONS["log-level"].set_value(pymux, "warning")
    assert log.logger.level == logging.WARNING


def test_a_name_that_is_not_a_level_is_refused():
    with pytest.raises(SetOptionError):
        ALL_OPTIONS["log-level"].set_value(Pymux(), "chatty")


def test_the_server_says_which_level_it_is_at():
    pymux = Pymux()
    pymux.log_level = "error"
    assert pymux.log_level == "error"


def test_a_running_server_takes_the_command():
    "The whole point: `pymux -S <sock> set-option log-level debug`."
    pymux = Pymux()
    pymux.handle_command("set-option log-level debug")
    assert pymux.log_level == "debug"


def test_the_server_says_its_level_when_asked_what_it_is_doing():
    """
    pymux has no `show-options`, so a person who turned debug on and
    forgot has nowhere else to read it back.
    """
    from pymux.introspect import _server

    pymux = Pymux()
    pymux.log_level = "debug"
    assert "at debug" in _server(pymux)


def test_a_level_nothing_here_set_reads_back_as_its_number():
    "Honest about a level that something else chose."
    log.logger.setLevel(logging.CRITICAL)
    assert log.level() == str(logging.CRITICAL)


def test_the_flag_and_the_option_take_the_same_words():
    """
    One list, so a person cannot learn one spelling and be refused the
    other.
    """
    from pymux.entry_points.run_pymux import _build_parser, _how_much_to_log

    for name, level in log.LEVELS.items():
        assert _how_much_to_log(name) == level

    action = [one for one in _build_parser()._actions if one.dest == "log_level"][0]
    assert sorted(action.choices) == sorted(log.LEVELS)


def test_no_flag_means_info():
    from pymux.entry_points.run_pymux import _how_much_to_log

    assert _how_much_to_log(None) == logging.INFO
