"""
How many turns of the event loop one keystroke costs.

`checks.pymux-keystroke` counts the work of a keystroke in bytecode, and
`checks.pymux-latency` times the whole thing across three processes. The
first is exact and sees no waiting; the second sees the waiting and
belongs to the machine that read it. **The gap between them was 45% of
a keystroke and nothing could say what it was.** Lillecarl/pymux#232.

A turn of the loop is the number that can. It goes up when pymux hands
control back and waits, which is exactly what an instruction count
cannot see, and it is decided by the code rather than by the machine, so
unlike a millisecond it can hold a budget.

## What one turn is

`SelectorEventLoop._run_once` is one trip: poll the selector, then run
every callback that is ready. `CountingLoop` counts them, and each
`Handle` says which callback it ran, so a total of seven comes with the
seven names that made it. A total says a path grew; the names say where.

**This is asyncio and not anyio, and it has to be.** The number being
counted is a method of asyncio's own loop. The rest of pymux uses anyio;
this measures the loop underneath it.

## Where the two cuts are

    the client writes an "in" packet          <- counted from here
      -> the server's read wakes
      -> the key reaches the pane's pty
      -> the program answers
      -> the parser, the screen, the renderer
      -> the frame goes back as an "out" packet
      -> the client's reader takes it         <- counted to here

Both cuts are taken where the packet is, not where a coroutine that
waited for it resumes: a resumption is a turn of its own and would be
counted as the path's. So the second cut is read inside the reader that
takes the packet off the queue.

**Nothing here polls.** A wait built on `asyncio.sleep` costs a turn
per loop, which is the thing being measured; the reader resolves a
future instead.

## The workload

The same program `checks.pymux-latency` drives, in a pane of its own: it
reads one byte and writes one back. One cell in, one cell out, which is
the steady state a keystroke is. The two checks are then the same path,
counted here and timed there.

Two things that move by themselves are turned off. `status-right` is
emptied, so no clock is drawn, and `status-interval` is set to its
longest, so the auto refresh cannot tick inside a keystroke.

## What the distribution says, and why the gate is the smallest of it

**On an idle machine it is eight turns, five hundred times out of five
hundred.** Under sixteen processes of `yes`, it is eight about six times
in ten and eleven the rest, and the traces say what the three extra
turns are.

A keystroke asks for a frame twice. The key press invalidates, and the
pane's answer invalidates. When the answer lands before the loop polls
again, one redraw carries both, and that is the eight. When it misses
that poll, the redraw of the key press runs by itself and draws nothing
-- no packet leaves -- and the answer then pays for a second one. Three
turns is what prompt_toolkit's postponed redraw costs, twice instead of
once.

So the number is not a race and it is not a single value either. **The
smallest count is the one the code decides**: load can only add the
second redraw, never take a turn away. That is what the check holds, and
the rest of the distribution is instrumentation -- it says how often a
loaded machine misses the poll, which is a property of the machine.

    nix build --file . checks.pymux-turns.run
    less result/log

    PYMUX_TURNS_SAMPLES=1000 nix build --file . checks.pymux-turns.run
    PYMUX_TURNS_TRACE=3    # three traces of each distinct count
"""

import asyncio
import json
import os
import statistics
import sys
import tempfile
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
# And pymux itself, which the sandbox copies beside `tests` rather than
# installing. A script's own directory is what Python puts on the path,
# not the directory above it.
sys.path.insert(1, str(Path(__file__).parent.parent))

from a_session import over_a_connection  # noqa: E402
from measure_latency import MARKERS, THE_CHILD  # noqa: E402
from prompt_toolkit.application.current import set_app  # noqa: E402
from prompt_toolkit.data_structures import Size  # noqa: E402

from pymux.main import Pymux  # noqa: E402

#: How many keystrokes to count. A turn count is nearly determined, so
#: this is about seeing the tail rather than about the average.
SAMPLES = int(os.environ.get("PYMUX_TURNS_SAMPLES") or 300)

#: How many keystrokes of each distinct count name their callbacks.
#:
#: **By the count and not by the order they came in.** The question a
#: trace answers is what the second number is made of, and a run whose
#: first samples were all the common one would never show it. Every
#: keystroke is watched, so that watching is not a difference between
#: them.
TRACE = int(os.environ.get("PYMUX_TURNS_TRACE") or 1)

#: How long to wait after a frame before the next keystroke goes out.
#:
#: The path is measured between two moments, so work that runs after the
#: frame costs the next sample and not this one. This is what keeps that
#: work out of the next sample.
PACE = float(os.environ.get("PYMUX_TURNS_PACE") or 0.03)

#: How long one keystroke may take before the run gives up.
PATIENCE = 10.0

#: The turns of the shortest keystroke, which is the one the code
#: decides. It is what this check holds; the docstring says why the
#: smallest of the distribution is the honest place to hold it.
#:
#:     the server reads the "in" packet            1
#:     the application takes the key, and writes
#:       it to the pane's pty                      1
#:     the pane answers, and the screen changes    1
#:     prompt_toolkit postpones the redraw         3
#:     the frame goes out as an "out" packet       1
#:     the client's reader takes it                1
#:
#: A run that says another number is not wrong. Read the traces, and
#: write the new one here with what moved it.
THE_BEST = 8

#: The client's terminal.
SIZE = Size(rows=24, columns=80)

#: The longest `status-interval` the option takes, in seconds. The auto
#: refresh arms itself for this far ahead, so no tick of it lands inside
#: a keystroke of a run that is over in seconds.
NO_REFRESH = 60


class CountingLoop(asyncio.SelectorEventLoop):
    """
    An event loop that says how many turns it has taken.

    One turn is one `_run_once`: poll the selector, then run whatever is
    ready. Everything that waits costs one.
    """

    def __init__(self):
        super().__init__()
        self.turns = 0

        #: `(turn, what ran)` for every callback, while somebody is
        #: watching. Off between samples, because a run of three
        #: hundred keystrokes would otherwise hold every callback of
        #: all of them.
        self.ran: list = []
        self.watching = False

    def _run_once(self):
        self.turns += 1
        super()._run_once()


def _naming_the_callbacks() -> None:
    """
    Make every `Handle` say what it ran, into the loop that ran it.

    A turn is a trip through the selector and the callbacks that were
    ready, so a count of turns says how many times pymux waited without
    saying what for. The callback names are the answer to that, and the
    `Handle` is the only place they all pass through.
    """
    ran_it = asyncio.events.Handle._run

    def _run(self):
        loop = self._loop
        if getattr(loop, "watching", False):
            loop.ran.append((loop.turns, _what_it_is(self._callback)))
        return ran_it(self)

    asyncio.events.Handle._run = _run


def _what_it_is(callback) -> str:
    """
    The name of a callback, as a reader can use it.

    A task's step is the same callback whichever coroutine it drives, so
    a task is named by its coroutine instead. Without that, half the
    turns of a keystroke read `Task.__step`.
    """
    holder = getattr(callback, "__self__", None)
    if isinstance(holder, asyncio.Task):
        coroutine = holder.get_coro()
        return getattr(coroutine, "__qualname__", None) or repr(coroutine)
    return getattr(callback, "__qualname__", None) or repr(callback)


class TheFrameComingBack:
    """
    The reader of the client's packets, and where the path ends.

    `over_a_connection` calls this with each packet the server sends.
    **The turn is read here** and not where the waiting coroutine
    resumes: a resumption is a turn of its own, and counting it would
    add one to every sample.
    """

    def __init__(self, loop):
        self.loop = loop
        self.marker = None
        self.arrived = None

    def waiting_for(self, marker: str) -> "asyncio.Future":
        self.marker = marker
        self.arrived = self.loop.create_future()
        return self.arrived

    def __call__(self, packet) -> None:
        if self.arrived is None or self.arrived.done():
            return

        if isinstance(packet, (bytes, bytearray)):
            packet = packet.decode("utf-8", "replace")
        try:
            given = json.loads(packet)
        except ValueError:
            return

        if given.get("cmd") != "out" or self.marker not in given.get("data", ""):
            return

        self.arrived.set_result(self.loop.turns)


async def a_keystroke(loop, session, state, coming_back, marker) -> tuple:
    """
    One keystroke, and the turns between the key and the frame.

    The count runs from the moment the packet is on the queue to the
    moment the frame is taken off it.
    """
    arrived = coming_back.waiting_for(marker)

    loop.ran = []
    loop.watching = True
    started = loop.turns
    session.typed(state, marker)

    try:
        landed = await asyncio.wait_for(arrived, PATIENCE)
    finally:
        loop.watching = False

    ran = loop.ran
    loop.ran = []
    await asyncio.sleep(PACE)
    return landed - started, ran


async def measure(loop, samples: int) -> tuple:
    """
    That many keystrokes over a real connection, and what each cost.

    The pane runs the program `checks.pymux-latency` drives, so the two
    checks measure the same path: one byte in, one byte back.
    """
    with tempfile.TemporaryDirectory() as name:
        tmp = Path(name)
        child = tmp / "echo_child.py"
        child.write_text(THE_CHILD)
        log = tmp / "echo.log"

        pymux = Pymux(
            startup_command="%s %s %s" % (sys.executable, child, log),
        )
        # The auto refresh arms itself before a client attaches, so this
        # is set before there is one. A tick inside a keystroke is a
        # turn that the keystroke did not ask for.
        pymux.status_interval = NO_REFRESH

        coming_back = TheFrameComingBack(loop)

        with over_a_connection(pymux=pymux, read_a_packet=coming_back) as session:
            state, _size = await session.attach("one", SIZE)

            with set_app(state.app):
                # The clock moves by itself, and a frame that redraws it
                # is a frame this did not ask for.
                pymux.handle_command("set-option status-right ''")

            counted = []
            traced: dict = {}

            # A warm keystroke first. The first frame paints the whole
            # screen, the first key press builds the bindings of the
            # whole layout, and the program in the pane has to have set
            # its own pty up before a byte means anything.
            for warm in range(2):
                await a_keystroke(loop, session, state, coming_back, MARKERS[warm])

            for number in range(samples):
                marker = MARKERS[number % len(MARKERS)]
                turns, ran = await a_keystroke(
                    loop, session, state, coming_back, marker
                )
                counted.append(turns)
                if len(traced.setdefault(turns, [])) < TRACE:
                    traced[turns].append(ran)

            await session.detach(state)

    return counted, traced


def report(counted: list, traced: list) -> int:
    seen = Counter(counted)

    print("\n--- turns of the event loop, per keystroke ---")
    for turns, times in sorted(seen.items()):
        print(
            "%3d turns %8d keystrokes  %5.1f%%"
            % (turns, times, times * 100.0 / len(counted))
        )

    print(
        "\nmedian %d, mean %.2f, %d to %d over %d keystrokes."
        % (
            statistics.median(counted),
            statistics.fmean(counted),
            min(counted),
            max(counted),
            len(counted),
        )
    )

    for turns in sorted(traced):
        for ran in traced[turns]:
            print("\n--- a keystroke of %d turns, callback by callback ---" % (turns,))
            first = ran[0][0] if ran else 0
            for turn, what in ran:
                print("  turn %-3d %s" % (turn - first, what))

    best = min(counted)
    if best != THE_BEST:
        print(
            "\nThe shortest keystroke took %d turns, and %d is what the code"
            "\ndecides. `tests/count_the_turns.py` says how to read the traces"
            "\nabove and where to write a new number." % (best, THE_BEST)
        )
        return 1

    print(
        "\nThe shortest keystroke took %d turns. Nothing judges the rest of"
        "\nthe distribution: a loaded machine misses a poll and pays for a"
        "\nsecond redraw, which is a property of the machine."
        "\nLillecarl/pymux#232." % (THE_BEST,)
    )
    return 0


def main() -> int:
    _naming_the_callbacks()

    async def run():
        return await measure(asyncio.get_running_loop(), SAMPLES)

    print("%d keystrokes, over the connection route." % (SAMPLES,))
    counted, traced = asyncio.run(run(), loop_factory=CountingLoop)
    return report(counted, traced)


if __name__ == "__main__":
    sys.exit(main())
