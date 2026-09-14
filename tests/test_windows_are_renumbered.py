"""
`renumber-windows`: closing a window closes the gap it leaves.

pymux fills the lowest free index when a window is made, and nothing
ever packed the rest down, so a session numbered 1, 2, 3 that loses
the second one stayed 1, 3 for good. tmux has a session option for it
and ships it off; this is the same option, the same default and the
same trigger. Lillecarl/pymux#342.
"""

import sys

from prompt_toolkit.application.current import set_app

from pymux.main import Pymux

from session import DEFAULT_SIZE, in_this_process

#: A pane that stays up, so a window lives until a test closes it.
WAITS = "%s -c 'import time; time.sleep(30)'" % (sys.executable,)


def _three_windows(pymux: Pymux) -> list:
    for _ in range(3):
        pymux.create_window(WAITS)
    return [window.index for window in pymux.arrangement.windows]


def _kill(pymux: Pymux, index: int) -> None:
    window = pymux.arrangement.get_window_by_index(index)
    for pane in list(window.panes):
        pymux.kill_pane(pane)


def _indices(pymux: Pymux) -> list:
    return sorted(window.index for window in pymux.arrangement.windows)


def _stop(pymux: Pymux) -> None:
    for window in list(pymux.arrangement.windows):
        for pane in list(window.panes):
            if not pane.process.is_terminated:
                pane.process.kill()


# ----------------------------------------------------------------------
# What the option does.


def test_without_it_the_gap_stays():
    "The behaviour pymux had, and the default it keeps."
    pymux = Pymux()
    try:
        assert _three_windows(pymux) == [1, 2, 3]

        _kill(pymux, 2)

        assert _indices(pymux) == [1, 3]
    finally:
        _stop(pymux)


def test_with_it_the_windows_pack_down():
    pymux = Pymux()
    try:
        _three_windows(pymux)
        pymux.handle_command("set-option renumber-windows on")

        _kill(pymux, 2)

        assert _indices(pymux) == [1, 2]
    finally:
        _stop(pymux)


def test_the_order_is_kept():
    "A pass that sorted by anything else would shuffle a person's windows."
    pymux = Pymux()
    try:
        _three_windows(pymux)
        pymux.handle_command("set-option renumber-windows on")
        third = pymux.arrangement.get_window_by_index(3)

        _kill(pymux, 1)

        assert [window.index for window in pymux.arrangement.windows] == [
            window.index
            for window in sorted(
                pymux.arrangement.windows, key=lambda one: one.index
            )
        ]
        assert third.index == 2
    finally:
        _stop(pymux)


def test_it_counts_from_the_base_index():
    pymux = Pymux()
    try:
        pymux.handle_command("set-option base-index 0")
        _three_windows(pymux)
        pymux.handle_command("set-option renumber-windows on")
        assert _indices(pymux) == [0, 1, 2]

        _kill(pymux, 1)

        assert _indices(pymux) == [0, 1]
    finally:
        _stop(pymux)


async def test_nobody_loses_the_window_they_were_on():
    """
    A client holds a `Window` and not an index, so a relabel cannot
    move anybody. The window it is on gets a new number and stays the
    window it is on.
    """
    async with in_this_process() as session:
        pymux = session.pymux
        _three_windows(pymux)
        pymux.handle_command("set-option renumber-windows on")
        state, _ = await session.attach("here", DEFAULT_SIZE)

        watching = pymux.arrangement.get_window_by_index(3)
        with set_app(state.app):
            pymux.arrangement.set_active_window(watching)

            _kill(pymux, 1)

            assert pymux.arrangement.get_active_window() is watching
        assert watching.index == 2


def test_an_unlinked_window_keeps_its_index():
    """
    `unlink-window` parks a window out of the order, and
    `link-window` puts it back where it was. Packing an order nobody
    sees would move a window a person never closed.
    Lillecarl/pymux#297.
    """
    pymux = Pymux()
    try:
        _three_windows(pymux)
        pymux.handle_command("set-option renumber-windows on")
        parked = pymux.arrangement.get_window_by_index(3)
        pymux.arrangement.unlink_window(parked)

        _kill(pymux, 1)

        assert parked.index == 3
        assert _indices(pymux) == [1]
    finally:
        _stop(pymux)


# ----------------------------------------------------------------------
# The option itself.


def test_it_reads_back():
    pymux = Pymux()
    pymux.command_output = []
    try:
        pymux.handle_command("set-option renumber-windows on")
        pymux.handle_command("show-options renumber-windows")
        assert pymux.command_output == ["on"]
    finally:
        pymux.command_output = None


def test_a_word_it_does_not_know_is_refused():
    pymux = Pymux()
    pymux.handle_command("set-option renumber-windows sometimes")

    assert pymux.arrangement.renumber_windows is False
