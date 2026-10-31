"""
What a server does on a slow clock while it serves: it raises the
activity and silence marks of the windows, and it forgets the jobs and
the notifications nobody needs any more. `Pymux.running` starts each
one, and none of them may take the server with it.
"""

from __future__ import annotations

import time
from functools import partial
from typing import TYPE_CHECKING

import anyio
import anyio.abc

from ..enums import Woke
from ..jobs.table import JOB_SWEEP_EVERY
from ..log import logger

if TYPE_CHECKING:
    from ..notify.notifications import Notification
    from .main import Pymux

__all__ = ["seen_of", "sweep", "watch_window_output"]


#: How often the windows are checked for output and for quiet, in
#: seconds: a mark goes up within this of what raised it.
WINDOW_OUTPUT_INTERVAL = 1.0


async def watch_window_output(pymux: Pymux) -> None:
    "Raise the activity and silence marks, once a second. Lillecarl/pymux#767."
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
