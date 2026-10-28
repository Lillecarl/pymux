"""
What each client moves on the wire, to it and from it.

The rules this judges: each end of a pipe counts what it sent and took
in, as the wire carried it, so a compressed stream counts its zstd
bytes; `#{client_bytes_down}` and `#{client_bytes_up}` read one client's
counts; and a client that leaves keeps its bytes in the server's total.
Lillecarl/pymux#677.
"""

from __future__ import annotations

import socket

import anyio
import pytest
from libpymux.wire import encode_json
from prompt_toolkit.data_structures import Size
from session import over_connection

from pymux.answer import Answer
from pymux.introspect import traffic
from pymux.pipes.memory import connect_in_memory
from pymux.pipes.posix import PosixSocketConnection

SIZE = Size(rows=24, columns=80)


async def test_a_memory_pipe_counts_both_ends():
    server_end, client_end = connect_in_memory()
    frame = encode_json({"cmd": "in", "data": "hello"})
    client_end.write_nowait(frame)
    await server_end.read()

    assert (client_end.bytes_out, server_end.bytes_in) == (len(frame), len(frame))
    assert (client_end.bytes_in, server_end.bytes_out) == (0, 0)


@pytest.fixture
def pair():
    server, client = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    client.setblocking(False)
    connection = PosixSocketConnection(server)
    yield connection, client
    connection.close()
    client.close()


async def _take(client: socket.socket) -> bytes:
    "Everything the socket holds now, once it holds something."
    await anyio.wait_readable(client)
    taken = b""
    while True:
        try:
            chunk = client.recv(1 << 20)
        except BlockingIOError:
            return taken
        taken += chunk


async def test_a_socket_counts_what_the_wire_carried(pair):
    "After zstd: what a metered link pays for, not the frame."
    connection, client = pair
    with anyio.fail_after(10):
        await connection.write(encode_json({"cmd": "ok"}), compress_after=True)
        frame = encode_json({"cmd": "out", "data": "x" * 100_000})
        await connection.write(frame)
        carried = b""
        while len(carried) < connection.bytes_out:
            carried += await _take(client)

        client.send(b"typed")
        while connection.bytes_in == 0:
            connection._reader.feed(await connection._read_chunk())

    assert connection.bytes_out == len(carried)
    assert connection.bytes_out < len(frame)
    assert connection.bytes_in == len(b"typed")


async def _lines(session, command) -> list[str]:
    session.pymux.answer = Answer()
    try:
        session.pymux.handle_command(command)
        return list(session.pymux.answer.output)
    finally:
        session.pymux.answer = None


async def test_each_client_reads_its_own_counts():
    async with over_connection() as session:
        here, _ = await session.attach("here", SIZE)
        there, _ = await session.attach("there", SIZE)

        said = await _lines(session, "list-clients -F '#{client_tty} #{client_bytes_down} #{client_bytes_up}'")

        pipes = {state.connection.name: state.connection.pipe_connection for state in (here, there)}
        counted = {name: (pipe.bytes_out, pipe.bytes_in) for name, pipe in pipes.items()}
        read = {name: (int(down), int(up)) for name, down, up in (line.split() for line in said)}
        assert read.keys() == counted.keys()
        for name, (down, up) in read.items():
            # A frame may go out between the listing and the count.
            assert 0 < down <= counted[name][0]
            assert 0 < up == counted[name][1]


async def test_a_client_reads_its_rate_in_a_unit():
    "The arithmetic is `test_a_rate.py`'s; this is the wiring. Lillecarl/pymux#678."
    async with over_connection() as session:
        await session.attach("here", SIZE)

        (said,) = await _lines(session, "list-clients -F '#{client_rate_down}|#{client_rate_up}'")

        down, up = said.split("|")
        assert down.endswith("B/s") and up.endswith("B/s")


async def test_a_client_that_leaves_stays_in_the_total():
    async with over_connection() as session:
        here, _ = await session.attach("here", SIZE)
        pipe = here.connection.pipe_connection
        before = traffic(session.pymux)

        await session.detach(here)

        assert session.pymux.counters.gone_bytes_out == pipe.bytes_out > 0
        assert session.pymux.counters.gone_bytes_in == pipe.bytes_in > 0
        after = traffic(session.pymux)
        assert after[0] >= before[0] and after[1] >= before[1]
