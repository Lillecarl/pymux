"""
What a pane that animates costs, when nobody is looking at it.

**A window nobody looks at is not drawn, so it should be nearly free.**
It is not free -- the bytes still reach a pty and a parser -- but the
cost should be the parsing and nothing else. cmatrix in a background
window pegged a core and drew almost nothing while it did
(Lillecarl/pymux#253).

The fault was `ptyhost`'s deferral polling for an idle loop. An idle
loop never comes while anything animates: a prompt_toolkit redraw
postpones itself by reposting on every turn, so the two waited for each
other. It cost 100% of a core to draw five frames in five seconds.

**The measurement needs a real program.** A writer in a `python -c`
loop does not reproduce it: what matters is a program that writes a
screenful at a steady rate, the way a terminal program does. So this
runs the real ones, out of nixpkgs.

**And it needs something animating in the window that is watched.**
That is what keeps the loop's queue full, and without it the fault
hides completely. The first shape of this test measured 7% of a core
either way and would have called the bug fixed.

The unit is a fraction of one core, over `PYMUX_BUSY_SECONDS`. That is
a second, which belongs to the machine that counted it, so the ceiling
is loose on purpose: the fault is an eight-fold difference, not a few
per cent. Nothing here is a budget to be tuned.

Knobs:

    PYMUX_BUSY_PROGRAMS   which of `PROGRAMS` to run, by name
    PYMUX_BUSY_SECONDS    how long to measure each one
    PYMUX_BUSY_CEILING    the most of one core a background pane may take
"""

import asyncio
import os
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
# And pymux itself, which the sandbox copies beside `tests` rather than
# installing. A script's own directory is what Python puts on the path,
# not the directory above it.
sys.path.insert(1, str(Path(__file__).parent.parent))

from session import over_a_connection  # noqa: E402
from prompt_toolkit.application.current import set_app  # noqa: E402
from prompt_toolkit.data_structures import Size  # noqa: E402

#: The programs that animate, by the name a knob takes. Each one writes
#: a screenful at its own rate and never stops.
PROGRAMS = {
    "cmatrix": ["cmatrix", "-u", "2"],
    "tty-clock": ["tty-clock", "-s", "-c"],
    "nyancat": ["nyancat"],
    # The binary is `pipes.sh`, not `pipes`.
    "pipes": ["pipes.sh"],
}

#: What runs in the window the person is looking at. Something has to,
#: or the loop goes idle and the fault cannot show. A shell writing a
#: line every ninety milliseconds is what an agent in a pane looks like
#: from here.
WATCHED_PROGRAM = (
    '%s -c "import sys, time\n'
    "while True:\n"
    "    sys.stdout.write('working\\r\\n')\n"
    "    sys.stdout.flush()\n"
    '    time.sleep(0.09)"' % (sys.executable,)
)

#: The size of Carl's terminal when it was reported. A bigger screen is
#: more cells to parse, so the number is worth pinning.
SIZE = Size(rows=59, columns=187)

HOW_LONG = float(os.environ.get("PYMUX_BUSY_SECONDS") or 5.0)

#: The most of one core a pane nobody looks at may take. The fault was
#: 100%; the fix measured 12%. Half a core is far above one and far
#: below the other, which is what a gate on a wall clock should be.
CEILING = float(os.environ.get("PYMUX_BUSY_CEILING") or 0.5)

#: The caps to compare, for a pane somebody is looking at. `0` is no
#: cap, which is what pymux did before `frame-rate` existed.
WHICH_CAPS = [int(one) for one in (os.environ.get("PYMUX_BUSY_CAPS") or "0 30").split()]


def programs() -> list:
    "The programs this run covers, by name."
    chosen = (os.environ.get("PYMUX_BUSY_PROGRAMS") or "").split()
    if not chosen:
        chosen = sorted(PROGRAMS)

    for name in chosen:
        if name not in PROGRAMS:
            raise SystemExit(
                "PYMUX_BUSY_PROGRAMS names %s, not %r"
                % (", ".join(sorted(PROGRAMS)), name)
            )
    return chosen


def looks_at(pymux, state):
    with set_app(state.app):
        return pymux.arrangement.get_active_window()


async def what_it_costs(name: str) -> tuple:
    """
    Run this program in a window nobody looks at, and measure.

    Returns the fraction of one core it took, and how many frames the
    client drew meanwhile.
    """
    command = " ".join(PROGRAMS[name])

    with over_a_connection() as session:
        pymux = session.pymux
        state, _ = await session.attach("only", SIZE)

        with set_app(state.app):
            pymux.create_window(WATCHED_PROGRAM)
        await asyncio.sleep(0.5)
        watched = looks_at(pymux, state)

        with set_app(state.app):
            pymux.create_window(command)
        await asyncio.sleep(1.0)

        # Back to the watched window, so nobody looks at the animation.
        with set_app(state.app):
            pymux.arrangement.set_active_window(watched)
        await asyncio.sleep(1.0)

        if looks_at(pymux, state) is not watched:
            raise SystemExit("the client did not go back to the watched window")

        before = time.process_time()
        frames = pymux.counters.frames
        await asyncio.sleep(HOW_LONG)

        cost = (time.process_time() - before) / HOW_LONG
        return cost, pymux.counters.frames - frames


async def what_it_costs_watched(name: str, rate: int) -> tuple:
    """
    Run this program in the window the client *does* look at, capped at
    `rate` frames a second, and measure.

    This is where `frame-rate` bites. The table above is the other
    case, where the window is not drawn at all and the cap has nothing
    to refuse.
    """
    command = " ".join(PROGRAMS[name])

    with over_a_connection() as session:
        pymux = session.pymux
        state, _ = await session.attach("only", SIZE)

        with set_app(state.app):
            pymux.create_window(command)
            pymux.handle_command("set-window-option frame-rate %d" % rate)
        await asyncio.sleep(1.5)

        before = time.process_time()
        frames = pymux.counters.frames
        await asyncio.sleep(HOW_LONG)

        cost = (time.process_time() - before) / HOW_LONG
        return cost, (pymux.counters.frames - frames) / HOW_LONG


async def cap() -> None:
    "What the cap buys for a pane somebody is looking at."
    print()
    print("--- what a pane somebody looks at costs, by frame-rate ---")
    print()
    print("%-12s %8s %14s %12s" % ("program", "cap", "of one core", "frames/s"))

    for name in programs():
        if shutil.which(PROGRAMS[name][0]) is None:
            continue
        for rate in WHICH_CAPS:
            cost, fps = await what_it_costs_watched(name, rate)
            print("%-12s %8s %13.0f%% %12.1f" % (name, rate or "none", 100 * cost, fps))


async def main() -> None:
    print("--- what a pane nobody looks at costs ---")
    print()
    print("%-12s %14s %8s" % ("program", "of one core", "frames"))

    over = []
    for name in programs():
        if shutil.which(PROGRAMS[name][0]) is None:
            print("%-12s %14s %8s" % (name, "not here", "-"))
            continue

        cost, frames = await what_it_costs(name)
        print("%-12s %13.0f%% %8d" % (name, 100 * cost, frames))
        if cost > CEILING:
            over.append((name, cost))

    print()
    if over:
        for name, cost in over:
            print(
                "%s took %.0f%% of a core with nobody looking at it. "
                "The ceiling is %.0f%%." % (name, 100 * cost, 100 * CEILING)
            )
        raise SystemExit(1)

    print("Every one of them stayed under %.0f%% of a core." % (100 * CEILING,))

    await cap()


asyncio.run(main())
