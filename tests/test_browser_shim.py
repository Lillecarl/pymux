"""
`pymux-browser`'s pipe mode: Playwright's NUL-terminated CDP messages,
carried to a browser's WebSocket and back. Lillecarl/pymux#655.
"""

from __future__ import annotations

import os

import anyio
from websockets.asyncio.server import serve

from pymux.browser_shim import bridge


async def _read_messages(fd: int, count: int) -> list[bytes]:
    os.set_blocking(fd, False)
    data = b""
    while data.count(b"\0") < count:
        await anyio.wait_readable(fd)
        try:
            data += os.read(fd, 1 << 20)
        except BlockingIOError:
            continue
    return data.split(b"\0")[:count]


async def test_each_message_crosses_both_ways_and_the_pipes_end_reaches_the_bridge():
    heard: list[str] = []

    async def browser(socket) -> None:
        async for message in socket:
            heard.append(message)
            await socket.send("answer to " + message[:20])

    # Chrome takes a message of any size, as the bridge does.
    async with serve(browser, "127.0.0.1", 0, max_size=None) as server:
        port = server.sockets[0].getsockname()[1]
        program_reads, bridge_writes = os.pipe()
        bridge_reads, program_writes = os.pipe()
        large = '{"id":3,"data":"%s"}' % ("x" * 3_000_000)
        with anyio.fail_after(10):
            async with anyio.create_task_group() as tasks:
                tasks.start_soon(bridge, "ws://127.0.0.1:%d" % port, bridge_reads, bridge_writes)
                # One message split over two writes, and one far over a
                # WebSocket's default limit of a megabyte.
                os.write(program_writes, b'{"id":1}\0{"id"')
                os.write(program_writes, b":2}\0")
                await anyio.to_thread.run_sync(os.write, program_writes, large.encode() + b"\0")
                answers = await _read_messages(program_reads, 3)
                os.close(program_writes)
        assert heard == ['{"id":1}', '{"id":2}', large]
        assert answers == [b'answer to {"id":1}', b'answer to {"id":2}', b"answer to " + large[:20].encode()]
