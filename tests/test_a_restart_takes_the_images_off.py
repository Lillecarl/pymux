"""
A restarting server's images come off the client's terminal.

The next server knows none of the kitty images the old one placed, and
draws its own over them, so the client deletes them when it hears the
restart. A terminal that never had one is never sent the APC: one with
no kitty graphics may print it. Lillecarl/pymux#399.
"""

from __future__ import annotations

import json
import os

import pytest
from libpymux.protocol import Field, Packet

from pymux.client.posix import PosixClient
from pymux.client.terminal import DELETE_EVERY_IMAGE, TerminalClient


class Waiting(PosixClient):
    "The client, told to wait for a server that never comes."

    def __init__(self, socket_name: str) -> None:
        TerminalClient.__init__(self)
        self.socket_name = socket_name


@pytest.mark.parametrize("placed", [True, False])
def test_a_restart_takes_the_images_off_only_where_there_were_some(capfdbinary, tmp_path, placed):
    client = Waiting(str(tmp_path / "gone.sock"))
    data = "frame\x1b_Ga=p,i=3,q=2\x1b\\" if placed else "frame only"
    client._process(json.dumps({Field.CMD: Packet.OUT, Field.DATA: data}).encode())

    keys, _ = os.pipe()
    client.restart_wait = 0.05
    assert not client._wait_for_the_next_server(keys)

    written = capfdbinary.readouterr().out
    assert b"pymux is restarting." in written
    assert (DELETE_EVERY_IMAGE in written) == placed
