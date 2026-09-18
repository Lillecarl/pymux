"""
`Pane.settle` catches the frame that follows a fence.

`middleman.py` puts bytes on the screen of a pane and gives back what
pymux wrote because of them. The fence proves the pane *consumed* the
bytes; the frame that pymux draws because of them can follow it,
because prompt_toolkit postpones a redraw. `settle` is what waits for
that frame, and every suite that judges our wire reads what it caught:
the Alacritty references, the libvterm files, the pictures.

These need no pymux. `settle` reads one file descriptor and appends to
`terminal.seen`, so a pipe and a stub say everything about it, in
milliseconds rather than in a suite.
"""

import os
import threading
import time

import pytest

from middleman import FIRST_BYTE, QUIET, Pane


class _Terminal:
    "As much of `drive_with_pty.Terminal` as `settle` touches."

    def __init__(self, master_fd: int) -> None:
        self.master_fd = master_fd
        self.seen = b""


@pytest.fixture
def a_pane(tmp_path):
    "A pane that reads one end of a pipe, and the other end to write."
    reader, writer = os.pipe()
    pane = Pane(tmp_path, "settle", 24, 80)
    pane.terminal = _Terminal(reader)
    try:
        yield pane, writer
    finally:
        os.close(reader)
        os.close(writer)


def test_a_frame_that_starts_late_is_still_caught(a_pane):
    """
    `FIRST_BYTE` is how long a frame may take to start, and `QUIET` is
    how long a gap inside one may be. Selecting on `QUIET` before the
    first byte spends the shorter of the two on the longer wait, so a
    frame that started between them was lost: `settle` came back having
    read nothing and the caller kept the bytes from before it.

    Measured on an idle machine, through the `colored_reset` recording
    of the Alacritty references: pymux took 0.114s to start the frame,
    `settle` gave up at 0.053s with nothing, and the judge read a wire
    of 551 bytes where the whole exchange is 9115. Lillecarl/pymux#425.
    """
    pane, writer = a_pane

    # Between the two waits, which is exactly where the frame was lost.
    late = (QUIET + FIRST_BYTE) / 2
    assert QUIET < late < FIRST_BYTE

    timer = threading.Timer(late, lambda: os.write(writer, b"the frame"))
    timer.start()
    try:
        pane.settle()
    finally:
        timer.cancel()

    assert pane.terminal.seen == b"the frame"


def test_a_gap_inside_a_frame_is_waited_through(a_pane):
    "A frame arrives in pieces, and a gap shorter than `QUIET` is one frame."
    pane, writer = a_pane
    os.write(writer, b"first")
    timer = threading.Timer(QUIET / 2, lambda: os.write(writer, b"second"))
    timer.start()
    try:
        pane.settle()
    finally:
        timer.cancel()

    assert pane.terminal.seen == b"firstsecond"


def test_a_write_that_draws_nothing_gives_up(a_pane):
    """
    A write that changes no cell makes no frame, and this is the one
    wait that is paid on every write. So it has to end, and `FIRST_BYTE`
    is the bound.
    """
    pane, _writer = a_pane

    started = time.monotonic()
    pane.settle()
    took = time.monotonic() - started

    assert pane.terminal.seen == b""
    assert FIRST_BYTE <= took < FIRST_BYTE * 3
