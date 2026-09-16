"""
The way out of the client's read loop owns the teardown, once.

A crash out of the loop used to restore raw mode and the kitty push
and nothing else: the alternate screen, the mouse reporting and a
hidden cursor stayed as the server's last bytes had set them. The
`finally` resets now, on every exit -- EOF, detach, exception -- and a
reset that cannot write (stdout is the thing that failed) must not
replace the error that ended the loop. Lillecarl/pymux#404.
"""

import os
import sys

import pytest

from pymux.client.posix import PosixClient
from pymux.client.terminal import TerminalClient


class FakeSocket:
    "A socket the loop can reach EOF on, and nothing more."

    def setblocking(self, on):
        pass

    def send(self, data):
        pass

    def recv(self, amount):
        return b""

    def fileno(self):
        return 7


class Recording(PosixClient):
    "The client, with the terminal reset made countable."

    def __init__(self):
        TerminalClient.__init__(self)
        self.socket = FakeSocket()
        self.resets = 0
        self.broken_reset = False

    def _reset_terminal(self):
        self.resets += 1
        if self.broken_reset:
            raise OSError("the terminal is the thing that failed")

    def _send_size(self):
        pass  # A size asks the real terminal; there is none here.


@pytest.fixture
def client(monkeypatch):
    "A client whose loop runs without a terminal, stdout swallowed."
    import contextlib

    monkeypatch.setattr("pymux.client.posix.raw_mode", lambda fd: contextlib.nullcontext())
    monkeypatch.setattr(sys, "stdin", open(os.devnull, "rb"))
    monkeypatch.setattr("pymux.client.posix.select", lambda fds, *a: ([7], [], []))
    monkeypatch.setattr(os, "write", lambda fd, data: None)

    yield Recording()


def test_eof_resets_once(client):
    client.attach()

    assert client.resets == 1


def test_a_crash_resets_too(client, monkeypatch):
    def ends_the_loop(*a):
        raise RuntimeError("the loop's own way out")

    monkeypatch.setattr("pymux.client.posix.select", ends_the_loop)

    with pytest.raises(RuntimeError):
        client.attach()

    assert client.resets == 1


def test_a_reset_that_cannot_write_loses_to_the_real_error(client, monkeypatch):
    client.broken_reset = True

    def ends_the_loop(*a):
        raise RuntimeError("the loop's own way out")

    monkeypatch.setattr("pymux.client.posix.select", ends_the_loop)

    with pytest.raises(RuntimeError):
        client.attach()

    assert client.resets == 1
