"""
A caller that arrives while the first window opens waits for it, where
it found no window and a command answered "no current window".
Lillecarl/pymux#571.
"""

from __future__ import annotations

import anyio
from prompt_toolkit.data_structures import Size
from session import in_this_process

from pymux.core.main import Pymux

SIZE = Size(rows=24, columns=80)


async def test_a_second_attach_finds_the_first_window():
    pymux = Pymux(startup_command="sleep 30")
    opening = pymux.panes.create_window

    async def slowly(**arguments):
        # The process takes its time to start, as it does under load.
        await anyio.sleep(0.3)
        await opening(**arguments)

    pymux.panes.create_window = slowly  # type: ignore[method-assign]
    windows_seen = []

    async with in_this_process(pymux) as session:

        async def second() -> None:
            await anyio.sleep(0.05)
            await session.attach("two", SIZE)
            windows_seen.append(len(pymux.sessions[0].arrangement.windows))

        async with anyio.create_task_group() as tasks:
            tasks.start_soon(session.attach, "one", SIZE)
            tasks.start_soon(second)

    assert windows_seen == [1]
