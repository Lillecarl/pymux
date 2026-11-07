"""
Opening an async SQLite connection, the one way pymux does it.

**The open is shielded.** `apsw.Connection.as_async` starts a worker
thread that is not a daemon, then awaits the open in it. A cancel during
that await leaves nobody to close the thread, and the process can never
exit: the server hung at `kill-server` this way. Shielded, the cancel
lands after the connection exists, and the caller's `aclose` ends the
thread. Lillecarl/pyterm#968.
"""

from __future__ import annotations

import anyio
import apsw

__all__ = ["connect"]


async def connect(filename: str) -> apsw.AsyncConnection:
    "An async connection to `filename`; the caller closes it."
    with anyio.CancelScope(shield=True):
        db = await apsw.Connection.as_async(filename)
    return db
