"""
A port a mirrored pane's loopback URL opened here closes once it sat
unused for its idle time: not while a connection is open, and at once
when the time runs out, with no poll in between. Lillecarl/pyterm#1015.
"""

from __future__ import annotations

import anyio
import anyio.abc
from anyio.streams.stapled import StapledObjectStream
from fairclock import anyio as fair_anyio

from pymux.mirror import tunnel

IDLE = 0.3
DEADLINE = 10.0


async def test_a_port_ends_after_its_idle_time_and_not_while_used(monkeypatch):
    far_end = anyio.Event()

    async def opened(cls, peer, pane_id, host, port):
        send, receive = anyio.create_memory_object_stream[bytes]()
        return StapledObjectStream(send, receive)

    async def carry(near, far):
        await far_end.wait()
        await near.aclose()

    monkeypatch.setattr(tunnel._LinkStream, "open", classmethod(opened))
    monkeypatch.setattr(tunnel, "_carry", carry)

    listening = tunnel._Listening(None, 1, "127.0.0.1", 1)
    listener = await anyio.create_tcp_listener(local_host="127.0.0.1", local_port=0)
    port = listener.extra(anyio.abc.SocketAttribute.local_port)
    ended = anyio.Event()

    async def serve():
        await listening.serve(listener, IDLE)
        ended.set()

    with fair_anyio.fail_after(DEADLINE):
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(serve)
            client = await anyio.connect_tcp("127.0.0.1", port)
            while not listening.open:
                await anyio.sleep(0.01)

            await anyio.sleep(IDLE * 2)
            assert not ended.is_set(), "the port closed under an open connection"

            far_end.set()
            closed_at = anyio.current_time()
            await ended.wait()
            waited = anyio.current_time() - closed_at
            await client.aclose()

    assert IDLE * 0.9 <= waited < IDLE + 2.0, waited
