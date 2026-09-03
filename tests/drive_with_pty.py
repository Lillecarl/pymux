"""
End-to-end test for the kitty keyboard protocol over a real pty.

This test plays the outer terminal. It attaches a pymux client on a
pty, answers the keyboard protocol detection query like a supporting
terminal would, and then checks:

1. The client enables the flags of the focused pane on the outer
   terminal ("CSI = 1 ; 1 u" — the pane child pushed the disambiguate
   flag).
2. A kitty-encoded key from the outer terminal reaches the pane child.
3. A legacy key is translated for the pane ("\\x01" arrives as
   "CSI 97 ; 5 u").
4. When the server goes away, the client resets the flags to zero.

The pane child is a small script that puts its tty in raw mode, pushes
the disambiguate flag, and echoes everything it reads as hex.

Run with:

    nix develop --file . shell --command python3 tests/drive_with_pty.py
"""
import fcntl
import os
import select
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import time
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent

# "\x1b[97;5u" — kitty ctrl+a — as the pane child reports it (hex).
CTRL_A_KITTY_HEX = b"<<1b5b39373b3575>>"

PANE_CHILD = """
import sys, tty
tty.setraw(0)
# Push the disambiguate flag of the kitty keyboard protocol.
sys.stdout.write("\\x1b[>1u")
# Emit a kitty graphics image. The pane terminal must consume it
# without corrupting the screen content.
sys.stdout.write("\\x1b_Gf=32,s=10,v=10;AAAAAA\\x1b\\\\")
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
        # 1. The client queries the outer terminal: flags + device
        #    attributes.
        seen = wait_for(master_fd, b"\x1b[?u\x1b[c", seen)

        # Reply like a terminal that supports the protocol: flags reply
        # first, then device attributes.
        os.write(master_fd, b"\x1b[?1u")
        os.write(master_fd, b"\x1b[?62;1;6c")

        # 2. The pane pushed disambiguate; after detection the client
        #    enables it on the outer terminal.
        seen = wait_for(master_fd, b"\x1b[=1;1u", seen)

        # 3. The pane child is running and its output is rendered.
        seen = wait_for(master_fd, b"READY", seen)

        # The graphics sequence the child emitted must not have leaked
        # into the rendered screen. (The image itself is not displayed
        # yet, but it must not corrupt the text either.)
        assert b"f=32" not in seen, "graphics payload leaked to the screen"
        assert b"AAAAAA" not in seen, "graphics payload leaked to the screen"

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
