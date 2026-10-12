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

msgpack because measured on what a client is sent -- a busy text pane
and a video in a window pane -- it costs a twenty-sixth of json across
encode and decode, and text is 23% smaller with no `\\u001b` for every
escape. Lillecarl/pymux#633.
"""

from __future__ import annotations

import json
from typing import Any

import ormsgpack

from .protocol import END, Field, Packet

__all__ = [
    "MSGPACK",
    "PacketReader",
    "encode_json",
    "encode_msgpack",
    "turns_to_msgpack",
]

#: The one format besides JSON, as `wire` names it.
MSGPACK = "msgpack"

#: The bytes before a msgpack message: its length.
_LENGTH = 4


def encode_json(packet: Any) -> bytes:
    return json.dumps(packet).encode("utf-8") + END


def encode_msgpack(packet: Any) -> bytes:
    # A `Field` key is a `StrEnum`, which ormsgpack refuses as a key
    # without this. 0.46 us a packet against 0.12, measured.
    message = ormsgpack.packb(packet, option=ormsgpack.OPT_NON_STR_KEYS)
    return len(message).to_bytes(_LENGTH, "big") + message


def turns_to_msgpack(packet: dict) -> bool:
    "Whether this is the `wire` packet after which its writer writes msgpack."
    return packet.get(Field.CMD) == Packet.WIRE and packet.get(Field.WIRE) == MSGPACK


class PacketReader:
    """
    The packets a byte stream holds, each once it is whole, in JSON
    until a `wire` packet turns it to msgpack.

    A message that does not decode is dropped and counted, the way a
    server has always ignored a bad packet, and the stream goes on.
    """

    def __init__(self) -> None:
        self._held = bytearray()
        #: How far `_held` was searched for `END` and found none.
        self._searched = 0
        self.msgpack = False
        self.dropped = 0

    def feed(self, data: bytes) -> list[dict]:
        self._held += data
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
            if turns_to_msgpack(packet):
                self.msgpack = True
        return packets
