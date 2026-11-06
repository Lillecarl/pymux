"""
A turn of the loop that holds it is logged, so a pane that stood still
says whether the server was the one held. Lillecarl/pymux#936.
"""

from __future__ import annotations

import logging
import time

import anyio

from pymux.core.chores import watch_the_loop


async def test_a_held_loop_is_logged(caplog):
    with caplog.at_level(logging.WARNING, logger="pymux"):
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(watch_the_loop, 0.01, 0.2)
            await anyio.sleep(0.05)
            time.sleep(0.3)  # The stall: nothing else runs.
            await anyio.sleep(0.05)
            tasks.cancel_scope.cancel()
    (record,) = [r for r in caplog.records if "stood still" in r.getMessage()]
    assert record.levelno == logging.WARNING


async def test_a_loop_that_turns_logs_nothing(caplog):
    with caplog.at_level(logging.WARNING, logger="pymux"):
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(watch_the_loop, 0.01, 0.2)
            await anyio.sleep(0.1)
            tasks.cancel_scope.cancel()
    assert not [r for r in caplog.records if "stood still" in r.getMessage()]
