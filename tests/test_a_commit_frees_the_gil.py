"""
A commit on a worker thread leaves the other threads running.

apsw's `with db:` commits through `sqlite3_exec`, which held the GIL, so
one slow fsync of the action log froze the server for 39s.
`nix/patches/apsw-context-manager-releases-the-gil.patch`,
Lillecarl/pymux#936.

A cursor dropped at a row commits too: its reset halts the statement,
and apsw reset with the GIL held. The action log's upkeep dropped its
`PRAGMA incremental_vacuum` at the first row, and the commit of that
row froze the server for 22s.
`nix/patches/apsw-reset-releases-the-gil.patch`, Lillecarl/pymux#940.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from itertools import pairwise

import apsw

#: Big enough that writing it out takes a commit tenths of a second.
SIZE = 128 << 20


def longest_hold(prepare: Callable[[], Callable[[], None]]) -> tuple[float, float]:
    """
    Run `prepare` on a worker, then time the call it answers while this
    thread ticks. Answers how long the call took and this thread's
    longest gap inside it.
    """
    ready = threading.Event()
    window: list[tuple[float, float]] = []

    def work() -> None:
        call = prepare()
        ready.set()
        start = time.monotonic()
        call()
        window.append((start, time.monotonic()))

    worker = threading.Thread(target=work)
    worker.start()
    # Not a wait on `ready` alone: a held GIL keeps this thread from
    # returning out of it until the call is over, and every tick after
    # would fall outside the window.
    ticks = []
    while not window:
        if ready.is_set():
            ticks.append(time.monotonic())
    worker.join()

    ((start, end),) = window
    inside = [start, *(t for t in ticks if start <= t <= end), end]
    return end - start, max(b - a for a, b in pairwise(inside))


def a_database(path) -> apsw.Connection:
    db = apsw.Connection(str(path))
    db.execute("PRAGMA auto_vacuum = INCREMENTAL")
    db.execute("PRAGMA journal_mode = WAL")
    # Every dirty page stays in the cache until the commit writes it.
    db.execute("PRAGMA cache_size = -%d" % (2 * SIZE >> 10))
    db.execute("CREATE TABLE t (b BLOB)")
    return db


def test_a_commit_lets_another_thread_run(tmp_path):
    def prepare():
        db = a_database(tmp_path / "big.db")
        db.__enter__()
        db.execute("INSERT INTO t VALUES (zeroblob(?))", (SIZE,))
        return lambda: db.__exit__(None, None, None)

    took, held = longest_hold(prepare)
    assert took > 0.05, "the commit is too quick to judge: %.3fs" % took
    assert held < took / 2, "held %.3fs of a %.3fs commit" % (held, took)


def test_a_dropped_cursor_lets_another_thread_run(tmp_path):
    def prepare():
        db = a_database(tmp_path / "big.db")
        # RETURNING writes every row before the first one comes back, so
        # the statement stops at a row with the whole write uncommitted.
        cursor = db.execute("INSERT INTO t VALUES (zeroblob(?)) RETURNING rowid", (SIZE,))
        return cursor.close

    took, held = longest_hold(prepare)
    assert took > 0.05, "the commit is too quick to judge: %.3fs" % took
    assert held < took / 2, "held %.3fs of a %.3fs commit" % (held, took)
