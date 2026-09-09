"""
What a keystroke costs pymux, in bytecode instructions.

`checks.pymux-latency` says what a keystroke costs in milliseconds, and
nothing judges it, because a millisecond belongs to the machine that
read it. **This is the same path counted instead of timed**, so a
number that goes up fails a build. Carl:

> I know different instructions are differently expensive but
> realistically "number go up = bad".

## Three stages, and none of them was counted before

`checks.ptterm-instructions` counts a parse and a widget's own render.
`checks.pymux-frame` counts laying a window out and drawing it into a
`Screen`. Between them they miss both ends of a keystroke:

- **key**: a key press through the client's key processor, into the
  binding, and out to the pane's pty. `Process.write_input` reaches
  `os.write` with nothing scheduled in between, so this is the whole
  path and not the start of one.
- **parse**: the answer arriving, through `Stream.feed`, into the
  screen. This one overlaps `ptterm-instructions`; it is here because
  a keystroke's parse is one cell and a recording's is a megabyte,
  and the two do not move together.
- **render**: `Renderer.render`, which lays the window out, draws it,
  **diffs it against the last frame** and writes the escape sequences.
  Nothing counted the diff or the writing before this, and they are
  the part a keystroke pays that a recording does not.

## Why it is deterministic, which is what makes it a gate

**Nothing runs the event loop.** A loop exists because
`KeyProcessor.process_keys` arms a flush timer, but it never turns,
so the pane's program can never write to the screen and the frame
cannot change under the measurement. Every byte on the screen got
there from a call in this file.

The clock is the other thing that moves by itself, so `status-right`
is emptied: the default draws a time, and a frame drawn either side of
a second is two different diffs.

## What it says about the milliseconds

The same work is timed as well, with the monitoring off, so the file
reports both. Read them together:

    instructions          what the work is, and a gate holds it
    in-process time       what that work takes here, with no
                          transport and no loop between the stages
    checks.pymux-latency  what the whole keystroke takes, across
                          three processes and an event loop

**The gap between the last two is what an instruction count cannot
see**: the socket, the loop's turns, and every moment nothing is
running. If they are close, counting instructions is a fair proxy for
latency and this gate protects it. If they are far apart, the
difference is waiting, and Lillecarl/pymux#232 is the measurement for
that.

    nix build --file . checks.pymux-keystroke.run
    less result/log
    cp result/keystroke-budgets.txt pymux/tests/keystroke-budgets.txt
"""

import asyncio
import io
import os
import re
import sys
import time
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(1, str(Path(__file__).parent.parent))

from prompt_toolkit.application.current import set_app  # noqa: E402
from prompt_toolkit.data_structures import Size  # noqa: E402
from prompt_toolkit.input import create_pipe_input  # noqa: E402
from prompt_toolkit.key_binding.key_processor import KeyPress  # noqa: E402
from prompt_toolkit.output import ColorDepth  # noqa: E402
from prompt_toolkit.output.vt100 import Vt100_Output  # noqa: E402

from instructions import count_instructions  # noqa: E402

from pymux.main import Pymux  # noqa: E402

HERE = Path(__file__).parent

#: The counts this check expects, one stage per line.
BUDGETS = HERE / "keystroke-budgets.txt"

#: How far a count may move before the check fails, as a percentage.
DEFAULT_TOLERANCE = 5.0

#: How many keystrokes the timing runs over. The instruction count
#: needs one, because it is exact; a clock needs a few hundred.
TIMED = int(os.environ.get("PYMUX_KEYSTROKE_TIMED") or 300)

#: The client's terminal.
SIZE = Size(rows=24, columns=80)

#: What the program in the pane answers with, and what a person typed.
#: One cell either way, which is the steady state a keystroke is: a
#: frame where one thing changed.
THE_ANSWER = "~"
THE_KEY = "a"


class _Connection:
    "What `Pymux` asks a connection for, and nothing else."

    kitty_source_flags = 0
    pointer_shape = None
    graphics = None

    def set_pointer_shape(self, shape):
        pass

    def _send_packet(self, packet):
        pass


@contextmanager
def a_client():
    """
    One session, one pane, one client, and no loop turning.

    The loop is made and never run. `KeyProcessor.process_keys` arms a
    flush timer, which needs a loop to arm against; nothing has to
    fire it, and nothing may, because a turn of the loop would let the
    pane's program write to the screen and the next frame would differ
    for a reason this file did not choose.

    **The application is told which loop that is.**
    `Application.create_background_task` reads `self.loop` and asks
    asyncio for a running one only when it is `None`, and `run_async`
    is what usually sets it. Nothing runs the application here, so
    this does what `run_async` would: the timer is armed against a
    loop that exists and never turns.
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    pymux = Pymux()
    try:
        with create_pipe_input() as pipe:
            output = Vt100_Output(stdout=io.StringIO(), get_size=lambda: SIZE)
            state = pymux.add_client(
                output=output,
                input=pipe,
                color_depth=ColorDepth.DEPTH_8_BIT,
                connection=_Connection(),
            )

            state.app.loop = loop

            with set_app(state.app):
                # The clock moves by itself, and a frame drawn either
                # side of a second is two different diffs.
                pymux.handle_command("set-option status-right ''")

            yield pymux, state
    finally:
        pymux.stop()

        # The flush timers were armed and never fired, because nothing
        # turned the loop. Closing it under them prints "Task was
        # destroyed but it is pending" once for each keystroke, so
        # they are cancelled and the loop is turned until they have
        # taken it. **After the measurement**, which is why a turn
        # here costs it nothing.
        pending = asyncio.all_tasks(loop)
        for task in pending:
            task.cancel()
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))

        asyncio.set_event_loop(None)
        loop.close()


def the_stages(pymux, state):
    """
    The three pieces of a keystroke, each as a callable.

    They are separate because they answer different questions. A parse
    that grew and a diff that grew are not the same fault and do not
    have the same fix.
    """
    pane = pymux.arrangement.get_active_window().active_pane
    stream = pane.terminal.terminal_control.stream
    processor = state.app.key_processor

    def key():
        processor.feed(KeyPress(THE_KEY, THE_KEY))
        processor.process_keys()

    def parse():
        stream.feed(THE_ANSWER)

    def render():
        state.app.renderer.render(state.app, state.app.layout)

    return {"key": key, "parse": parse, "render": render}


def settle(state, stages):
    """
    Draw once before anything is counted.

    The renderer holds the last frame and diffs against it, and it
    holds nothing at all to begin with, so the first render paints the
    whole screen. That is a real number and it is not this one: a
    keystroke lands on a screen that is already drawn.
    """
    with set_app(state.app):
        stages["parse"]()
        stages["render"]()


def counted(state, stages):
    "The instructions each stage takes. Exact, and the same every run."
    found = {}
    with set_app(state.app):
        for name, work in stages.items():
            found[name] = count_instructions(work)
    return found


def timed(state, stages, rounds):
    """
    The same stages on a clock, with the monitoring off.

    **Not under `count_instructions`.** `sys.monitoring` calls back
    into Python on every instruction and turns the interpreter's
    specialisation off, so work measured while it counts takes many
    times longer than the same work alone.
    """
    found = {}
    with set_app(state.app):
        for name, work in stages.items():
            # A warm round, so that nothing is being imported or
            # branched for the first time inside the clock.
            work()
            started = time.perf_counter()
            for _ in range(rounds):
                work()
            found[name] = (time.perf_counter() - started) / rounds * 1e6
    return found


def read_budgets():
    if not BUDGETS.is_file():
        return {}
    budgets = {}
    for line in BUDGETS.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, _space, count = line.rpartition(" ")
        budgets[name.strip()] = int(count)
    return budgets


def write_budgets(counts):
    out = os.environ.get("PYMUX_KEYSTROKE_OUT", "")
    if not out:
        return
    lines = [
        "# What one keystroke costs pymux, in bytecode instructions.\n",
        "# `tests/measure_a_keystroke.py` says what each stage covers\n",
        "# and why the number is the same on every machine.\n",
        "#\n",
        "# This is what the run saw. To make it what the check expects:\n",
        "#     nix build --file . checks.pymux-keystroke.run\n",
        "#     cp result/keystroke-budgets.txt pymux/tests/keystroke-budgets.txt\n",
    ]
    lines += ["%s %d\n" % (name, counts[name]) for name in sorted(counts)]
    (Path(out) / "keystroke-budgets.txt").write_text("".join(lines))


def main() -> int:
    include = os.environ.get("PYMUX_KEYSTROKE_INCLUDE", "")
    tolerance = float(os.environ.get("PYMUX_KEYSTROKE_TOLERANCE") or DEFAULT_TOLERANCE)

    with a_client() as (pymux, state):
        stages = the_stages(pymux, state)
        if include:
            stages = {n: w for n, w in stages.items() if re.search(include, n)}
        if not stages:
            print("Nothing matched %r." % (include,))
            return 1

        settle(state, stages)
        counts = counted(state, stages)
        microseconds = timed(state, stages, TIMED)

    print("\n--- what one keystroke costs ---")
    print("%-12s %14s %12s" % ("", "instructions", "in-process"))
    for name in ("key", "parse", "render"):
        if name in counts:
            print("%-12s %14d %10.1f us" % (name, counts[name], microseconds[name]))

    print(
        "%-12s %14d %10.1f us"
        % ("all of it", sum(counts.values()), sum(microseconds.values()))
    )

    print(
        "\n**The in-process time is not the latency.** It holds no socket,"
        "\nno client process and no turn of the event loop. What"
        "\n`checks.pymux-latency` measures across three processes minus this"
        "\nis what an instruction count cannot see. Lillecarl/pymux#232."
    )

    write_budgets(counts)

    budgets = read_budgets()
    over = []
    for name, count in sorted(counts.items()):
        budget = budgets.get(name)
        if budget is None:
            over.append("%s has no budget, and this run took %d" % (name, count))
            continue
        moved = abs(count - budget) * 100.0 / max(1, budget)
        if moved > tolerance:
            over.append(
                "%s takes %d, and its budget is %d: %.1f%% away"
                % (name, count, budget, moved)
            )

    if over:
        print("\n--- past the budget ---")
        for line in over:
            print(line)
        print("\n`tests/measure_a_keystroke.py` says how to record a new budget.")
        return 1

    print("\nEvery stage is within %.1f%% of its budget." % (tolerance,))
    return 0


if __name__ == "__main__":
    sys.exit(main())
