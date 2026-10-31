"""
The owner's side of a peer link: one connection from another server,
which mirrors any number of this server's panes.

**One link per peer, not one connection per pane.** Everything a peer
hears of this server travels on it: the mirrors, and what is not a
pane's screen at all. One writer drains one queue, so the packets of a
pane go in the order its screen took them. A peer that cannot keep up
holds up the link, and each mirror then stops sending reads on its own
(`source.LAG_BYTES`) until its share has drained.
"""

from __future__ import annotations

from collections import deque
from typing import TYPE_CHECKING, ClassVar

import anyio
from libpymux.protocol import Field, Message, Packet
from libpymux.wire import MSGPACK_ZSTD, choose, encode_json, encode_msgpack
from ptterm import Terminal
from pyte.keep import Keep

from ..connection.packet_table import PacketTable
from ..ids import PaneId
from ..pipes.base import BrokenPipeError
from .source import MirrorSource

if TYPE_CHECKING:
    from pymux.connection.server import ServerConnection

__all__ = ["PeerLink"]

#: The method of `PeerLink` that handles each kind of packet.
_PACKETS: PacketTable[PeerLink] = PacketTable()

#: The packets a peer sends on a link, which the connection hands here.
PEER_PACKETS = (
    Packet.MIRROR,
    Packet.MIRROR_PAUSE,
    Packet.MIRROR_RESUME,
    Packet.MIRROR_STOP,
    Packet.MIRROR_INPUT,
)


class PeerLink:
    "The panes one peer mirrors, and the queue of what it has not been sent."

    KEEP: ClassVar[dict[str, Keep]] = {
        # The peer reconnects to the next server and mirrors again.
        "connection": Keep.DROPPED,
        "sources": Keep.DROPPED,
        "_queue": Keep.DROPPED,
        "_ready": Keep.DROPPED,
    }

    def __init__(self, connection: ServerConnection, offered: object) -> None:
        self.connection = connection
        self.sources: dict[PaneId, MirrorSource] = {}
        # Each packet with the mirror it is for, and whether the stream
        # turns to zstd after it.
        self._queue: deque[tuple[MirrorSource | None, bytes, bool]] = deque()
        self._ready = anyio.Event()

        # The `wire` packet goes first in this queue, so nothing encoded
        # after the turn can be written before it.
        wire = choose(offered, connection.pipe_connection.can_compress)
        if wire is not None and connection.encode is encode_json:
            self._queue.append((None, encode_json({Field.CMD: Packet.WIRE, Field.WIRE: wire}), wire == MSGPACK_ZSTD))
            connection.encode = encode_msgpack

    def queue(self, source: MirrorSource, packet: Message) -> int:
        "Encode a packet now, queue it behind the others, and say its size."
        encoded = self.connection.encode(packet)
        self._queue.append((source, encoded, False))
        self._ready.set()
        return len(encoded)

    async def run(self) -> None:
        "Write what is queued until the connection goes, then let every pane go."
        try:
            while not self.connection.closed:
                if not self._queue:
                    await self._ready.wait()
                    self._ready = anyio.Event()
                    continue
                source, encoded, compress_after = self._queue.popleft()
                await self.connection.write_encoded(encoded, compress_after)
                if source is not None:
                    source.sent(len(encoded))
        except BrokenPipeError:
            pass
        finally:
            self.close()

    def close(self) -> None:
        for source in self.sources.values():
            source.stop()
        self.sources.clear()
        self._queue.clear()
        self._ready.set()

    def lost(self, source: MirrorSource) -> None:
        "A mirror this side could not keep sending: it ends, and the peer hears why."
        self._drop(source)
        self.connection.send_packet(
            {
                Field.CMD: Packet.MIRROR_REFUSED,
                Field.PANE_ID: int(source.pane.pane_id),
                Field.ERROR: "sending it failed",
            }
        )

    def _drop(self, source: MirrorSource) -> None:
        source.stop()
        self.sources.pop(source.pane.pane_id, None)

    def handle(self, packet: Message) -> None:
        _PACKETS.handle(self, packet)

    def _source_of(self, packet: Message) -> MirrorSource | None:
        return self.sources.get(PaneId(int(packet.get(Field.PANE_ID, -1))))

    @_PACKETS.handles(Packet.MIRROR)
    def _on_mirror(self, packet: Message) -> None:
        pane_id = PaneId(int(packet.get(Field.PANE_ID, -1)))
        if pane_id in self.sources:
            return
        pane = self.connection.pymux.panes_by_id.get(pane_id)
        terminal = pane.terminal if pane is not None else None
        if pane is None or not isinstance(terminal, Terminal):
            # A window pane has no screen to lend. Lillecarl/pymux#620.
            self.connection.send_packet(
                {Field.CMD: Packet.MIRROR_REFUSED, Field.PANE_ID: int(pane_id), Field.ERROR: "no such pane"}
            )
            return
        source = self.sources[pane_id] = MirrorSource(self, pane, terminal.terminal_control)
        source.start()

    @_PACKETS.handles(Packet.MIRROR_PAUSE)
    def _on_pause(self, packet: Message) -> None:
        source = self._source_of(packet)
        if source is not None:
            source.pause()

    @_PACKETS.handles(Packet.MIRROR_RESUME)
    def _on_resume(self, packet: Message) -> None:
        source = self._source_of(packet)
        if source is not None:
            source.resume()

    @_PACKETS.handles(Packet.MIRROR_STOP)
    def _on_stop(self, packet: Message) -> None:
        source = self._source_of(packet)
        if source is not None:
            self._drop(source)

    @_PACKETS.handles(Packet.MIRROR_INPUT)
    def _on_input(self, packet: Message) -> None:
        source = self._source_of(packet)
        if source is not None:
            source.control.process.write_input(packet.get(Field.DATA, ""))
