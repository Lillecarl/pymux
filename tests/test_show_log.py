"""
`show-log` reads the log of this server, and over the link it already
holds, the log of the machine a client or a peer runs on.
Lillecarl/pymux#934.
"""

from __future__ import annotations

import socket

import anyio
import pytest
from libpymux.protocol import Field, Packet
from prompt_toolkit.data_structures import Size
from session import once, over_connection
from test_a_pane_asks_for_a_file import AskedClient
from test_a_server_mirrors_a_server import (  # noqa: F401 (fixtures)
    shown_here,
    the_probe_does_not_leave_this_machine,
    two_servers,
)

from pymux import log
from pymux.answer import Answer, current_answer
from pymux.commands import handle_command

LINES = "the first line\nthe second line\nthe last line\n"


@pytest.fixture(autouse=True)
def a_log(tmp_path, monkeypatch):
    path = tmp_path / "server.log"
    path.write_text(LINES)
    monkeypatch.setattr(log, "_logfile", path)
    return path


async def printed(pymux, line: str) -> list[str]:
    "What `line` prints on `pymux`, as a command over the socket prints it."
    reply = Answer()
    token = current_answer.set(reply)
    try:
        answer = handle_command(pymux, line)
        if answer is not None:
            with anyio.fail_after(10):
                await answer
    finally:
        current_answer.reset(token)
    assert not reply.errors, reply.errors
    return reply.output


async def test_the_log_here(two_servers):
    owner, _peer, _client = two_servers
    assert await printed(owner, "show-log -n 2") == ["the second line\nthe last line"]


def test_a_long_line_is_cut(a_log):
    a_log.write_text("x" * (log.LINE_LIMIT + 5) + "\n")
    assert log.tail(1) == "x" * log.LINE_LIMIT + "… (5 more)\n"


async def test_each_end_of_a_peer_link_reads_the_other(two_servers):
    owner, peer, client = two_servers
    await shown_here(owner, peer, client)
    (link,) = peer.peers.by_address.values()
    assert await printed(peer, "show-log -n 1 -p %s" % (link.address,)) == ["the last line"]

    (owner_side,) = owner.peer_links()
    assert owner_side.host == socket.gethostname().partition(".")[0]
    assert await printed(owner, "show-log -n 1 -p %s" % (owner_side.host,)) == ["the last line"]


async def test_a_client_sends_its_machines_log(tmp_path):
    "The answer comes from the client process's own code, `TerminalClient`."
    config = tmp_path / "pymux.conf"
    config.write_text("")
    far: list[AskedClient] = []

    def hear(packet) -> None:
        if packet.get(Field.CMD) == Packet.REPORT_LOG:
            far[0]._process(packet)

    async with anyio.create_task_group() as tasks, over_connection(read_packet=hear) as session:
        state, _ = await session.attach("far", Size(rows=24, columns=80))
        connection = state.client.connection
        client = AskedClient(config, send=connection._process)
        client.tasks = tasks
        far.append(client)

        got = await session.command("show-log -n 2 -t %s" % (connection.name,))
        await once(
            lambda: any(packet.get(Field.CMD) == Packet.EXIT for packet in got),
            10.0,
            lambda: "show-log never answered: %r" % (got,),
        )
        assert [packet[Field.DATA] for packet in got if packet.get(Field.CMD) == Packet.OUT] == [
            "the second line\nthe last line\n"
        ], got
        tasks.cancel_scope.cancel()
