"""
A browser a program on the owner drives, for a person who reaches the
owner through a pymux server of their own: the peer asks them, starts the
browser on its client and forwards the port back on the link's SSH
connection. The client is the test, which answers for the browser.
Lillecarl/pyterm#1068.
"""

from __future__ import annotations

import anyio
import pytest
from fairclock import anyio as fair_anyio
from libpymux.connection import CommandError
from libpymux.objects import Server
from libpymux.protocol import Field, Packet
from test_a_server_mirrors_a_server import run_as, the_probe_does_not_leave_this_machine, two_servers, until
from test_forwards_through_a_peer import asked_there, linked_over_ssh

from pymux.mirror.dial import abort

__all__ = ["the_probe_does_not_leave_this_machine", "two_servers"]


def forwarded(peer_link, port: int) -> list[dict]:
    "What the peer forwards to the owner on `port`."
    return [one for one in peer_link.forwards.report() if one["port"] == port and one["direction"] == "remote"]


def browser_packet(sent: list) -> dict | None:
    return next((one for one in sent if one.get(Field.CMD) == Packet.BROWSER), None)


async def started_there(owner, peer, client, here, there, sent, tasks, results):
    """
    A launch on the owner that the person here said yes to, and whose
    browser the client here started: the owner's launch and the peer's.
    """
    client.client.starts_browsers = True
    client.client.connection.send_packet = sent.append

    async def launch() -> None:
        results.append(await asked_there(owner, there, "launch-browser", "--", "https://example.org"))

    tasks.start_soon(launch)
    await until(lambda: (client.confirm_command or "").startswith("launch-browser -c "), "the person here is asked")
    assert "wants to drive a browser on this machine (https://example.org)" in (client.confirm_text or "")
    assert "pane %%%d" % (there.pane_id,) in (client.confirm_text or "")
    run_as(peer, client, client.answer())

    await until(lambda: browser_packet(sent) is not None, "the client here is told to start it", seen=lambda: sent)
    packet = browser_packet(sent)
    assert packet is not None
    (asked,) = owner.browsers.launches.values()
    # The same number at both ends, which Chrome's `/json/version` needs.
    assert packet[Field.PORT] == asked.port
    peer.browsers.started({Field.CMD: Packet.BROWSER_STARTED, Field.BROWSER: packet[Field.BROWSER], Field.PATH: "/p"})
    await until(lambda: results, "the launch on the owner answers")
    return asked, packet[Field.BROWSER]


async def test_a_yes_here_starts_the_browser_and_forwards_its_port_to_the_owner(two_servers, tmp_path):
    owner, peer, client = two_servers
    sent: list = []
    results: list = []
    with fair_anyio.fail_after(20):
        async with linked_over_ssh(owner, peer, client, tmp_path) as (here, there), anyio.create_task_group() as tasks:
            asked, mine = await started_there(owner, peer, client, here, there, sent, tasks, results)
            (said,) = results
            assert said.exit_code == 0, said
            assert said.stdout == "%d /p %s cdp\n" % (asked.port, asked.launch_id)

            (one,) = forwarded(here, asked.port)
            assert not one["error"], one
            # Something listens on the port on the owner's machine: the forward.
            stream = await anyio.connect_tcp("127.0.0.1", asked.port)
            await stream.aclose()

            # The browser ends by itself here: the owner hears it, and the port goes.
            peer.browsers.ended(mine)
            await until(lambda: not owner.browsers.launches, "the owner hears the browser ended")
            await until(lambda: not forwarded(here, asked.port), "the forward goes with the browser")
            assert not here.browsers


async def test_a_no_here_reaches_the_program_there(two_servers, tmp_path):
    owner, peer, client = two_servers
    client.client.starts_browsers = True
    with fair_anyio.fail_after(20):
        async with linked_over_ssh(owner, peer, client, tmp_path) as (here, there), anyio.create_task_group() as tasks:
            refused = anyio.Event()

            async def launch() -> None:
                with pytest.raises(CommandError, match="the person said no"):
                    await asked_there(owner, there, "launch-browser")
                refused.set()

            tasks.start_soon(launch)
            await until(lambda: client.confirm_command is not None, "the person here is asked")
            client.answer()
            await refused.wait()
            assert not owner.browsers.launches
            assert not here.forwards.report()


async def test_the_owner_stops_the_browser_here(two_servers, tmp_path):
    "`hold-browser -s`, which a killed `pymux-browser` amounts to: the client here is told, and the port goes."
    owner, peer, client = two_servers
    sent: list = []
    with fair_anyio.fail_after(20):
        async with linked_over_ssh(owner, peer, client, tmp_path) as (here, there), anyio.create_task_group() as tasks:
            asked, mine = await started_there(owner, peer, client, here, there, sent, tasks, [])
            said = await Server(owner.socket_name).cmd(["hold-browser", "-s", asked.launch_id])
            assert said.exit_code == 0, said
            stop = {Field.CMD: Packet.BROWSER_STOP, Field.BROWSER: mine}
            await until(lambda: stop in sent, "the client here is told to stop it", seen=lambda: sent)
            await until(lambda: not forwarded(here, asked.port), "the forward goes with the browser")
            assert not peer.browsers.launches and not here.browsers


async def test_a_link_that_goes_ends_the_browser_at_both_ends(two_servers, tmp_path):
    "The owner's program cannot reach the browser any more, so `hold-browser` there answers and the browser stops."
    owner, peer, client = two_servers
    sent: list = []
    with fair_anyio.fail_after(20):
        async with linked_over_ssh(owner, peer, client, tmp_path) as (here, there), anyio.create_task_group() as tasks:
            _asked, mine = await started_there(owner, peer, client, here, there, sent, tasks, [])
            abort(here.stream)
            await until(lambda: not owner.browsers.launches, "the owner ends the launch")
            stop = {Field.CMD: Packet.BROWSER_STOP, Field.BROWSER: mine}
            await until(lambda: stop in sent, "the client here is told to stop it", seen=lambda: sent)
            assert not here.browsers
