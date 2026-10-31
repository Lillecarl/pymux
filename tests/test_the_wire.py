"""
The wire turns from JSON to msgpack where both ends agree, one direction
at a time, at a `wire` packet. Lillecarl/pymux#633.
"""

from __future__ import annotations

import socket

import anyio
from libpymux.protocol import READ_SIZE
from libpymux.wire import MSGPACK, MSGPACK_ZSTD, Compressor, PacketReader, encode_json, encode_msgpack

from pymux.client.terminal import TerminalClient
from pymux.connection.server import ServerConnection
from pymux.core.main import Pymux
from pymux.pipes.memory import connect_in_memory
from pymux.pipes.posix import COMPRESS_IN_A_THREAD, PosixSocketConnection

TURN = {"cmd": "wire", "wire": MSGPACK}


def test_a_reader_turns_at_the_wire_packet_inside_one_read():
    reader = PacketReader()
    stream = (
        encode_json({"cmd": "out", "data": "json"}) + encode_json(TURN) + encode_msgpack({"cmd": "out", "data": "\0"})
    )
    assert reader.feed(stream) == [{"cmd": "out", "data": "json"}, TURN, {"cmd": "out", "data": "\0"}]


def test_a_msgpack_frame_cut_across_reads_comes_out_whole_and_once():
    reader = PacketReader()
    reader.feed(encode_json(TURN))
    frame = encode_msgpack({"cmd": "out", "data": "x" * 100_000})
    got = []
    for at in range(0, len(frame), 1000):
        got += reader.feed(frame[at : at + 1000])
    assert got == [{"cmd": "out", "data": "x" * 100_000}]


def test_a_reader_turns_to_zstd_inside_one_read_and_reads_it_cut_anywhere():
    compressor = Compressor()
    packets = [{"cmd": "out", "data": "\x1b[1mline %d\x1b[0m\r\n" % n * 20} for n in range(50)]
    stream = encode_json({"cmd": "wire", "wire": MSGPACK_ZSTD}) + b"".join(
        compressor.compress(encode_msgpack(packet)) for packet in packets
    )
    reader = PacketReader()
    got = []
    for at in range(0, len(stream), 777):
        got += reader.feed(stream[at : at + 777])
    assert got == [{"cmd": "wire", "wire": MSGPACK_ZSTD}, *packets]
    assert reader.zstd


def test_a_packet_that_does_not_decode_is_dropped_and_the_rest_read():
    reader = PacketReader()
    assert reader.feed(b"not json\0" + encode_json({"cmd": "in"})) == [{"cmd": "in"}]
    assert reader.dropped == 1


START_GUI = {
    "cmd": "start-gui",
    "detach-others": False,
    "color-depth": None,
    "term": "xterm-256color",
    "data": "",
}


async def frames_until_out(client_end) -> list:
    said = []
    with anyio.fail_after(10):
        while not any(packet.get("cmd") == "out" for packet in said):
            said.append(await client_end.read())
    return said


async def test_a_server_writes_msgpack_to_a_client_that_offers_it():
    pymux = Pymux()
    async with pymux.running():
        server_end, client_end = connect_in_memory()
        ServerConnection(pymux, server_end)
        client_end.write_nowait(encode_json({**START_GUI, "wire": [MSGPACK]}))

        said = await frames_until_out(client_end)

        assert said[0] == TURN
        assert client_end._reader.msgpack
        pymux.stop()


async def test_a_server_keeps_json_for_a_client_that_does_not():
    pymux = Pymux()
    async with pymux.running():
        server_end, client_end = connect_in_memory()
        ServerConnection(pymux, server_end)
        client_end.write_nowait(encode_json(START_GUI))

        said = await frames_until_out(client_end)

        assert TURN not in said
        assert not client_end._reader.msgpack
        pymux.stop()


async def test_a_socket_compresses_for_a_client_that_asks():
    "The socket route, which an ssh channel ends in."
    pymux = Pymux()
    async with pymux.running():
        server_socket, client_socket = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        client_socket.setblocking(False)
        ServerConnection(pymux, PosixSocketConnection(server_socket))
        client_socket.sendall(encode_json({**START_GUI, "wire": [MSGPACK_ZSTD, MSGPACK]}))

        reader = PacketReader()
        said: list = []
        with anyio.fail_after(10):
            while not any(packet.get("cmd") == "out" for packet in said):
                await anyio.wait_readable(client_socket)
                said += reader.feed(client_socket.recv(READ_SIZE))

        assert said[0] == {"cmd": "wire", "wire": MSGPACK_ZSTD}
        assert reader.zstd
        pymux.stop()
        client_socket.close()


async def test_a_frame_compressed_in_a_thread_keeps_its_place_in_the_stream():
    """
    A packet past `COMPRESS_IN_A_THREAD` is compressed off the loop, and
    a small one written while it is goes into the zstd stream after it.
    Lillecarl/pymux#632.
    """
    server_socket, client_socket = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    client_socket.setblocking(False)
    connection = PosixSocketConnection(server_socket)
    turn = {"cmd": "wire", "wire": MSGPACK_ZSTD}
    big = {"cmd": "out", "data": "".join("%08d" % n for n in range(COMPRESS_IN_A_THREAD))}
    small = {"cmd": "out", "data": "after"}
    reader = PacketReader()
    said: list = []

    async def read_all() -> None:
        while len(said) < 3:
            await anyio.wait_readable(client_socket)
            said.extend(reader.feed(client_socket.recv(READ_SIZE)))

    with anyio.fail_after(10):
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(read_all)
            await connection.write(encode_json(turn), compress_after=True)
            tasks.start_soon(connection.write, encode_msgpack(big))
            await anyio.sleep(0)
            await connection.write(encode_msgpack(small))

    assert said == [turn, big, small]
    connection.close()
    client_socket.close()


async def test_the_memory_pipe_never_compresses():
    pymux = Pymux()
    async with pymux.running():
        server_end, client_end = connect_in_memory()
        ServerConnection(pymux, server_end)
        client_end.write_nowait(encode_json({**START_GUI, "wire": [MSGPACK_ZSTD, MSGPACK]}))

        said = await frames_until_out(client_end)

        assert said[0] == TURN
        pymux.stop()


def test_a_client_answers_the_turn_in_json_and_writes_msgpack_after():
    sent = []

    class Recording(TerminalClient):
        def send_packet(self, data):
            sent.append(self._encode(data))

    client = Recording()
    client._process(TURN)
    client.send_packet({"cmd": "in", "data": "x"})

    assert sent == [encode_json(TURN), encode_msgpack({"cmd": "in", "data": "x"})]
