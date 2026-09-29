"""
What a client reads when the server will not have it.

`pymux integrated` holds the server and its one terminal in one
process, so a second terminal cannot attach over the socket and the
server says so: "This pymux runs in one terminal, and that terminal is
taken." Lillecarl/pymux#159.

**The sentence has to reach the person.**
`ServerConnection._refuse_the_attach` awaits both writes before it
closes, so the reason is on its way. The client is still in its
handshake at that moment, and its next packet meets a socket with no
reader. That raised, so the person read "BrokenPipeError: [Errno 32]
Broken pipe" instead -- on a loaded machine, where the close wins the
race. It also took `check_second_terminal` down with it, and a red run
of a check stays in the store until somebody deletes it by hand.
Lillecarl/pymux#479.
"""

import json
import socket

import pytest

from pymux.client.posix import PosixClient
from pymux.protocol import Packet
from pymux.server import CANNOT_ATTACH


@pytest.fixture
def refused():
    """
    A client whose server wrote the refusal and closed, as the server
    really does it: the reason, the code to leave with, then the close.
    """
    here, there = socket.socketpair()
    client = PosixClient.__new__(PosixClient)
    client.socket = here
    client.exit_code = 0

    for packet in (
        {"cmd": Packet.OUT, "data": CANNOT_ATTACH},
        {"cmd": Packet.EXIT, "code": 1},
    ):
        there.sendall(json.dumps(packet).encode("utf-8") + b"\0")
    there.close()

    try:
        yield client
    finally:
        here.close()


def test_the_next_packet_of_the_handshake_does_not_raise(refused):
    "`_start_gui` sends one more after the server has already gone."
    refused._send_packet({"cmd": Packet.KITTY_DETECT})


def test_the_reason_is_still_there_to_read(refused):
    """
    The whole point. What the server wrote before it closed waits in the
    buffer on this side, and a client that died sending never read it.
    """
    refused._send_packet({"cmd": Packet.KITTY_DETECT})

    said = b""
    while True:
        piece = refused.socket.recv(4096)
        if not piece:
            break
        said += piece

    assert b"that terminal is taken" in said


def test_the_code_to_leave_with_is_there_too(refused):
    "A script has only the code to tell a refusal from a detach. #332."
    refused._send_packet({"cmd": Packet.KITTY_DETECT})

    codes = [
        json.loads(one.decode("utf-8"))
        for one in refused.socket.recv(4096).split(b"\0")
        if one
    ]
    assert [one["code"] for one in codes if one["cmd"] == Packet.EXIT] == [1]
