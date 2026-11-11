"""
A loopback URL from a mirrored pane, made true on this machine.

`http://localhost:8000` from a program means the machine the program
runs on, which is the owner's, or its owner's at a greater depth. So
this server listens on the same port here, and each connection to it
goes along the peer links to that machine (`Packet.TUNNEL`). A server
that only mirrors the pane passes the tunnel one hop on, so any depth
works with no code per hop. The port stays the program's unless it is
taken here, because a redirect URL of a login flow names it. The URL
then opens the way any URL does, so a client attached over SSH forwards
the port once more. Lillecarl/pyterm#855.
"""

from __future__ import annotations

import math
import time
from contextlib import suppress
from typing import TYPE_CHECKING, ClassVar

import anyio
import anyio.abc
from libpymux.protocol import Field, Message, Packet
from libpymux.wire import MSGPACK, PacketReader, encode_json, encode_msgpack
from pyte.keep import Keep

from ..commands import CommandException
from ..core.forwarding import loopback_of, with_port
from ..ids import PaneId
from ..log import logger
from .dial import dial_beside
from .program import RemoteProgram

if TYPE_CHECKING:
    from ..connection.server import ServerConnection
    from ..core.main import Pymux
    from .peer import Peer

__all__ = ["OwnerTunnel", "Tunnels"]

#: How much one read takes from a TCP connection.
CHUNK = 65536

#: How often a port checks whether it sat unused long enough to close,
#: as the client reaps its own forwards (`client.ssh_requests.REAP_POLL`).
IDLE_POLL = 30.0

#: The bytes a tunnel copies that fail as an ended stream.
_ENDED = (anyio.EndOfStream, anyio.BrokenResourceError, anyio.ClosedResourceError, OSError)


class _LinkStream(anyio.abc.ByteStream):
    "The bytes of a tunnel, over a stream beside a peer link."

    def __init__(self, stream: anyio.abc.ByteStream) -> None:
        self._stream = stream
        self._reader = PacketReader()
        self._ready: list[bytes] = []

    @classmethod
    async def open(cls, peer: Peer, pane_id: int, host: str, port: int) -> _LinkStream:
        if peer.stream is None:
            raise CommandException("the link to %s is down" % (peer.address,))
        stream = await dial_beside(peer.address, peer.stream)
        # JSON cannot carry bytes, so the stream turns at once.
        await stream.send(encode_json({Field.CMD: Packet.WIRE, Field.WIRE: MSGPACK}))
        await stream.send(
            encode_msgpack(
                {Field.CMD: Packet.TUNNEL, Field.PANE_ID: pane_id, Field.DEST_HOST: host, Field.DEST_PORT: port}
            )
        )
        return cls(stream)

    async def receive(self, max_bytes: int = CHUNK) -> bytes:
        while not self._ready:
            for packet in self._reader.feed(await self._stream.receive()):
                if packet.get(Field.CMD) == Packet.TUNNEL_DATA and packet.get(Field.DATA):
                    self._ready.append(packet[Field.DATA])
        return self._ready.pop(0)

    async def send(self, item: bytes) -> None:
        await self._stream.send(encode_msgpack({Field.CMD: Packet.TUNNEL_DATA, Field.DATA: item}))

    async def send_eof(self) -> None:
        await self._stream.aclose()

    async def aclose(self) -> None:
        await self._stream.aclose()


async def _carry(near: anyio.abc.ByteStream, far: anyio.abc.ByteStream) -> None:
    "Copy both ways until either side ends, then close both."

    async def copy(source: anyio.abc.ByteStream, sink: anyio.abc.ByteStream, tasks: anyio.abc.TaskGroup) -> None:
        with suppress(*_ENDED):
            while True:
                await sink.send(await source.receive(CHUNK))
        tasks.cancel_scope.cancel()

    async with near, far, anyio.create_task_group() as tasks:
        tasks.start_soon(copy, near, far, tasks)
        tasks.start_soon(copy, far, near, tasks)


async def _toward(pymux: Pymux, pane_id: int, host: str, port: int) -> anyio.abc.ByteStream:
    "The machine pane `pane_id` runs on: its port here, or a tunnel to its owner."
    pane = pymux.panes_by_id.get(PaneId(pane_id))
    if pane is None:
        raise CommandException("no pane %%%d" % (pane_id,))
    program = pane.process
    if isinstance(program, RemoteProgram):
        return await _LinkStream.open(program.peer, program.pane_id, host, port)
    return await anyio.connect_tcp(host, port)


class _ConnectionStream(anyio.abc.ByteStream):
    "The bytes of a tunnel, as the connection that asked for it carries them."

    def __init__(self, connection: ServerConnection) -> None:
        self._connection = connection
        self._send, self._receive = anyio.create_memory_object_stream[bytes](math.inf)

    def take(self, data: bytes) -> None:
        self._send.send_nowait(data)

    async def receive(self, max_bytes: int = CHUNK) -> bytes:
        return await self._receive.receive()

    async def send(self, item: bytes) -> None:
        if self._connection.closed:
            raise anyio.BrokenResourceError
        await self._connection.write_packet({Field.CMD: Packet.TUNNEL_DATA, Field.DATA: item})

    async def send_eof(self) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        self._send.close()
        self._connection.detach_and_close()


class OwnerTunnel:
    "A tunnel a peer asked this server for, on the connection it asked on."

    def __init__(self, connection: ServerConnection) -> None:
        self.connection = connection
        self.near = _ConnectionStream(connection)
        # The answer turns to msgpack too: it carries bytes.
        connection.send_packet({Field.CMD: Packet.WIRE, Field.WIRE: MSGPACK})
        connection.encode = encode_msgpack

    def handle(self, packet: Message) -> None:
        "The bytes of the peer's end (`Role`)."
        if packet.get(Field.CMD) == Packet.TUNNEL_DATA and packet.get(Field.DATA):
            self.near.take(packet[Field.DATA])

    async def run(self, pane_id: int, host: str, port: int) -> None:
        try:
            far = await _toward(self.connection.pymux, pane_id, host, port)
        except (CommandException, OSError) as error:
            logger.info("A tunnel to %s:%d at pane %%%d could not open: %s", host, port, pane_id, error)
            await self.near.aclose()
            return
        await _carry(self.near, far)


class _Listening:
    "One port here, and the pane whose machine its connections reach."

    def __init__(self, peer: Peer, pane_id: int, host: str, port: int) -> None:
        self.peer = peer
        self.pane_id = pane_id
        self.host = host
        self.port = port
        self.open = 0
        self.last = time.monotonic()

    def goes_to(self, peer: Peer, pane_id: int, port: int) -> bool:
        return (self.peer, self.pane_id, self.port) == (peer, pane_id, port)

    async def serve(self, listener: anyio.abc.Listener[anyio.abc.SocketStream], idle: float) -> None:
        "Carry each connection, until the port sat unused for `idle` seconds."
        async with listener, anyio.create_task_group() as tasks:
            tasks.start_soon(listener.serve, self._carry, tasks)
            while self.open or time.monotonic() - self.last < idle:
                await anyio.sleep(IDLE_POLL)
            tasks.cancel_scope.cancel()

    async def _carry(self, near: anyio.abc.ByteStream) -> None:
        self.open += 1
        try:
            try:
                far = await _LinkStream.open(self.peer, self.pane_id, self.host, self.port)
            except (CommandException, *_ENDED) as error:
                logger.info("Could not reach %s:%d at %s: %s", self.host, self.port, self.peer.address, error)
                await near.aclose()
                return
            await _carry(near, far)
        finally:
            self.open -= 1
            self.last = time.monotonic()


class Tunnels:
    "The ports this server listens on for loopback URLs of mirrored panes, by the port here."

    KEEP: ClassVar[dict[str, Keep]] = {
        # A listener ends with the server; a later open listens again.
        "pymux": Keep.REBUILT,
        "listening": Keep.DROPPED,
    }

    def __init__(self, pymux: Pymux) -> None:
        self.pymux = pymux
        self.listening: dict[int, _Listening] = {}

    async def opened(self, program: RemoteProgram, url: str, idle: float) -> str:
        """
        `url` as this machine reaches it: on a port here that leads to
        the machine `program` runs on. The program's own port when it is
        free here, or one that already leads there.
        """
        found = loopback_of(url)
        assert found is not None
        host, port = found.host, found.port
        peer, pane_id = program.peer, program.pane_id
        for here, listening in self.listening.items():
            if listening.goes_to(peer, pane_id, port) and (found.movable or here == port):
                return url if here == port else with_port(url, here)
        try:
            listener = await anyio.create_tcp_listener(local_host=host, local_port=port)
        except OSError:
            if not found.movable:
                raise
            # Any free port, on one address: "localhost" names two, and
            # each would get a port of its own.
            listener = await anyio.create_tcp_listener(
                local_host="127.0.0.1" if host == "localhost" else host, local_port=0
            )
        here = listener.extra(anyio.abc.SocketAttribute.local_port)
        listening = self.listening[here] = _Listening(peer, pane_id, host, port)
        assert self.pymux.tasks is not None
        self.pymux.tasks.start_soon(self._serve, here, listening, listener, idle)
        return url if here == port else with_port(url, here)

    async def _serve(
        self, here: int, listening: _Listening, listener: anyio.abc.Listener[anyio.abc.SocketStream], idle: float
    ) -> None:
        try:
            await listening.serve(listener, idle)
        finally:
            del self.listening[here]
