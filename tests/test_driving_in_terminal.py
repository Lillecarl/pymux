"""
The relay that lets a picture of a terminal be a picture of pymux.

`tests/drive_in_terminal.py` runs as a terminal's child, runs a
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
import shlex
import subprocess
import sys
import termios
import time
from fcntl import ioctl
from struct import pack

import pytest

RELAY = os.path.join(os.path.dirname(__file__), "drive_in_terminal.py")

#: Long enough for a program to start and answer on a loaded machine,
#: short enough that a test that goes wrong does not hold the suite.
HOLD = 5.0
PATIENCE = 20.0


def create_terminal(rows=24, columns=80):
    "A pty, sized, for the relay to run in."
    master, slave = pty.openpty()
    ioctl(slave, termios.TIOCSWINSZ, pack("HHHH", rows, columns, 0, 0))
    return master, slave


def run(keys, argv, rows=24, columns=80, hold=HOLD, wanted=b"", answer=b""):
    """
    Run the relay in a pty and give back everything it wrote.

    It stops as soon as `wanted` has arrived, so a test that passes
    does not wait out the hold.

    `answer` is what this terminal says back, written once the relay
    has drawn something: it stands for the reply a real terminal sends
    when the program asks it a question. Lillecarl/pymux#350.
    """
    master, slave = create_terminal(rows, columns)
    process = subprocess.Popen(
        [sys.executable, RELAY, str(keys), str(hold), "--"] + argv,
        stdin=slave,
        stdout=slave,
        stderr=subprocess.PIPE,
    )
    os.close(slave)

    seen = b""
    said = not answer
    deadline = time.monotonic() + PATIENCE
    try:
        while time.monotonic() < deadline:
            if not said and seen:
                # The relay has the terminal in hand by now: it has
                # taken the echo off and is watching this side.
                os.write(master, answer)
                said = True
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


def create_keys_file(tmp_path, text):
    path = tmp_path / "keys"
    path.write_text(text)
    return path


# ----------------------------------------------------------------------
# Keys reach the program.


def test_keys_reach_program(tmp_path):
    "Which is the whole reason this exists."
    keys = create_keys_file(tmp_path, '0.2 b"hello\\n"\n')

    seen, _ = run(
        keys,
        ["sh", "-c", "stty -echo; read x; printf 'got:%s.' \"$x\"; exec sleep 30"],
        wanted=b"got:hello.",
    )

    assert b"got:hello." in seen


def test_steps_happen_in_order(tmp_path):
    keys = create_keys_file(tmp_path, '0.1 b"one\\n"\n0.1 b"two\\n"\n')

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


def test_what_the_terminal_answers_reaches_the_program(tmp_path):
    """
    A program asks its terminal what it draws with, and the answer
    arrives on this process's stdin rather than the program's. Nothing
    read it before, so pymux in a picture heard nothing back from a
    terminal that had answered. Lillecarl/pymux#350.
    """
    keys = create_keys_file(tmp_path, "")

    seen, error = run(
        keys,
        [
            "sh",
            "-c",
            # It draws first, so the answer is written to a relay that
            # is up and watching rather than to one still starting.
            # `read -r`, because an answer holds a backslash and `read`
            # without it reads that as "the line goes on".
            "stty -echo; printf 'asking.'; read -r x;"
            " printf 'answered:%s.' \"$x\"; exec sleep 30",
        ],
        wanted=b"answered:",
        answer=b"\x1b]11;rgb:1d1d/2020/2121\x1b\\\n",
    )

    assert b"answered:" in seen, error
    assert b"rgb:1d1d/2020/2121" in seen


# ----------------------------------------------------------------------
# The program's own bytes come back.


def test_what_program_writes_reaches_terminal(tmp_path):
    keys = create_keys_file(tmp_path, "")

    seen, _ = run(
        keys,
        ["sh", "-c", "stty -echo; printf 'drawn.'; exec sleep 30"],
        wanted=b"drawn.",
    )

    assert b"drawn." in seen


def test_program_is_given_size_of_terminal(tmp_path):
    """
    A program lays its screen out for the size it is told. The wrong
    size is a picture of the right program at the wrong shape, which
    reads as a drawing fault.
    """
    keys = create_keys_file(tmp_path, "")

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
def test_keys_file_that_does_not_parse_is_fault(tmp_path, text):
    """
    Never something to skip over. A script whose keys quietly did
    nothing would photograph the screen before them, which reads as a
    pass.
    """
    keys = create_keys_file(tmp_path, text)

    done = subprocess.run(
        [sys.executable, RELAY, str(keys), "1", "--", "true"],
        capture_output=True,
    )

    assert done.returncode != 0
    assert str(keys).encode() in done.stderr


def test_comment_and_blank_line_are_nothing(tmp_path):
    keys = create_keys_file(tmp_path, "# a comment\n\n0.1 b'x'  # and one here\n")

    from drive_in_terminal import read_keys

    assert read_keys(str(keys)) == [(0.1, b"x")]


# ----------------------------------------------------------------------
# How it ends.


def test_it_asks_for_program_to_run(tmp_path):
    keys = create_keys_file(tmp_path, "")

    done = subprocess.run([sys.executable, RELAY, str(keys), "1"], capture_output=True)

    assert done.returncode != 0


# ----------------------------------------------------------------------
# The fence.


def run_fenced(tmp_path, keys_text, pane):
    """
    Run the relay with the fence args, and give back what it copied,
    the fence file, and the relay's stderr.

    The pane is the body of the `sh -c` that runs in it. The relay
    stops as soon as the fence file is there, so a test that passes
    does not wait out the hold.
    """
    from middleman import FORWARDER

    forwarder = tmp_path / "forwarder.py"
    forwarder.write_text(FORWARDER)

    fifo = tmp_path / "payload.fifo"
    os.mkfifo(fifo)
    fence_seen = tmp_path / "fence"
    keys = create_keys_file(tmp_path, keys_text)

    process = subprocess.Popen(
        [
            sys.executable,
            RELAY,
            str(keys),
            str(HOLD),
            str(fifo),
            str(fence_seen),
            "--",
            "sh",
            "-c",
            pane
            % (
                shlex.quote(str(forwarder)),
                shlex.quote(str(fifo)),
                shlex.quote(str(tmp_path / "pane-size")),
            ),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    seen = b""
    deadline = time.monotonic() + PATIENCE
    try:
        while time.monotonic() < deadline and not fence_seen.exists():
            fd = process.stdout.fileno()
            if not select.select([fd], [], [], 0.2)[0]:
                if process.poll() is not None:
                    break
                continue
            piece = os.read(fd, 65536)
            if not piece:
                break
            seen += piece
    finally:
        process.kill()
        error = process.communicate()[1]

    return seen, fence_seen, error


def test_fence_comes_back_and_is_taken_out(tmp_path):
    """
    The fence proves the keys were consumed, and the terminal never
    sees it: what the relay copies is the frame, and not the
    scaffolding. Lillecarl/pymux#275.
    """
    pane = (
        "stty -echo; printf 'up.'; (sleep 0.3; printf 'framed.') &"
        " exec python3 %s %s %s"
    )
    seen, fence_seen, error = run_fenced(tmp_path, '0.1 b"hello\\n"\n', pane)

    assert fence_seen.exists()
    assert b"up." in seen
    assert b"framed." in seen
    assert b"52;" not in seen
    assert b"ZmVuY2U" not in seen


def test_the_timeline_says_what_happened_and_when(tmp_path):
    """
    A run that photographed the wrong state leaves this file and the
    program's own log beside it, and the two read together say which
    key the program never acted on. It was empty before, and a lost
    key was a guess. Lillecarl/pymux#353.
    """
    pane = (
        "stty -echo; printf 'up.'; (sleep 0.3; printf 'framed.') &"
        " exec python3 %s %s %s"
    )
    _, fence_seen, error = run_fenced(tmp_path, '0.1 b"hello\\n"\n', pane)

    assert fence_seen.exists()
    assert b"the first frame" in error, error
    assert b"key b'hello\\n'" in error, error
    assert b"the fence came" in error, error


def test_pane_that_never_takes_fifo_is_fault(tmp_path):
    """
    A pymux that never starts its pane is a run that photographs
    nothing, and this is where it says so instead of blocking.
    """
    keys = create_keys_file(tmp_path, "")
    fifo = tmp_path / "payload.fifo"
    os.mkfifo(fifo)
    fence_seen = tmp_path / "fence"

    done = subprocess.run(
        [
            sys.executable,
            RELAY,
            str(keys),
            "1",
            str(fifo),
            str(fence_seen),
            "--",
            "sleep",
            "30",
        ],
        capture_output=True,
        timeout=PATIENCE,
    )

    assert done.returncode != 0
    assert b"never took" in done.stderr
    assert not fence_seen.exists()


def test_hold_is_bound(tmp_path):
    """
    A program that is still up when the hold runs out is the normal
    case: a picture is taken while it is on the screen. The relay ends
    anyway, so a run that goes wrong leaves nothing behind for longer.
    """
    keys = create_keys_file(tmp_path, "")

    started = time.monotonic()
    done = subprocess.run(
        [sys.executable, RELAY, str(keys), "0.5", "--", "sleep", "30"],
        capture_output=True,
        timeout=PATIENCE,
    )

    assert done.returncode == 0
    assert time.monotonic() - started < PATIENCE
