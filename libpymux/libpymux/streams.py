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

**asyncio, from the standard library.** A caller that relays to a browser
is a web server and already runs a loop, and `libpymux` promises one
dependency. So this is `asyncio.open_unix_connection` and nothing else --
no anyio here, whatever the rest of pyterm uses, because a library that
made its callers take a dependency to read a pane would be the wrong
trade.
"""

import asyncio
import json
from typing import Any, AsyncIterator, Dict, Optional

from .protocol import Packet

__all__ = ["PaneStream", "StreamRefused"]

#: The byte that ends one JSON message on the wire.
_END = b"\0"

#: How much to read at a time. A frame of a wide pane's first screen is
#: tens of kilobytes; every frame after it is small.
_CHUNK = 65536


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
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._buffer = b""

    async def __aenter__(self) -> "PaneStream":
        await self.open()
        return self

    async def __aexit__(self, *_exception) -> None:
        await self.close()

    async def open(self) -> None:
        self._reader, self._writer = await asyncio.open_unix_connection(
            self.socket_path
        )
        await self._packet(
            {
                "cmd": Packet.STREAM_PANE,
                "pane": self.pane_id,
                "writable": self.writable,
            }
        )

    async def close(self) -> None:
        if self._writer is None:
            return
        self._writer.close()
        try:
            await self._writer.wait_closed()
        except (OSError, asyncio.CancelledError):
            pass
        self._writer = None
        self._reader = None

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
            kind = packet.get("cmd")
            if kind == Packet.STREAM_OUT:
                yield json.loads(packet["data"])
            elif kind == Packet.ERR:
                raise StreamRefused(packet.get("data", "").strip())
            # "exit" and anything else: the server is finishing.

    async def _next_packet(self) -> Optional[Dict[str, Any]]:
        assert self._reader is not None, "the stream is not open"

        while _END not in self._buffer:
            data = await self._reader.read(_CHUNK)
            if not data:
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
        await self._packet({"cmd": Packet.STREAM_IN, "data": json.dumps(message)})

    async def _packet(self, packet: Dict[str, Any]) -> None:
        assert self._writer is not None, "the stream is not open"
        self._writer.write(json.dumps(packet).encode("utf-8") + _END)
        await self._writer.drain()
