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

`middleman.py` holds the pane and the fence, because the shape is not
libvterm's: any borrowed suite can be run through it, and
`drive_with_alacritty.py` is the other one that does. What is here is
the part that speaks libvterm's protocol.

Three variables reach this file from `pymux/nix/checks.nix`.
`PYMUX_VTERM_HARNESS` names the built `t/harness`, and nothing works
without it. `PYMUX_VTERM_TMP` is a directory to work in.
"""
import os
import subprocess
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from tests.middleman import Pane  # noqa: E402

#: The screen that libvterm's `INIT` makes. A full screen pane is the
#: whole terminal, so the pty of the client is the same size.
ROWS, COLUMNS = 25, 80

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
        self.judge = Judge(program)
        self.pane = Pane(tmp, "vterm", ROWS, COLUMNS, trace=_trace)
        self.started = False

    def write(self, data: bytes) -> bytes:
        "Put bytes on the screen of the pane, and read our own wire back."
        wire = self.pane.write(data)
        if os.environ.get("PYMUX_VTERM_TRACE"):
            self.pane.trace_the_pane()
        return wire

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
            if not self.started:
                self.pane.start()
                self.started = True
                # Whatever pymux drew on the way up goes to the judge
                # as well, so that the two screens start as one.
                self.feed_the_judge(self.pane.seen)
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
        self.pane.close()
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
