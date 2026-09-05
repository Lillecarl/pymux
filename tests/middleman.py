"""
A full screen pymux pane, and the wire that comes out of it.

This is the middle of the middle man. A borrowed suite writes bytes, a
program in the pane puts them on the screen, ptterm parses them, pymux
renders and emits, and what pymux emitted is what a judge reads.

    the suite ── bytes ──▶ this ──▶ a program in a full screen pane
                                         │
                                    ptterm parses
                                         │
                                 pymux renders and emits
                                         │
             a judge ◀── the wire ───────┘

Nothing here judges anything. `write()` returns the bytes pymux wrote
to its client, and what to make of them belongs to the suite that asked.
`vterm_middleman.py` gives them to libvterm one `PUSH` at a time;
`drive_with_alacritty.py` gives a whole recording to an Alacritty
`Term`. The pane is the same either way, which is the point of this
file.

## How a write is fenced

The bytes reach the pane through a fifo, which a forwarder in the pane
copies to its own output. Then this has to know when pymux has finished
drawing what those bytes did.

A fence goes down the fifo behind the payload: an OSC 52, which ptterm
hands to pymux and pymux writes to the terminal of every client. Seeing
it on the wire proves the pane consumed the payload.

It does not prove the frame arrived. prompt_toolkit may postpone a
redraw, so the frame can follow the fence. A short settle after the
fence covers that, and it is short because the fence has already done
the waiting.

The fence is taken back out before anything sees the wire. A judge
would only ignore it, but a stream that holds our own scaffolding is a
stream nobody can read.
"""
import base64
import os
import re
import select
import sys
import time
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).parent.parent))

from tests.drive_with_pty import Terminal, run_cli  # noqa: E402

#: How long the wire has to stay quiet after the fence before a frame
#: counts as finished, in seconds.
#:
#: The fence has already waited for the pane, so this only covers a
#: redraw that prompt_toolkit postponed, and it waits for silence rather
#: than for the clock. A suite holds hundreds of writes, and a fixed
#: wait on each of them is the difference between a check that runs in a
#: minute and one nobody runs.
QUIET = 0.05

#: How long to wait for the first byte of that frame. A write that
#: changes nothing on the screen makes no frame at all, so this has to
#: give up, and it is the one wait that is paid every time.
FIRST_BYTE = 0.15

#: How long to wait for the pane to reach the size the suite asks
#: about, in seconds. A pane opens at the size pymux gives it and is
#: resized when the client says how big it is.
SIZE_TIMEOUT = 10.0

#: The program in the pane. It copies the fifo to its own output and
#: changes nothing.
#:
#: `stty -opost` is the part that matters. A pty turns "\n" into "\r\n"
#: on the way out, and a suite writes both, so without it every line
#: feed arrives as something else.
FORWARDER = r"""
import fcntl, os, signal, struct, sys, termios
os.system("stty -opost -echo")

# The size of the pane, written where the driver can read it, and
# written again every time it changes. Every assertion names a row and a
# column, so a pane that is not the size the suite thinks it is turns
# every one of them into a different question.
#
# It changes at least once: a pane opens at the size pymux gives it and
# is resized when the client says how big it is, which is after this
# program has already started.
def report(*_):
    rows, columns = struct.unpack(
        "HHHH", fcntl.ioctl(1, termios.TIOCGWINSZ, b"\0" * 8)
    )[:2]
    with open(sys.argv[2], "w") as out:
        out.write("%d %d\n" % (rows, columns))

signal.signal(signal.SIGWINCH, report)
report()

fd = os.open(sys.argv[1], os.O_RDONLY)
while True:
    data = os.read(fd, 65536)
    if not data:
        break
    os.write(1, data)
"""

#: What pymux writes for the fence, and what to take back out of the
#: wire before a judge sees it.
FENCE = re.compile(rb"\x1b\]52;[^\x07\x1b]*(?:\x07|\x1b\\)")


class Pane:
    """
    One pymux, one full screen pane, and the wire out of it.

    A full screen pane is every cell of the terminal: no status line and
    no pane title. Without it the pane sits two rows short and every row
    of every assertion is out by one.
    """

    def __init__(
        self,
        tmp: Path,
        name: str,
        rows: int,
        columns: int,
        trace: Callable[[str], None] | None = None,
        colorterm: str = "",
    ) -> None:
        self.tmp = tmp
        self.name = name
        self.rows = rows
        self.columns = columns
        self.colorterm = colorterm
        self._trace = trace or (lambda message: None)
        self.terminal: Terminal | None = None
        self.fifo = tmp / ("%s-payload.fifo" % name)
        self.writer = -1
        self.fence = 0

    def start(self) -> None:
        "Bring up pymux with one pane that copies the fifo to the screen."
        os.mkfifo(self.fifo)

        forwarder = self.tmp / ("%s-forwarder.py" % self.name)
        forwarder.write_text(FORWARDER)

        config = self.tmp / ("%s.conf" % self.name)
        config.write_text("set full-screen on\n")

        size = self.tmp / ("%s-pane-size.txt" % self.name)

        self.terminal = Terminal(
            self.tmp,
            self.name,
            colorterm=self.colorterm,
            command="%s %s %s %s" % (sys.executable, forwarder, self.fifo, size),
            rows=self.rows,
            columns=self.columns,
            config=config,
        )

        # Opening for writing is what lets the forwarder past its own
        # open, so the pane is running from here on. It stays open for
        # the whole session: closing it is what ends the forwarder.
        self.writer = os.open(self.fifo, os.O_WRONLY)

        self.terminal.wait_for_the_queries()
        self.terminal.drain(0.5)

        # A pane of the wrong size makes every assertion a different
        # question, and the answers still look like answers. So this
        # waits for the size the suite asks about, and stops rather than
        # reporting a screen nobody asked about.
        found = ""
        deadline = time.monotonic() + SIZE_TIMEOUT
        while time.monotonic() < deadline:
            found = size.read_text().split()
            if [int(one) for one in found] == [self.rows, self.columns]:
                return
            self.settle()
        raise AssertionError(
            "the pane is %s and the suite asks about %d rows by %d columns"
            % (" by ".join(found) or "not saying", self.rows, self.columns)
        )

    @property
    def seen(self) -> bytes:
        "Everything pymux has written to its client, fence taken out."
        assert self.terminal is not None
        return FENCE.sub(b"", self.terminal.seen)

    def write(self, data: bytes, timeout: float = 15.0) -> bytes:
        """
        Put bytes on the screen of the pane, and return what pymux
        wrote to its client because of them.

        `timeout` is how long the fence may take. The default suits a
        line of a test file; a recording of a third of a megabyte needs
        far longer, because every byte of it is parsed and drawn before
        the fence can come back.
        """
        assert self.terminal is not None
        self.fence += 1
        token = base64.b64encode(b"fence-%d" % self.fence).decode()

        mark = self.terminal.mark()
        os.write(self.writer, data + b"\x1b]52;c;%s\x07" % token.encode())

        self.terminal.wait_for(token.encode(), timeout=timeout)
        self.settle()
        return FENCE.sub(b"", self.terminal.since(mark))

    def trace_the_pane(self) -> None:
        """
        Write the screen of the pane itself into the trace.

        The caller decides when to ask, because this runs `capture-pane`
        in a second process and that is far too expensive to pay on
        every write.

        A difference is between three screens: the one the program drew,
        the one pymux keeps for the pane, and the one a terminal builds
        from what pymux emitted. Only the middle one is invisible from
        outside, and without it a fault cannot be placed.
        """
        assert self.terminal is not None
        done = run_cli(self.terminal.sock_path, ["capture-pane", "-p"])
        for number, row in enumerate(done.stdout.decode().splitlines()):
            if row.strip():
                self._trace("pane %2d |%s|" % (number, row))

    def settle(self) -> None:
        """
        Read until the wire has been quiet for a while.

        The frame can follow the fence, because prompt_toolkit may
        postpone a redraw. Waiting for silence rather than for a fixed
        time costs nothing when there is no frame to wait for, and waits
        as long as it has to when there is.
        """
        assert self.terminal is not None
        deadline = time.monotonic() + FIRST_BYTE
        while time.monotonic() < deadline:
            readable, _, _ = select.select([self.terminal.master_fd], [], [], QUIET)
            if not readable:
                return
            self.terminal.seen += os.read(self.terminal.master_fd, 65536)
            deadline = time.monotonic() + FIRST_BYTE

    def close(self) -> None:
        if self.writer >= 0:
            os.close(self.writer)
            self.writer = -1
        if self.terminal is not None:
            self.terminal.close()
            self.terminal = None
