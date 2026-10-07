"""
The jobs database: what the server ran, in sqlite the agents can ask.

The streams stay in Python -- deques of offset chunks on the live job
-- and everything a question could filter on lives here: the id, the
command, where it ran, whether it runs, what it exited with, and when.
The database is in-memory and rebuilt on every open, so a restart
resets it and there is nothing to migrate; `user_version` says which
shape this server built, so an agent that learned an older one knows
to look again.

One read/write connection owns the database and keeps it alive; reads
come from a small pool of read-only connections that die idle. sqlite
cannot share a plain `:memory:` database between connections, so both
sides open the same shared-cache URI; the last close destroys it,
which is why `close` kills the readers before the writer. A read-only
connection cannot write even by mistake -- `query_only` is set on each
one -- and statement gating for the `sql` command arrives with the
gateway that uses this pool.
"""

from __future__ import annotations

import secrets
import sqlite3
import time
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any

import aiosqlite
import anyio

#: What this server built. Agents read it with `PRAGMA user_version`
#: and re-read the schema when it differs from what they were told.
SCHEMA_VERSION = 6

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  command TEXT NOT NULL,
  directory TEXT,
  status TEXT NOT NULL,
  returncode INTEGER,
  error TEXT,
  started REAL NOT NULL,
  finished REAL,
  pty INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS saved_queries(
  name TEXT PRIMARY KEY,
  sql TEXT NOT NULL,
  description TEXT
);
CREATE TABLE IF NOT EXISTS job_tags(
  job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  key TEXT NOT NULL,
  value TEXT,
  PRIMARY KEY (job_id, key)
);
CREATE INDEX IF NOT EXISTS job_tags_key ON job_tags(key);
"""

#: The queries every boot starts with. A name an agent saves later
#: replaces one of these for the rest of the boot; the next boot
#: seeds again.
SEEDS = [
    (
        "running",
        "SELECT id, command FROM jobs WHERE status = 'running' ORDER BY id",
        "Jobs still running, oldest first.",
    ),
    (
        "failed",
        (
            "SELECT id, command, returncode, error FROM jobs "
            "WHERE status = 'done' AND (returncode != 0 OR error IS NOT NULL) ORDER BY id"
        ),
        "Finished jobs that did not succeed.",
    ),
    (
        "recent",
        "SELECT id, command, status, returncode FROM jobs ORDER BY id DESC LIMIT :n",
        "Newest jobs first; :n caps them.",
    ),
    (
        "by_tag",
        (
            "SELECT DISTINCT j.id, j.command FROM jobs j "
            "JOIN job_tags t ON t.job_id = j.id WHERE t.key = :tag ORDER BY j.id"
        ),
        "Jobs carrying a tag key; :tag names it.",
    ),
]

#: The shape of the one database every connection of one store sees.
#: sqlite cannot share a plain `:memory:` database between connections,
#: so both sides open a shared-cache URI; the last close destroys it,
#: which is why `close` kills the readers before the writer. The name
#: carries a secret per store, so two servers in one process -- the
#: tests build one per test -- never see each other's jobs the way two
#: processes never do.
SHARED_URI = "file:pymux-jobs-%s?mode=memory&cache=shared"


class JobStore:
    """
    The sqlite behind the job table: one writer, a pool of readers.

    The pool holds seats, not threads: a reader is checked out, read
    from, and returned, and a reader idle past its time is closed on
    the next checkout or return. Nothing runs in the background, so
    there is no task to start, stop, or leak; the sweep rides along
    with work that happens anyway. The writer retries a locked
    database briefly: on shared-cache a read statement holds its table
    while it steps, and a write landing in that instant answers
    `SQLITE_LOCKED` rather than waiting.
    """

    def __init__(
        self,
        *,
        max_readers: int = 8,
        min_idle_readers: int = 0,
        reader_idle_ttl: float = 60.0,
        write_retries: int = 50,
    ) -> None:
        self._max_readers = max_readers
        self._min_idle = min_idle_readers
        self._idle_ttl = reader_idle_ttl
        self._write_retries = write_retries
        self._uri = SHARED_URI % secrets.token_hex(8)
        self._rw: aiosqlite.Connection | None = None
        self._opened = False
        self._state = anyio.Lock()
        self._seats = anyio.Semaphore(max_readers)
        self._idle: list[tuple[aiosqlite.Connection, float]] = []

    async def open(self) -> None:
        "Build the database. Idle when already open; safe to share."
        async with self._state:
            if self._opened:
                return
            rw = await aiosqlite.connect(self._uri, uri=True)
            try:
                await rw.execute("PRAGMA foreign_keys = ON")
                await rw.executescript(SCHEMA)
                for name, sql, description in SEEDS:
                    await rw.execute(
                        "INSERT OR IGNORE INTO saved_queries(name, sql, description) VALUES (?, ?, ?)",
                        (name, sql, description),
                    )
                await rw.execute("PRAGMA user_version = %d" % SCHEMA_VERSION)
                await rw.commit()
            except BaseException:
                await rw.close()
                raise
            self._rw = rw
            self._opened = True

    async def close(self) -> None:
        """
        Kill the readers, then the writer that kept the database alive.

        Shielded: the server calls this after cancelling its scope,
        where an unshielded close would die halfway and leak the one
        connection that keeps the database -- and its thread -- alive.
        """
        with anyio.CancelScope(shield=True):
            async with self._state:
                idle, self._idle = self._idle, []
                rw, self._rw = self._rw, None
                self._opened = False
            for connection, _ in idle:
                await connection.close()
            if rw is not None:
                await rw.close()

    async def write(self, sql: str, params: Sequence[Any] = ()) -> Any:
        "Run one statement on the writer and commit it. Retries a lock."
        await self.open()
        assert self._rw is not None
        errors = 0
        while True:
            try:
                cursor = await self._rw.execute(sql, params)
                await self._rw.commit()
                return cursor
            except sqlite3.OperationalError as e:
                errors += 1
                if "locked" not in str(e).lower() or errors > self._write_retries:
                    raise
                await anyio.sleep(0.002 * errors)

    @asynccontextmanager
    async def reader(self) -> AsyncIterator[aiosqlite.Connection]:
        "One read-only connection, returned to the pool (or closed) after."
        connection = await self._checkout()
        try:
            yield connection
        finally:
            await self._checkin(connection)

    async def _checkout(self) -> aiosqlite.Connection:
        await self.open()
        await self._seats.acquire()
        async with self._state:
            await self._sweep()
            if self._idle:
                connection, _ = self._idle.pop()
                return connection
        try:
            connection = await aiosqlite.connect(self._uri, uri=True)
        except BaseException:
            self._seats.release()
            raise
        try:
            await connection.execute("PRAGMA query_only = ON")
        except BaseException:
            await connection.close()
            self._seats.release()
            raise
        return connection

    async def _checkin(self, connection: aiosqlite.Connection) -> None:
        async with self._state:
            await self._sweep()
            if len(self._idle) < self._min_idle and self._opened:
                self._idle.append((connection, time.monotonic()))
                kept = True
            else:
                kept = False
        if not kept:
            await connection.close()
        self._seats.release()

    async def _sweep(self) -> None:
        "Close what idled past its time. Runs under the state lock."
        now = time.monotonic()
        keep: list[tuple[aiosqlite.Connection, float]] = []
        close: list[tuple[aiosqlite.Connection, float]] = []
        for index, item in enumerate(self._idle):
            if index < self._min_idle or now - item[1] <= self._idle_ttl:
                keep.append(item)
            else:
                close.append(item)
        self._idle = keep
        for connection, _ in close:
            await connection.close()

    async def idle_readers(self) -> int:
        "How many readers the pool holds. Tests read this, nothing else."
        async with self._state:
            return len(self._idle)


#: What a question may start with, after comments and whitespace are
#: gone. Everything else -- INSERT, UPDATE, DELETE, DROP, ATTACH, and
#: the pragmas that write -- is refused before sqlite ever sees it;
#: the reader's `query_only` stands behind this, not instead of it.
_READ_STARTS = ("select", "with", "explain", "pragma", "values")


def is_read_only_query(sql: str) -> bool:
    "True when the statement reads. Comments do not hide a write."
    text = _without_comments(sql).strip().lower()
    return text.startswith(_READ_STARTS)


def _without_comments(sql: str) -> str:
    """
    The statement with `--` and `/* */` comments taken out.

    A character loop rather than a pattern, because a pattern cannot
    tell a comment from a string that looks like one: `'-- x'` is a
    value, and stripping it would both corrupt the query and teach
    the gate to misread what follows it.
    """
    out = []
    i, n = 0, len(sql)
    while i < n:
        char = sql[i]
        two = sql[i : i + 2]
        if two == "--":
            end = sql.find("\n", i)
            i = n if end < 0 else end + 1
        elif two == "/*":
            end = sql.find("*/", i + 2)
            i = n if end < 0 else end + 2
        elif char in ("'", '"', "`"):
            end = i + 1
            while end < n:
                if sql[end] == char:
                    if end + 1 < n and sql[end + 1] == char:
                        end += 1
                    else:
                        break
                end += 1
            out.append(sql[i : end + 1])
            i = end + 1
        else:
            out.append(char)
            i += 1
    return "".join(out)
