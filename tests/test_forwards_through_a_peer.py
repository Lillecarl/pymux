"""
A person who reaches a server through a pymux server of their own
(`connect ssh://...`) holds the SSH connection there, so a forward the
owner is asked for goes along the peer link, and the peer asks them.
Lillecarl/pyterm#976.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import anyio
from libpymux.objects import Server
from libpymux.protocol import Capability
from test_a_server_mirrors_a_server import run_as, shown_here, the_probe_does_not_leave_this_machine, two_servers, until
from test_port_forwarding import _exactly, _socks5_to, echoing
from test_ssh_client import create_ssh_server

from pymux.mirror.dial import abort

__all__ = ["the_probe_does_not_leave_this_machine", "two_servers"]


@asynccontextmanager
async def linked_over_ssh(owner, peer, client, tmp_path):
    "The peer linked to the owner over SSH, and the client here on the owner's session."
    server, port, client_key = await create_ssh_server(tmp_path, owner.socket_name, allow_exec=False)
    try:
        here = peer.peers.of(
            "ssh://127.0.0.1:%d%s" % (port, owner.socket_name),
            known_hosts=None,
            client_keys=[client_key],
            username="anybody",
        )
        there, _mirror = await shown_here(owner, peer, client, here)
        await until(lambda: any(link.can(Capability.FORWARDS) for link in owner.peer_links()), "the link forwards")
        yield here, there
    finally:
        server.close()


def owners_forwards(owner) -> list[dict]:
    return [one for link in owner.peer_links() for one in link.forwards]


async def asked_there(owner, there, *words: str):
    "Run `words` on the owner as a program in its pane `there` does."
    return await Server(owner.socket_name).cmd(list(words), pane_id=str(there.pane_id))


async def test_a_peer_on_a_unix_socket_offers_no_forwards(two_servers):
    "Nothing to forward through, so the owner never picks it."
    owner, peer, client = two_servers
    await shown_here(owner, peer, client)
    (link,) = owner.peer_links()
    assert not link.can(Capability.FORWARDS)
    assert link.can(Capability.FILES)


async def test_a_program_there_asks_the_person_here_and_the_yes_forwards_socks(two_servers, tmp_path):
    """
    `browser.open via=client` on a server the person mirrors: the
    question shows here, a yes makes the SOCKS forward on the link's
    SSH connection, and the owner's table shows the port it listens on.
    """
    owner, peer, client = two_servers
    with anyio.fail_after(20):
        async with echoing() as echo_port, linked_over_ssh(owner, peer, client, tmp_path) as (_here, there):
            said = await asked_there(owner, there, "forward-port", "-R", "127.0.0.1:0")
            assert said.exit_code == 0, said
            assert "The person there confirms it." in said.stdout

            await until(lambda: client.confirm_command is not None, "the person here is asked")
            assert (
                "asks: Forward -R 127.0.0.1:0 (any free port) as SOCKS, connecting from this machine?"
                in client.confirm_text
            )
            assert not owners_forwards(owner), "nothing is forwarded before the yes"

            await run_as(peer, client, client.answer())
            await until(
                lambda: owners_forwards(owner), "the owner hears the table", seen=lambda: owners_forwards(owner)
            )
            (one,) = owners_forwards(owner)
            assert one["dest"] == "socks" and not one["error"] and one["port"] != 0, one

            stream, reply = await _socks5_to(one["port"], "localhost", echo_port)
            async with stream:
                assert reply == 0
                await stream.send(b"hello")
                assert await _exactly(stream, 5) == b"HELLO"


async def test_a_yes_from_a_program_here_counts_for_nothing(two_servers, tmp_path):
    "`-c` from a pane is no answer, here as on the owner: the person is asked anyway."
    owner, peer, client = two_servers
    with anyio.fail_after(20):
        async with linked_over_ssh(owner, peer, client, tmp_path) as (here, _there):
            said = await Server(peer.socket_name).cmd(["forward-port", "-c", "-p", here.address, "-R", "127.0.0.1:0"])
            assert said.exit_code == 0, said
            assert client.confirm_command is not None
            assert not here.forwards.report()


async def test_the_owner_unforwards_through_the_peer(two_servers, tmp_path):
    owner, peer, client = two_servers
    with anyio.fail_after(20):
        async with linked_over_ssh(owner, peer, client, tmp_path) as (here, there):
            await run_as(peer, client, "forward-port -c -p %s -R 127.0.0.1:0" % (here.address,))
            await until(lambda: owners_forwards(owner), "the forward is made")
            (one,) = owners_forwards(owner)

            said = await asked_there(owner, there, "unforward-port", "-R", "127.0.0.1:%d" % (one["port"],))
            assert said.exit_code == 0, said
            await until(lambda: not owners_forwards(owner), "the owner hears it is gone")
            assert not here.forwards.report()


async def test_a_forward_comes_back_with_the_link(two_servers, tmp_path):
    "A link that goes takes the listener with its connection; the next link opens it again and tells the owner."
    owner, peer, client = two_servers
    with anyio.fail_after(20):
        async with linked_over_ssh(owner, peer, client, tmp_path) as (here, _there):
            await run_as(peer, client, "forward-port -c -p %s -R 127.0.0.1:0" % (here.address,))
            await until(lambda: owners_forwards(owner), "the forward is made")
            first = owners_forwards(owner)[0]["port"]

            (link,) = owner.peer_links()
            abort(here.stream)
            here.dial_now()
            await until(
                lambda: [one for new in owner.peer_links() if new is not link for one in new.forwards],
                "the forward is open again on the new link",
                seen=lambda: owners_forwards(owner),
            )
            (again,) = owners_forwards(owner)
            assert not again["error"], again
            # On the port it had: whatever holds the number keeps working. Lillecarl/pyterm#978.
            assert again["port"] == first


async def test_the_forwards_table_names_the_peer(two_servers, tmp_path):
    "`browser.open via=client` reads the table, so a peer's row is there as a client's is."
    owner, peer, client = two_servers
    with anyio.fail_after(20):
        async with linked_over_ssh(owner, peer, client, tmp_path) as (here, _there):
            await run_as(peer, client, "forward-port -c -p %s -R 127.0.0.1:0" % (here.address,))
            await until(lambda: owners_forwards(owner), "the forward is made")
            rows = await Server(owner.socket_name).sql(
                "SELECT client_id, listening, error FROM forwards WHERE direction = 'remote' AND dest = 'socks'"
            )
            (row,) = rows
            assert row["client_id"].startswith("peer:") and not row["error"], row
