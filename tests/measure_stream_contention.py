"""
What a keystroke costs while another pane streams.

Lillecarl/pymux#507: scrolling in one pane lags while an agent streams
in another, and the whole session feels it. `tests/count_turns.py`
holds the turns of one keystroke on a quiet server; this runs the same
keystroke while a second pane writes a line every few milliseconds,
which is what an agent's output looks like to the loop. The two phases
run back to back in one server, so the machine is the same for both
and only the streaming differs.

## The shape

One window, two panes. The first runs the echo child the turns check
drives (one byte in, one byte back); the second is split in later and
streams lines, which is what an agent's output looks like to the loop.
The client looks at the window and types markers at the echo pane,
first while it is the only pane and then while the other streams. Each
sample is the turns from the key to the frame that carries its answer,
cut the way the turns check cuts them.

Both panes share every frame the client draws, so the stream contends
for the same redraw the keystroke waits for. A stream in a window
nobody looks at would contend for nothing: no client draws it.

## What it judges

The shortest keystroke of the streaming phase, against the same
budget the turns check holds: load can add a redraw but never take a
turn away, so a minimum past it is contention the code allows and not
the machine having a moment. The rest of each distribution is
instrumentation -- median and tail say how often a person feels it,
which is what the issue is about.

    PYMUX_CONTENTION_SAMPLES=100 nix build --file . checks.pymux-contention.run
"""

from __future__ import annotations

import asyncio
import os
import shlex
import statistics
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
# And pymux itself, which the sandbox copies beside `tests` rather than
# installing. A script's own directory is what Python puts on the path,
# not the directory above it.
sys.path.insert(1, str(Path(__file__).parent.parent))

from count_turns import (
    BEST,
    NO_REFRESH,
    SIZE,
    CountingLoop,
    TheFrameComingBack,
    _naming_callbacks,
    create_keystroke,
)
from measure_latency import CHILD, MARKERS
from prompt_toolkit.application.current import set_app
from session import over_connection

from pymux.main import Pymux

#: Keystrokes per phase. Two phases run in one server, so this is the
#: length of each and not of the run.
SAMPLES = int(os.environ.get("PYMUX_CONTENTION_SAMPLES") or 100)

#: How long to wait after a frame before the next keystroke goes out.
#: Long enough that a round trip finishes first, so each sample is one
#: keystroke and not a queue of them. `tests/count_turns.py` keeps the
#: same distance for the same reason.
PACE = float(os.environ.get("PYMUX_CONTENTION_PACE") or 0.03)

#: How long the streaming pane sleeps between lines, in seconds. An
#: agent writes in bursts with thinking in between; a steady ten
#: milliseconds is the burst without the thinking, which is the half
#: that contends.
SPIN_SECONDS = float(os.environ.get("PYMUX_CONTENTION_SPIN_MS") or 10) / 1000.0

#: How long the stream runs before the first keystroke of its phase.
#: The pane has to have filled the pipe, asked its first frames and
#: reached the steady state the samples judge.
SETTLE = float(os.environ.get("PYMUX_CONTENTION_SETTLE") or 1.0)

#: The program that streams. One line of nothing a marker could hide
#: behind: the markers the keystrokes are matched by never appear in
#: it, so a frame matched is a frame that carried the answer.
SPIN = r"""
import sys, time

log = open(sys.argv[1], "w", buffering=1)
count = 0
while True:
    sys.stdout.write("x" * 70 + "\n")
    sys.stdout.flush()
    count += 1
    log.write("%%d\n" %% count)
    time.sleep(%r)
""" % (SPIN_SECONDS,)


async def sample(loop, session, state, coming_back, marker) -> tuple:
    "One keystroke, and the trace of the slowest of the phase."
    turns, ran = await create_keystroke(loop, session, state, coming_back, marker)
    await asyncio.sleep(PACE)
    return turns, ran


async def phase(loop, session, state, coming_back, samples: int, name: str) -> tuple:
    "That many keystrokes, and the trace of the slowest of them."
    counted = []
    worst = None
    for number in range(samples):
        marker = MARKERS[number % len(MARKERS)]
        try:
            turns, ran = await sample(loop, session, state, coming_back, marker)
        except TimeoutError:
            print("%s: keystroke %d never came back" % (name, number))
            raise SystemExit("a keystroke of the %s phase never came back" % name)
        counted.append(turns)
        if worst is None or turns > worst[0]:
            worst = (turns, ran)
    return counted, worst


async def measure(loop, samples: int) -> tuple:
    """
    One server, two phases: the keystroke alone, then under a stream.

    The streaming pane does not exist during the first phase, so
    nothing of it can reach the samples. It is split in before the
    second, unfocused, so the keys keep reaching the echo pane: a
    keystroke typed at the stream would never be answered, which is a
    routing fault and not a slow one.
    """
    with tempfile.TemporaryDirectory() as name:
        tmp = Path(name)
        child = tmp / "echo_child.py"
        child.write_text(CHILD)
        spin = tmp / "spin_child.py"
        spin.write_text(SPIN)
        spin_log = tmp / "spin.log"

        pymux = Pymux(
            startup_command="%s %s %s" % (sys.executable, child, tmp / "echo.log"),
        )
        # The auto refresh arms itself before a client attaches, so this
        # is set before there is one. A tick inside a keystroke is a
        # turn that the keystroke did not ask for.
        pymux.status_interval = NO_REFRESH

        coming_back = TheFrameComingBack(loop)

        async with over_connection(pymux=pymux, read_packet=coming_back) as session:
            state, _size = await session.attach("one", SIZE)

            with set_app(state.app):
                # The clock moves by itself, and a frame that redraws it
                # is a frame this did not ask for. `test-mode` pins it
                # rather than emptying `status-right`, so the status
                # line counted here is the one a person has.
                pymux.handle_command("set-option test-mode on")

            # A warm keystroke first. The first frame paints the whole
            # screen, and the program in the pane has to have set its
            # own pty up before a byte means anything.
            for warm in range(2):
                await sample(loop, session, state, coming_back, MARKERS[warm])

            frames_before = pymux.counters.frames
            idle, idle_worst = await phase(loop, session, state, coming_back, samples, "idle")
            idle_frames = pymux.counters.frames - frames_before

            with set_app(state.app):
                pane_command = shlex.quote("%s %s %s" % (sys.executable, spin, spin_log))
                pymux.handle_command("split-window -d %s" % pane_command)
            time.sleep(SETTLE)

            # The stream has to be flowing: a phase measured against a
            # pane that never wrote is a second idle phase wearing the
            # other's name.
            streamed = spin_log.read_text() if spin_log.exists() else ""
            if streamed.count("\n") < samples:
                raise SystemExit(
                    "the streaming pane wrote %d lines, so it never reached a steady state" % streamed.count("\n")
                )

            # And the keys still have to reach the echo pane: a marker
            # answered proves it, and a missing one here is a routing
            # fault the samples must not pay for.
            try:
                await sample(loop, session, state, coming_back, MARKERS[2])
            except TimeoutError:
                raise SystemExit("the keys stopped reaching the echo pane after the split")

            frames_before = pymux.counters.frames
            started = time.monotonic()
            streaming, streaming_worst = await phase(loop, session, state, coming_back, samples, "streaming")
            streaming_seconds = time.monotonic() - started
            streaming_frames = pymux.counters.frames - frames_before

            await session.detach(state)

    return (idle, idle_worst, idle_frames), (streaming, streaming_worst, streaming_frames, streaming_seconds)


def report(name: str, counted: list, worst) -> None:
    "One phase as a distribution, and what its slowest keystroke did."
    seen = Counter(counted)
    print("\n--- %s: turns of the event loop, per keystroke ---" % name)
    for turns, times in sorted(seen.items()):
        print("%3d turns %8d keystrokes  %5.1f%%" % (turns, times, times * 100.0 / len(counted)))
    print(
        "median %d, mean %.2f, %d to %d over %d keystrokes."
        % (
            statistics.median(counted),
            statistics.fmean(counted),
            min(counted),
            max(counted),
            len(counted),
        )
    )
    turns, ran = worst
    print("\n--- the slowest %s keystroke: %d turns, callback by callback ---" % (name, turns))
    first = ran[0][0] if ran else 0
    for turn, what in ran:
        print("  turn %-3d %s" % (turn - first, what))


def main() -> int:
    _naming_callbacks()

    async def run():
        return await measure(asyncio.get_running_loop(), SAMPLES)

    print("%d keystrokes per phase, over the connection route." % (SAMPLES,))
    (idle, idle_worst, idle_frames), (streaming, streaming_worst, streaming_frames, streaming_seconds) = asyncio.run(
        run(), loop_factory=CountingLoop
    )

    print("\n%d frames went out during the idle phase." % (idle_frames,))
    print("%d frames went out during %.1f seconds of streaming." % (streaming_frames, streaming_seconds))

    report("idle", idle, idle_worst)
    report("streaming", streaming, streaming_worst)

    best = min(streaming)
    if best != BEST:
        print(
            "\nThe shortest streaming keystroke took %d turns, and %d is what"
            "\nthe code decides. That is contention a quiet server does not"
            "\nhave. Lillecarl/pymux#507." % (best, BEST)
        )
        return 1

    print(
        "\nThe shortest streaming keystroke took %d turns, like a quiet"
        "\none. Whatever the stream costs, it is not a turn the minimum"
        "\nhas to pay." % (BEST,)
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
