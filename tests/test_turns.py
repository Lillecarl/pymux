"""
`Turns`: one writer at a time, and sound ahead of whatever waits.
Lillecarl/pyterm#659.

Each step waits for the writers to queue, not for a sleep: under six
suites a 0.01 s sleep outlasted part 1's 0.05 s on the wire, and part 2
took the turn before sound came. Lillecarl/pyterm#1000.
"""

from __future__ import annotations

import anyio
from anyio.lowlevel import checkpoint

from pymux.pipes.base import Turns


async def queued(turns: Turns, waiting: int, urgent: int = 0) -> None:
    "Until that many writers wait, each kind."
    while (len(turns._waiting), len(turns._urgent)) != (waiting, urgent):
        await checkpoint()


async def test_an_urgent_writer_goes_ahead_of_every_waiting_one():
    turns = Turns()
    order: list[str] = []
    started = anyio.Event()
    # The part on the wire: everything below queues behind it.
    sent = anyio.Event()

    async def write(name: str, urgent: bool = False) -> None:
        async with turns.turn(urgent):
            order.append(name)
            if name == "part 1":
                started.set()
                await sent.wait()

    with anyio.fail_after(10):
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(write, "part 1")
            await started.wait()
            tasks.start_soon(write, "part 2")
            tasks.start_soon(write, "part 3")
            await queued(turns, 2)
            tasks.start_soon(write, "sound", True)
            await queued(turns, 2, 1)
            sent.set()

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

    with anyio.fail_after(10):
        async with anyio.create_task_group() as tasks:
            async with turns.turn():
                tasks.start_soon(gives_up)
                tasks.start_soon(waits)
                await queued(turns, 1, 1)
                scopes[0].cancel()
                await queued(turns, 1, 0)

    assert order == ["waited"]
