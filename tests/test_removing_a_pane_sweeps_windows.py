"""
`Arrangement.remove_pane` walks every window, and the list it walks
shrinks under it.

A window whose last pane went is taken out of `self.windows` inside a
`for` over that same list, so the window after it is skipped. The pane
it would have been asked about belongs to one window, so the skipped
call does nothing -- but the same pass also sweeps up a window that is
already empty, and that sweep is what the skip loses.
Lillecarl/pymux#351.
"""

import sys

from pymux.main import Pymux

WAITS = "%s -c 'import time; time.sleep(30)'" % (sys.executable,)


def _stop(pymux: Pymux) -> None:
    for window in list(pymux.arrangement.windows):
        for pane in list(window.panes):
            if not pane.process.is_terminated:
                pane.process.kill()


def test_an_empty_window_after_a_closing_one_is_swept_up():
    """
    Two windows, the second already empty. Closing the first used to
    leave the second in the order for ever: the list shrank under the
    loop and the empty one was the element it skipped.
    """
    pymux = Pymux()
    try:
        pymux.create_window(WAITS)
        pymux.create_window(WAITS)
        first, second = pymux.arrangement.windows

        # Empty the second one without going through `remove_pane`,
        # which is what a window looks like the moment its last pane
        # has gone and before anybody has swept it.
        for pane in list(second.panes):
            second.remove_pane(pane)
        assert not second.has_panes

        pymux.arrangement.remove_pane(first.panes[0])

        assert first not in pymux.arrangement.windows
        assert second not in pymux.arrangement.windows, (
            "the window after the one that closed was skipped"
        )
    finally:
        _stop(pymux)


def test_the_pane_still_goes_from_the_window_that_held_it():
    pymux = Pymux()
    try:
        pymux.create_window(WAITS)
        window = pymux.arrangement.windows[0]
        pymux.add_process(WAITS, window=window)
        assert len(window.panes) == 2
        going = window.panes[1]

        pymux.arrangement.remove_pane(going)

        assert going not in window.panes
        assert window in pymux.arrangement.windows
    finally:
        _stop(pymux)
