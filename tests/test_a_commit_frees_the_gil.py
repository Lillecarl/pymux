"""
A commit on a worker thread leaves the other threads running. apsw's
`with db:` commits through `sqlite3_exec`, which held the GIL, so one
slow fsync of the action log froze the server for 39s.
`nix/patches/apsw-context-manager-releases-the-gil.patch`,
Lillecarl/pymux#936.
"""

from __future__ import annotations

import threading
import time
from itertools import pairwise

import apsw

#: Big enough that writing it out takes a commit tenths of a second.
SIZE = 128 << 20


def test_a_commit_lets_another_thread_run(tmp_path):
    ready = threading.Event()
    window: list[tuple[float, float]] = []

    def commit() -> None:
        db = apsw.Connection(str(tmp_path / "big.db"))
        db.execute("PRAGMA journal_mode = WAL")
        # Every dirty page stays in the cache until the commit writes it.
        db.execute("PRAGMA cache_size = -%d" % (2 * SIZE >> 10))
        db.execute("CREATE TABLE t (b BLOB)")
        db.__enter__()
        db.execute("INSERT INTO t VALUES (zeroblob(?))", (SIZE,))
        ready.set()
        start = time.monotonic()
        db.__exit__(None, None, None)
        window.append((start, time.monotonic()))
        db.close()

    worker = threading.Thread(target=commit)
    worker.start()
    # Not a wait on `ready` alone: a held GIL keeps this thread from
    # returning out of it until the commit is over, and every tick after
    # would fall outside the window.
    ticks = []
    while not window:
        if ready.is_set():
            ticks.append(time.monotonic())
    worker.join()

    ((start, end),) = window
    inside = [start, *(t for t in ticks if start <= t <= end), end]
    longest = max(b - a for a, b in pairwise(inside))
    assert end - start > 0.05, "the commit is too quick to judge: %.3fs" % (end - start)
    assert longest < (end - start) / 2, "held %.3fs of a %.3fs commit" % (longest, end - start)
