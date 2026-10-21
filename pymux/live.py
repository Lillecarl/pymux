"""
What windows, panes, sessions and clients are, as live SQL tables.

The objects stay the truth, and these tables follow them the way the
job store follows `JobTable`: whatever changes them marks the store,
and a moment later (`WRITE_AFTER`) the store writes the rows that
changed since its last write and deletes the rows whose objects are
gone (`tables.ChangedRows`). So a burst of changes costs one write, a
keystroke costs the rows it changed and not the whole server, and
nothing reads a step half done. A reader (`sql`) has the store write
first, so it never reads the moment's lag. Step 5 of
Lillecarl/pymux#399, and Lillecarl/pymux#557; Carl chose the shape.

The schema is the snapshot's own, up to the jobs (`tables.LIVE_SCHEMA`),
and the snapshot's rows fill it (`snapshot.rows_of`). No screen goes in,
ever: what a program draws is not window management, and only the
hand-over file of an upgrade may hold it.

**One connection, on the loop thread.** A mark comes from a key
binding or a command handler, which are not coroutines, and the write
must happen before anything awaits. The database is a shared-cache
memory one, so the readers that `sql` uses on other threads can attach
it by its URI.
"""

from __future__ import annotations

import secrets
import sqlite3
from typing import TYPE_CHECKING

import anyio

from .log import logger
from .tables import LIVE_ROWS, LIVE_SCHEMA, ChangedRows

if TYPE_CHECKING:
    from .main import Pymux

__all__ = ["LIVE_URI", "LiveStore"]

#: Shared-cache and in memory, per store, like `jobstore.SHARED_URI`.
LIVE_URI = "file:pymux-live-%s?mode=memory&cache=shared"

#: Seconds before a write that failed is tried again.
RETRY_AFTER = 0.05

#: Seconds between a change and the write that follows it, so that a
#: burst of them -- typing -- costs one write after it.
WRITE_AFTER = 0.25


class LiveStore:
    "The live tables, and whether what they say is behind the objects."

    def __init__(self) -> None:
        # Here and not at the top: the snapshot reaches `pymux.main`,
        # which builds this store.
        from . import snapshot

        self._snapshot = snapshot
        self.uri = LIVE_URI % secrets.token_hex(8)
        # No busy wait: this writes on the loop, and sqlite3's default
        # five seconds froze the server whole while a reader held a
        # lock. Measured. A write that meets one fails, and `keep`
        # tries again.
        self.db = sqlite3.connect(self.uri, uri=True, timeout=0)
        self.db.executescript(LIVE_SCHEMA)
        self._rows = ChangedRows(LIVE_ROWS)
        #: Something changed since the last write.
        self.dirty = True
        self._woken: anyio.Event | None = None

    def mark(self) -> None:
        "Say that the objects changed. Cheap: the write waits for the loop."
        self.dirty = True
        if self._woken is not None:
            self._woken.set()

    def write(self, pymux: Pymux) -> None:
        "Write the rows that changed since the last write, if anything was marked."
        if not self.dirty:
            return
        with self.db:
            written = self._rows.write(self.db, self._snapshot.rows_of(pymux))
        self._rows.written = written
        self.dirty = False

    async def keep(self, pymux: Pymux) -> None:
        """
        Write after each loop step that changed something, for as long
        as the server runs. A write that fails says why in the log and
        tries again shortly, the store still marked.
        """
        while True:
            self._woken = anyio.Event()
            try:
                self.write(pymux)
            except Exception:
                logger.exception("The live store could not write its tables.")
                await anyio.sleep(RETRY_AFTER)
                continue
            await self._woken.wait()
            # Not at once: a write in the step after a key lands before
            # the frame that answers it, and a person typing waits for
            # it. Measured, 0.7 ms on the median round trip. A reader
            # has the store write first, so it never sees the wait.
            await anyio.sleep(WRITE_AFTER)

    def close(self) -> None:
        self.db.close()
