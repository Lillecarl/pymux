"""
What windows, panes, sessions and clients are, as SQL tables to query.

The objects stay the truth. These tables catch up with them only when
somebody asks (`sql`): the store writes the rows that changed since
its last write and deletes the rows whose objects are gone
(`tables.ChangedRows`). **Nothing else writes them**, so typing, a
frame or a pane's output never touches SQLite. Step 5 of
Lillecarl/pymux#399, and Lillecarl/pymux#557.

The schema is the snapshot's own, up to the jobs (`tables.LIVE_SCHEMA`),
and the snapshot's rows fill it (`snapshot.rows_of`). No screen goes in,
ever, and no keyboard state: what a program draws and what a person is
typing are not window management, and only the hand-over file of an
upgrade may hold them.

The database is a shared-cache memory one, so the readers that `sql`
uses on other threads can attach it by its URI.
"""

from __future__ import annotations

import secrets
import sqlite3
from typing import TYPE_CHECKING

from .tables import LIVE_ROWS, LIVE_SCHEMA, ChangedRows

if TYPE_CHECKING:
    from .main import Pymux

__all__ = ["LIVE_URI", "LiveStore"]

#: Shared-cache and in memory, per store, like `jobstore.SHARED_URI`.
LIVE_URI = "file:pymux-live-%s?mode=memory&cache=shared"


class LiveStore:
    "The tables `sql` reads, written when it asks."

    def __init__(self) -> None:
        # Here and not at the top: the snapshot reaches `pymux.main`,
        # which builds this store.
        from . import snapshot

        self._snapshot = snapshot
        self.uri = LIVE_URI % secrets.token_hex(8)
        # No busy wait: this writes on the loop, and sqlite3's default
        # five seconds froze the server whole while a reader held a
        # lock. Measured. The readers take none
        # (`test_the_live_store_writes_while_questions_read`).
        self.db = sqlite3.connect(self.uri, uri=True, timeout=0)
        self.db.executescript(LIVE_SCHEMA)
        self._rows = ChangedRows(LIVE_ROWS)

    def write(self, pymux: Pymux) -> None:
        "Write the rows that changed since the last write."
        with self.db:
            written = self._rows.write(self.db, self._snapshot.rows_of(pymux))
        self._rows.written = written

    def close(self) -> None:
        self.db.close()
