"""
The in-memory pipe carries what the socket carries.

`pymux integrated` puts a server and a client in one process, and this
pair of ends joins them. It has to behave like the socket: packets
arrive whole and in order, decoded the same way, and the end of one
side is the end of the other.
"""

from __future__ import annotations

import anyio
import pytest
from libpymux.wire import MSGPACK, encode_json, encode_msgpack

from pymux.pipes import BrokenPipeError, connect_in_memory


def run(func, *args):
    return anyio.run(func, *args)


def test_packet_arrives_as_it_was_written():
    async def go():
        server, client = connect_in_memory()
        await server.write(encode_json({"cmd": "out", "data": "héllo"}))
        return await client.read()

    assert run(go) == {"cmd": "out", "data": "héllo"}


def test_packets_arrive_in_order_they_were_written():
    async def go():
        server, client = connect_in_memory()
        for i in range(10):
            await server.write(encode_json({"n": i}))
        return [await client.read() for _ in range(10)]

    assert run(go) == [{"n": i} for i in range(10)]


def test_a_wire_packet_turns_the_reader_to_msgpack():
    async def go():
        server, client = connect_in_memory()
        await server.write(encode_json({"cmd": "wire", "wire": MSGPACK}))
        await server.write(encode_msgpack({"cmd": "out", "data": "\x1b[1mbold\0"}))
        return [await client.read() for _ in range(2)]

    assert run(go) == [{"cmd": "wire", "wire": MSGPACK}, {"cmd": "out", "data": "\x1b[1mbold\0"}]


def test_both_ends_carry_their_own_way():
    async def go():
        server, client = connect_in_memory()
        await server.write(encode_json({"to": "client"}))
        await client.write(encode_json({"to": "server"}))
        return await client.read(), await server.read()

    assert run(go) == ({"to": "client"}, {"to": "server"})


def test_read_waits_for_write():
    async def go():
        server, client = connect_in_memory()

        async def later():
            await anyio.sleep(0)
            await server.write(encode_json({"late": True}))

        async with anyio.create_task_group() as tg:
            tg.start_soon(later)
            packet = await client.read()
        return packet

    assert run(go) == {"late": True}


def test_closed_end_ends_read_of_peer():
    async def go():
        server, client = connect_in_memory()
        server.close()
        with pytest.raises(BrokenPipeError):
            await client.read()

    run(go)


def test_what_was_written_before_close_still_arrives():
    async def go():
        server, client = connect_in_memory()
        await server.write(encode_json({"last": "word"}))
        server.close()
        first = await client.read()
        with pytest.raises(BrokenPipeError):
            await client.read()
        return first

    assert run(go) == {"last": "word"}


def test_write_to_closed_peer_is_broken_pipe():
    async def go():
        server, client = connect_in_memory()
        client.close()
        with pytest.raises(BrokenPipeError):
            await server.write(encode_json({}))

    run(go)


def test_write_after_this_end_closed_is_broken_pipe():
    async def go():
        server, client = connect_in_memory()
        server.close()
        with pytest.raises(BrokenPipeError):
            await server.write(encode_json({}))

    run(go)


def test_closing_twice_does_nothing_second_time():
    async def go():
        server, client = connect_in_memory()
        server.close()
        server.close()
        with pytest.raises(BrokenPipeError):
            await client.read()

    run(go)
