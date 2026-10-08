"""
A pane, streamed to a caller that draws it.

`Pane.capture_html` gives a whole screen each time it is asked. This
gives the rows that changed, once each, as the frames of
Lillecarl/pymux#461 -- which is what a caller that draws a pane several
times a second wants, and what a relay in front of a browser carries.

**A frame is a dict and nothing here reads one.** The shape of a frame is
the protocol; this carries them. So a relay opens a stream, sends each
frame on as it arrives, and sends back what its viewer says, without
knowing what a style table is.

**anyio, like the rest of pyterm.** A caller that relays to a browser
is a web server and already runs a loop, so this is
`anyio.connect_unix` and nothing else: one bidirectional stream,
sent and received on.
"""

import json
from typing import Any, AsyncIterator, Dict, Optional

import anyio

from .protocol import Field, Packet

__all__ = ["PaneStream", "StreamRefused"]

#: The byte that ends one JSON message on the wire.
_END = b"\0"


class StreamRefused(RuntimeError):
    "The server would not stream that pane, and said why."


class PaneStream:
    """
    One open stream of one pane.

    Use it as an async context manager, and read it as an async iterator:

        async with PaneStream(server.socket_path, pane.id) as stream:
            async for frame in stream:
                draw(frame)

    `writable` asks for a stream that takes input. Without it `send_keys`
    and `paste` are refused by the server, which is worth having even
    though a caller holding pymux's socket could always `send-keys`: it
    stops a bug in a relay's own loop turning a read-only viewer into a
    writing one.
    """

    def __init__(
        self, socket_path: str, pane_id: str, writable: bool = False
    ) -> None:
        self.socket_path = socket_path
        self.pane_id = pane_id
        self.writable = writable
        self._stream: Optional[anyio.abc.ByteStream] = None
        self._buffer = b""

    async def __aenter__(self) -> "PaneStream":
        await self.open()
        return self

    async def __aexit__(self, *_exception) -> None:
        await self.close()

    async def open(self) -> None:
        self._stream = await anyio.connect_unix(self.socket_path)
        await self._packet(
            {
                Field.CMD: Packet.STREAM_PANE,
                Field.PANE: self.pane_id,
                Field.WRITABLE: self.writable,
            }
        )

    async def close(self) -> None:
        if self._stream is None:
            return
        try:
            await self._stream.aclose()
        except OSError:
            pass
        self._stream = None

    # -- reading -------------------------------------------------------

    def __aiter__(self) -> AsyncIterator[Dict[str, Any]]:
        return self._frames()

    async def _frames(self) -> AsyncIterator[Dict[str, Any]]:
        """
        Every frame, until the server closes.

        **The close is the end, and a `gone` frame is a courtesy.** A pane
        that ends usually sends one; the last pane of a server does not,
        because its going stops the server and there is nothing left to
        speak. So a caller reads the loop ending as the pane ending, and
        `gone` only as the reason.
        """
        while True:
            packet = await self._next_packet()
            if packet is None:
                return
            kind = packet.get(Field.CMD)
            if kind == Packet.STREAM_OUT:
                yield json.loads(packet[Field.DATA])
            elif kind == Packet.ERR:
                raise StreamRefused(packet.get(Field.DATA, "").strip())
            # "exit" and anything else: the server is finishing.

    async def _next_packet(self) -> Optional[Dict[str, Any]]:
        assert self._stream is not None, "the stream is not open"

        while _END not in self._buffer:
            try:
                data = await self._stream.receive()
            except anyio.EndOfStream, anyio.ClosedResourceError:
                return None
            self._buffer += data

        raw, self._buffer = self._buffer.split(_END, 1)
        if not raw:
            return await self._next_packet()
        return json.loads(raw.decode("utf-8"))

    # -- writing -------------------------------------------------------

    async def send_keys(self, keys: str) -> None:
        """
        Send one or more keys, **by name**: `C-c`, `Escape`, `a`.

        The server spells them, because it is what knows which keyboard
        protocol the program in the pane asked for. A caller that sent an
        escape sequence would be guessing, and would be wrong the moment
        the program changed its mind.
        """
        await self.send({"type": "input", "keys": keys})

    async def paste(self, text: str) -> None:
        """
        Send text as a paste.

        The server wraps it in the paste markers when the program asked
        for those, and leaves it alone when it did not.
        """
        await self.send({"type": "paste", "text": text})

    async def send(self, message: Dict[str, Any]) -> None:
        """
        Send one message as it stands.

        This is what a relay uses: a viewer's message goes through
        without being read here.
        """
        await self._packet({Field.CMD: Packet.STREAM_IN, Field.DATA: json.dumps(message)})

    async def _packet(self, packet: Dict[str, Any]) -> None:
        assert self._stream is not None, "the stream is not open"
        await self._stream.send(json.dumps(packet).encode("utf-8") + _END)
