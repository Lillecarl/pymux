"""
Moving a window from one session to another.

`move-window -t` used to name an index of the current arrangement
only, and `link-window -t` the same: a window had no route between
the sessions of one server. `-t` takes an optional session in front
of the index now, and `-s` names the window, so both commands move
across (`cmd-move-window.c`). Lillecarl/pymux#533.
"""

from __future__ import annotations

import sys

import pytest

from pymux.commands import call_command_handler
from pymux.main import Pymux

ENDS_AT_ONCE = "%s -c pass" % (sys.executable,)

#: A pane that stays up, so a test can tell a move did not kill it.
WAITS = "%s -c 'import time; time.sleep(30)'" % (sys.executable,)


@pytest.fixture
async def pymux():
    "A server with two sessions, `home` and `work`."
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


async def run(mux, command, *arguments):
    "Run a command the way the socket does, and give back what it said."
    errors = []
    mux.add_command_error = errors.append
    mux.show_message = lambda message: None
    mux.command_output = []
    try:
        answer = call_command_handler(command, mux, list(arguments))
        if answer is not None:
            await answer
    finally:
        mux.command_output = None
    return errors


async def new_session(mux, name) -> None:
    mux.command_output = []
    try:
        answer = call_command_handler("new-session", mux, ["-d", "-s", name, ENDS_AT_ONCE])
        if answer is not None:
            await answer
    finally:
        mux.command_output = None


def indices(mux, session):
    "The sorted indexes of one session's order."
    return sorted(window.index for window in mux.get_session(session).arrangement.windows)


async def two_sessions(mux, home_windows=2, work_windows=1):
    """
    `home` and `work`, each with its windows.

    The sessions come in this order, so the caller's session -- what
    a bare `-t` lands in -- is `work`.
    """
    await new_session(mux, "home")
    home = mux.get_session("home")
    for _ in range(home_windows - 1):
        await mux.create_window(ENDS_AT_ONCE, session=home)
    await new_session(mux, "work")
    work = mux.get_session("work")
    for _ in range(work_windows - 1):
        await mux.create_window(ENDS_AT_ONCE, session=work)


# ----------------------------------------------------------------------
# move-window across.


async def test_a_window_moves_to_another_session(pymux):
    await two_sessions(pymux)
    home = pymux.get_session("home")
    moving = home.arrangement.get_window_by_index(1)

    assert await run(pymux, "move-window", "-s", "home:1", "-t", "work:7") == []

    assert moving.index == 7
    assert indices(pymux, "home") == [2]
    assert indices(pymux, "work") == [1, 7]
    assert pymux.session_of_window(moving) is pymux.get_session("work")


async def test_a_live_pane_survives_the_move(pymux):
    "The point of moving and not of killing and reopening."
    await new_session(pymux, "home")
    home = pymux.get_session("home")
    await pymux.create_window(WAITS, session=home)
    moving = home.arrangement.get_window_by_index(2)
    await new_session(pymux, "work")

    assert await run(pymux, "move-window", "-s", "home:2", "-t", "work:3") == []

    assert len(moving.panes) == 1
    assert not moving.panes[0].process.is_terminated
    assert indices(pymux, "work") == [1, 3]


async def test_moving_the_last_window_empties_the_session(pymux):
    "An empty arrangement is a state a server has always been in."
    await two_sessions(pymux, home_windows=1)

    assert await run(pymux, "move-window", "-s", "home:1", "-t", "work:5") == []

    assert pymux.get_session("home").arrangement.windows == []
    assert indices(pymux, "work") == [1, 5]
    assert pymux.get_session("home") in pymux.sessions


async def test_a_taken_index_in_the_destination_is_refused(pymux):
    await two_sessions(pymux)
    moving = pymux.get_session("home").arrangement.get_window_by_index(1)

    assert await run(pymux, "move-window", "-s", "home:1", "-t", "work:1") == [
        "pymux: Can't move window: index in use."
    ]

    assert moving.index == 1
    assert indices(pymux, "home") == [1, 2]
    assert indices(pymux, "work") == [1]


async def test_kill_takes_the_destination_index(pymux):
    "The kill runs where the window lands, not where it leaves."
    await two_sessions(pymux)
    home = pymux.get_session("home")
    moving = home.arrangement.get_window_by_index(1)
    doomed = pymux.get_session("work").arrangement.get_window_by_index(1)

    assert await run(pymux, "move-window", "-k", "-s", "home:1", "-t", "work:1") == []

    assert moving.index == 1
    assert doomed not in pymux.get_session("work").arrangement.windows
    assert indices(pymux, "home") == [2]
    assert indices(pymux, "work") == [1]


async def test_before_inserts_at_the_destination_index(pymux):
    await two_sessions(pymux, work_windows=2)
    moving = pymux.get_session("home").arrangement.get_window_by_index(1)
    second = pymux.get_session("work").arrangement.get_window_by_index(2)

    assert await run(pymux, "move-window", "-b", "-s", "home:1", "-t", "work:1") == []

    assert moving.index == 1
    assert second.index == 3
    assert indices(pymux, "work") == [1, 2, 3]


async def test_a_move_inside_one_session_still_works(pymux):
    "`-s` names a window of the destination session itself."
    await two_sessions(pymux)
    moving = pymux.get_session("home").arrangement.get_window_by_index(2)

    assert await run(pymux, "move-window", "-s", "home:2", "-t", "home:7") == []

    assert moving.index == 7
    assert indices(pymux, "home") == [1, 7]
    assert indices(pymux, "work") == [1]


async def test_an_unknown_destination_session_is_an_error(pymux):
    await two_sessions(pymux)

    assert await run(pymux, "move-window", "-s", "home:1", "-t", "nope:5") == ["pymux: can't find session: nope:5"]

    assert indices(pymux, "home") == [1, 2]


async def test_a_destination_that_is_not_an_index_is_an_error(pymux):
    await two_sessions(pymux)

    assert await run(pymux, "move-window", "-s", "home:1", "-t", "work:five") == [
        "pymux: Can't move window: bad index."
    ]

    assert indices(pymux, "home") == [1, 2]


# ----------------------------------------------------------------------
# link-window across.


async def test_a_window_links_into_another_session(pymux):
    await two_sessions(pymux)
    home = pymux.get_session("home")
    moving = home.arrangement.get_window_by_index(1)

    assert await run(pymux, "link-window", "-s", "home:1", "-t", "work:9") == []

    assert moving.index == 9
    assert indices(pymux, "home") == [2]
    assert indices(pymux, "work") == [1, 9]


async def test_a_session_without_an_index_parks_after_the_last_window(pymux):
    await two_sessions(pymux)
    moving = pymux.get_session("home").arrangement.get_window_by_index(1)

    assert await run(pymux, "link-window", "-s", "home:1", "-t", "work:") == []

    assert moving.index == 2
    assert indices(pymux, "home") == [2]
    assert indices(pymux, "work") == [1, 2]


async def test_an_unlinked_window_links_into_another_session(pymux):
    "The pen is per session; the link reaches across."
    await two_sessions(pymux, home_windows=1, work_windows=2)
    work = pymux.get_session("work")
    moving = work.arrangement.get_window_by_index(1)

    assert await run(pymux, "unlink-window", "-t", "work:1") == []
    assert await run(pymux, "link-window", "-s", "@%d" % moving.window_id, "-t", "home:") == []

    assert work.arrangement._unlinked_windows == []
    assert indices(pymux, "work") == [2]
    assert indices(pymux, "home") == [1, 2]
    assert pymux.session_of_window(moving) is pymux.get_session("home")
