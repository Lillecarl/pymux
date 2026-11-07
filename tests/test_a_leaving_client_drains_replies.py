"""
A client that leaves reads away the terminal's answers to what it asked,
so they do not reach the shell as typed text. Lillecarl/pyterm#960.

The terminal here is the master side of a pty, and the client reads the
slave side in raw mode, as an attached client does.
"""

from __future__ import annotations

import os
import pty
import select
import threading
import time
import tty

import pytest

from pymux.client.terminal import STATUS_OK, STATUS_QUERY, drain_replies

#: What kitty answered on Carl's MacBook, cut short: the keyboard flags
#: and the cell size, then DA1, which closes the detection.
EARLY = b"\x1b[?31u\x1b[6;40;21t"
LATE = b"\x1b[?62;52;c"


@pytest.fixture
def terminal():
    "The terminal's end and the client's end of a pty, the client's raw."
    master, slave = pty.openpty()
    tty.setraw(slave)
    try:
        yield master, slave
    finally:
        os.close(master)
        os.close(slave)


def waiting(fd: int) -> bytes:
    "What the fd holds to read now."
    got = b""
    while select.select([fd], [], [], 0.05)[0]:
        got += os.read(fd, 4096)
    return got


def answer_after_the_question(master: int, then: bytes) -> threading.Thread:
    "A terminal that answers `then` once it reads the client's last question."

    def answer() -> None:
        heard = b""
        deadline = time.monotonic() + 2
        while STATUS_QUERY not in heard and time.monotonic() < deadline:
            if select.select([master], [], [], 0.05)[0]:
                heard += os.read(master, 4096)
        os.write(master, then)

    thread = threading.Thread(target=answer, daemon=True)
    thread.start()
    return thread


def test_the_answers_before_the_last_one_are_read_away(terminal):
    master, slave = terminal
    os.write(master, EARLY)
    thread = answer_after_the_question(master, LATE + STATUS_OK)

    drain_replies(slave, lambda data: os.write(slave, data), within=2)
    thread.join(2)

    assert waiting(slave) == b"", "nothing is left for the shell"


def test_a_terminal_that_never_answers_costs_the_deadline(terminal):
    master, slave = terminal
    os.write(master, EARLY)
    started = time.monotonic()

    drain_replies(slave, lambda data: os.write(slave, data), within=0.2)

    assert time.monotonic() - started < 1
    assert waiting(slave) == b"", "the answers that came are read away all the same"


def test_a_client_without_a_terminal_asks_nothing():
    read, write = os.pipe()
    asked = []
    try:
        drain_replies(read, asked.append)
    finally:
        os.close(read)
        os.close(write)
    assert asked == []
