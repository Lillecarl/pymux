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
from pymux.blocks import LOWER_HALF, UPPER_HALF  # noqa: E402
from pymux.terminfo import terminal_name  # noqa: E402

REPO_ROOT = Path(__file__).parent.parent

# "\x1b[97;5u" — kitty ctrl+a — as the pane child reports it (hex).
CTRL_A_KITTY_HEX = b"<<1b5b39373b3575>>"

# The same key when the release is made up rather than reported: the
# press and the release arrive in one read, so the pane echoes both.
CTRL_A_KITTY_PRESS_AND_RELEASE = b"<<1b5b39373b35751b5b39373b353a3375>>"

# A key that came back up, as a terminal that speaks the protocol
# sends it, and as the pane child reports it. Only a pane that asked
# for the event types of a key may see one.
KEY_RELEASE = b"\x1b[97;1:3u"
# What the pane reads. The form is the one that kitty writes: a field
# that holds its default stays empty, so the modifiers of a release
# with none are an empty field and not the value one.
KEY_RELEASE_HEX = b"<<%s>>" % b"\x1b[97;:3u".hex().encode("ascii")

# The release that pymux makes up for a terminal that sends none. The
# check looks for the hex alone, because the press of the key may
# share a read with it or may not.
MADE_UP_RELEASE_HEX = b"\x1b[97;:3u".hex().encode("ascii")

# The pane asks "CSI ? u" when it reads a "Q", and echoes the answer.
# The answer holds the flags that the pane really gets, so it depends
# on what the terminal of the client can report.
ASK_THE_FLAGS = b"Q"


def flags_answer(flags: int) -> bytes:
    "The answer of the pane to the flags query, as the pane echoes it."
    return b"<<%s>>" % ("\x1b[?%du" % flags).encode().hex().encode("ascii")

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

# A hyperlink belongs to the cells that carry it, so it does not travel
# as a sequence. The pane draws it, the cells hold the target, and the
# renderer opens the link again on the terminal of the user.
HYPERLINK_TARGET = "https://example.com/pymux"
PANE_HYPERLINK = "\x1b]8;;%s\x1b\\LINK\x1b]8;;\x1b\\plain" % HYPERLINK_TARGET

# The shape of an underline and its colour belong to a cell as well.
# The pane draws a curly red line; the terminal of the user has to get
# the sub-parameter and the colour back.
PANE_UNDERLINE = "\x1b[4:3;58:2::255:0:0mCURLY\x1b[0m"

# The program that runs in the pane. It puts its tty in raw mode, asks
# for the kitty keyboard protocol, draws one image, sends the OSC
# sequences, and then echoes everything it reads as hex.
PANE_CHILD = """
import os, sys, tty
tty.setraw(0)
sys.stdout.write("\\x1b[>3u")
sys.stdout.write(%r if sys.argv[1] == "kitty" else %r)
sys.stdout.write(%r)
sys.stdout.write("READY")
sys.stdout.write("\\r\\nENV<%%s|%%s|%%s|%%s>" %% (
    os.environ.get("TERM", ""),
    os.environ.get("COLORTERM", ""),
    os.environ.get("KITTY_WINDOW_ID", ""),
    os.environ.get("TERM_PROGRAM", ""),
))
sys.stdout.write("\\r\\n" + %r)
sys.stdout.write("\\r\\n" + %r)
sys.stdout.flush()
while True:
    data = sys.stdin.buffer.read1(256)
    if not data:
        break
    if b"Q" in data:
        # Ask what the terminal really does with the keyboard. The
        # answer arrives as input, so the echo below shows it. Asking
        # on demand and not at the start keeps it after the detection
        # of the client, which is what decides the answer.
        sys.stdout.write("\\x1b[?u")
        sys.stdout.flush()
    # A line of its own: a long echo must not wrap, or the check for
    # it reads the wrapping as part of the text.
    sys.stdout.write("\\r\\n<<%%s>>" %% data.hex())
    sys.stdout.flush()
""" % (KITTY_IMAGE, SIXEL_IMAGE, PANE_OSC, PANE_HYPERLINK, PANE_UNDERLINE)


#: A program that draws nothing and waits. A pane running it asks for
#: no pointer shape, which is what the check of the pointer needs.
QUIET_CHILD = """
import sys, time
sys.stdout.write("QUIET")
sys.stdout.flush()
time.sleep(600)
"""


#: The program of an overlay pane: it names itself, echoes what it
#: reads and waits.
OVERLAY_CHILD = """
import sys, tty
tty.setraw(0)
sys.stdout.write("OVERLAYMARK")
sys.stdout.flush()
while True:
    data = sys.stdin.buffer.read1(256)
    if not data:
        break
    sys.stdout.write("\\r\\n[[%s]]" % data.decode("utf-8", "replace"))
    sys.stdout.flush()
"""


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


def attach_client(sock_path, stderr_path, colorterm=""):
    """
    Attach a client to a server that is already running, on a pty of
    its own. Returns the master side, the process and the stderr file.
    """
    master_fd, slave_fd = os.openpty()
    fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))

    stderr = open(stderr_path, "wb")
    process = subprocess.Popen(
        [sys.executable, "-m", "pymux", "-S", str(sock_path), "attach"],
        cwd=str(REPO_ROOT),
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=stderr,
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
    return master_fd, process, stderr


class Attached:
    "What every client on a pty can do: read it, write to it, watch it."

    master_fd: int
    seen: bytes
    stderr_path = None

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

    def report(self):
        print(self.seen[-4000:].decode("utf-8", "replace"))
        if self.stderr_path is not None and self.stderr_path.exists():
            print(
                "--- client stderr ---\n"
                + self.stderr_path.read_text(errors="replace")
            )
        log = getattr(self, "server_log", None)
        if log is not None and log.exists():
            # Only what the server complained about: the whole log is
            # every frame of every render.
            lines = [
                line
                for line in log.read_text(errors="replace").splitlines()
                if "ERROR" in line or "Traceback" in line or line.startswith("  ")
            ]
            if lines:
                print("--- server errors ---\n" + "\n".join(lines[-60:]))
            probes = [
                line
                for line in log.read_text(errors="replace").splitlines()
                if "PROBE" in line
            ]
            if probes:
                print("--- probes ---\n" + "\n".join(probes[-25:]))


class SecondClient(Attached):
    """
    Another client on the same server, playing another terminal.

    `Terminal` starts a server and attaches the first client. This one
    joins a server that is already there, so a check can put two
    terminals of different abilities in front of one session.
    """

    def __init__(self, tmp, sock_path, name, colorterm=""):
        self.sock_path = sock_path
        self.stderr_path = tmp / ("%s-stderr.log" % name)
        self.master_fd, self.client, self.stderr = attach_client(
            sock_path, self.stderr_path, colorterm
        )
        self.seen = b""

    def close(self):
        "Leave. The server stays: the first client owns it."
        if self.client.poll() is None:
            self.client.send_signal(signal.SIGTERM)
        try:
            self.client.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.client.kill()
        self.stderr.close()
        os.close(self.master_fd)


class Terminal(Attached):
    "One pymux server with one client attached to a pty."

    def __init__(self, tmp, mode, colorterm=""):
        self.tmp = tmp
        self.sock_path = tmp / ("%s.sock" % mode)
        self.stderr_path = tmp / ("%s-stderr.log" % mode)
        # The server runs as a daemon, so nothing of it reaches this
        # process. A check that fails is unreadable without this.
        self.server_log = tmp / ("%s-server.log" % mode)

        child_path = tmp / "pane_child.py"
        child_path.write_text(PANE_CHILD)

        started = run_cli(
            self.sock_path,
            [
                "--log",
                str(self.server_log),
                "new-session",
                "-d",
                "-s",
                "test",
                "python3 %s %s" % (child_path, mode),
            ],
        )
        assert started.returncode == 0, started.stderr

        self.master_fd, self.client, self.stderr = attach_client(
            self.sock_path, self.stderr_path, colorterm
        )
        self.seen = b""

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


def check_the_hyperlink(terminal):
    """
    The link that the pane drew reaches the terminal of the user.

    It opens before the text it belongs to and closes after it, and the
    target never travels as text.
    """
    opened = ("\x1b]8;;%s\x1b\\" % HYPERLINK_TARGET).encode()
    assert opened in terminal.seen, "the hyperlink never opened"
    after = terminal.seen[terminal.seen.index(opened) + len(opened):]
    assert b"L" in after[:40], "no text after the link opened"
    assert b"\x1b]8;;\x1b\\" in after, "the hyperlink never closed"
    assert terminal.seen.count(HYPERLINK_TARGET.encode()) == 1, (
        "the target was written more than once"
    )
    print("hyperlink: ok")


def check_the_underline(terminal):
    """
    The shape of the line and its colour reach the terminal of the user.

    Neither is a sequence that pymux passes on: both belong to a cell,
    travel in the style of that cell, and the renderer writes them
    again.
    """
    assert b"4:3" in terminal.seen, "the shape of the line never arrived"
    assert b"58:2::255:0:0" in terminal.seen, "the colour of the line never arrived"
    print("underline: ok")


def check_kitty_terminal(tmp):
    "A terminal that speaks every kitty protocol and 24 bit colour."
    terminal = Terminal(tmp, "kitty")
    try:
        # 1. The client asks its terminal what it can do.
        mark = terminal.wait_for_the_queries()
        assert CELL_SIZE_QUERY.encode() in terminal.seen
        assert TRUECOLOR_PROBE.encode() in terminal.seen

        # Answer like a terminal that supports all of it. The keyboard
        # query asks for every flag, and a real kitty takes them all.
        terminal.write(b"\x1b[?31u")  # Keyboard flags.
        terminal.write(b"\x1b_Gi=31;OK\x1b\\")  # Kitty graphics.
        terminal.write(b"\x1b[6;20;10t")  # Cell size.
        terminal.write(b"\x1bP1$r38:2::1:2:3m\x1b\\")  # The colour survived.
        terminal.write(b"\x1b[?62;1;6c")  # Device attributes: no sixel.

        # 2. The pane pushed the disambiguate flag and the event types;
        #    the client enables both on the outer terminal.
        terminal.wait_for(b"\x1b[=3;1u")

        # 3. The pane child runs and its output is rendered.
        terminal.wait_for(b"READY")

        #    Its environment describes the pane, not the terminal that
        #    the client attached from: a pane takes 24 bit colour, and
        #    it is no kitty window.
        # A pane is told what it really is. The name is "pymux" when
        # the entry of terminfo was built and installed, and the name
        # of xterm when it was not.
        # A pane is told what it really is. The name is "pymux" when
        # the entry of terminfo is there, and the name of xterm when
        # it is not; the server and this check read the same rule.
        terminal.wait_for(
            b"ENV<%s|truecolor||>" % terminal_name().encode("ascii")
        )
        #    A hyperlink belongs to a cell, so the pane keeps it and the
        #    renderer opens it again on the terminal of the user.
        terminal.wait_for(b"plain")
        check_the_hyperlink(terminal)
        check_the_underline(terminal)

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

        #    A key that came back up reaches the pane as well. Only a
        #    terminal that speaks the protocol sends one, and only a
        #    pane that asked for the event types may read it.
        terminal.write(KEY_RELEASE)
        terminal.wait_for(KEY_RELEASE_HEX)

        #    And the pane hears the truth about its keyboard: this
        #    terminal serves every flag, so the pane keeps both of the
        #    flags that it pushed.
        terminal.write(ASK_THE_FLAGS)
        terminal.wait_for(flags_answer(3))

        # Let the pane finish reading. One read of the pane returns
        # whatever has arrived, so a write that comes too soon lands in
        # the same read as this one and the check below looks for a
        # block that never appears on its own.
        terminal.drain(0.5)

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
        assert b"\x1b[=" not in terminal.since(mark)
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

        # The pane asked for the disambiguate flag and the event types.
        # This terminal reports no key release, so pymux makes one up
        # and the pane keeps both flags.
        terminal.write(ASK_THE_FLAGS)
        terminal.wait_for(flags_answer(3))

        # And a key really comes up: one press of "a" reaches the pane
        # as the character and the release of that key.
        terminal.write(b"a")
        terminal.wait_for(MADE_UP_RELEASE_HEX)
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


def check_the_pointer_shape(tmp):
    """
    The shape of the pointer follows the pane that the client looks at.

    The first pane asks for a shape. A split into a pane that asks for
    none has to take the shape away again, or the pointer keeps the
    shape of a pane that the user left.
    """
    terminal = Terminal(tmp, "kitty")
    try:
        terminal.wait_for_the_queries()
        terminal.write(b"\x1b[?1u")
        terminal.write(b"\x1b_Gi=31;OK\x1b\\")
        terminal.write(b"\x1b[6;20;10t")
        terminal.write(b"\x1b[?62;1;6c")
        terminal.wait_for(b"READY")
        terminal.wait_for(OSC_POINTER.encode())

        # A second pane, which asks for no shape.
        child = tmp / "quiet-child.py"
        child.write_text(QUIET_CHILD)
        mark = terminal.mark()
        split = run_cli(
            terminal.sock_path,
            ["split-window", "%s %s" % (sys.executable, child)],
        )
        assert split.returncode == 0, split.stderr
        terminal.wait_for(b"QUIET")
        terminal.drain(1.0)
        assert b"\x1b]22;\x1b\\" in terminal.since(mark), (
            "the pointer kept the shape of the pane that the user left"
        )

        # Back to the pane that asked for the shape.
        mark = terminal.mark()
        back = run_cli(terminal.sock_path, ["last-pane"])
        assert back.returncode == 0, back.stderr
        terminal.drain(1.0)
        assert OSC_POINTER.encode() in terminal.since(mark), (
            "the shape did not come back with the pane"
        )
    except BaseException:
        terminal.report()
        raise
    finally:
        terminal.close()
    print("pointer shape: ok")


def check_an_overlay_pane(tmp):
    """
    An overlay pane floats over the layout and takes the keyboard.

    It opens in the middle of the screen, the program in it writes
    there, and the overlay goes away when that program ends.
    """
    terminal = Terminal(tmp, "kitty")
    try:
        terminal.wait_for_the_queries()
        terminal.write(b"\x1b[?1u")
        terminal.write(b"\x1b_Gi=31;OK\x1b\\")
        terminal.write(b"\x1b[6;20;10t")
        terminal.write(b"\x1b[?62;1;6c")
        terminal.wait_for(b"READY")

        # An overlay that prints a mark, echoes what it reads and
        # waits. The command of a pane is split on whitespace, so it
        # goes into a file of its own.
        child = tmp / "overlay-child.py"
        child.write_text(OVERLAY_CHILD)
        opened = run_cli(
            terminal.sock_path,
            ["display-popup", "-T", "the title", "%s %s" % (sys.executable, child)],
        )
        assert opened.returncode == 0, opened.stderr

        terminal.wait_for(b"OVERLAYMARK")
        tail = terminal.since(0)
        assert b"the title" in tail, "the overlay drew no title bar"

        # The keyboard reaches the overlay, not the pane behind it.
        # One read of the overlay returns whatever has arrived, so two
        # letters in a row can come back in one block. The next letter
        # goes out only after the echo of the one before it, which
        # keeps every block a single letter.
        mark = terminal.mark()
        for letter in b"OVER":
            terminal.write(bytes([letter]))
            terminal.wait_for(b"[[%c]]" % letter)
        typed = terminal.since(mark)
        assert b"<<" not in typed, "the pane behind the overlay received the keys"

        # Closing it takes the program with it.
        closed = run_cli(terminal.sock_path, ["close-popup"])
        assert closed.returncode == 0, closed.stderr
        terminal.drain(1.5)

        quiet = terminal.mark()
        terminal.drain(2.0)
        after = terminal.since(quiet)
        assert (
            b"\x1b[?1049l" not in after
        ), "the client left the alternate screen after the overlay closed"
        assert len(after) < 200000, (
            "the client repainted without end after the overlay closed: %i bytes"
            % len(after)
        )
    except BaseException:
        terminal.report()
        raise
    finally:
        terminal.close()
    print("overlay pane: ok")


def check_two_terminals_of_different_abilities(tmp):
    """
    Two clients, two terminals that can do different things, one
    session.

    One speaks the kitty graphics protocol. The other answers nothing
    but the device attributes, so it draws the same image as half
    blocks. Each gets what its own terminal takes, and neither gets the
    other's.

    The keyboard goes the other way, and on purpose. Only what every
    attached client can report counts, so a plain terminal joining
    would take the key releases away from the pane. Synthesis puts them
    back: a pane keeps the flags it pushed, and a client that cannot
    send a release has one made for it. A pane is never told that a
    capability went away because somebody else attached.
    """
    terminal = Terminal(tmp, "kitty")
    second = None
    try:
        terminal.wait_for_the_queries()
        # This terminal serves every flag of the keyboard protocol, so
        # a release it sends is a real one.
        terminal.write(b"\x1b[?31u")
        terminal.write(b"\x1b_Gi=31;OK\x1b\\")
        terminal.write(b"\x1b[6;20;10t")
        terminal.write(b"\x1b[?62;1;6c")
        terminal.wait_for(b"READY")

        # The pane pushed flags 1 and 2, and this terminal serves both.
        terminal.write(ASK_THE_FLAGS)
        terminal.wait_for(flags_answer(3))

        # One client, and it reports a release itself. The pane hears
        # the press on its own.
        terminal.write(b"\x01")
        terminal.wait_for(CTRL_A_KITTY_HEX)
        terminal.drain(1.0)

        # A second terminal joins. It answers the device attributes and
        # nothing else: no kitty keyboard, no kitty graphics, no sixel.
        second = SecondClient(tmp, terminal.sock_path, "plain")
        # After the queries: the graphics query carries "\x1b_G" itself,
        # so a check that it never arrives has to start past it.
        after_queries = second.wait_for_the_queries()
        second.write(b"\x1b[?62;1;6c")
        second.drain(2.0)
        drawn = second.since(after_queries)

        # Each terminal drew the image the way it can.
        assert b"\x1b_Ga=" in terminal.seen, "the kitty client drew no image"
        assert b"READY" in second.seen, "the second client never drew the pane"
        assert UPPER_HALF.encode() in drawn or LOWER_HALF.encode() in drawn, (
            "the plain client drew no half blocks"
        )

        # And neither got what it cannot read.
        assert b"\x1b_Ga=" not in drawn, (
            "kitty graphics reached a terminal that never claimed them"
        )
        assert b"\x1bP0;" not in drawn, "sixel reached a terminal without it"

        # The pane keeps both flags. The client that cannot report a
        # release has one made for it, so nothing was taken away.
        terminal.write(ASK_THE_FLAGS)
        terminal.wait_for(flags_answer(3))

        # Both clients still work: a key from either reaches the pane,
        # in the encoding the pane asked for.
        #
        # It arrives as a press and a release together. Only what every
        # client can report counts, and the plain terminal reports no
        # release, so the release is made rather than taken away. The
        # pane reads every key going down and coming up; only the time
        # between them is lost.
        terminal.write(b"\x01")
        terminal.wait_for(CTRL_A_KITTY_PRESS_AND_RELEASE)
        second.write(b"\x01")
        second.wait_for(CTRL_A_KITTY_PRESS_AND_RELEASE)

        # The second client leaves. The made up release goes with the
        # client that needed it: the pane hears one press again.
        second.close()
        second = None
        terminal.drain(1.0)
        mark = terminal.mark()
        terminal.write(b"\x01")
        terminal.wait_for(CTRL_A_KITTY_HEX)
        assert CTRL_A_KITTY_PRESS_AND_RELEASE not in terminal.since(mark), (
            "the release is still made up after the plain client left"
        )

        # The server is whole: one session, one window, one pane.
        listed = run_cli(terminal.sock_path, ["list-panes", "-a", "-F", "#{pane_id}"])
        assert listed.returncode == 0, listed.stderr
        assert len(listed.stdout.split()) == 1, listed.stdout

    except BaseException:
        terminal.report()
        if second is not None:
            second.report()
        raise
    finally:
        if second is not None:
            second.close()
        terminal.close()
    print("two terminals: ok")


def check_libpymux(tmp):
    """
    libpymux against a server that is really there.

    The unit tests in `test_libpymux.py` answer with a socket that says
    what the test tells it to say. This one asks the real server, so it
    catches a format variable that went away, a command that changed its
    options, and output that does not parse.
    """
    from libpymux import CommandError, Server

    terminal = Terminal(tmp, "kitty")
    try:
        terminal.wait_for_the_queries()
        terminal.write(b"\x1b[?1u")
        terminal.write(b"\x1b_Gi=31;OK\x1b\\")
        terminal.write(b"\x1b[6;20;10t")
        terminal.write(b"\x1b[?62;1;6c")
        terminal.wait_for(b"READY")

        server = Server(str(terminal.sock_path))
        assert server.is_alive(), "the library cannot reach the server"

        # The session. A pymux server runs one.
        session = server.session
        assert session.name == "test", session.name
        assert session.id == "$0", session.id
        assert session.attached >= 1, session.attached
        assert len(server.sessions) == 1

        # The window and the pane that the session started with.
        windows = server.windows
        assert len(windows) == 1, windows
        window = windows[0]
        assert window.id.startswith("@"), window.id
        assert window.active, "the only window is not the active one"

        panes = server.panes
        assert len(panes) == 1, panes
        pane = panes[0]
        assert pane.id.startswith("%"), pane.id
        assert pane.pid > 0, pane.pid
        assert pane.width > 0 and pane.height > 0, (pane.width, pane.height)
        assert not pane.dead
        assert pane.window_id == window.id
        assert window.panes[0].id == pane.id
        assert window.active_pane.id == pane.id
        assert server.pane(pane.id).id == pane.id
        assert server.pane("%99999") is None

        # Every field the library asks for comes back with something in
        # it. A variable that the server dropped would be empty here.
        for name in ("pane_id", "pane_index", "window_id", "pane_width"):
            assert pane.get(name), "%s came back empty" % (name,)

        # A second pane, made through the library.
        # A python with no arguments waits on its stdin, so the pane
        # stays alive for the rest of this check.
        second = window.split(command=sys.executable)
        assert second is not None, "split-window said nothing"
        terminal.drain(2.0)
        assert len(server.panes) == 2, server.panes
        assert len(window.panes) == 2, window.panes
        assert second.id != pane.id

        # Reading a pane back.
        text = pane.capture()
        assert "READY" in text, text[:200]

        # The escape hatch, and the errors.
        assert server.cmd("list-windows").ok
        assert server.has_session()
        assert not server.has_session("not-a-session")
        try:
            server.cmd(["select-window", "-t", "99"])
        except CommandError as error:
            assert error.result.exit_code != 0
        else:
            raise AssertionError("a command that fails has to raise")

        # Killing the pane that the library made.
        second.kill()
        terminal.drain(2.0)
        assert len(server.panes) == 1, server.panes
    except BaseException:
        terminal.report()
        raise
    finally:
        terminal.close()
    print("libpymux: ok")


def main() -> None:
    # The server inherits this, and a pane must not: it names the
    # terminal that started the server, which is not what a pane is.
    os.environ["KITTY_WINDOW_ID"] = "1"
    os.environ["TERM_PROGRAM"] = "kitty"
    # And a pane takes 24 bit colour whatever this says.
    os.environ.pop("COLORTERM", None)

    tmp = Path(tempfile.mkdtemp(prefix="pymux-pty-test-"))
    check_kitty_terminal(tmp)
    check_sixel_terminal(tmp)
    check_colorterm_terminal(tmp)
    check_plain_terminal(tmp)
    check_a_closing_split(tmp)
    check_the_pointer_shape(tmp)
    check_an_overlay_pane(tmp)
    check_two_terminals_of_different_abilities(tmp)
    check_libpymux(tmp)
    print("All pty checks passed.")


if __name__ == "__main__":
    main()
