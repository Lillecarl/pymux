"""
Speak the protocol of libvterm's test harness, with pymux in the middle.

`ptterm/tests/vterm_harness.py` plugs ptterm in where libvterm stands:
it answers the suite out of ptterm's own screen. This is the other
shape, and it goes one level further.

    the test file ── PUSH ──▶ this ──▶ a program in a full screen pane
                                            │
                                            ▼
                                       ptterm parses
                                            │
                                            ▼
                                    pymux renders and emits
                                            │
       the test file ◀── an answer ── libvterm reads what pymux emitted

So nothing of ours answers anything. `t/harness.c` is built as it
stands, a real libvterm sits behind it, and the assertions of the suite
are answered in libvterm's own words about what came off our wire.

**That is what makes the check worth having.** The direct plug-in
measures our model. This measures what we emit, which is what a
terminal on the other end of pymux actually receives. A judge holds
things our model does not, so a suite can stay green on them as long as
we emit them faithfully.

**And it is why a large part of the suite has to be left out.** A wire
carries a screen. It does not carry what style the next character will
take, whether a line is a continuation, or which mode is set, so `?pen`
and `?lineinfo` can never be answered here however faithfully we emit.
`drive_with_vterm.py` holds that list with the reason.

## How a PUSH is fenced

The bytes of the suite reach the pane through a fifo, which a forwarder
in the pane copies to its own output. Then this has to know when pymux
has finished drawing what those bytes did.

A fence goes down the fifo behind the payload: an OSC 52, which ptterm
hands to pymux and pymux writes to the terminal of every client. Seeing
it on the wire proves the pane consumed the payload.

It does not prove the frame arrived. prompt_toolkit may postpone a
redraw, so the frame can follow the fence. A short settle after the
fence covers that, and it is short because the fence has already done
the waiting.

Three variables reach this file from `pymux/nix/checks.nix`.
`PYMUX_VTERM_HARNESS` names the built `t/harness`, and nothing works
without it. `PYMUX_VTERM_TMP` is a directory to work in.
"""
import base64
import os
import re
import select
import subprocess
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from tests.drive_with_pty import Terminal, run_cli  # noqa: E402

#: The screen that libvterm's `INIT` makes. A full screen pane is the
#: whole terminal, so the pty of the client is the same size.
ROWS, COLUMNS = 25, 80

#: How long the wire has to stay quiet after the fence before a frame
#: counts as finished, in seconds.
#:
#: The fence has already waited for the pane, so this only covers a
#: redraw that prompt_toolkit postponed, and it waits for silence rather
#: than for the clock. A test file holds hundreds of pushes, and a fixed
#: wait on each of them is the difference between a check that runs in a
#: minute and one nobody runs.
QUIET = 0.05

#: How long to wait for the first byte of that frame. A push that
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
#: on the way out, and the suite pushes both, so without it every line
#: feed of a test file arrives as something else.
FORWARDER = r"""
import fcntl, os, signal, struct, sys, termios
os.system("stty -opost -echo")

# The size of the pane, written where the harness can read it, and
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
#: wire before the judge sees it. The judge would only ignore it, but a
#: stream that holds our own scaffolding is a stream nobody can read.
_FENCE = re.compile(rb"\x1b\]52;[^\x07\x1b]*(?:\x07|\x1b\\)")

#: How many bytes of a frame go into one `PUSH` to the judge. Its line
#: buffer holds 1024 bytes, a byte costs two of hex, and "PUSH " costs
#: five more.
_MOST_OF_A_LINE = 480


def _trace(message: str) -> None:
    """
    Write one line of the exchange to the standard error.

    `PYMUX_VTERM_TRACE` turns it on. The driver keeps stderr in the log,
    so a run with it on says what every side said, which is the only way
    to read a fault in a chain of three programs.
    """
    if os.environ.get("PYMUX_VTERM_TRACE"):
        sys.stderr.write(message + "\n")
        sys.stderr.flush()


class Judge:
    """
    libvterm, answering for itself.

    `t/harness.c` built as it stands, driven the way `run-test.pl`
    drives it. Every assertion of the suite goes through here unchanged.
    """

    def __init__(self, program: str) -> None:
        self.process = subprocess.Popen(
            [program],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            bufsize=0,
        )

    def line(self, command: str) -> str:
        "Send one line and read the one line that answers it."
        _trace("judge <- %s" % command)
        self.process.stdin.write(command.encode() + b"\n")
        answer = self.process.stdout.readline()
        _trace("judge -> %r" % answer)
        return answer.decode("utf-8", "replace").rstrip("\n")

    def command(self, command: str) -> None:
        """
        Send one command and read to the end of its answer.

        A command is answered with whatever the callbacks of libvterm
        emitted and then "DONE". Nothing here wants those lines: the
        suite is asking about our wire, and what libvterm redrew while
        reading it is libvterm's business.
        """
        _trace("judge <- %s" % command)
        self.process.stdin.write(command.encode() + b"\n")
        while True:
            answer = self.process.stdout.readline()
            _trace("judge -> %r" % answer)
            if not answer or answer in (b"DONE\n", b"?\n"):
                return

    def close(self) -> None:
        self.process.stdin.close()
        self.process.wait(timeout=10)


class MiddleMan:
    "One pymux, one pane, and one libvterm reading what pymux emits."

    def __init__(self, tmp: Path, program: str) -> None:
        self.tmp = tmp
        self.judge = Judge(program)
        self.terminal = None
        self.fifo = tmp / "payload.fifo"
        self.writer = -1
        self.fence = 0

    # -- the pane -------------------------------------------------------

    def start(self) -> None:
        "Bring up pymux with one pane that copies the fifo to the screen."
        os.mkfifo(self.fifo)

        forwarder = self.tmp / "forwarder.py"
        forwarder.write_text(FORWARDER)

        # A full screen pane is every cell of the terminal: no status
        # line and no pane title. Without it the pane sits two rows
        # short and every row of every assertion is out by one.
        config = self.tmp / "vterm.conf"
        config.write_text("set full-screen on\n")

        size = self.tmp / "pane-size.txt"

        self.terminal = Terminal(
            self.tmp,
            "vterm",
            command="%s %s %s %s" % (sys.executable, forwarder, self.fifo, size),
            rows=ROWS,
            columns=COLUMNS,
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
            if [int(one) for one in found] == [ROWS, COLUMNS]:
                return
            self.settle()
        raise AssertionError(
            "the pane is %s and the suite asks about %d rows by %d columns"
            % (" by ".join(found) or "not saying", ROWS, COLUMNS)
        )

    def write(self, data: bytes) -> bytes:
        """
        Put bytes on the screen of the pane, and return what pymux
        wrote to its client because of them.
        """
        assert self.terminal is not None
        self.fence += 1
        token = base64.b64encode(b"fence-%d" % self.fence).decode()

        mark = self.terminal.mark()
        os.write(self.writer, data + b"\x1b]52;c;%s\x07" % token.encode())

        self.terminal.wait_for(token.encode())
        self.settle()
        self.trace_the_pane()
        return _FENCE.sub(b"", self.terminal.since(mark))

    def trace_the_pane(self) -> None:
        """
        Write the screen of the pane itself into the trace.

        A difference here is between three screens: the one the program
        drew, the one pymux keeps for the pane, and the one a terminal
        builds from what pymux emitted. Only the middle one is invisible
        from outside, and without it a fault cannot be placed.
        """
        if not os.environ.get("PYMUX_VTERM_TRACE"):
            return
        done = run_cli(self.terminal.sock_path, ["capture-pane", "-p"])
        for number, row in enumerate(done.stdout.decode().splitlines()):
            if row.strip():
                _trace("pane %2d |%s|" % (number, row))

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

    def feed_the_judge(self, data: bytes) -> None:
        """
        Give libvterm what came off the wire, a piece at a time.

        `t/harness.c` reads with `fgets` into a buffer of 1024 bytes. A
        longer line is not an error there: it comes back as two, and the
        second one starts in the middle of the hex and is read as a
        command nobody knows. One frame of a full screen redraw is far
        longer than that, so the answers walk one step out of place and
        stay there.

        The suite never meets this, because a line of a test file is
        short. Everything this forwards is a frame.
        """
        for start in range(0, len(data), _MOST_OF_A_LINE):
            piece = data[start : start + _MOST_OF_A_LINE]
            self.judge.command("PUSH " + piece.hex())

    # -- the protocol ---------------------------------------------------

    def command(self, line: str) -> bool:
        "Do one command. False means this harness does not know it."
        if line == "INIT":
            self.judge.command("INIT")
            if self.terminal is None:
                self.start()
                # Whatever pymux drew on the way up goes to the judge
                # as well, so that the two screens start as one.
                self.feed_the_judge(self.terminal.seen)
            return True

        if line == "WANTPARSER" or line.split(" ")[0] in (
            "WANTSTATE",
            "WANTSCREEN",
            "WANTENCODING",
            "DAMAGEMERGE",
            "DAMAGEFLUSH",
        ):
            # These say which callbacks libvterm should report. Nothing
            # here reads those, but "WANTSCREEN" is also what builds the
            # screen that every "?screen_" assertion needs.
            self.judge.command(line)
            return True

        if line.startswith("UTF8 "):
            self.judge.command(line)
            return True

        if line == "RESET":
            # Both ends. The judge goes back to a blank screen with the
            # cursor home, and the pane is sent RIS. What pymux draws
            # for the reset then goes to the judge, so the two agree
            # again before the next test case.
            self.judge.command("RESET")
            self.feed_the_judge(self.write(b"\x1bc"))
            return True

        if line.startswith("PUSH "):
            self.feed_the_judge(self.write(bytes.fromhex(line[5:].strip())))
            return True

        # A resize would have to reach the pty of the client, and the
        # pane would then be a different size from the judge until the
        # frame that follows. Every file that needs one is left out, so
        # this says so rather than pretending.
        return False

    def assertion(self, line: str) -> str:
        "Ask libvterm, which is the only thing here that answers."
        return self.judge.line(line)

    def close(self) -> None:
        if self.writer >= 0:
            os.close(self.writer)
        if self.terminal is not None:
            self.terminal.close()
        self.judge.close()


def main() -> int:
    program = os.environ.get("PYMUX_VTERM_HARNESS", "")
    if not program:
        print("PYMUX_VTERM_HARNESS names no harness", file=sys.stderr)
        return 1

    tmp = Path(os.environ.get("PYMUX_VTERM_TMP", ".")) / ("run-%d" % os.getpid())
    tmp.mkdir(parents=True, exist_ok=True)

    middle = MiddleMan(tmp, program)
    try:
        for raw in sys.stdin:
            line = raw.rstrip("\n")
            if not line:
                continue

            _trace("suite -> %s" % line)

            if line.startswith("?"):
                try:
                    answer = middle.assertion(line)
                except Exception:
                    traceback.print_exc(file=sys.stderr)
                    answer = "!"
                sys.stdout.write(answer + "\n")
                sys.stdout.flush()
                continue

            try:
                known = middle.command(line)
            except Exception:
                traceback.print_exc(file=sys.stderr)
                known = False
            sys.stdout.write("DONE\n" if known else "?\n")
            sys.stdout.flush()
    finally:
        try:
            middle.close()
        except Exception:
            traceback.print_exc(file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
