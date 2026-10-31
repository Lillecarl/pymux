"""
The peer's side of a peer link: this server, holding mirrors of panes
another server owns.

One link per server, whatever number of its panes are mirrored here.
`link.py` is the other end.
"""

from __future__ import annotations

import math
from contextlib import suppress
from typing import TYPE_CHECKING, Any, ClassVar

import anyio
from libpymux.protocol import Field, Message, Packet
from libpymux.wire import MSGPACK, MSGPACK_ZSTD, PacketReader, encode_json
from pyte.freeze import freeze_value
from pyte.keep import Keep

from ..client.defaults import is_ssh_url
from ..log import logger
from .dial import dial

if TYPE_CHECKING:
    from ..core.main import Pymux
    from ..terminals import PeerTerminal
    from .sink import MirrorSink

__all__ = ["Peer", "Peers"]


class Peers:
    "This server's links to other servers, one per address."

    KEEP: ClassVar[dict[str, Keep]] = {
        "pymux": Keep.REBUILT,
        "by_address": Keep.DROPPED,
    }

    def __init__(self, pymux: Pymux) -> None:
        self.pymux = pymux
        self.by_address: dict[str, Peer] = {}

    def of(self, address: str, **connect_with: Any) -> Peer:
        "The link to `address`, opened in the server's scope if there is none yet."
        peer = self.by_address.get(address)
        if peer is None:
            if self.pymux.tasks is None:
                raise RuntimeError("A peer link was asked for outside `Pymux.running`, so nothing would hold it.")
            peer = self.by_address[address] = Peer(address, **connect_with)
            peer.tell_terminal(self.pymux.terminals.peer_terminal())
            self.pymux.tasks.start_soon(peer.run)
        return peer


class Peer:
    """
    A link to the server at `address`, and the mirrors on it.

    What this side sends goes into one queue that a writer drains, so
    a packet sent before the link is up waits and goes first: the hello,
    then each mirror asked for meanwhile.
    """

    KEEP: ClassVar[dict[str, Keep]] = {
        # A mirror is rebuilt by its program after an upgrade, and asks
        # the next link for a fresh freeze.
        "address": Keep.REBUILT,
        "connect_with": Keep.REBUILT,
        "sinks": Keep.DROPPED,
        "connected": Keep.DROPPED,
        "error": Keep.DROPPED,
        "_send": Keep.DROPPED,
        "_receive": Keep.DROPPED,
        "_terminal_told": Keep.DROPPED,
    }

    def __init__(self, address: str, **connect_with: Any) -> None:
        self.address = address
        self.connect_with = connect_with
        self.sinks: dict[int, MirrorSink] = {}
        #: Whether the link is up.
        self.connected = False
        #: Why the link went or never came, for the person to read.
        self.error: str | None = None
        self._send, self._receive = anyio.create_memory_object_stream[bytes](math.inf)
        self._terminal_told: PeerTerminal | None = None
        # Zstd only across a network, as a client offers it.
        offer = [MSGPACK_ZSTD, MSGPACK] if is_ssh_url(address) else [MSGPACK]
        self.send({Field.CMD: Packet.PEER, Field.WIRE: offer})

    def send(self, packet: Message) -> None:
        self._send.send_nowait(encode_json(packet))

    # -- what a mirror asks --------------------------------------------

    def mirror(self, pane_id: int, sink: MirrorSink) -> None:
        self.sinks[pane_id] = sink
        self.send({Field.CMD: Packet.MIRROR, Field.PANE_ID: pane_id})

    def stop(self, pane_id: int) -> None:
        if self.sinks.pop(pane_id, None) is not None:
            self.send({Field.CMD: Packet.MIRROR_STOP, Field.PANE_ID: pane_id})

    def pause(self, pane_id: int) -> None:
        self.send({Field.CMD: Packet.MIRROR_PAUSE, Field.PANE_ID: pane_id})

    def resume(self, pane_id: int) -> None:
        self.send({Field.CMD: Packet.MIRROR_RESUME, Field.PANE_ID: pane_id})

    def type(self, pane_id: int, data: str) -> None:
        self.send({Field.CMD: Packet.MIRROR_INPUT, Field.PANE_ID: pane_id, Field.DATA: data})

    def focus(self, pane_id: int, focused: bool) -> None:
        self.send({Field.CMD: Packet.MIRROR_FOCUS, Field.PANE_ID: pane_id, Field.FOCUSED: focused})

    def tell_terminal(self, terminal: PeerTerminal) -> None:
        "Tell the owner what the terminals here say, when it changed (`Terminals.tell_peers`)."
        if terminal == self._terminal_told:
            return
        self._terminal_told = terminal
        self.send(
            {
                Field.CMD: Packet.PEER_TERMINAL,
                Field.CELL_SIZE: None if terminal.cell_size is None else list(terminal.cell_size),
                Field.COLOR_BASE: None if terminal.color_base is None else freeze_value(terminal.color_base),
                Field.KEYBOARD: terminal.keyboard_source_flags,
            }
        )

    # -- the link ------------------------------------------------------

    async def run(self) -> None:
        """
        Hold the link until it goes. A mirror keeps the last screen it
        was sent when the link goes.
        """
        try:
            stream = await dial(self.address, **self.connect_with)
        except Exception as error:
            self.error = str(error) or type(error).__name__
            logger.warning("Could not reach the server at %s: %s", self.address, self.error)
            return
        self.connected = True
        # This runs in the server's own scope: what it raises would end
        # every client, so the link's trouble stays the link's.
        try:
            async with stream, anyio.create_task_group() as tasks:
                tasks.start_soon(self._write, stream)
                reader = PacketReader()
                while True:
                    try:
                        data = await stream.receive()
                    except (anyio.EndOfStream, anyio.BrokenResourceError, anyio.ClosedResourceError) as error:
                        self.error = str(error) or "the server closed the link"
                        break
                    for packet in reader.feed(data):
                        self._take(packet)
                tasks.cancel_scope.cancel()
        except Exception as error:
            self.error = str(error) or type(error).__name__
            logger.exception("The link to %s failed.", self.address)
        finally:
            self.connected = False
            logger.info("The link to %s went: %s", self.address, self.error)

    async def _write(self, stream: anyio.abc.ByteStream) -> None:
        # A send that fails ends the link the read loop is holding.
        with suppress(anyio.BrokenResourceError, anyio.ClosedResourceError):
            async for encoded in self._receive:
                await stream.send(encoded)
        await stream.aclose()

    def _take(self, packet: Message) -> None:
        pane_id = packet.get(Field.PANE_ID)
        sink = self.sinks.get(pane_id) if isinstance(pane_id, int) else None
        if sink is None:
            return
        try:
            sink.take(packet)
        except Exception:
            # One mirror that cannot take what it was sent must not end
            # the link for the others.
            logger.exception("Mirror of pane %s on %s failed a packet.", pane_id, self.address)
