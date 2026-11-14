"""
Plugin messages across a peer link: a plugin asks the plugin of the same
name on a linked server, and reads its answer. pymux carries the message
and never reads it. Lillecarl/pyterm#988.

**No command sends one.** Only a plugin inside the server process can,
so no other process on the machine reaches the far plugin through
pymux. A message goes one hop: a server never passes one on.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

import anyio
from libpymux.protocol import Capability, Field, Message, Packet
from pyte.keep import Keep

from ..log import logger

if TYPE_CHECKING:
    from ..core.main import Pymux

__all__ = ["PeerMessage", "PeerUnanswered", "PluginAsks", "plugin_answer", "reachable"]


class PeerUnanswered(LookupError):
    "A plugin message got no answer, and this says why."


@dataclass(frozen=True, slots=True)
class PeerMessage:
    "One message from the plugin of the same name on a linked server."

    #: The server that asked, by the name `Host.ask_peer` takes.
    peer: str
    #: Whether this server dialled the asker. Then `peer` is the address
    #: this side chose. Otherwise it is the host the asker names itself.
    dialled: bool
    # JSON as the far plugin sent it: pymux gives it no shape.
    data: object


class PluginAsks:
    "The `plugin-ask` requests one end of a link sent and waits on, by id."

    KEEP: ClassVar[dict[str, Keep]] = {
        # A plugin that waits hears that the link went.
        "_send": Keep.REBUILT,
        "_waiting": Keep.DROPPED,
    }

    def __init__(self, send: Callable[[Message], None]) -> None:
        self._send = send
        self._waiting: dict[str, tuple[anyio.Event, list[Message]]] = {}

    async def ask(self, plugin: str, data: object, timeout: float) -> object:
        "What the far plugin `plugin` answers to `data`. Raises `PeerUnanswered` saying why there is no answer."
        request = secrets.token_hex(8)
        done = anyio.Event()
        answer: list[Message] = []
        self._waiting[request] = (done, answer)
        try:
            self._send({Field.CMD: Packet.PLUGIN_ASK, Field.REQUEST: request, Field.NAME: plugin, Field.DATA: data})
            with anyio.move_on_after(timeout):
                await done.wait()
        finally:
            self._waiting.pop(request, None)
        if not answer:
            raise PeerUnanswered("no answer in %g seconds" % (timeout,))
        if answer[0].get(Field.ERROR) is not None:
            raise PeerUnanswered(str(answer[0][Field.ERROR]))
        return answer[0].get(Field.DATA)

    def answered(self, packet: Message) -> None:
        waiting = self._waiting.get(str(packet.get(Field.REQUEST)))
        if waiting is not None:
            done, answer = waiting
            answer.append(packet)
            done.set()

    def gone(self) -> None:
        "The link went: nothing waits for it any more."
        for done, answer in self._waiting.values():
            answer.append({Field.ERROR: "the link went before it answered"})
            done.set()


async def plugin_answer(pymux: Pymux, packet: Message, peer: str, dialled: bool) -> Message:
    "The `plugin-answer` to a `plugin-ask` from `peer`: the plugin's answer, or why there is none."
    reply: Message = {Field.CMD: Packet.PLUGIN_ANSWER, Field.REQUEST: packet.get(Field.REQUEST)}
    name = str(packet.get(Field.NAME) or "")
    try:
        reply[Field.DATA] = await pymux.plugins.peer_message(name, PeerMessage(peer, dialled, packet.get(Field.DATA)))
    except PeerUnanswered as why:
        reply[Field.ERROR] = str(why)
    except Exception as error:
        # The message may carry a secret, so the log names only the failure.
        logger.warning("pymux: the plugin %s failed a message from %s: %s", name, peer, type(error).__name__)
        reply[Field.ERROR] = "the plugin %s failed: %s: %s" % (name, type(error).__name__, error)
    return reply


def reachable(pymux: Pymux) -> list[tuple[str, PluginAsks]]:
    """
    Each linked server a plugin message reaches, by name: first each
    server that dialled in, by its host, the one a person used last
    first; then each address this server dialled.
    """
    links = sorted(pymux.peer_links(), key=lambda link: link.last_used, reverse=True)
    found = [(link.host, link.plugin_asks) for link in links if link.host and link.can(Capability.PLUGIN_MESSAGES)]
    found += [
        (peer.address, peer.plugin_asks)
        for peer in pymux.peers.by_address.values()
        if peer.connected and peer.accepted and peer.can(Capability.PLUGIN_MESSAGES)
    ]
    return found
