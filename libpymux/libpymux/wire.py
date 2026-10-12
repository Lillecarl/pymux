"""
How a packet is written on the wire, and read back.

Every connection starts in JSON, each message ended by `END`. An
attached client offers msgpack in `start-gui`. A server that takes the
offer writes one last JSON packet, `wire`, and every packet after it as
msgpack: four bytes of length, big end first, then the message. The
client does the same in its own direction when it reads that packet. So
each reader turns at a packet it reads, and nothing races: a packet
already on its way is read as what it was written in.

A peer that never offers, or never answers, keeps JSON. That is an old
client across `upgrade-server`, and every command, stream and relay.

A client over ssh offers `msgpack+zstd` first: the server's frames then
go inside one zstd stream, flushed at every packet. The client's own
packets stay plain msgpack -- keystrokes, too small for zstd to win on.

msgpack because measured on what a client is sent -- a busy text pane
and a video in a window pane -- it costs a twenty-sixth of json across
encode and decode, and text is 23% smaller with no `\\u001b` for every
escape. Lillecarl/pymux#633.
"""

from __future__ import annotations

import json
from compression import zstd
from typing import Any

import ormsgpack

from .protocol import END, Field, Packet

__all__ = [
    "MSGPACK",
    "MSGPACK_ZSTD",
    "Compressor",
    "PacketReader",
    "choose",
    "encode_json",
    "encode_msgpack",
    "turned_to",
]

#: The formats besides JSON, as `wire` names them.
MSGPACK = "msgpack"
#: msgpack frames inside one zstd stream, flushed at every packet. For a
#: link that crosses a network: it makes text 6.5x smaller at 570 MB/s,
#: and on a local socket it would only cost the CPU. Lillecarl/pymux#633.
MSGPACK_ZSTD = "msgpack+zstd"
FORMATS = (MSGPACK_ZSTD, MSGPACK)

#: The bytes before a msgpack message: its length.
_LENGTH = 4

#: zstd's fastest level: 15% of the wire for text, against 14% at
#: level 3, and twice the speed on video. Measured in #633.
_LEVEL = 1


def encode_json(packet: Any) -> bytes:
    return json.dumps(packet).encode("utf-8") + END


def encode_msgpack(packet: Any) -> bytes:
    # A `Field` key is a `StrEnum`, which ormsgpack refuses as a key
    # without this. 0.46 us a packet against 0.12, measured.
    message = ormsgpack.packb(packet, option=ormsgpack.OPT_NON_STR_KEYS)
    return len(message).to_bytes(_LENGTH, "big") + message


def turned_to(packet: dict) -> str | None:
    "The format its writer writes after this packet, when it is a `wire` packet."
    if packet.get(Field.CMD) != Packet.WIRE:
        return None
    wire = packet.get(Field.WIRE)
    return wire if wire in FORMATS else None


def choose(offered: object, can_compress: bool) -> str | None:
    "The first format of an offer this side writes, in the order offered."
    if not isinstance(offered, list):
        return None
    for wire in offered:
        if wire == MSGPACK or (wire == MSGPACK_ZSTD and can_compress):
            return wire
    return None


class Compressor:
    "One direction's zstd stream, flushed at every frame so each arrives whole."

    def __init__(self) -> None:
        self._zstd = zstd.ZstdCompressor(level=_LEVEL)

    def compress(self, frame: bytes) -> bytes:
        return self._zstd.compress(frame, zstd.ZstdCompressor.FLUSH_BLOCK)


class PacketReader:
    """
    The packets a byte stream holds, each once it is whole, in JSON
    until a `wire` packet turns it to msgpack, or to msgpack inside a
    zstd stream.

    A message that does not decode is dropped and counted, the way a
    server has always ignored a bad packet, and the stream goes on.
    """

    def __init__(self) -> None:
        self._held = bytearray()
        #: How far `_held` was searched for `END` and found none.
        self._searched = 0
        self.msgpack = False
        self._zstd: zstd.ZstdDecompressor | None = None
        self.dropped = 0

    @property
    def zstd(self) -> bool:
        return self._zstd is not None

    def feed(self, data: bytes) -> list[dict]:
        self._held += self._zstd.decompress(data) if self._zstd is not None else data
        packets: list[dict] = []
        while True:
            if self.msgpack:
                if len(self._held) < _LENGTH:
                    break
                length = int.from_bytes(self._held[:_LENGTH], "big")
                if len(self._held) < _LENGTH + length:
                    break
                message = self._held[_LENGTH : _LENGTH + length]
                del self._held[: _LENGTH + length]
                try:
                    packet = ormsgpack.unpackb(message)
                except ormsgpack.MsgpackDecodeError:
                    self.dropped += 1
                    continue
            else:
                end = self._held.find(END, self._searched)
                if end < 0:
                    self._searched = len(self._held)
                    break
                message = self._held[:end]
                del self._held[: end + 1]
                self._searched = 0
                try:
                    packet = json.loads(message)
                except ValueError:
                    self.dropped += 1
                    continue
            if not isinstance(packet, dict):
                self.dropped += 1
                continue
            packets.append(packet)
            wire = turned_to(packet)
            if wire is not None and not self.msgpack:
                self.msgpack = True
                if wire == MSGPACK_ZSTD:
                    # What came after the `wire` packet in this read is
                    # already the zstd stream.
                    self._zstd = zstd.ZstdDecompressor()
                    self._held = bytearray(self._zstd.decompress(bytes(self._held)))
        return packets
