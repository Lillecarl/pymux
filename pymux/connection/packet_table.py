"""
Which method handles each kind of packet, for both ends of a connection.
A method says what it handles with `@table.handles(Packet.X)`, and the
reader hands each packet to `table.handle`. Lillecarl/pymux#775.
"""

from __future__ import annotations

from collections.abc import Callable

from libpymux.protocol import Field, Message

__all__ = ["PacketTable"]

type Handler[T] = Callable[[T, Message], None]


class PacketTable[T]:
    "The handler of each kind of packet, for the methods of one class."

    def __init__(self) -> None:
        self._by_kind: dict[str, Handler[T]] = {}

    def handles(self, *kinds: str) -> Callable[[Handler[T]], Handler[T]]:
        "Make the method below the handler of each kind named."

        def register(method: Handler[T]) -> Handler[T]:
            for kind in kinds:
                assert kind not in self._by_kind, kind
                self._by_kind[kind] = method
            return method

        return register

    def handle(self, owner: T, packet: Message) -> None:
        "Hand the packet to the method for its kind. A kind with none is dropped."
        handler = self._by_kind.get(packet.get(Field.CMD, ""))
        if handler is not None:
            handler(owner, packet)
