"""
`move-window` at an index that is taken.

A bare `-t` refuses one, which is what pymux always did and what tmux
does. The other two things tmux does with a taken destination are
insert -- move the occupant and the run above it up one -- and kill,
and both are flags on this command: `-a`, `-b` and `-k`
(`cmd-move-window.c:93`). The insert is `make_room_at`, which
`new-window -a` already used. Lillecarl/pymux#191,
Lillecarl/pymux#343.
"""

import sys

from prompt_toolkit.application.current import set_app

from pymux.main import Pymux

from session import DEFAULT_SIZE, in_this_process

#: A pane that stays up, so a window lives until a test closes it.
WAITS = "%s -c 'import time; time.sleep(30)'" % (sys.executable,)


async def _ready(session, how_many: int = 3):
    """
    A client, and exactly this many windows numbered from one.

    **The client comes first.** `Pymux.startup` makes a window when
    the first one attaches, so windows made before that are one short
    of what a test counted.
    """
    state, _ = await session.attach("here", DEFAULT_SIZE)
    for _ in range(how_many - len(session.pymux.arrangement.windows)):
        session.pymux.create_window(WAITS)
    return state


def _indices(pymux: Pymux) -> list:
    return sorted(window.index for window in pymux.arrangement.windows)


def _stop(pymux: Pymux) -> None:
    for window in list(pymux.arrangement.windows):
        for pane in list(window.panes):
            if not pane.process.is_terminated:
                pane.process.kill()


def _run(session, state, command: str) -> None:
    with set_app(state.app):
        session.pymux.handle_command(command)


# ----------------------------------------------------------------------
# A free index, and a taken one with no flag.


async def test_a_free_index_takes_the_window():
    async with in_this_process() as session:
        pymux = session.pymux
        state = await _ready(session, 2)
        moving = pymux.arrangement.get_window_by_index(1)
        with set_app(state.app):
            pymux.arrangement.set_active_window(moving)

        _run(session, state, "move-window -t 7")

        assert moving.index == 7
        assert _indices(pymux) == [2, 7]
        _stop(pymux)


async def test_a_taken_index_is_refused():
    "Which is what pymux always did, and what tmux does."
    async with in_this_process() as session:
        pymux = session.pymux
        state = await _ready(session, 3)
        moving = pymux.arrangement.get_window_by_index(1)
        with set_app(state.app):
            pymux.arrangement.set_active_window(moving)

        # The refusal is a message, which is what a person reads: the
        # command layer turns a `CommandException` into one.
        _run(session, state, "move-window -t 3")

        assert "in use" in (state.message or "")
        assert moving.index == 1
        assert _indices(pymux) == [1, 2, 3]
        _stop(pymux)


# ----------------------------------------------------------------------
# The insert.


async def test_before_takes_the_index_and_moves_the_rest_up():
    async with in_this_process() as session:
        pymux = session.pymux
        state = await _ready(session, 3)
        moving = pymux.arrangement.get_window_by_index(3)
        second = pymux.arrangement.get_window_by_index(2)
        with set_app(state.app):
            pymux.arrangement.set_active_window(moving)

        _run(session, state, "move-window -b -t 2")

        assert moving.index == 2
        assert second.index == 3
        assert _indices(pymux) == [1, 2, 3]
        _stop(pymux)


async def test_after_lands_one_past_the_index():
    async with in_this_process() as session:
        pymux = session.pymux
        state = await _ready(session, 3)
        moving = pymux.arrangement.get_window_by_index(3)
        first = pymux.arrangement.get_window_by_index(1)
        second = pymux.arrangement.get_window_by_index(2)
        with set_app(state.app):
            pymux.arrangement.set_active_window(moving)

        _run(session, state, "move-window -a -t 1")

        assert first.index == 1
        assert moving.index == 2
        assert second.index == 3
        _stop(pymux)


async def test_only_the_run_in_the_way_moves():
    """
    `make_room_at` stops at the first gap, which is tmux's own wording:
    "moving windows up if necessary". A 7 that somebody put there
    stays 7.
    """
    async with in_this_process() as session:
        pymux = session.pymux
        state = await _ready(session, 3)
        far = pymux.arrangement.get_window_by_index(3)
        with set_app(state.app):
            pymux.arrangement.set_active_window(far)
        pymux.arrangement.move_window(far, 7)

        moving = pymux.arrangement.get_window_by_index(1)
        with set_app(state.app):
            pymux.arrangement.set_active_window(moving)

        _run(session, state, "move-window -b -t 2")

        assert far.index == 7, "a window past the gap was moved"
        assert moving.index == 2
        _stop(pymux)


# ----------------------------------------------------------------------
# The kill.


async def test_kill_takes_the_place_of_what_was_there():
    async with in_this_process() as session:
        pymux = session.pymux
        state = await _ready(session, 3)
        moving = pymux.arrangement.get_window_by_index(1)
        killed = pymux.arrangement.get_window_by_index(3)
        with set_app(state.app):
            pymux.arrangement.set_active_window(moving)

        _run(session, state, "move-window -k -t 3")

        assert moving.index == 3
        assert killed not in pymux.arrangement.windows
        assert _indices(pymux) == [2, 3]
        _stop(pymux)


async def test_moving_a_window_to_where_it_is_changes_nothing():
    async with in_this_process() as session:
        pymux = session.pymux
        state = await _ready(session, 2)
        moving = pymux.arrangement.get_window_by_index(2)
        with set_app(state.app):
            pymux.arrangement.set_active_window(moving)

        _run(session, state, "move-window -b -t 2")

        assert moving.index == 2
        assert _indices(pymux) == [1, 2]
        _stop(pymux)
