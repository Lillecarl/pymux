"""
A session's options: its own values over the server's defaults.

`SessionOptions` holds what one session set itself, and reads the
default on `Pymux` for the rest, the way tmux's `options_get` walks to
the parent table. Lillecarl/pymux#756.
"""

from __future__ import annotations

import sys

import pytest

from pymux.actions import Origin, started_by
from pymux.commands import call_command_handler
from pymux.main import Pymux
from pymux.options import ALL_OPTIONS
from pymux.session import SessionOptions

ENDS_AT_ONCE = "%s -c pass" % (sys.executable,)


@pytest.fixture
async def two():
    "A server with two sessions, `home` and `work`, each with a window."
    mux = Pymux()
    async with mux.running():
        for name in ("home", "work"):
            await run(mux, "new-session", "-d", "-s", name, ENDS_AT_ONCE)
        yield mux


async def run(mux, command, *arguments):
    "Run a command the way the socket does; give back its errors and what it answered."
    errors = []
    mux.add_command_error = errors.append
    mux.show_message = lambda message: None
    mux.command_output = []
    try:
        answer = call_command_handler(command, mux, list(arguments))
        if answer is not None:
            await answer
        said = list(mux.command_output)
    finally:
        mux.command_output = None
    assert errors == []
    return said


def right(mux, session):
    return mux.get_session(session).options.status_right


async def test_t_sets_one_session_s_own_value(two):
    await run(two, "set-option", "-t", "home", "status-right", "mine")

    assert right(two, "home") == "mine"
    assert right(two, "work") == two.status_right != "mine"


async def test_g_sets_the_default_a_session_without_its_own_reads(two):
    await run(two, "set-option", "-t", "home", "status-right", "mine")
    await run(two, "set-option", "-g", "status-right", "everyone")

    assert right(two, "work") == "everyone"
    assert right(two, "home") == "mine"


async def test_gg_reaches_every_session(two):
    await run(two, "set-option", "-t", "home", "status-right", "mine")
    await run(two, "set-option", "-gg", "status-right", "everyone")

    assert right(two, "home") == right(two, "work") == "everyone"
    assert not two.get_session("home").options.holds("status_right")


async def test_no_flag_sets_the_caller_s_session(two):
    caller = two.current_session
    await run(two, "set-option", "status-right", "here")

    assert caller.options.own() == {"status_right": "here"}
    assert two.status_right != "here"


async def test_a_configuration_line_sets_the_default(two):
    "pymux makes a session before the file runs; a line in it means every session."
    with started_by(Origin.CONFIG):
        await run(two, "set-option", "status-right", "configured")

    assert two.status_right == "configured"
    assert all(not session.options.own() for session in two.sessions)


async def test_g_renumber_windows_reaches_a_session_made_earlier(two):
    "What #756 was first seen as: renumber-windows -g reached one session."
    await run(two, "set-option", "-g", "renumber-windows", "on")

    assert all(session.arrangement.renumber_windows for session in two.sessions)


async def test_show_options_reads_the_session_and_the_default(two):
    await run(two, "set-option", "-t", "home", "status-right", "mine")

    assert await run(two, "show-options", "-t", "home", "status-right") == ["mine"]
    assert await run(two, "show-options", "-g", "status-right") == [two.status_right]


async def test_a_marks_what_a_session_did_not_set(two):
    await run(two, "set-option", "-t", "home", "status-right", "mine")

    (listing,) = await run(two, "show-options", "-A", "-t", "home")
    names = {line.split()[0] for line in listing.splitlines()}
    assert "status-right" in names
    assert "status-left*" in names


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
