"""
End-to-end tests for the terminal features over a real pty.

These tests play the outer terminal. Each one attaches a pymux client
on a pty, answers the detection queries the way a chosen terminal
would, and checks what the server then sends.

Three terminals are played:

* A kitty-like terminal. It speaks the keyboard protocol, the graphics
  protocol and 24 bit colour. The checks are that the client enables
  the keyboard flags of the focused pane, that keys reach the pane in
  the encoding the pane asked for, that the image of the pane is
  re-transmitted and placed, and that the colours are 24 bit.
* An xterm-like terminal. It draws sixel and takes 256 colours. The
  check is that the sixel image of the pane is re-encoded for the cell
  size that the terminal reports.
* A terminal that answers no probe but sets `COLORTERM`. The check is
  that the environment still raises the colour depth.
* A plain terminal that answers nothing but the device attributes. The
  check is that no image and no keyboard sequence is sent at all.

Run with:

    nix develop --file . shell --command python3 tests/drive_with_pty.py
"""
import fcntl
import os
import re
import select
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from ptterm.sixel import decode_sixel  # noqa: E402

from pymux.colors import TRUECOLOR_PROBE  # noqa: E402
from pymux.graphics import CELL_SIZE_QUERY  # noqa: E402
from pymux.graphics import QUERY_SEQUENCE as GRAPHICS_QUERY  # noqa: E402

REPO_ROOT = Path(__file__).parent.parent

# "\x1b[97;5u" — kitty ctrl+a — as the pane child reports it (hex).
CTRL_A_KITTY_HEX = b"<<1b5b39373b3575>>"

# The kitty image that the pane child transmits: 2x2 pixels, RGB. It
# is placed over three columns and two rows.
IMAGE_PAYLOAD = "AAECAwQFBgcICQoL"
KITTY_IMAGE = (
    "\x1b_Ga=T,f=24,s=2,v=2,i=7,c=3,r=2,C=1;" + IMAGE_PAYLOAD + "\x1b\\"
)

# The sixel image that the pane child draws: 20 by 12 pixels, red. With
# the cell that a pane assumes that is two columns and one row.
SIXEL_IMAGE = '\x1bP0;0;0q"1;1;20;12#4;2;100;0;0#4!20~-!20~\x1b\\'

# Cell size that the sixel terminal reports, in pixels.
CELL_WIDTH, CELL_HEIGHT = 8, 17

# The OSC sequences that the pane child sends. Only the terminal of the
# user can serve them, so pymux passes them on. The query does not go:
# a pane may write the clipboard, not read it.
OSC_CLIPBOARD = "\x1b]52;c;aGVsbG8=\x1b\\"
OSC_CLIPBOARD_QUERY = "\x1b]52;c;?\x1b\\"
# The pane asks to be told when the user clicks the notification.
OSC_NOTIFICATION = "\x1b]99;i=mine:a=report;done\x1b\\"
# What pymux sends on: the identifier names the pane, not the program.
OSC_NOTIFICATION_RE = rb"\x1b\]99;i=(\d+):a=report;done\x1b\\"
# And the answer that comes back to the pane carries the name of the
# program again.
OSC_NOTIFICATION_ANSWER = "\x1b]99;i=mine\x1b\\"
OSC_POINTER = "\x1b]22;pointer\x1b\\"
PANE_OSC = OSC_CLIPBOARD + OSC_CLIPBOARD_QUERY + OSC_NOTIFICATION + OSC_POINTER

# The program that runs in the pane. It puts its tty in raw mode, asks
# for the kitty keyboard protocol, draws one image, sends the OSC
# sequences, and then echoes everything it reads as hex.
PANE_CHILD = """
import sys, tty
tty.setraw(0)
sys.stdout.write("\\x1b[>1u")
sys.stdout.write(%r if sys.argv[1] == "kitty" else %r)
sys.stdout.write(%r)
sys.stdout.write("READY")
sys.stdout.flush()
while True:
    data = sys.stdin.buffer.read1(256)
    if not data:
        break
    # A line of its own: a long echo must not wrap, or the check for
    # it reads the wrapping as part of the text.
    sys.stdout.write("\\r\\n<<%%s>>" %% data.hex())
    sys.stdout.flush()
""" % (KITTY_IMAGE, SIXEL_IMAGE, PANE_OSC)


class Failed(AssertionError):
    pass


def wait_for(master_fd, pattern, seen, timeout=15.0):
    """
    Read from the pty master until `pattern` arrives. `seen` accumulates
    everything that was ever read. Returns `seen`.
    """
    deadline = time.time() + timeout
    while pattern not in seen:
        if time.time() > deadline:
            raise Failed(
                "Timeout waiting for %r. Got: %r" % (pattern, seen[-2000:])
            )
        readable, _, _ = select.select([master_fd], [], [], 0.1)
        if readable:
            seen += os.read(master_fd, 65536)
    return seen


def drain(master_fd, seen, seconds=1.0):
    "Read whatever arrives for a while. (For checks that expect nothing.)"
    deadline = time.time() + seconds
    while time.time() < deadline:
        readable, _, _ = select.select([master_fd], [], [], 0.1)
        if readable:
            seen += os.read(master_fd, 65536)
    return seen


def run_cli(sock_path, args):
    "Run a pymux CLI command against the server (like libtmux does)."
    return subprocess.run(
        [sys.executable, "-m", "pymux", "-S", str(sock_path)] + args,
        cwd=str(REPO_ROOT),
        capture_output=True,
        timeout=20,
        check=False,
    )


class Terminal:
    "One pymux server with one client attached to a pty."

    def __init__(self, tmp, mode, colorterm=""):
        self.tmp = tmp
        self.sock_path = tmp / ("%s.sock" % mode)
        self.stderr_path = tmp / ("%s-stderr.log" % mode)

        child_path = tmp / "pane_child.py"
        child_path.write_text(PANE_CHILD)

        started = run_cli(
            self.sock_path,
            [
                "new-session",
                "-d",
                "-s",
                "test",
                "python3 %s %s" % (child_path, mode),
            ],
        )
        assert started.returncode == 0, started.stderr

        self.master_fd, slave_fd = os.openpty()
        fcntl.ioctl(
            slave_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0)
        )

        self.stderr = open(self.stderr_path, "wb")
        self.client = subprocess.Popen(
            [sys.executable, "-m", "pymux", "-S", str(self.sock_path), "attach"],
            cwd=str(REPO_ROOT),
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=self.stderr,
            env={
                **os.environ,
                "TERM": "xterm-256color",
                "LANG": "C.UTF-8",
                # The tests decide what the environment says about
                # colour; the shell that runs them must not.
                "COLORTERM": colorterm,
            },
        )
        os.close(slave_fd)
        self.seen = b""

    def wait_for(self, pattern):
        self.seen = wait_for(self.master_fd, pattern, self.seen)

    def drain(self, seconds=1.0):
        self.seen = drain(self.master_fd, self.seen, seconds)

    def write(self, data):
        os.write(self.master_fd, data)

    def wait_for_the_queries(self):
        """
        Wait until the client asked everything, up to the fence.

        Returns the length of what was read. The queries carry the
        sequences of every protocol, so a check that something is never
        sent has to look at what comes after them.
        """
        self.wait_for(GRAPHICS_QUERY.encode())
        self.wait_for(b"\x1b[c")
        return len(self.seen)

    def mark(self):
        "A point in the output, for `since`."
        return len(self.seen)

    def since(self, mark):
        "Everything that arrived after `mark`."
        return self.seen[mark:]

    def close(self):
        run_cli(self.sock_path, ["kill-server"])
        if self.client.poll() is None:
            self.client.send_signal(signal.SIGTERM)
        try:
            self.client.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.client.kill()
        self.stderr.close()
        os.close(self.master_fd)
        time.sleep(0.3)
        run_cli(self.sock_path, ["kill-server"])

    def report(self):
        print(self.seen[-4000:].decode("utf-8", "replace"))
        if self.stderr_path.exists():
            print(
                "--- client stderr ---\n"
                + self.stderr_path.read_text(errors="replace")
            )


# ----------------------------------------------------------------------


def check_the_osc_sequences(terminal, tail):
    """
    The three OSC sequences of the pane reach the terminal, and the
    clipboard query does not.
    """
    assert OSC_CLIPBOARD.encode() in tail, "the clipboard write did not arrive"
    assert OSC_POINTER.encode() in tail, "the pointer shape did not arrive"
    assert (
        OSC_CLIPBOARD_QUERY.encode() not in tail
    ), "the pane read the clipboard of the user"

    found = re.search(OSC_NOTIFICATION_RE, tail)
    assert found, "the notification did not arrive"
    return found.group(1)


def check_kitty_terminal(tmp):
    "A terminal that speaks every kitty protocol and 24 bit colour."
    terminal = Terminal(tmp, "kitty")
    try:
        # 1. The client asks its terminal what it can do.
        mark = terminal.wait_for_the_queries()
        assert CELL_SIZE_QUERY.encode() in terminal.seen
        assert TRUECOLOR_PROBE.encode() in terminal.seen

        # Answer like a terminal that supports all of it.
        terminal.write(b"\x1b[?1u")  # Keyboard flags.
        terminal.write(b"\x1b_Gi=31;OK\x1b\\")  # Kitty graphics.
        terminal.write(b"\x1b[6;20;10t")  # Cell size.
        terminal.write(b"\x1bP1$r38:2::1:2:3m\x1b\\")  # The colour survived.
        terminal.write(b"\x1b[?62;1;6c")  # Device attributes: no sixel.

        # 2. The pane pushed the disambiguate flag; the client enables
        #    it on the outer terminal.
        terminal.wait_for(b"\x1b[=1;1u")

        # 3. The pane child runs and its output is rendered.
        terminal.wait_for(b"READY")
        assert b"a=T" not in terminal.seen, "graphics command leaked as text"
        assert (
            IMAGE_PAYLOAD.encode() not in terminal.seen
        ), "graphics payload leaked as text"

        # 4. The server re-transmits the image and puts it on screen.
        terminal.wait_for(b"\x1b_Ga=t,i=")
        terminal.wait_for(b"a=p,i=")
        transmit = re.search(
            rb"\x1b_Ga=t,i=(\d+),t=d,q=2,f=24,s=2,v=2,o=z", terminal.seen
        )
        assert transmit, "no image transmission on the outer terminal"
        image_id = transmit.group(1)
        put = re.search(
            rb"\x1b\[(\d+);(\d+)H\x1b_Ga=p,i="
            + image_id
            + rb",p=1,c=3,r=2,C=1,q=2",
            terminal.seen,
        )
        assert put, "no placement on the outer terminal"
        # The pane starts on the second row: the titlebar takes the first.
        assert (int(put.group(1)), int(put.group(2))) == (2, 1)

        # 5. The colours are 24 bit, because the probe came back.
        terminal.wait_for(b"\x1b[0;38;2;")

        # 6. Keys reach the pane in the encoding that the pane asked for.
        terminal.write(b"\x1b[97;5u")
        terminal.wait_for(CTRL_A_KITTY_HEX)
        terminal.write(b"\x01")  # The legacy encoding is translated too.
        terminal.wait_for(CTRL_A_KITTY_HEX)

        # 7. The clipboard, the notification and the pointer shape of
        #    the pane reach the terminal.
        terminal.wait_for(OSC_POINTER.encode())
        identifier = check_the_osc_sequences(terminal, terminal.since(mark))

        # 8. The user clicks the notification. The answer goes to the
        #    pane that asked, under the name that the pane chose.
        terminal.write(b"\x1b]99;i=" + identifier + b"\x1b\\")
        terminal.wait_for(
            ("<<%s>>" % OSC_NOTIFICATION_ANSWER.encode().hex()).encode()
        )

        # 9. A reply of the outer terminal does not reach the pane. The
        #    pane echoes everything it reads between "<<" and ">>", so
        #    a leak shows up there.
        terminal.drain(0.5)
        quiet = terminal.mark()
        terminal.write(b"\x1b]11;rgb:dead/beef/cafe\x1b\\")
        terminal.write(b"\x1b]10;rgb:1234/5678/9abc\x07")
        terminal.drain(1.0)
        assert (
            b"<<" not in terminal.since(quiet)
        ), "a reply of the terminal reached the pane"

        # 10. The server goes away: the client resets the flags.
        run_cli(terminal.sock_path, ["kill-server"])
        terminal.wait_for(b"\x1b[=0;1u")
    except BaseException:
        terminal.report()
        raise
    finally:
        terminal.close()
    print("kitty terminal: ok")


def check_sixel_terminal(tmp):
    "An xterm-like terminal: sixel, no kitty protocols, 256 colours."
    terminal = Terminal(tmp, "sixel")
    try:
        mark = terminal.wait_for_the_queries()

        # Answer only the cell size and the device attributes. The "4"
        # says sixel; nothing answers the kitty or the colour query.
        terminal.write(
            ("\x1b[6;%i;%it" % (CELL_HEIGHT, CELL_WIDTH)).encode()
        )
        terminal.write(b"\x1b[?62;1;4;6c")

        terminal.wait_for(b"READY")

        # The sixel that the pane child drew must not reach the screen
        # as text.
        assert b"!20~" not in terminal.seen, "sixel body leaked as text"

        # The server re-encodes the image for the cell size that this
        # terminal reported. The pane image is 20x12 pixels, which is
        # two columns and one row; the terminal draws that as
        # 2 * 8 by 1 * 17 pixels.
        terminal.wait_for(b"\x1bP0;1;0q")
        found = re.search(
            rb"\x1b\[(\d+);(\d+)H\x1bP([^\x1b]*)\x1b\\", terminal.seen
        )
        assert found, "no sixel image on the outer terminal"
        decoded = decode_sixel(found.group(3).decode("latin-1"))
        assert decoded is not None, "the sixel image does not decode"
        assert (decoded[0], decoded[1]) == (
            2 * CELL_WIDTH,
            1 * CELL_HEIGHT,
        ), "the image was not scaled to the cells: %r" % (decoded[:2],)

        # The colours stay at 256: nothing said more.
        assert b"\x1b[0;38;5;" in terminal.seen, "no 256 colour output"
        assert b";38;2;" not in terminal.seen, "24 bit colour without a probe"

        # No kitty protocol was claimed, so nothing of it is sent.
        assert b"\x1b_G" not in terminal.since(mark)
        assert b"\x1b[=1;1u" not in terminal.since(mark)
    except BaseException:
        terminal.report()
        raise
    finally:
        terminal.close()
    print("sixel terminal: ok")


def check_colorterm_terminal(tmp):
    "A terminal that answers no probe but sets COLORTERM."
    terminal = Terminal(tmp, "sixel", colorterm="truecolor")
    try:
        terminal.wait_for_the_queries()
        terminal.write(b"\x1b[?62;1;6c")  # Nothing else is answered.
        terminal.wait_for(b"READY")
        # The environment is the fallback when the probe stays quiet.
        terminal.wait_for(b"\x1b[0;38;2;")
    except BaseException:
        terminal.report()
        raise
    finally:
        terminal.close()
    print("COLORTERM terminal: ok")


def check_plain_terminal(tmp):
    "A terminal that answers only the device attributes."
    terminal = Terminal(tmp, "sixel")
    try:
        mark = terminal.wait_for_the_queries()
        terminal.write(b"\x1b[?1;2c")  # A VT100: no sixel, nothing else.

        terminal.wait_for(b"READY")
        terminal.drain(1.5)

        tail = terminal.since(mark)
        # Passing an OSC sequence on asks nothing of the terminal, so a
        # terminal that answers no query still receives them.
        check_the_osc_sequences(terminal, tail)
        assert b"\x1b_G" not in tail, "images without support"
        assert b"\x1bP" not in tail, "sixel without support"
        assert b"\x1b[=" not in tail, "keyboard flags without support"
        assert b";38;2;" not in tail, "24 bit colour without support"
    except BaseException:
        terminal.report()
        raise
    finally:
        terminal.close()
    print("plain terminal: ok")


def check_a_closing_split(tmp):
    """
    A split that closes must not upset the client.

    A task of the server that nobody holds can be collected while it
    still runs. The loop reports that, and prompt_toolkit answers an
    exception in the loop by leaving the alternate screen and asking
    for a key press. Nobody is there to press one, so the answer fails
    and reports again: the client used to repaint without end.

    This is a smoke check, not a proof: the collection depends on when
    the garbage collector runs. `tests/test_server_tasks.py` pins the
    two halves of the fix.
    """
    terminal = Terminal(tmp, "kitty")
    try:
        terminal.wait_for_the_queries()
        terminal.write(b"\x1b[?1u")
        terminal.write(b"\x1b_Gi=31;OK\x1b\\")
        terminal.write(b"\x1b[6;20;10t")
        terminal.write(b"\x1b[?62;1;6c")
        terminal.wait_for(b"READY")

        # A second pane that ends on its own.
        split = run_cli(
            terminal.sock_path, ["split-window", "%s -c pass" % sys.executable]
        )
        assert split.returncode == 0, split.stderr
        terminal.drain(2.0)

        quiet = terminal.mark()
        terminal.drain(3.0)
        tail = terminal.since(quiet)

        assert (
            b"\x1b[?1049l" not in tail
        ), "the client left the alternate screen after a split closed"
        assert len(tail) < 200000, (
            "the client repainted without end after a split closed: %i bytes"
            % len(tail)
        )
    except BaseException:
        terminal.report()
        raise
    finally:
        terminal.close()
    print("closing split: ok")


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="pymux-pty-test-"))
    check_kitty_terminal(tmp)
    check_sixel_terminal(tmp)
    check_colorterm_terminal(tmp)
    check_plain_terminal(tmp)
    check_a_closing_split(tmp)
    print("All pty checks passed.")


if __name__ == "__main__":
    main()
