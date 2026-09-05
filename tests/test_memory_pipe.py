"""
The in-memory pipe carries what the socket carries.

`pymux integrated` puts a server and a client in one process, and this
pair of ends joins them. It has to behave like the socket: packets
arrive whole and in order, and the end of one side is the end of the
other.
"""
import asyncio

import pytest

from pymux.pipes import BrokenPipeError, connect_in_memory


def run(coro):
    return asyncio.run(coro)


def test_a_packet_arrives_as_the_bytes_that_were_written():
    async def go():
        server, client = connect_in_memory()
        await server.write('{"cmd": "out", "data": "héllo"}')
        return await client.read()

    assert run(go()) == b'{"cmd": "out", "data": "h\xc3\xa9llo"}'


def test_the_packets_arrive_in_the_order_they_were_written():
    async def go():
        server, client = connect_in_memory()
        for i in range(10):
            await server.write(str(i))
        return [await client.read() for _ in range(10)]

    assert run(go()) == [str(i).encode() for i in range(10)]


def test_both_ends_carry_their_own_way():
    async def go():
        server, client = connect_in_memory()
        await server.write("to the client")
        await client.write("to the server")
        return await client.read(), await server.read()

    assert run(go()) == (b"to the client", b"to the server")


def test_a_read_waits_for_a_write():
    async def go():
        server, client = connect_in_memory()

        async def later():
            await asyncio.sleep(0)
            await server.write("late")

        task = asyncio.ensure_future(later())
        packet = await client.read()
        await task
        return packet

    assert run(go()) == b"late"


def test_a_closed_end_ends_the_read_of_the_peer():
    async def go():
        server, client = connect_in_memory()
        server.close()
        with pytest.raises(BrokenPipeError):
            await client.read()

    run(go())


def test_what_was_written_before_the_close_still_arrives():
    async def go():
        server, client = connect_in_memory()
        await server.write("last word")
        server.close()
        first = await client.read()
        with pytest.raises(BrokenPipeError):
            await client.read()
        return first

    assert run(go()) == b"last word"


def test_a_write_to_a_closed_peer_is_a_broken_pipe():
    async def go():
        server, client = connect_in_memory()
        client.close()
        with pytest.raises(BrokenPipeError):
            await server.write("nobody reads this")

    run(go())


def test_a_write_after_this_end_closed_is_a_broken_pipe():
    async def go():
        server, client = connect_in_memory()
        server.close()
        with pytest.raises(BrokenPipeError):
            await server.write("nothing")

    run(go())


def test_closing_twice_does_nothing_the_second_time():
    async def go():
        server, client = connect_in_memory()
        server.close()
        server.close()
        with pytest.raises(BrokenPipeError):
            await client.read()

    run(go())
