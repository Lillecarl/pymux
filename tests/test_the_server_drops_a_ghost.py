"""
The server drops a client that stops answering. Lillecarl/pymux#446.

**A closed socket was the only thing that dropped a client**, and a
socket outlives the person. A laptop that sleeps leaves a dead TCP
connection, the far sshd holds it until it gives up, and the forwarded
unix socket here stays open behind it. Nothing else asked, because an
attached client that is quiet sends nothing -- it reports its size only
when the size changes.

What the ghost costs is not the socket. `window-size smallest` is the
default, so a ghost holding a stale terminal size keeps the window that
size for whoever is really looking at it.

Lillecarl/pymux#445 is the other half and the other side: there the
client notices its own machine slept. This is what the server does
about the client it was left with.
"""

from __future__ import annotations

import json

from prompt_toolkit.data_structures import Size

from pymux import server as pymux_server
from pymux.client.ssh import SshClient

from session import once, over_connection

SIZE = Size(rows=24, columns=80)

#: Short enough that a test is not a wait, long enough that a loaded
#: machine does not lose the race to it.
QUICKLY = 0.05


def _pings(seen: list) -> list:
    "The pings among the packets a client received."
    asked = []

    for packet in seen:
        if isinstance(packet, (bytes, bytearray)):
            packet = packet.decode("utf-8")
        for one in packet.split("\0"):
            if one.strip() and json.loads(one).get("cmd") == "ping":
                asked.append(one)

    return asked


async def test_a_client_that_answers_is_asked_and_kept(monkeypatch):
    "The ordinary case: the server asks, and asking costs the client its place in nothing."
    monkeypatch.setattr(pymux_server, "PING_INTERVAL", QUICKLY)
    seen = []

    async with over_connection(read_packet=seen.append) as session:
        state, _size = await session.attach("the client", SIZE, pings=True)
        connection = state.connection

        await once(lambda: _pings(seen), 5.0, "the server to ask")

        # What a real client does with it. The count clears, so the
        # client keeps its place however many pings go by.
        for _ in range(5):
            connection._unanswered = 0
            await once(lambda: True, 0.1, "a moment")

        assert session.pymux.clients, "a client that answers must not be dropped"


async def test_a_client_that_stops_answering_is_dropped(monkeypatch):
    """
    **The ghost.** Nothing here answers a ping, which is what a client
    on a sleeping laptop does. The socket stays open the whole time --
    the harness never closes it -- so the drop can only have come from
    the pings going unanswered.
    """
    monkeypatch.setattr(pymux_server, "PING_INTERVAL", QUICKLY)

    async with over_connection() as session:
        await session.attach("the ghost", SIZE, pings=True)
        assert session.pymux.clients

        await once(
            lambda: not session.pymux.clients,
            5.0,
            "the server to drop a client that stopped answering",
        )


async def test_a_client_that_never_said_it_answers_is_left_alone(monkeypatch):
    """
    **The one mistake this must not make.** A client that does not know
    the packet would be reaped for staying quiet, and staying quiet is
    what an attached client does. So the server only drops one that
    said in `start-gui` that it answers.
    """
    monkeypatch.setattr(pymux_server, "PING_INTERVAL", QUICKLY)
    seen = []

    async with over_connection(read_packet=seen.append) as session:
        await session.attach("an older client", SIZE)

        await once(lambda: True, 0.5, "long enough for several pings")

        assert not _pings(seen), "a client that said nothing must not be pinged"
        assert session.pymux.clients, "and must never be dropped for not answering"


async def test_a_client_answers_a_ping():
    """
    The client's half, on the code every transport shares. Without this
    the server drops everybody that told it to ask.
    """
    sent = []

    client = SshClient("ssh://127.0.0.1/tmp/nowhere.sock")
    client._send_packet = sent.append

    client._process(json.dumps({"cmd": "ping"}).encode("utf-8"))

    assert sent == [{"cmd": "pong"}]
