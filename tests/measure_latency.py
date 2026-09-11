"""
What pymux costs a keystroke, in milliseconds.

Lillecarl/pymux#8 asked for three numbers. Throughput and CPU were
answered by the instruction budgets, which count bytecode and so mean
the same thing on every machine. **Latency is the third, and nothing
had touched it.** Lillecarl/pymux#140.

It is the number a person feels. Every other check here says whether
pymux draws the right cells; none of them says whether it draws them
soon enough, and for a multiplexer that is the quality the whole
argument is about.

## The path, and where it is cut

    a key reaches the client's terminal
      -> the client writes a packet
      -> the server feeds it to the pane's pty        <- "in" below
      -> the program writes an answer                 <- "out" below
      -> the server parses it into cells
      -> prompt_toolkit renders and diffs the layout
      -> the connection writes the frame to the client
      -> the client writes it to the real terminal    <- "seen" below

One keystroke gives three numbers, and they add up:

- **input**: from the key going into the client's pty to the program
  reading it. The client, the socket and the server's pipe input.
- **output**: from the program writing to the frame reaching the
  client's terminal. The parser, the screen, the renderer and the
  diff. **This is the headline**, because it is the half that a
  program's own output goes through -- scrolling a log or a build
  never touches the input half at all.
- **round trip**: the two together, which is what a person sees when
  they hold a key down.

**The program timestamps its own two moments**, into a file the driver
reads afterwards. `time.time()` is one clock for the whole machine, so
the child's numbers and the driver's are directly comparable, and the
cut between the halves is exact rather than inferred.

## What it is compared against

The same program on a **bare pty**, with nothing between it and the
master. Without that the number says nothing: a millisecond is a
millisecond on the machine that measured it and nowhere else.

**The gap is not "what the fork costs".** A bare pty has no renderer,
no diff and no layout, so the difference is the fork *and* everything
pymux draws. That is the honest reading, and it is still the useful
one: it is what a person gives up by running a multiplexer at all.

## Why nothing judges this

A wall clock belongs to the machine that read it, and this runs in a
build sandbox beside other jobs. `checks.ptterm-instructions` counts
bytecode for exactly that reason. So this is instrumentation, like
`checks.pymux-profile`: it is not in `checks.all`, it holds no budget,
and reading it is the work.

Report the distribution and not a mean. **The tail is what a person
notices**: a p99 of eighty milliseconds is felt every few seconds, and
a mean of four hides it.

    nix build --file . checks.pymux-latency.run
    less result/log

    PYMUX_LATENCY_SAMPLES=500 nix build --file . checks.pymux-latency.run
    PYMUX_ROUTE=integrated nix build --file . checks.pymux-latency.run
"""

import os
import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from drive_with_pty import (  # noqa: E402
    ROUTE,
    Attached,
    Failed,
    Terminal,
    on_a_pty,
)

#: How many keystrokes each path is measured over.
SAMPLES = int(os.environ.get("PYMUX_LATENCY_SAMPLES") or 200)

#: How long to wait between one keystroke and the next.
#:
#: Long enough that a round trip finishes first, so each sample is one
#: keystroke and not a queue of them. It is also what keeps this from
#: measuring throughput by accident: a person types at a few keys a
#: second, and a renderer that coalesces two keystrokes into one frame
#: is answering a different question.
PACE = float(os.environ.get("PYMUX_LATENCY_PACE") or 0.03)

#: How long one round trip may take before the run gives up.
PATIENCE = 10.0

#: The characters a sample is marked with, cycled.
#:
#: **None of them can appear in the chrome.** The status line draws a
#: session name, a window list and a clock, and a title bar draws the
#: command; a marker that collided with one of those would be matched
#: by a redraw that has nothing to do with the keystroke. "*" is out
#: because the window list marks the active window with it.
MARKERS = "~!@#$^&"

#: The program in the pane. It reads one byte at a time and writes one
#: back, and writes down when it did each.
#:
#: `tty.setraw` is what makes the two paths the same: the pty of a
#: pane and a bare pty both start with echo and line buffering on, and
#: either would put the kernel's own answer in front of the program's.
#: With it off, one byte in is exactly one byte out.
#:
#: The "\r" keeps every answer in the first column. Without it the row
#: fills up and the line wraps after eighty samples, and a wrap
#: redraws a whole row where a single cell had been enough.
#:
#: **The second stamp is taken before the write and not after.** The
#: output path starts when the program asks for the byte to go out,
#: and a stamp taken afterwards starts it later than that: on a bare
#: pty the driver can read the byte before the program has written its
#: own note about it, and the measurement came out at minus nothing.
CHILD = r"""
import os, sys, time, tty

log = open(sys.argv[1], "w", buffering=1)
tty.setraw(0)

while True:
    data = os.read(0, 1)
    if not data:
        break
    got = time.time()
    gave = time.time()
    os.write(1, b"\r" + data)
    log.write("%s %.6f %.6f\n" % (data.decode("latin-1"), got, gave))
"""


class Bare(Attached):
    """
    The same program on a pty of its own, with nothing in between.

    It is an `Attached` so that the driver below cannot tell the two
    apart: the same writes, the same waits, the same arrival times.
    What differs is only what sits between the master and the program,
    which is the whole measurement.
    """

    def __init__(self, tmp, child, log):
        self.stderr_path = tmp / "bare-stderr.log"
        self.master_fd, self.process, self.stderr = on_a_pty(
            [sys.executable, str(child), str(log)],
            self.stderr_path,
            env={"LANG": "C.UTF-8"},
        )
        self.seen = b""
        self.arrivals = []
        self.passed = 0
        self.passed_keys = 0

    def close(self):
        self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except Exception:
            self.process.kill()
        self.stderr.close()
        os.close(self.master_fd)


def when_it_landed(attached, offset):
    """
    The clock when the read that carried this byte arrived.

    `Attached.read_once` stamps every read, so a byte's moment is the
    moment of the read it came in. That is the last thing the driver
    can see, and it is where the path ends.
    """
    landed = None
    for at, start in attached.arrivals:
        if start > offset:
            break
        landed = at
    if landed is None:
        raise Failed("nothing arrived before offset %d" % (offset,))
    return landed


def what_the_child_wrote(log):
    "Each marker the program answered, with when it read and wrote."
    written = {}
    for line in Path(log).read_text().splitlines():
        marker, got, answered = line.split()
        written.setdefault(marker, []).append((float(got), float(answered)))
    return written


def measure(attached, log, samples):
    """
    That many keystrokes, and the three times each one took.

    One keystroke at a time, and the answer waited for before the next
    goes out. A queue of keystrokes measures how fast the path drains,
    which is throughput, and the parts of it that overlap are exactly
    the parts a person never sees.
    """
    sent = []

    for number in range(samples):
        marker = MARKERS[number % len(MARKERS)]
        wrote = time.time()
        attached.write(marker.encode())

        attached.wait_for(marker.encode(), timeout=PATIENCE)
        landed = when_it_landed(attached, attached.passed - 1)

        sent.append((marker, wrote, landed))
        time.sleep(PACE)

    # The child's own file is read at the end, so that writing it is
    # not in the path. It holds one line per answer, in order, so the
    # nth use of a marker pairs with the nth answer carrying it.
    answered = what_the_child_wrote(log)
    seen: dict = {}

    found = {"input": [], "output": [], "round trip": []}
    for marker, wrote, landed in sent:
        place = seen.get(marker, 0)
        seen[marker] = place + 1
        times = answered.get(marker, [])
        if place >= len(times):
            raise Failed("the program never answered %r a %dth time" % (marker, place))
        got, gave = times[place]

        found["input"].append((got - wrote) * 1000.0)
        found["output"].append((landed - gave) * 1000.0)
        found["round trip"].append((landed - wrote) * 1000.0)

    return found


def create_distribution(name, milliseconds):
    ordered = sorted(milliseconds)

    def at(fraction):
        return ordered[min(len(ordered) - 1, int(len(ordered) * fraction))]

    return "%-22s %8.2f %8.2f %8.2f %8.2f %8.2f" % (
        name,
        ordered[0],
        at(0.50),
        at(0.90),
        at(0.99),
        ordered[-1],
    )


def report(measured):
    print("\n%-22s %8s %8s %8s %8s %8s" % ("", "min", "p50", "p90", "p99", "max"))
    for path in ("input", "output", "round trip"):
        for where in ("bare", "pymux"):
            if where in measured:
                print(create_distribution("%s %s" % (where, path), measured[where][path]))
        print()


def gap(measured):
    "What pymux adds to the middle of each path, at the median."
    if "bare" not in measured or "pymux" not in measured:
        return

    print("--- what pymux adds, at the median ---")
    for path in ("input", "output", "round trip"):
        bare = statistics.median(measured["bare"][path])
        ours = statistics.median(measured["pymux"][path])
        print("%-22s %8.2f ms" % (path, ours - bare))

    print(
        "\n**Not the cost of the fork alone.** A bare pty has no renderer,"
        "\nno diff and no layout, so this is the fork and everything pymux"
        "\ndraws. It is what a person gives up by running a multiplexer."
    )


def main() -> int:
    with tempfile.TemporaryDirectory() as name:
        tmp = Path(name)
        child = tmp / "echo_child.py"
        child.write_text(CHILD)

        print(
            "%d keystrokes a path, %.0f ms apart, over the %s route."
            % (SAMPLES, PACE * 1000.0, ROUTE)
        )

        measured = {}

        bare_log = tmp / "bare.log"
        bare = Bare(tmp, child, bare_log)
        try:
            bare.drain(0.5)
            measured["bare"] = measure(bare, bare_log, SAMPLES)
        finally:
            bare.close()

        ours_log = tmp / "pymux.log"
        terminal = Terminal(
            tmp,
            "latency",
            command="%s %s %s" % (sys.executable, child, ours_log),
        )
        try:
            terminal.wait_for_the_queries()
            # The first frame draws the whole screen, and the program
            # has to have set its pty up before a keystroke means
            # anything. Neither belongs in the number.
            terminal.drain(1.5)
            measured["pymux"] = measure(terminal, ours_log, SAMPLES)
        finally:
            terminal.close()

    report(measured)
    gap(measured)

    print(
        "\nNothing judges these. A wall clock belongs to the machine that"
        "\nread it, and this ran in a build sandbox beside other jobs."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
