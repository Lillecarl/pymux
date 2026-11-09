"""
The SOCKS5 answer at the client's end of a remote SOCKS forward
(`forward-port -R 1080`): the far sshd listens on the server's machine,
and each connection it carries here names where it goes. This side
connects there, from the machine the person sits at.
Lillecarl/pyterm#974.

**asyncssh answers SOCKS for a local listener only.** Read in asyncssh
2.24.0: `forward_socks` builds `socks.SSHSOCKSForwarder`, which reads a
socket and opens a channel. This side reads a channel and opens a
socket, so it reads the request itself.

SOCKS5 CONNECT with no authentication, RFC 1928: what Chromium, Firefox
and curl send for `socks5://` and `socks5h://`.
"""

from __future__ import annotations

import ipaddress
import struct
from enum import IntEnum
from typing import TYPE_CHECKING, Final

import anyio
from anyio import connect_tcp

if TYPE_CHECKING:
    from anyio.abc import SocketStream
    from asyncssh import SSHReader, SSHWriter

__all__ = ["answer"]

VERSION: Final = 5
NO_AUTHENTICATION: Final = 0
NO_ACCEPTABLE_METHOD: Final = 0xFF
CONNECT: Final = 1

#: How long a destination has to accept, in seconds. The browser waiting
#: on the far side gives up on its own clock; this one only stops a
#: connection to a black hole from holding a channel for ever.
CONNECT_DEADLINE: Final = 30.0

CHUNK: Final = 1 << 16


class Reply(IntEnum):
    SUCCEEDED = 0
    GENERAL_FAILURE = 1
    HOST_UNREACHABLE = 4
    CONNECTION_REFUSED = 5
    COMMAND_NOT_SUPPORTED = 7
    ADDRESS_TYPE_NOT_SUPPORTED = 8


class AddressType(IntEnum):
    IPV4 = 1
    DOMAIN = 3
    IPV6 = 4


class Refused(Exception):
    "A request this side does not carry, and the reply that says so, if any."

    def __init__(self, reply: Reply | None) -> None:
        super().__init__(reply)
        self.reply = reply


async def answer(reader: SSHReader[bytes], writer: SSHWriter[bytes]) -> None:
    "Carry one SOCKS connection to where it asks, until both ends finish."
    try:
        try:
            host, port = await read_request(reader, writer)
        except Refused as refused:
            if refused.reply is not None:
                _reply(writer, refused.reply)
            return

        try:
            with anyio.fail_after(CONNECT_DEADLINE):
                stream = await connect_tcp(host, port)
        except TimeoutError:
            _reply(writer, Reply.HOST_UNREACHABLE)
            return
        except OSError as error:
            _reply(writer, Reply.CONNECTION_REFUSED if _refused(error) else Reply.HOST_UNREACHABLE)
            return

        # The bound address is all zeroes: a client connecting through a
        # proxy has no use for the proxy's own outgoing address, and
        # this one would name the wrong machine to it anyway.
        _reply(writer, Reply.SUCCEEDED)

        try:
            async with stream, anyio.create_task_group() as tasks:
                tasks.start_soon(_to_destination, reader, stream)
                await _to_channel(stream, writer)
        except* GONE:
            pass
    except GONE:
        pass
    finally:
        writer.close()


#: What one end going away raises. Each connection is its own, so it
#: ends that one and nothing else.
GONE: Final = (OSError, EOFError, anyio.BrokenResourceError, anyio.ClosedResourceError)


async def read_request(reader: SSHReader[bytes], writer: SSHWriter[bytes]) -> tuple[str, int]:
    "The host and port a SOCKS5 client asks to reach, after its greeting."
    version, count = await reader.readexactly(2)
    if version != VERSION:
        raise Refused(None)  # Not SOCKS5, so there is no reply it reads.

    methods = await reader.readexactly(count)
    if NO_AUTHENTICATION not in methods:
        writer.write(bytes([VERSION, NO_ACCEPTABLE_METHOD]))
        raise Refused(None)
    writer.write(bytes([VERSION, NO_AUTHENTICATION]))

    _, command, _, kind = await reader.readexactly(4)

    if kind == AddressType.IPV4:
        host = str(ipaddress.IPv4Address(await reader.readexactly(4)))
    elif kind == AddressType.IPV6:
        host = str(ipaddress.IPv6Address(await reader.readexactly(16)))
    elif kind == AddressType.DOMAIN:
        (length,) = await reader.readexactly(1)
        try:
            host = (await reader.readexactly(length)).decode("ascii")
        except UnicodeDecodeError:
            raise Refused(Reply.ADDRESS_TYPE_NOT_SUPPORTED) from None
    else:
        raise Refused(Reply.ADDRESS_TYPE_NOT_SUPPORTED)

    (port,) = struct.unpack("!H", await reader.readexactly(2))

    if command != CONNECT:
        raise Refused(Reply.COMMAND_NOT_SUPPORTED)

    return host, port


def _refused(error: OSError) -> bool:
    """
    Whether every address of the destination refused. anyio 4 raises a
    bare `OSError` for a failed `connect_tcp`, with the real error, or
    a group of one per address, as its cause.
    """
    cause = error.__cause__
    if isinstance(cause, ExceptionGroup):
        return all(isinstance(one, ConnectionRefusedError) for one in cause.exceptions)
    return isinstance(cause or error, ConnectionRefusedError)


def _reply(writer: SSHWriter[bytes], reply: Reply) -> None:
    writer.write(bytes([VERSION, reply, 0, AddressType.IPV4, 0, 0, 0, 0, 0, 0]))


async def _to_destination(reader: SSHReader[bytes], stream: SocketStream) -> None:
    while data := await reader.read(CHUNK):
        await stream.send(data)
    await stream.send_eof()


async def _to_channel(stream: SocketStream, writer: SSHWriter[bytes]) -> None:
    async for data in stream:
        writer.write(data)
        await writer.drain()
    writer.write_eof()
