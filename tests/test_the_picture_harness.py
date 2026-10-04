"""
What the picture harness promises before it opens a display server.

Every other question about a picture needs an X server, a compositor
and three terminal emulators, so it is asked in `checks.pymux-pictures`
and takes twenty minutes. These are the ones that need none of that,
and the first of them is the one a burst got wrong.
"""
from __future__ import annotations

from pyterm_pytest.seats import (
    APPEAR_TIMEOUT,
    BLINK_FRAMES,
    BLINK_GAP,
    BLINK_START,
    SETTLE_TIMEOUT,
)
from take_picture import BLINK_FIXTURES, HOLD, blink_program

#: The longest one picture took on this machine, measured with three
#: times as many busy processes as cores: 0.85 seconds. A burst takes
#: one for every frame, on top of its own waits.
A_SLOW_PICTURE = 1.0


def holds_for(program: str) -> float:
    "The seconds a fixture's program holds its window open."
    return float(program.rstrip().rsplit("sleep ", 1)[1])


def test_a_blink_fixture_holds_its_window_past_its_whole_burst():
    """
    `import` does not fail on a window that has gone. It waits, and the
    harness's budget is the only thing that ends the wait. So a program
    that ends inside its own burst reads as a screenshot that timed
    out, which is what `bare.7` of `cursor-blink` did on a loaded
    machine. Lillecarl/pymux#462.
    """
    burst = BLINK_START + BLINK_FRAMES * (BLINK_GAP + A_SLOW_PICTURE)

    for name in BLINK_FIXTURES:
        assert holds_for(blink_program(name)) >= burst, (
            "%s holds its window for less than its burst takes" % (name,)
        )


def test_a_still_fixture_holds_its_window_past_all_three_waits():
    """
    A still picture waits three times before it is taken: for the
    window, for the fence to reach the clipboard, and for the screen to
    settle. The hold covered two of them, so a picture whose first two
    waits both ran long lost its window while the settle was still
    going. Lillecarl/pymux#462.
    """
    assert HOLD >= APPEAR_TIMEOUT + APPEAR_TIMEOUT + SETTLE_TIMEOUT
