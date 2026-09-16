"""
The kitty keyboard flags the client puts on the outer terminal.

The protocol holds these on a stack, and the point of the stack is the
program after us: a shell that pushed its own flags, most of all a
nested pymux. So the first enable pushes, a later change sets over our
own push, and the way out pops -- never a set back to zero, which
would leave the state beneath ours gone.
Lillecarl/pymux#403.
"""

import json
import os

from pymux.client.terminal import TerminalClient


class Recorder(TerminalClient):
    "A client that answers for the transport and records what it wrote."

    def __init__(self):
        super().__init__()
        self.sent = []

    def _send_packet(self, data):
        self.sent.append(data)


def _writes(monkeypatch):
    "What the client wrote to the outer terminal, one entry per call."
    written = []
    monkeypatch.setattr(os, "write", lambda fd, data: written.append(data))
    return written


def _process(client, **data):
    "One packet from the server, the way the read loop unpacks it."
    client._process(json.dumps({"cmd": "kitty-keyboard", "data": data}).encode())


def test_the_first_enable_is_a_push(monkeypatch):
    written = _writes(monkeypatch)
    client = Recorder()
    client._kitty_supported = True

    _process(client, flags=1)

    assert written == [b"\x1b[>1u"]
    assert client._kitty_flags == 1


def test_a_later_change_is_a_set_over_our_push(monkeypatch):
    written = _writes(monkeypatch)
    client = Recorder()
    client._kitty_supported = True

    _process(client, flags=1)
    _process(client, flags=8)

    assert written == [b"\x1b[>1u", b"\x1b[=8;1u"]


def test_the_way_out_pops_what_we_pushed(monkeypatch):
    written = _writes(monkeypatch)
    client = Recorder()
    client._kitty_supported = True

    _process(client, flags=1)
    client._pop_kitty_flags()

    assert written[-1] == b"\x1b[<1u"
    assert client._kitty_flags is None


def test_a_second_pop_writes_nothing(monkeypatch):
    written = _writes(monkeypatch)
    client = Recorder()
    client._kitty_supported = True

    _process(client, flags=1)
    client._pop_kitty_flags()
    written.clear()
    client._pop_kitty_flags()

    assert written == []


def test_zero_before_any_push_writes_nothing(monkeypatch):
    written = _writes(monkeypatch)
    client = Recorder()
    client._kitty_supported = True

    _process(client, flags=0)

    assert written == []
    assert client._kitty_flags is None


def test_an_unsupported_terminal_enables_nothing(monkeypatch):
    written = _writes(monkeypatch)
    client = Recorder()

    _process(client, supported=False, flags=1)
    client._pop_kitty_flags()

    assert written == []
    assert client._kitty_flags is None
