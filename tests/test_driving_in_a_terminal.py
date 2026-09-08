"""
The relay that lets a picture of a terminal be a picture of pymux.

`tests/drive_in_a_terminal.py` runs as a terminal's child, runs a
program on a pty of its own, copies what the program writes to the
terminal's tty, and writes scripted keys into the pty. That is how a
check presses a key at pymux: a headless compositor owns no input
device, and the terminal's own pty belongs to the terminal.
Lillecarl/pymux#161.

**These tests need no display.** The relay's job is bytes in one
direction and keys in the other, and a pty of the test's own is a
terminal as far as it is concerned. The picture check is what needs a
compositor, and this is the part of it that can be judged without one.
"""

import os
import pty
import select
import subprocess
import sys
import termios
import time
from fcntl import ioctl
from struct import pack

import pytest

RELAY = os.path.join(os.path.dirname(__file__), "drive_in_a_terminal.py")

#: Long enough for a program to start and answer on a loaded machine,
#: short enough that a test that goes wrong does not hold the suite.
HOLD = 5.0
PATIENCE = 20.0


def a_terminal(rows=24, columns=80):
    "A pty, sized, for the relay to run in."
    master, slave = pty.openpty()
    ioctl(slave, termios.TIOCSWINSZ, pack("HHHH", rows, columns, 0, 0))
    return master, slave


def run(keys, argv, rows=24, columns=80, hold=HOLD, wanted=b""):
    """
    Run the relay in a pty and give back everything it wrote.

    It stops as soon as `wanted` has arrived, so a test that passes
    does not wait out the hold.
    """
    master, slave = a_terminal(rows, columns)
    process = subprocess.Popen(
        [sys.executable, RELAY, str(keys), str(hold), "--"] + argv,
        stdin=slave,
        stdout=slave,
        stderr=subprocess.PIPE,
    )
    os.close(slave)

    seen = b""
    deadline = time.monotonic() + PATIENCE
    try:
        while time.monotonic() < deadline:
            if wanted and wanted in seen:
                break
            if not select.select([master], [], [], 0.2)[0]:
                if process.poll() is not None:
                    break
                continue
            try:
                piece = os.read(master, 65536)
            except OSError:
                break
            if not piece:
                break
            seen += piece
    finally:
        process.kill()
        error = process.communicate()[1]
        os.close(master)

    return seen, error


def a_keys_file(tmp_path, text):
    path = tmp_path / "keys"
    path.write_text(text)
    return path


# ----------------------------------------------------------------------
# Keys reach the program.


def test_the_keys_reach_the_program(tmp_path):
    "Which is the whole reason this exists."
    keys = a_keys_file(tmp_path, '0.2 b"hello\\n"\n')

    seen, _ = run(
        keys,
        ["sh", "-c", "stty -echo; read x; printf 'got:%s.' \"$x\"; exec sleep 30"],
        wanted=b"got:hello.",
    )

    assert b"got:hello." in seen


def test_the_steps_happen_in_order(tmp_path):
    keys = a_keys_file(tmp_path, '0.1 b"one\\n"\n0.1 b"two\\n"\n')

    seen, _ = run(
        keys,
        [
            "sh",
            "-c",
            'stty -echo; read a; read b; printf \'%s-%s.\' "$a" "$b"; exec sleep 30',
        ],
        wanted=b"one-two.",
    )

    assert b"one-two." in seen


# ----------------------------------------------------------------------
# The program's own bytes come back.


def test_what_the_program_writes_reaches_the_terminal(tmp_path):
    keys = a_keys_file(tmp_path, "")

    seen, _ = run(
        keys,
        ["sh", "-c", "stty -echo; printf 'drawn.'; exec sleep 30"],
        wanted=b"drawn.",
    )

    assert b"drawn." in seen


def test_the_program_is_given_the_size_of_the_terminal(tmp_path):
    """
    A program lays its screen out for the size it is told. The wrong
    size is a picture of the right program at the wrong shape, which
    reads as a drawing fault.
    """
    keys = a_keys_file(tmp_path, "")

    seen, _ = run(
        keys,
        ["sh", "-c", "stty -echo; stty size; exec sleep 30"],
        rows=30,
        columns=100,
        wanted=b"30 100",
    )

    assert b"30 100" in seen


# ----------------------------------------------------------------------
# A keys file that does not parse.


@pytest.mark.parametrize(
    "text",
    [
        "notanumber b'x'\n",
        "0.1\n",
        "0.1 b'unclosed\n",
        "0.1 'a string and not bytes'\n",
    ],
)
def test_a_keys_file_that_does_not_parse_is_a_fault(tmp_path, text):
    """
    Never something to skip over. A script whose keys quietly did
    nothing would photograph the screen before them, which reads as a
    pass.
    """
    keys = a_keys_file(tmp_path, text)

    done = subprocess.run(
        [sys.executable, RELAY, str(keys), "1", "--", "true"],
        capture_output=True,
    )

    assert done.returncode != 0
    assert str(keys).encode() in done.stderr


def test_a_comment_and_a_blank_line_are_nothing(tmp_path):
    keys = a_keys_file(tmp_path, "# a comment\n\n0.1 b'x'  # and one here\n")

    from drive_in_a_terminal import read_the_keys

    assert read_the_keys(str(keys)) == [(0.1, b"x")]


# ----------------------------------------------------------------------
# How it ends.


def test_it_asks_for_a_program_to_run(tmp_path):
    keys = a_keys_file(tmp_path, "")

    done = subprocess.run([sys.executable, RELAY, str(keys), "1"], capture_output=True)

    assert done.returncode != 0


def test_the_hold_is_a_bound(tmp_path):
    """
    A program that is still up when the hold runs out is the normal
    case: a picture is taken while it is on the screen. The relay ends
    anyway, so a run that goes wrong leaves nothing behind for longer.
    """
    keys = a_keys_file(tmp_path, "")

    started = time.monotonic()
    done = subprocess.run(
        [sys.executable, RELAY, str(keys), "0.5", "--", "sleep", "30"],
        capture_output=True,
        timeout=PATIENCE,
    )

    assert done.returncode == 0
    assert time.monotonic() - started < PATIENCE
