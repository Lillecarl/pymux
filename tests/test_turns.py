"""
`Turns`: one writer at a time, and sound ahead of whatever waits.
Lillecarl/pymux#657.
"""

from __future__ import annotations

import anyio

from pymux.pipes.base import Turns


async def test_an_urgent_writer_goes_ahead_of_every_waiting_one():
    turns = Turns()
    order: list[str] = []
    started = anyio.Event()

    async def write(name: str, urgent: bool = False) -> None:
        async with turns.turn(urgent):
            order.append(name)
            if name == "part 1":
                started.set()
                # The part on the wire: everything below queues behind it.
                await anyio.sleep(0.05)

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(write, "part 1")
        await started.wait()
        tasks.start_soon(write, "part 2")
        tasks.start_soon(write, "part 3")
        await anyio.sleep(0.01)
        tasks.start_soon(write, "sound", True)

    assert order == ["part 1", "sound", "part 2", "part 3"]


async def test_a_writer_cancelled_while_it_waits_leaves_the_turn_to_the_next():
    turns = Turns()
    order: list[str] = []
    scopes: list[anyio.CancelScope] = []

    async def gives_up() -> None:
        with anyio.CancelScope() as scope:
            scopes.append(scope)
            async with turns.turn(urgent=True):
                order.append("gave up")

    async def waits() -> None:
        async with turns.turn():
            order.append("waited")

    async with anyio.create_task_group() as tasks:
        async with turns.turn():
            tasks.start_soon(gives_up)
            tasks.start_soon(waits)
            await anyio.sleep(0.01)
            scopes[0].cancel()
            await anyio.sleep(0.01)

    assert order == ["waited"]
