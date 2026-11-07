"""
What a server does on a slow clock while it serves: it raises the
activity and silence marks of the windows, and it forgets the jobs and
the notifications nobody needs any more. `Pymux.running` starts each
one, and none of them may take the server with it.
"""

from __future__ import annotations

import faulthandler
import time
from functools import partial
from typing import TYPE_CHECKING, TextIO

import anyio
import anyio.abc

from ..enums import Woke
from ..jobs.table import JOB_SWEEP_EVERY
from ..log import logger

if TYPE_CHECKING:
    from ..notify.notifications import Notification
    from .main import Pymux

__all__ = ["seen_of", "sweep", "watch_the_loop", "watch_window_output"]


#: How often the loop watch wakes, and how late a wake may be before it
#: is logged, in seconds. A live pane's reads once stood still 3.85s
#: with nothing in the log to say whether the server was the one held.
#: Lillecarl/pyterm#938.
LOOP_TICK = 0.25
LOOP_STALL = 1.0


async def watch_the_loop(tick: float = LOOP_TICK, stall: float = LOOP_STALL, stacks: TextIO | None = None) -> None:
    """
    Log each turn of the loop that held it for `stall` seconds or more.

    With `stacks`, faulthandler's own thread writes every stack there
    while the loop is still held: a log line after the stall cannot
    say what held it. The C timer needs no GIL, so a stall inside one C
    call is caught too. Lillecarl/pyterm#938.
    """
    try:
        while True:
            if stacks is not None:
                faulthandler.dump_traceback_later(tick + stall, file=stacks)
            before = time.monotonic()
            await anyio.sleep(tick)
            late = time.monotonic() - before - tick
            if late >= stall:
                if stacks is not None:
                    logger.warning("The loop stood still %.2fs; its stacks are in %s.", late, stacks.name)
                else:
                    logger.warning("The loop stood still %.2fs.", late)
    finally:
        if stacks is not None:
            faulthandler.cancel_dump_traceback_later()


#: How often the windows are checked for output and for quiet, in
#: seconds: a mark goes up within this of what raised it.
WINDOW_OUTPUT_INTERVAL = 1.0


async def watch_window_output(pymux: Pymux) -> None:
    "Raise the activity and silence marks, once a second. Lillecarl/pyterm#769."
    while True:
        await anyio.sleep(WINDOW_OUTPUT_INTERVAL)
        now = anyio.current_time()
        try:
            raised = False
            for session in pymux.sessions:
                raised |= session.arrangement.check_output(pymux.apps, now)
            if raised:
                pymux.invalidate(Woke.WINDOW_MARKED)
        except Exception:
            # Like the clock: this must not take the server with it.
            logger.exception("Checking the windows for output failed.")


async def sweep(pymux: Pymux) -> None:
    """
    Forget the jobs idle past `job_ttl`, and the notifications seen
    longer ago than `notification-keep-seen`, on a slow clock.

    The time to live is read each turn, so a `set-option job-ttl`
    reaches the sweep after this one.
    """
    while True:
        await anyio.sleep(JOB_SWEEP_EVERY)
        try:
            if pymux.options.job_ttl:
                pymux.jobs.sweep(pymux.options.job_ttl)
        except Exception:
            logger.exception("The sweep of idle jobs failed.")
        try:
            pymux.notification_center.sweep(partial(seen_of, pymux), pymux.options.notification_keep_seen, time.time())
        except Exception:
            logger.exception("The sweep of seen notifications failed.")


def seen_of(pymux: Pymux, record: Notification) -> float | None:
    """
    When a client saw the window a notification came from, or None.
    One with no window to look at is seen now, so it goes in time.
    """
    found = pymux.place_of_pane_id(record.pane_id)
    if found is None:
        return time.time()
    session, window, _pane = found
    if window.seen_at >= record.at:
        return window.seen_at
    return time.time() if session.arrangement.watched(window, pymux.apps) else None
