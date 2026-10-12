"""
What a client's socket holds for it: one packet goes through whatever
its size, a client that stops reading is still let go, and `drained`
says when the terminal has taken everything. Lillecarl/pymux#418, #629.
"""

from __future__ import annotations

import socket

import anyio
import pytest

from pymux.pipes.base import BrokenPipeError
from pymux.pipes.posix import TOO_SLOW_BYTES, PosixSocketConnection


@pytest.fixture
def pair():
    server, client = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    client.setblocking(False)
    connection = PosixSocketConnection(server)
    yield connection, client
    connection.close()
    client.close()


async def read_all(client: socket.socket, length: int) -> int:
    taken = 0
    while taken < length:
        await anyio.wait_readable(client)
        taken += len(client.recv(1 << 20))
    return taken


async def test_one_packet_larger_than_the_bound_goes_through(pair):
    # A window frame is megabytes.
    connection, client = pair
    packet = "x" * (TOO_SLOW_BYTES * 3)
    with anyio.fail_after(10):
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(connection.write, packet)
            assert await read_all(client, len(packet) + 1) == len(packet) + 1


async def test_a_client_that_stops_reading_is_let_go(pair):
    connection, _client = pair
    with anyio.fail_after(10):
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(connection.write, "x" * (TOO_SLOW_BYTES * 2))
            await anyio.wait_all_tasks_blocked()
            with pytest.raises(BrokenPipeError):
                await connection.write("one more")
            tasks.cancel_scope.cancel()


async def test_drained_waits_for_the_terminal_to_take_it(pair):
    connection, client = pair
    packet = "x" * (TOO_SLOW_BYTES * 2)
    with anyio.fail_after(10):
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(connection.write, packet)
            await anyio.wait_all_tasks_blocked()
            assert connection.outstanding
            tasks.start_soon(read_all, client, len(packet) + 1)
            await connection.drained()
            assert not connection.outstanding
