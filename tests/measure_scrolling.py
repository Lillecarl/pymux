"""
How long a wheel step takes to show, down and up, beside a pane that streams.

Two reports wait on this number. Scrolling in opencode lagged the
whole session while an agent streamed (Lillecarl/pymux#507), and
scrolling down was slower than scrolling up (Lillecarl/pymux#512).
Neither had a reproducer that a person can run without opencode and an
agent.

## What runs

Two panes side by side. The left one is the agent: it streams lines
and a spinner, but only in the streaming half of the run. The right one
has the focus and is the viewer: it turns the mouse on, and each wheel
event moves its document one line in the direction asked, then writes a
marker on its last row. The driver writes one wheel event to the
client's terminal and waits for the marker of that step to arrive.

The viewer draws the way `tests/scroll_app.py` draws, in two modes:

- **region**: a title above and a status and a prompt below stand
  still, and only the middle scrolls. That is an agent harness.
- **redraw**: every step rewrites the viewport. That is a program
  that renders a whole frame and lets a diff decide.

Each mode runs four ways: down then up, with the agent quiet and with
it streaming. The asymmetry of #512 is the difference between the
first two columns; the lag of #507 is the difference between the two
halves.

## Why the marker alternates

A frame is a diff against the last one. A marker that changes one
character between steps goes out as one character, and a wait for the
whole marker would wait forever. So two markers alternate, with no
character in common, and each step's wait starts past the last one.

## Why nothing judges this

A wall clock belongs to the machine that read it, as
`tests/measure_latency.py` says. This is instrumentation, outside
`checks.all`.

    nix build --file . checks.pymux-scrolling.run
    PYMUX_SCROLLING_STEPS=200 PYMUX_SCROLLING_RATE=100 nix build --file . checks.pymux-scrolling.run
"""

from __future__ import annotations

import os
import statistics
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

from drive_with_pty import ROUTE, Failed, Terminal, _reading, run_cli
from measure_latency import when_it_landed

STEPS = int(os.environ.get("PYMUX_SCROLLING_STEPS") or 60)
ROWS = int(os.environ.get("PYMUX_SCROLLING_ROWS") or 90)
COLUMNS = int(os.environ.get("PYMUX_SCROLLING_COLUMNS") or 300)
#: Lines a second the agent writes while it streams.
RATE = float(os.environ.get("PYMUX_SCROLLING_RATE") or 50)
#: Between one step's marker and the next wheel event: a hand on a
#: wheel, not a queue of events.
PACE = float(os.environ.get("PYMUX_SCROLLING_PACE") or 0.03)
PATIENCE = 10.0
#: Where the bytes of every step go, one file per mode, agent and direction.
OUT = os.environ.get("PYMUX_SCROLLING_OUT")

MARKERS = (b"@@@@@@@@@@@@", b"&&&&&&&&&&&&")

#: SGR wheel buttons, as xterm numbers them: 64 is button 4, up.
WHEEL = {"down": 65, "up": 64}

VIEWER = r"""
import os, re, sys, tty
sys.path.insert(0, sys.argv[1])
from scroll_app import (
    ALTERNATE_SCREEN, LINES, HEADER_ROWS, FOOTER_ROWS,
    region_scroll_step, region_viewport_bytes, viewport_bytes, window_size,
)

mode = sys.argv[2]
heard = open(sys.argv[3], "ab", buffering=0)
markers = (b"@@@@@@@@@@@@", b"&&&&&&&&&&&&")
rows, columns = window_size()
span = rows - HEADER_ROWS - FOOTER_ROWS if mode == "region" else rows - 1
top = LINES // 2
steps = 0

def draw():
    if mode == "region":
        return region_viewport_bytes(top, rows, columns, styled=True)
    return viewport_bytes(top, rows, columns, styled=True)

tty.setraw(0)
os.write(1, ALTERNATE_SCREEN.encode() + b"\x1b[?1000h\x1b[?1006h" + draw() + b"\x1b[%d;40HREADY" % rows)
wheel = re.compile(rb"\x1b\[<(\d+);\d+;\d+[mM]")
while True:
    data = os.read(0, 4096)
    if not data:
        break
    heard.write(data)
    for match in wheel.finditer(data):
        direction = {b"65": 1, b"64": -1}.get(match.group(1))
        if direction is None:
            continue
        coming = min(max(1, top + direction), LINES - span + 1)
        if mode == "region":
            out = region_scroll_step(top, direction, rows, columns, styled=True)
        else:
            out = viewport_bytes(coming, rows, columns, styled=True)
        top = coming
        out += b"\x1b[%d;40H" % rows + markers[steps % 2]
        steps += 1
        os.write(1, out)
"""

AGENT = r"""
import os, sys, time
flag, rate = sys.argv[1], float(sys.argv[2])
words = "the agent reads a file and writes a plan while the spinner turns over".split()
spinner = "|/-\\"
count = 0
while True:
    if not os.path.exists(flag):
        time.sleep(0.05)
        continue
    line = " ".join(words[(count + shift) % len(words)] for shift in range(14))
    sys.stdout.write("\r\x1b[K" + line + "\n" + spinner[count % 4] + " thinking")
    sys.stdout.flush()
    count += 1
    time.sleep(1.0 / rate)
"""


def wheel(direction: str, column: int, row: int) -> bytes:
    return b"\x1b[<%d;%d;%dM" % (WHEEL[direction], column, row)


def scroll(terminal, direction: str, column: int, row: int, steps: int, first: int) -> tuple[list, list, list]:
    "Milliseconds from each wheel event to its marker, and the size and bytes of each step."
    delays, sizes, frames = [], [], []
    for number in range(first, first + steps):
        marker = MARKERS[number % 2]
        start = terminal.passed
        wrote = time.time()
        terminal.write(wheel(direction, column, row))
        terminal.wait_for(marker, timeout=PATIENCE)
        landed = when_it_landed(terminal, terminal.passed - 1)
        delays.append((landed - wrote) * 1000.0)
        sizes.append(terminal.passed - start)
        frames.append(terminal.seen[start : terminal.passed])
        terminal.drain(PACE)
    return delays, sizes, frames


def distribution(name: str, values: list) -> str:
    ordered = sorted(values)

    def at(fraction: float) -> float:
        return ordered[min(len(ordered) - 1, int(len(ordered) * fraction))]

    return "%-28s %8.2f %8.2f %8.2f %8.2f %8.2f" % (name, ordered[0], at(0.5), at(0.9), at(0.99), ordered[-1])


def measure(tmp: Path, mode: str) -> dict:
    viewer, agent, flag = tmp / "viewer.py", tmp / "agent.py", tmp / ("%s-streams" % mode)
    heard = tmp / ("%s-heard" % mode)
    viewer.write_text(VIEWER)
    agent.write_text(AGENT)
    terminal = Terminal(
        tmp,
        "scrolling-%s" % mode,
        command="%s %s %s %s" % (sys.executable, agent, flag, RATE),
        rows=ROWS,
        columns=COLUMNS,
    )
    found: dict = {}
    try:
        terminal.wait_for_queries()
        # The answer to the client's device attributes query: a client
        # holds its input until its queries are answered.
        terminal.write(b"\x1b[?62;1;6c")
        split = _reading(
            terminal,
            [
                "split-window",
                "-h",
                "-t",
                terminal.session_name,
                "%s %s %s %s %s" % (sys.executable, viewer, HERE, mode, heard),
            ],
        )
        if split.returncode != 0:
            raise Failed("split-window: %r" % (split.stderr,))
        terminal.wait_for(b"READY", timeout=PATIENCE)
        terminal.drain(1.0)
        column, row = COLUMNS * 3 // 4, ROWS // 2
        steps = 0
        for agent_state in ("quiet", "streaming"):
            if agent_state == "streaming":
                flag.touch()
                terminal.drain(1.0)
            for direction in ("down", "up"):
                delays, sizes, frames = scroll(terminal, direction, column, row, STEPS, steps)
                steps += STEPS
                found[(agent_state, direction)] = (delays, sizes)
                if OUT:
                    # A separator no frame holds, so a reader can split the steps apart.
                    Path(OUT, "%s-%s-%s.bin" % (mode, agent_state, direction)).write_bytes(b"\n=====\n".join(frames))
    except Exception:
        terminal.report()
        print("--- what the viewer read ---\n%r" % (heard.read_bytes()[-400:] if heard.exists() else None,))
        panes = run_cli(
            terminal.sock_path, ["list-panes", "-F", "#{pane_id} #{pane_active} #{pane_left} #{pane_width}"]
        )
        print("--- panes ---\n%s" % panes.stdout.decode())
        raise
    finally:
        terminal.close()
    return found


def report(mode: str, found: dict) -> None:
    print("\n--- %s: milliseconds from a wheel event to its step on screen ---" % mode)
    print("%-28s %8s %8s %8s %8s %8s" % ("", "min", "p50", "p90", "p99", "max"))
    for (agent_state, direction), (delays, _sizes) in found.items():
        print(distribution("%s, agent %s" % (direction, agent_state), delays))
    print(
        "bytes a step, agent quiet: down %d, up %d (median)"
        % tuple(statistics.median(found[("quiet", direction)][1]) for direction in ("down", "up"))
    )


def main() -> int:
    print(
        "%d wheel steps each way, %.0f ms apart, over the %s route, on a terminal of %dx%d; "
        "the agent streams %.0f lines a second." % (STEPS, PACE * 1000.0, ROUTE, COLUMNS, ROWS, RATE)
    )
    with tempfile.TemporaryDirectory() as name:
        for mode in ("region", "redraw"):
            report(mode, measure(Path(name), mode))
    print(
        "\nNothing judges these. A wall clock belongs to the machine that"
        "\nread it, and this ran in a build sandbox beside other jobs."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
