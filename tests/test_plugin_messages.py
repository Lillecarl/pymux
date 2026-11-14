"""
A plugin asks the plugin of the same name on a linked server, over the
peer link, and reads its answer. Lillecarl/pyterm#988.
"""

from __future__ import annotations

import anyio
import pytest
from test_a_server_mirrors_a_server import (  # noqa: F401 (fixtures)
    shown_here,
    the_probe_does_not_leave_this_machine,
    two_servers,
    until,
)

from pymux.ids import PaneId
from pymux.plugin_api import BellRang, Host, PeerMessage, PeerUnanswered, Plugin
from pymux.plugin_api.runner import PluginState

DEADLINE = 10


class Echo(Plugin):
    "Answers what it is sent, and who sent it. `wait` never answers; `raise` fails."

    def __init__(self, host: Host) -> None:
        super().__init__(host)
        self.heard: list[PeerMessage] = []
        self.bells: list[BellRang] = []
        self.waiting = anyio.Event()

    async def on_peer_message(self, message: PeerMessage) -> object:
        self.heard.append(message)
        if message.data == "wait":
            self.waiting.set()
            await anyio.sleep_forever()
        if message.data == "raise":
            raise RuntimeError("asked to fail")
        return {"echo": message.data, "from": message.peer, "dialled": message.dialled}

    async def on_bell(self, event: BellRang) -> None:
        self.bells.append(event)


async def _echo(pymux) -> Echo:
    await until(lambda: "echo" in pymux.plugins.slots and pymux.plugins.slots["echo"].plugin, "the plugin runs")
    return pymux.plugins.slots["echo"].plugin


@pytest.fixture
async def linked(two_servers):
    "Both servers linked, each running `Echo`: the owner's, the peer's, and the names each calls the other."
    owner, peer, client = two_servers
    await shown_here(owner, peer, client)
    (dialled,) = peer.peers.by_address.values()
    (dialled_in,) = owner.peer_links()
    async with anyio.create_task_group() as tasks:
        tasks.start_soon(owner.plugins.serve, "echo", Echo)
        tasks.start_soon(peer.plugins.serve, "echo", Echo)
        yield await _echo(owner), await _echo(peer), dialled_in.host, dialled.address
        tasks.cancel_scope.cancel()


async def test_each_end_asks_the_other(linked):
    at_owner, at_peer, peer_host, owner_address = linked
    with anyio.fail_after(DEADLINE):
        assert at_owner.host.peers() == [peer_host]
        assert await at_owner.host.ask_peer(peer_host, {"n": 1}, DEADLINE) == {
            "echo": {"n": 1},
            "from": owner_address,
            "dialled": True,
        }
        assert at_peer.host.peers() == [owner_address]
        assert await at_peer.host.ask_peer(owner_address, [2], DEADLINE) == {
            "echo": [2],
            "from": peer_host,
            "dialled": False,
        }


async def test_a_server_without_the_plugin_says_so(linked):
    at_owner, at_peer, peer_host, _ = linked
    async with anyio.create_task_group() as tasks:
        missing = Host(at_owner.host.pymux, "missing", tasks)
        with (
            anyio.fail_after(DEADLINE),
            pytest.raises(PeerUnanswered, match="%s: it runs no plugin missing" % peer_host),
        ):
            await missing.ask_peer(peer_host, None, DEADLINE)


async def test_a_plugin_without_a_handler_takes_no_message(linked):
    at_owner, at_peer, peer_host, _ = linked

    class Deaf(Plugin):
        pass

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(at_peer.host.pymux.plugins.serve, "deaf", Deaf)
        await until(lambda: "deaf" in at_peer.host.pymux.plugins.slots, "the plugin runs")
        deaf = Host(at_owner.host.pymux, "deaf", tasks)
        with anyio.fail_after(DEADLINE), pytest.raises(PeerUnanswered, match="takes no messages"):
            await deaf.ask_peer(peer_host, None, DEADLINE)
        tasks.cancel_scope.cancel()


async def test_an_unknown_peer_is_refused_at_once(linked):
    at_owner, _, peer_host, _ = linked
    with anyio.fail_after(DEADLINE), pytest.raises(PeerUnanswered, match="linked: %s" % peer_host):
        await at_owner.host.ask_peer("elsewhere", None, DEADLINE)


async def test_a_link_without_the_capability_reaches_nothing(linked):
    at_owner, _, peer_host, _ = linked
    (link,) = at_owner.host.pymux.peer_links()
    link.capabilities = frozenset()
    assert at_owner.host.peers() == []
    with anyio.fail_after(DEADLINE), pytest.raises(PeerUnanswered, match="no linked server"):
        await at_owner.host.ask_peer(peer_host, None, DEADLINE)


async def test_a_plugin_that_raises_answers_why_and_runs_on(linked):
    at_owner, at_peer, peer_host, _ = linked
    with anyio.fail_after(DEADLINE):
        with pytest.raises(PeerUnanswered, match="the plugin echo failed: RuntimeError: asked to fail"):
            await at_owner.host.ask_peer(peer_host, "raise", DEADLINE)
        assert at_peer.host.pymux.plugins.slots["echo"].state is PluginState.RUNNING
        assert (await at_owner.host.ask_peer(peer_host, "again", DEADLINE))["echo"] == "again"


async def test_the_asker_gives_up_at_its_deadline(linked):
    at_owner, _, peer_host, _ = linked
    with anyio.fail_after(DEADLINE), pytest.raises(PeerUnanswered, match=r"no answer in 0\.2 seconds"):
        await at_owner.host.ask_peer(peer_host, "wait", 0.2)


async def test_a_link_that_goes_ends_the_wait(linked):
    at_owner, at_peer, _, owner_address = linked
    (link,) = at_owner.host.pymux.peer_links()

    async def close_when_it_waits() -> None:
        await at_owner.waiting.wait()
        link.connection.close_connection()

    with anyio.fail_after(DEADLINE):
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(close_when_it_waits)
            with pytest.raises(PeerUnanswered, match="the link went before it answered"):
                await at_peer.host.ask_peer(owner_address, "wait", DEADLINE)


async def test_a_message_that_waits_holds_no_event(linked):
    at_owner, at_peer, peer_host, _ = linked
    with anyio.fail_after(DEADLINE):
        async with anyio.create_task_group() as tasks:

            async def ask() -> None:
                with pytest.raises(PeerUnanswered):
                    await at_owner.host.ask_peer(peer_host, "wait", 1)

            tasks.start_soon(ask)
            await at_peer.waiting.wait()
            at_peer.host.pymux.plugins.emit(BellRang(PaneId(1)))
            await until(lambda: at_peer.bells, "the bell reaches the plugin")
