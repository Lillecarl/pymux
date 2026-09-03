"""
End-to-end test for the kitty protocols over a real pty.

This test plays the outer terminal. It attaches a pymux client on a
pty, answers the detection queries like a supporting terminal would,
and then checks:

1. The client enables the flags of the focused pane on the outer
   terminal ("CSI = 1 ; 1 u" — the pane child pushed the disambiguate
   flag).
2. A kitty-encoded key from the outer terminal reaches the pane child.
3. A legacy key is translated for the pane ("\\x01" arrives as
   "CSI 97 ; 5 u").
4. The image that the pane child transmits reaches the outer terminal:
   the raw sequence never lands on the screen as text, the server
   re-transmits the pixel data under its own image id, and it puts the
   image at the cell of the pane.
5. When the server goes away, the client resets the flags to zero.

The pane child is a small script that puts its tty in raw mode, pushes
the disambiguate flag, transmits a small image and echoes everything it
reads as hex.

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

from pymux.graphics import QUERY_SEQUENCE as GRAPHICS_QUERY  # noqa: E402

REPO_ROOT = Path(__file__).parent.parent

# "\x1b[97;5u" — kitty ctrl+a — as the pane child reports it (hex).
CTRL_A_KITTY_HEX = b"<<1b5b39373b3575>>"

# The image that the pane child transmits: 2x2 pixels, RGB.
IMAGE_PAYLOAD = "AAECAwQFBgcICQoL"

PANE_CHILD = """
import sys, tty
tty.setraw(0)
# Push the disambiguate flag of the kitty keyboard protocol.
sys.stdout.write("\\x1b[>1u")
# Transmit a kitty graphics image and place it at the cursor. The
# pane terminal must consume the sequence without corrupting the
# screen, store the image, and place it.
sys.stdout.write("\\x1b_Ga=T,f=24,s=2,v=2,i=7,c=3,r=2,C=1;AAECAwQFBgcICQoL\\x1b\\\\")
sys.stdout.write("READY")
sys.stdout.flush()
while True:
    data = sys.stdin.buffer.read1(256)
    if not data:
        break
    sys.stdout.write("<<%s>>" % data.hex())
    sys.stdout.flush()
"""


def wait_for(master_fd, pattern, seen, timeout=10.0):
    """
    Read from the pty master until `pattern` arrives. `seen` accumulates
    everything that was ever read. Returns `seen`.
    """
    deadline = time.time() + timeout
    while pattern not in seen:
        if time.time() > deadline:
            raise AssertionError(
                "Timeout waiting for %r. Got: %r" % (pattern, seen[-2000:])
            )
        r, _, _ = select.select([master_fd], [], [], 0.1)
        if r:
            seen += os.read(master_fd, 65536)
    return seen


def run_cli(sock_path, args):
    "Run a pymux CLI command against the server (like libtmux does)."
    return subprocess.run(
        [sys.executable, "-m", "pymux", "-S", sock_path] + args,
        cwd=str(REPO_ROOT),
        capture_output=True,
        timeout=20,
    )


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="pymux-pty-test-"))
    sock_path = tmp / "test.sock"
    client_stderr_path = tmp / "client-stderr.log"

    pane_child_path = tmp / "pane_child.py"
    pane_child_path.write_text(PANE_CHILD)

    # The pane child.
    child_cli = run_cli(
        sock_path,
        ["new-session", "-d", "-s", "test", "python3 %s" % pane_child_path],
    )
    assert child_cli.returncode == 0, child_cli.stderr

    # The outer terminal: a pty pair. The client runs on the slave side.
    master_fd, slave_fd = os.openpty()
    fcntl.ioctl(
        slave_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0)
    )

    client_stderr = open(client_stderr_path, "wb")
    client = subprocess.Popen(
        [sys.executable, "-m", "pymux", "-S", str(sock_path), "attach"],
        cwd=str(REPO_ROOT),
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=client_stderr,
        env={
            **os.environ,
            "TERM": "xterm-256color",
            "LANG": "C.UTF-8",
        },
    )
    os.close(slave_fd)

    seen = b""
    try:
        # 1. The client queries the outer terminal: keyboard flags,
        #    graphics support and device attributes.
        seen = wait_for(master_fd, GRAPHICS_QUERY.encode(), seen)
        seen = wait_for(master_fd, b"\x1b[c", seen)

        # Reply like a terminal that supports both protocols: flags
        # reply, graphics reply, then device attributes.
        os.write(master_fd, b"\x1b[?1u")
        os.write(master_fd, b"\x1b_Gi=31;OK\x1b\\")
        os.write(master_fd, b"\x1b[?62;1;6c")

        # 2. The pane pushed disambiguate; after detection the client
        #    enables it on the outer terminal.
        seen = wait_for(master_fd, b"\x1b[=1;1u", seen)

        # 3. The pane child is running and its output is rendered.
        seen = wait_for(master_fd, b"READY", seen)

        # The graphics sequence the child emitted must not have leaked
        # into the rendered screen as text.
        assert b"a=T" not in seen, "graphics command leaked to the screen"
        assert (
            IMAGE_PAYLOAD.encode() not in seen
        ), "graphics payload leaked to the screen"

        # The server re-transmits the image under its own id and puts
        # it on the outer terminal.
        seen = wait_for(master_fd, b"\x1b_Ga=t,i=", seen)
        seen = wait_for(master_fd, b"a=p,i=", seen)
        transmit = re.search(rb"\x1b_Ga=t,i=(\d+),t=d,q=2,f=24,s=2,v=2,o=z", seen)
        assert transmit, "no image transmission on the outer terminal"
        outer_id = transmit.group(1)
        put = re.search(
            rb"\x1b\[(\d+);(\d+)H\x1b_Ga=p,i=" + outer_id + rb",p=1,c=3,r=2,C=1,q=2",
            seen,
        )
        assert put, "no placement on the outer terminal: %r" % seen[-2000:]
        # The pane starts on the second row (the window titlebar takes
        # the first one) and the child wrote nothing before the image.
        assert (int(put.group(1)), int(put.group(2))) == (2, 1)

        # 4. A kitty-encoded key from the outer terminal reaches the
        #    pane (translated to the same encoding for the pane).
        os.write(master_fd, b"\x1b[97;5u")
        seen = wait_for(master_fd, CTRL_A_KITTY_HEX, seen)

        # 5. A legacy ctrl+a is translated for the pane as well.
        os.write(master_fd, b"\x01")
        seen = wait_for(master_fd, CTRL_A_KITTY_HEX, seen)

        # 6. The server goes away: the client resets the flags.
        run_cli(sock_path, ["kill-server"])
        seen = wait_for(master_fd, b"\x1b[=0;1u", seen)
    except AssertionError:
        print(seen[-4000:].decode("utf-8", "replace"))
        if client_stderr_path.exists():
            print(
                "--- client stderr ---\n"
                + client_stderr_path.read_text(errors="replace")
            )
        raise
    finally:
        if client.poll() is None:
            client.send_signal(signal.SIGTERM)
        try:
            client.wait(timeout=5)
        except subprocess.TimeoutExpired:
            client.kill()
        client_stderr.close()
        os.close(master_fd)

        # Give the server a moment, then make sure it is gone.
        time.sleep(0.5)
        subprocess.run(
            [sys.executable, "-m", "pymux", "-S", str(sock_path), "kill-server"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            timeout=10,
        )

    client.wait(timeout=5)
    print("All pty checks passed.")


if __name__ == "__main__":
    main()
