"""
A log of what a person does with pymux, for reading back later: which
commands, started how, and the clicks that run none. Never what a pane
shows, and never what a person types into one. `action-log` turns it
on; it is off unless a person asks.

**It is a SQLite file of its own**, `actions.db` beside the server's
log, and not a table of the snapshot: it outlives every server, and the
servers of every socket write to it at once, which WAL and a busy
timeout allow.

**A record never waits for the disk.** The caller puts an `Action` on a
memory stream and returns; `ActionLog.keep`, a task in the server's
group, writes what has gathered in one transaction on apsw's worker
thread. A stream that is full drops the action and counts it, rather
than slow the key that made it.

**The file keeps itself small.** After a write, at most once per
`UPKEEP_EVERY`, the oldest rows past `action-log-limit` go, the WAL is
checkpointed and truncated, the free pages are given back, and SQLite
is asked to `optimize`.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import os
import time
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import astuple, dataclass, fields
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Final

import anyio
import apsw

if TYPE_CHECKING:
    import argparse

    from .client_state import ClientState
    from .main import Pymux

__all__ = [
    "Action",
    "ActionLog",
    "Kind",
    "Origin",
    "arguments_of",
    "awaited_as",
    "default_path",
    "note",
    "note_keys",
    "started_by",
    "words_of",
]

logger = logging.getLogger(__name__)

#: What a schema change bumps. A file of another version is renamed
#: aside and a new one started: the log is for reading, and an old one
#: is still there to read.
SCHEMA_VERSION: Final = 1

SCHEMA: Final = """
CREATE TABLE IF NOT EXISTS actions(
  id INTEGER PRIMARY KEY,
  at REAL NOT NULL,
  kind TEXT NOT NULL,
  name TEXT NOT NULL,
  arguments TEXT,
  origin TEXT NOT NULL,
  key_table TEXT,
  key TEXT,
  client TEXT,
  session TEXT,
  window_id INTEGER,
  pane_id INTEGER,
  program TEXT,
  error TEXT
);
CREATE INDEX IF NOT EXISTS actions_at ON actions(at);
CREATE INDEX IF NOT EXISTS actions_name ON actions(name);
"""

#: Actions waiting for the writer before a new one is dropped.
MAX_WAITING: Final = 10_000

#: Seconds between two upkeeps, at the least.
UPKEEP_EVERY: Final = 600.0

#: How long a write waits for another server's transaction.
BUSY_TIMEOUT_MS: Final = 5_000

#: The arguments that hold what a person typed or kept, by command and
#: argparse dest. Each is logged as its length.
CONTENT: Final = {
    "send-keys": "keys",
    "set-buffer": "value",
}


class Kind(StrEnum):
    COMMAND = "command"
    #: A click that runs no command: a pane or a window chosen.
    CLICK = "click"
    #: A row taken in a chooser; what it runs, if anything, follows.
    CHOICE = "choice"
    #: A key that runs no command: a move in a chooser, leaving one, or
    #: a command of copy mode's or the prompt's Helix editor.
    KEY = "key"


class Origin(StrEnum):
    "What started an action."

    KEY = "key"
    PROMPT = "prompt"
    MENU = "menu"
    CHOOSER = "chooser"
    CONFIRM = "confirm"
    MOUSE = "mouse"
    HOOK = "hook"
    CONFIG = "config"
    SOCKET = "socket"
    #: pymux itself, or a route nobody has named yet.
    INTERNAL = "internal"


@dataclass(frozen=True, slots=True)
class Source:
    origin: Origin
    key_table: str | None = None
    key: str | None = None


_source: contextvars.ContextVar[Source] = contextvars.ContextVar("action_source", default=Source(Origin.INTERNAL))


@contextlib.contextmanager
def started_by(origin: Origin, key_table: str | None = None, key: str | None = None) -> Iterator[None]:
    """
    Name what starts the commands run inside. The outermost one wins: a
    `source-file` bound to a key is the key's, and so is every line it
    reads.
    """
    if _source.get().origin is not Origin.INTERNAL:
        yield
        return
    token = _source.set(Source(origin, key_table, key))
    try:
        yield
    finally:
        _source.reset(token)


def started_by_now() -> Origin:
    "What started the command running now."
    return _source.get().origin


async def awaited_as[T](origin: Origin, answer: Awaitable[T]) -> T:
    """
    Await a command's rest under `origin`. A task takes the context it
    is started in, so a coroutine made inside `started_by` and started
    after it would run as nobody's.
    """
    with started_by(origin):
        return await answer


@dataclass(frozen=True, slots=True)
class Action:
    "One row of `actions`, in its column order after `id`."

    at: float
    kind: Kind
    name: str
    arguments: str | None
    origin: Origin
    key_table: str | None
    key: str | None
    client: str | None
    session: str | None
    window_id: int | None
    pane_id: int | None
    program: str | None
    error: str | None


_INSERT: Final = "INSERT INTO actions (%s) VALUES (%s)" % (
    ", ".join(field.name for field in fields(Action)),
    ", ".join("?" * len(fields(Action))),
)


def arguments_of(command: str, namespace: argparse.Namespace) -> str:
    """
    What a command was given, as JSON, by argparse dest. The content of
    `CONTENT` is its length; a `send-keys -X` names copy mode commands,
    which are kept.
    """
    given = {name: value for name, value in vars(namespace).items() if not name.startswith("_")}
    content = CONTENT.get(command)
    if content is not None and not (command == "send-keys" and given.get("X")):
        value = given.get(content)
        if value is not None:
            given[content] = {"length": len(value) if isinstance(value, str) else sum(len(str(v)) for v in value)}
    return json.dumps(given, default=str, sort_keys=True)


def words_of(command: str, arguments: list[str]) -> str | None:
    "The words of a command that did not parse, or None when they may hold content."
    if command in CONTENT:
        return None
    return json.dumps(arguments)


def default_path() -> Path:
    "Beside the server's log, under the user's state directory."
    state = os.environ.get("XDG_STATE_HOME") or "~/.local/state"
    return Path(os.path.expanduser(state)) / "pymux" / "actions.db"


def note(pymux: Pymux, kind: Kind, name: str, arguments: str | None = None, error: str | None = None) -> None:
    "Log one action, when `action-log` is on, with where it happened and what started it."
    if not pymux.options.action_log:
        return
    try:
        client = pymux.get_client_state()
    except ValueError:
        client = None
    if client is not None and client.temporary:
        # A command over the socket runs under a client that is nobody.
        client = None
    source = _source.get()
    pymux.actions.record(
        Action(
            time.time(),
            kind,
            name,
            arguments,
            source.origin,
            source.key_table,
            source.key,
            *where(client),
            error,
        )
    )


def where(client: ClientState | None) -> tuple[str | None, str | None, int | None, int | None, str | None]:
    "The client, session, window, pane and program an action happened at."
    if client is None:
        return None, None, None, None, None
    session = client.session
    window = session.arrangement.get_active_window() if session.arrangement.windows else None
    pane = window.active_pane if window is not None else None
    program = None
    if pane is not None:
        name = pane.process.get_name()
        program = os.path.basename(name) if name else None
    return (
        client.options.name or client.connection.name,
        session.name,
        window.window_id if window is not None else None,
        pane.pane_id if pane is not None else None,
        program,
    )


class ActionLog:
    """
    The stream actions wait on and the task that writes them. The server
    holds one; `Pymux.action_log` says whether anything is put on it.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_path()
        self._send, self._receive = anyio.create_memory_object_stream[Action](MAX_WAITING)
        #: Since start: actions recorded, turned away by a full stream,
        #: written, and lost to a write that failed.
        self.recorded = 0
        self.dropped = 0
        self.written = 0
        self.lost = 0
        self._wrote = anyio.Event()

    def record(self, action: Action) -> None:
        self.recorded += 1
        try:
            self._send.send_nowait(action)
        except anyio.WouldBlock:
            self.dropped += 1

    async def settled(self) -> None:
        "Wait until every action recorded so far is written or lost."
        while self.written + self.dropped + self.lost < self.recorded:
            await self._wrote.wait()

    def _done(self, written: int, lost: int) -> None:
        self.written += written
        self.lost += lost
        self._wrote.set()
        self._wrote = anyio.Event()

    async def keep(self, limit: Callable[[], int]) -> None:
        """
        Write what gathers, until the server's scope ends; then write what
        is left, shielded, so that the last actions before an exit stay.
        `limit` is read at each upkeep, so `set-option` reaches the next.
        """
        db: apsw.AsyncConnection | None = None
        upkept = 0.0
        try:
            while True:
                batch = [await self._receive.receive()]
                batch.extend(self._waiting())
                try:
                    if db is None:
                        db = await _open(self.path)
                    await db.async_run(_write, db, batch)
                    self._done(len(batch), 0)
                    if time.monotonic() - upkept >= UPKEEP_EVERY:
                        upkept = time.monotonic()
                        await db.async_run(_upkeep, db, limit())
                except apsw.Error, OSError:
                    # The log must not take the server with it; the next
                    # action opens the file again.
                    logger.exception("Writing %d actions to %s failed.", len(batch), self.path)
                    self._done(0, len(batch))
                    if db is not None:
                        await _close(db)
                        db = None
        finally:
            with anyio.CancelScope(shield=True):
                left = self._waiting()
                try:
                    if left:
                        if db is None:
                            db = await _open(self.path)
                        await db.async_run(_write, db, left)
                        self._done(len(left), 0)
                except apsw.Error, OSError:
                    logger.exception("Writing the last %d actions to %s failed.", len(left), self.path)
                    self._done(0, len(left))
                if db is not None:
                    await _close(db)

    def _waiting(self) -> list[Action]:
        taken = []
        while True:
            try:
                taken.append(self._receive.receive_nowait())
            except anyio.WouldBlock:
                return taken


async def _open(path: Path) -> apsw.AsyncConnection:
    await anyio.Path(path.parent).mkdir(parents=True, exist_ok=True)
    db = await apsw.Connection.as_async(str(path))
    try:
        version = await db.async_run(_version, db)
        if version is not None and version != SCHEMA_VERSION:
            await _close(db)
            aside = path.with_name("%s.v%d%s" % (path.stem, version, path.suffix))
            logger.warning("%s is schema %d, not %d: moved to %s.", path, version, SCHEMA_VERSION, aside)
            await anyio.Path(path).rename(aside)
            for suffix in ("-wal", "-shm"):
                with contextlib.suppress(FileNotFoundError):
                    await anyio.Path(str(path) + suffix).unlink()
            db = await apsw.Connection.as_async(str(path))
        await db.async_run(_prepare, db)
    except BaseException:
        await _close(db)
        raise
    return db


async def _close(db: apsw.AsyncConnection) -> None:
    with anyio.CancelScope(shield=True):
        await db.aclose()


def _version(db: apsw.Connection) -> int | None:
    "The schema the file holds, or None for an empty file."
    db.set_busy_timeout(BUSY_TIMEOUT_MS)
    if not db.execute("SELECT count(*) FROM sqlite_schema").get:
        return None
    return db.execute("PRAGMA user_version").get


def _prepare(db: apsw.Connection) -> None:
    db.set_busy_timeout(BUSY_TIMEOUT_MS)
    # Only an empty file takes it; a file made with it keeps it.
    db.execute("PRAGMA auto_vacuum = INCREMENTAL")
    db.execute("PRAGMA journal_mode = WAL")
    db.execute("PRAGMA synchronous = NORMAL")
    with db:
        db.execute(SCHEMA)
        db.execute("PRAGMA user_version = %d" % SCHEMA_VERSION)


def _write(db: apsw.Connection, batch: list[Action]) -> None:
    with db:
        db.executemany(_INSERT, [astuple(action) for action in batch])


def _upkeep(db: apsw.Connection, limit: int) -> None:
    if limit:
        with db:
            db.execute("DELETE FROM actions WHERE id <= (SELECT max(id) FROM actions) - ?", (limit,))
    db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    db.execute("PRAGMA incremental_vacuum")
    db.execute("PRAGMA optimize")


def note_keys(pymux: Pymux, key_table: str, keys: str) -> None:
    "Log a command of a Helix editor by its keys: `w`, `gg`. The table says which editor."
    with started_by(Origin.KEY, key_table, keys):
        note(pymux, Kind.KEY, keys)
