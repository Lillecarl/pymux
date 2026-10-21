"""
A server's window management, in an sqlite file a new build can load.

A hot upgrade writes this, and the new build reads it back
(Lillecarl/pymux#399). The tables are meant to become the live store
later, so they are shaped for a person or an agent to query: one row
per session, window, split and pane, and a column named after the
`#{format}` variable where one exists.

**It runs synchronously, on purpose.** The pause of an upgrade is the
event loop standing still. A writer that awaited anything would let a
pane read between two tables, and the file would describe two moments.

**Screen state belongs to the transition, never to the live store.**
`pane_programs`, `screen_rows` and `screen_appearances` exist only in
this file, written once while handing over and read once by the new
build. The live store holds the window-management tables and not
these three: a store that held every pane's cells would serialize them
on every write.

What each class writes is declared in `WRITES`. `tests/test_snapshot.py`
holds it against every `Keep.SAVED` in the classes' `KEEP`, so a saved
field that no table holds fails a test instead of vanishing in an
upgrade.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Callable
from dataclasses import Field, dataclass, fields
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, NamedTuple, Protocol, cast
from weakref import ref

from prompt_toolkit.clipboard import ClipboardData
from prompt_toolkit.data_structures import Size
from prompt_toolkit.selection import SelectionType
from ptterm import Terminal
from ptyhost.backends.posix import PosixBackend
from ptyhost.backends.posix_utils import PtyReader
from ptyhost.held import HeldBackend
from ptyhost.process import Process
from pyte.freeze import Freezer, Frozen, freeze, saved_fields, thaw
from pyte.images import GraphicsImage, GraphicsPlacement, GraphicsState
from pyte.keep import Keep
from pyte.osc import ColorOverrides, PointerShapes
from pyte.page import CursorPosition, Page
from pyte.screen import Screen
from pyte.streams import Stream
from pyte.titles import Titles

from .arrangement import HSplit, LayoutTypes, Pane, VSplit, Window, _Split, node_key
from .commands.wait_for import WaitChannel
from .enums import AudioFrame, ClientAudio, WindowScroll
from .ids import PaneId, SessionId, WindowId, WindowIndex
from .jobs import Job, JobId
from .notifications import Notification
from .options import ALL_OPTIONS
from .pipes.posix import PosixSocketListener
from .session import Session

if TYPE_CHECKING:
    from .main import ClientState, Pymux

__all__ = ["SNAPSHOT_VERSION", "Snapshot", "load", "program_ids", "save", "start"]

#: The shape this build writes, in `PRAGMA user_version`. A load
#: takes an older one up through `STEPS`, and refuses any other.
SNAPSHOT_VERSION = 21


#: A client's own settings, each a column of `clients` and an attribute
#: of `ClientState`, with the type sqlite gives back as.
_CLIENT_SETTING_TYPES: dict[str, type] = {
    "name": str,
    "last_used": int,
    "read_only": bool,
    "ignore_size": bool,
    "full_screen": bool,
    "theme": str,
    "swap_dark_and_light": bool,
    "message": str,
    "window_scroll": WindowScroll,
    "browser_program": str,
    "browser_profile": str,
    "audio": ClientAudio,
    "audio_rtt_multiplier": float,
    "audio_frame": AudioFrame,
}
CLIENT_SETTINGS = tuple(_CLIENT_SETTING_TYPES)
CLIENT_COLUMNS = ("client_id", "session_id", "previous_session_id", *CLIENT_SETTINGS, "modes", "connected")

#: What takes a snapshot of the version before this one up to it. A
#: snapshot only ever goes from a running build to the next one, so a
#: change of shape adds the one step from the version before it and
#: deletes any older step. Version 12 is the first a holder hands over.
STEPS: dict[int, str] = {
    # 21 keeps a running job the holder forked: its program and pid. The
    # build before refused to write a running job, so every row is done.
    # Lillecarl/pymux#556.
    20: """
ALTER TABLE jobs ADD COLUMN program_id INTEGER;
ALTER TABLE jobs ADD COLUMN pid INTEGER;
""",
}

#: The fields `ClientState.modes` holds, one JSON value in `clients`.
CLIENT_MODES = frozenset(
    {
        "has_prefix",
        "key_tables",
        "confirmations",
        "prompt_text",
        "prompt_command",
        "prompt_buffer",
        "command_buffer",
        "display_popup",
        "menu_entries",
        "menu_title",
        "chooser_return_to",
        "chooser",
        "choose_window_index",
        "choose_window_filter",
        "choose_window_command",
    }
)

SCHEMA = """
CREATE TABLE server(
  name TEXT PRIMARY KEY,
  value
);
CREATE TABLE counters(
  name TEXT PRIMARY KEY,
  value INTEGER NOT NULL
);
CREATE TABLE options(
  name TEXT PRIMARY KEY,
  value
);
CREATE TABLE global_environment(
  name TEXT PRIMARY KEY,
  value TEXT
);
CREATE TABLE sessions(
  session_id INTEGER PRIMARY KEY,
  session_name TEXT NOT NULL,
  position INTEGER NOT NULL,
  session_created REAL NOT NULL,
  last_used INTEGER NOT NULL,
  default_height INTEGER NOT NULL,
  default_width INTEGER NOT NULL,
  base_index INTEGER NOT NULL,
  renumber_windows INTEGER NOT NULL,
  last_active_window_id INTEGER,
  overlay_pane_id INTEGER,
  overlay_title TEXT NOT NULL,
  overlay_width TEXT,
  overlay_height TEXT
);
CREATE TABLE session_environment(
  session_id INTEGER NOT NULL REFERENCES sessions(session_id),
  name TEXT NOT NULL,
  value TEXT,
  PRIMARY KEY (session_id, name)
);
CREATE TABLE window_defaults(
  session_id INTEGER NOT NULL REFERENCES sessions(session_id),
  name TEXT NOT NULL,
  value,
  PRIMARY KEY (session_id, name)
);
CREATE TABLE windows(
  window_id INTEGER PRIMARY KEY,
  session_id INTEGER NOT NULL REFERENCES sessions(session_id),
  linked INTEGER NOT NULL,
  position INTEGER NOT NULL,
  window_index INTEGER NOT NULL,
  window_name TEXT,
  root_split_id INTEGER NOT NULL,
  active_pane_id INTEGER,
  previous_active_pane_id INTEGER,
  previous_layout TEXT,
  strip INTEGER NOT NULL,
  window_size TEXT NOT NULL,
  manual_height INTEGER,
  manual_width INTEGER,
  zoom INTEGER NOT NULL,
  synchronize_panes INTEGER NOT NULL,
  frame_rate INTEGER NOT NULL,
  frame_rate_set INTEGER NOT NULL,
  wayland_scale TEXT NOT NULL,
  window_codec TEXT NOT NULL
);
CREATE TABLE splits(
  split_id INTEGER PRIMARY KEY,
  window_id INTEGER NOT NULL REFERENCES windows(window_id),
  kind TEXT NOT NULL CHECK (kind IN ('hsplit', 'vsplit')),
  parent_split_id INTEGER REFERENCES splits(split_id),
  position INTEGER
);
CREATE TABLE panes(
  pane_id INTEGER PRIMARY KEY,
  window_id INTEGER REFERENCES windows(window_id),
  parent_split_id INTEGER REFERENCES splits(split_id),
  position INTEGER,
  pane_name TEXT,
  clock_mode INTEGER NOT NULL,
  pane_current_path TEXT,
  current_host TEXT,
  command_zone TEXT,
  last_exit_status INTEGER
);
CREATE TABLE pane_marks(
  pane_id INTEGER NOT NULL REFERENCES panes(pane_id),
  position INTEGER NOT NULL,
  row INTEGER NOT NULL,
  PRIMARY KEY (pane_id, position)
);
CREATE TABLE pane_user_vars(
  pane_id INTEGER NOT NULL REFERENCES panes(pane_id),
  name TEXT NOT NULL,
  value TEXT NOT NULL,
  PRIMARY KEY (pane_id, name)
);
CREATE TABLE split_weights(
  split_id INTEGER NOT NULL REFERENCES splits(split_id),
  kind TEXT NOT NULL CHECK (kind IN ('pane', 'split')),
  id INTEGER NOT NULL,
  weight INTEGER NOT NULL,
  PRIMARY KEY (split_id, kind, id)
);
CREATE TABLE column_widths(
  window_id INTEGER NOT NULL REFERENCES windows(window_id),
  kind TEXT NOT NULL CHECK (kind IN ('pane', 'split')),
  id INTEGER NOT NULL,
  width REAL NOT NULL,
  PRIMARY KEY (window_id, kind, id)
);
CREATE TABLE clients(
  client_id TEXT PRIMARY KEY,
  session_id INTEGER REFERENCES sessions(session_id),
  previous_session_id INTEGER REFERENCES sessions(session_id),
  name TEXT,
  last_used INTEGER,
  read_only INTEGER,
  ignore_size INTEGER,
  full_screen INTEGER,
  theme TEXT,
  swap_dark_and_light INTEGER,
  message TEXT,
  modes TEXT,
  connected REAL,
  window_scroll TEXT,
  browser_program TEXT,
  browser_profile TEXT,
  audio TEXT,
  audio_rtt_multiplier REAL,
  audio_frame TEXT
);
CREATE TABLE client_windows(
  client_id TEXT NOT NULL REFERENCES clients(client_id),
  session_id INTEGER NOT NULL REFERENCES sessions(session_id),
  active_window_id INTEGER REFERENCES windows(window_id),
  previous_window_id INTEGER REFERENCES windows(window_id),
  PRIMARY KEY (client_id, session_id)
);
CREATE TABLE named_buffers(
  name TEXT PRIMARY KEY,
  text TEXT NOT NULL
);
CREATE TABLE server_lists(
  list TEXT NOT NULL CHECK (list IN ('prompt_history', 'message_log')),
  position INTEGER NOT NULL,
  text TEXT NOT NULL,
  PRIMARY KEY (list, position)
);
CREATE TABLE hooks(
  hook TEXT NOT NULL,
  position INTEGER NOT NULL,
  command TEXT NOT NULL,
  PRIMARY KEY (hook, position)
);
CREATE TABLE key_bindings(
  key_table TEXT NOT NULL,
  written TEXT NOT NULL,
  command TEXT NOT NULL,
  arguments TEXT NOT NULL,
  PRIMARY KEY (key_table, written)
);
CREATE TABLE notifications(
  notification_id INTEGER PRIMARY KEY,
  title TEXT NOT NULL,
  body TEXT NOT NULL,
  urgency INTEGER NOT NULL,
  pane_id INTEGER,
  at REAL NOT NULL,
  position INTEGER UNIQUE,
  identifier TEXT,
  CHECK ((position IS NULL) != (identifier IS NULL))
);
CREATE TABLE notification_routes(
  ours TEXT PRIMARY KEY,
  pane_id INTEGER NOT NULL,
  identifier TEXT NOT NULL,
  position INTEGER NOT NULL UNIQUE
);
CREATE TABLE wait_channels(
  name TEXT PRIMARY KEY,
  woken INTEGER NOT NULL,
  locked INTEGER NOT NULL
);
CREATE TABLE saved_queries(
  name TEXT PRIMARY KEY,
  sql TEXT NOT NULL,
  description TEXT
);
CREATE TABLE jobs(
  job_id INTEGER PRIMARY KEY,
  command TEXT NOT NULL,
  directory TEXT,
  pty INTEGER NOT NULL,
  status TEXT NOT NULL,
  returncode INTEGER,
  error TEXT,
  started REAL NOT NULL,
  finished REAL,
  last_active REAL NOT NULL,
  env TEXT NOT NULL,
  stdout_dropped INTEGER NOT NULL,
  stderr_dropped INTEGER NOT NULL,
  program_id INTEGER,
  pid INTEGER
);
CREATE TABLE job_tags(
  job_id INTEGER NOT NULL REFERENCES jobs(job_id),
  key TEXT NOT NULL,
  value TEXT,
  PRIMARY KEY (job_id, key)
);
CREATE TABLE job_output(
  job_id INTEGER NOT NULL REFERENCES jobs(job_id),
  stream TEXT NOT NULL CHECK (stream IN ('stdout', 'stderr')),
  start INTEGER NOT NULL,
  data BLOB NOT NULL,
  PRIMARY KEY (job_id, stream, start)
);
CREATE TABLE pane_programs(
  pane_id INTEGER PRIMARY KEY REFERENCES panes(pane_id),
  pane_pid INTEGER NOT NULL,
  pane_revision INTEGER NOT NULL,
  screen TEXT NOT NULL,
  stream TEXT NOT NULL,
  process TEXT NOT NULL,
  program_id INTEGER
);
CREATE TABLE screen_appearances(
  pane_id INTEGER NOT NULL REFERENCES panes(pane_id),
  appearance INTEGER NOT NULL,
  value TEXT NOT NULL,
  PRIMARY KEY (pane_id, appearance)
);
CREATE TABLE screen_rows(
  pane_id INTEGER NOT NULL REFERENCES panes(pane_id),
  buffer TEXT NOT NULL,
  number INTEGER NOT NULL,
  wrapped INTEGER NOT NULL,
  text TEXT NOT NULL,
  cells TEXT NOT NULL,
  PRIMARY KEY (pane_id, buffer, number)
);
"""

#: The tables in the order a dump compares them.
TABLES = (
    "server",
    "counters",
    "options",
    "global_environment",
    "sessions",
    "session_environment",
    "window_defaults",
    "windows",
    "splits",
    "panes",
    "pane_marks",
    "pane_user_vars",
    "split_weights",
    "column_widths",
    "clients",
    "client_windows",
    "named_buffers",
    "server_lists",
    "hooks",
    "key_bindings",
    "notifications",
    "notification_routes",
    "wait_channels",
    "saved_queries",
    "jobs",
    "job_tags",
    "job_output",
    "pane_programs",
    "screen_appearances",
    "screen_rows",
)

_JOB_TABLES = ("jobs", "job_tags", "job_output")

#: A pane's program and its screen, which only the transition holds.
_SCREEN_TABLES = ("pane_programs", "screen_appearances", "screen_rows")

#: The tables a later write replaces whole. A finished job never
#: changes, and the screens' tables take only what changed since the
#: write before.
WHOLE_TABLES = tuple(table for table in TABLES if table not in _JOB_TABLES + _SCREEN_TABLES)


def _schema_of(tables: tuple[str, ...]) -> str:
    "The part of `SCHEMA` that creates these tables, as sqlite read it."
    db = sqlite3.connect(":memory:")
    try:
        db.executescript(SCHEMA)
        made = dict(db.execute("SELECT name, sql FROM sqlite_master WHERE type = 'table'").fetchall())
    finally:
        db.close()
    return "".join("%s;\n" % made[table] for table in tables)


#: The tables of `WHOLE_TABLES`, which are what the live store holds:
#: no job, which the job store holds live, and no screen, which never
#: goes in a live store (Lillecarl/pymux#399).
LIVE_SCHEMA = _schema_of(WHOLE_TABLES)

_SPLIT_KINDS: dict[type[_Split], str] = {HSplit: "hsplit", VSplit: "vsplit"}


def _server_options() -> dict[str, str]:
    "Option name to `Pymux` attribute, for every option the server holds."
    from .main import Pymux

    return {
        name: option.attribute_name
        for name, option in ALL_OPTIONS.items()
        if option.attribute_name is not None and Pymux.KEEP.get(option.attribute_name) is not None
    }


def _window_option_names() -> dict[str, str]:
    "Window attribute to option name, for `window_defaults`."
    from .options import ALL_WINDOW_OPTIONS

    return {option.attribute_name: name for name, option in ALL_WINDOW_OPTIONS.items() if option.attribute_name}


#: What each class writes into a table. `tests/test_snapshot.py` says
#: this equals the class's `Keep.SAVED` fields.
WRITES: dict[str, frozenset[str]] = {
    "pymux.session.Session": frozenset(Session.KEEP),
    "pymux.arrangement.Arrangement": frozenset(
        {
            "windows",
            "_unlinked_windows",
            "base_index",
            "renumber_windows",
            "window_defaults",
            "_last_active_window",
            "_active_window_for_cli",
            "_window_history_for_cli",
        }
    ),
    "pymux.arrangement.Window": frozenset(Window.KEEP),
    "pymux.arrangement._Split": frozenset(_Split.KEEP),
    "pymux.arrangement.Pane": frozenset(name for name, fate in Pane.KEEP.items() if fate == Keep.SAVED),
    "pymux.main.ClientState": frozenset({"session", "previous_session", *CLIENT_SETTINGS, *CLIENT_MODES}),
    # The record goes by the client's id, in `clients`.
    "pymux.server.ServerConnection": frozenset({"client_state", "created"}),
    "pymux.jobs.JobTable": frozenset({"_jobs", "last_id", "saved_queries"}),
    # The kept counts are the lengths of the chunks `job_output` holds.
    "pymux.jobs.Job": frozenset(name for name, fate in Job.KEEP.items() if fate == Keep.SAVED),
    "ptterm.terminal.Terminal": frozenset({"terminal_control"}),
    "ptterm.terminal._TerminalControl": frozenset({"screen", "stream", "process"}),
    # What `pyte.freeze` walks writes every saved field, by construction.
    **{
        "%s.%s" % (cls.__module__, cls.__qualname__): frozenset(saved_fields(cls))
        for cls in (
            Screen,
            Page,
            CursorPosition,
            Titles,
            ColorOverrides,
            PointerShapes,
            GraphicsState,
            GraphicsImage,
            GraphicsPlacement,
            Stream,
            Process,
            PosixBackend,
            HeldBackend,
            PtyReader,
        )
    },
    "pymux.main.Pymux": frozenset(
        {"sessions", "_session_counter", "_uses", "global_environment", "returning_clients", "jobs"}
        | {"created", "socket_name", "listener", "original_cwd", "source_file", "_startup_done"}
        | {"clipboard", "named_buffers", "prompt_history", "message_log", "hooks", "key_bindings_manager"}
        | {"notifications", "notification_center", "wait_channels"}
        | {"startup_command"}
        | set(_server_options().values())
    ),
}


def _value(value: Any) -> Any:
    "A value as sqlite holds it. An enum is its value; anything odd raises."
    if isinstance(value, Enum):
        return value.value
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    raise TypeError("a snapshot cannot hold %r" % (value,))


def _like(default: Any, value: Any) -> Any:
    "A value read back, in the type of the fresh object's own default."
    if value is None:
        return None
    if isinstance(default, bool):
        return bool(value)
    if isinstance(default, Enum):
        return type(default)(value)
    return value


def _optional_id(item) -> int | None:
    return None if item is None else int(node_key(item)[1])


@dataclass(frozen=True, slots=True)
class WindowRow:
    TABLE: ClassVar[str] = "windows"
    window_id: int
    session_id: int
    linked: int
    position: int
    window_index: int
    window_name: str | None
    root_split_id: int
    active_pane_id: int | None
    previous_active_pane_id: int | None
    previous_layout: str | None
    strip: int
    window_size: str
    manual_height: int | None
    manual_width: int | None
    zoom: int
    synchronize_panes: int
    frame_rate: int
    frame_rate_set: int
    wayland_scale: str
    window_codec: str


@dataclass(frozen=True, slots=True)
class SplitRow:
    TABLE: ClassVar[str] = "splits"
    split_id: int
    window_id: int
    kind: str
    parent_split_id: int | None
    position: int | None


@dataclass(frozen=True, slots=True)
class PaneRow:
    TABLE: ClassVar[str] = "panes"
    pane_id: int
    window_id: int | None
    parent_split_id: int | None
    position: int | None
    pane_name: str | None
    clock_mode: int
    pane_current_path: str | None
    current_host: str | None
    command_zone: str | None
    last_exit_status: int | None


#: Every table a dataclass names. A test holds each against the schema.
ROWS = (WindowRow, SplitRow, PaneRow)


class _Row(Protocol):
    TABLE: ClassVar[str]
    __dataclass_fields__: ClassVar[dict[str, Field[Any]]]


def _insert(db: sqlite3.Connection, row: _Row) -> None:
    names = [field.name for field in fields(row)]
    db.execute(
        "INSERT INTO %s (%s) VALUES (%s)" % (row.TABLE, ", ".join(names), ", ".join(":" + name for name in names)),
        {name: getattr(row, name) for name in names},
    )


def _insert_many(db: sqlite3.Connection, table: str, columns: tuple[str, ...], rows, replace: bool = False) -> None:
    """
    Rows into `table`, each in the order `columns` names. Never the
    table's own order: a column that moves in `SCHEMA` would take
    another column's value without a word.
    """
    db.executemany(
        "INSERT %sINTO %s (%s) VALUES (%s)"
        % ("OR REPLACE " if replace else "", table, ", ".join(columns), ", ".join("?" * len(columns))),
        rows,
    )


def _select[R: _Row](db: sqlite3.Connection, cls: type[R], where: str, *params) -> list[R]:
    "`cls` for each row of its table that `where` picks. A column it does not name raises."
    return [cls(**dict(row)) for row in _rows(db, "SELECT * FROM %s %s" % (cls.TABLE, where), *params)]


def save(pymux: Pymux, path: str | os.PathLike[str]) -> None:
    "Write `pymux` to `path` in one go. `Snapshot` says how."
    snapshot = Snapshot(path)
    try:
        snapshot.write(pymux)
        snapshot.finish()
    except BaseException:
        snapshot.abandon()
        raise


class Snapshot:
    """
    A snapshot being written: once whole, then again with what changed.

    An upgrade writes the first while the server still serves, which
    costs the depth of every history, and the second in the pause,
    which costs what the programs wrote in between
    (`pyte.freeze.Freezer`). `finish` then makes the file appear whole
    or not at all: it is written beside `path`, synced, and renamed
    over it.
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self.partial = self.path.with_name(self.path.name + ".partial")
        # sqlite replays a journal it finds beside a new file into it, so
        # a crashed save's journal goes with the crashed save.
        for leftover in (self.partial, self.partial.with_name(self.partial.name + "-journal")):
            leftover.unlink(missing_ok=True)
        # Private before the first byte: the screens hold what was typed.
        # sqlite gives its journal the file's mode.
        os.close(os.open(self.partial, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
        self.db = sqlite3.connect(self.partial)
        self.db.executescript(SCHEMA)
        self.db.execute("PRAGMA user_version = %d" % SNAPSHOT_VERSION)
        self.freezers: dict[int, Freezer] = {}
        self.jobs_written: set[int] = set()

    def write(self, pymux: Pymux) -> None:
        "Write what `pymux` holds now over what the write before held."
        _refuse_the_server(pymux)
        panes = _panes_of(pymux)
        for pane in panes:
            _refuse_what_cannot_be_saved(pane)
        jobs = pymux.jobs.listing()
        for job in jobs:
            if not job.is_done and job.program_id is None:
                # Only the holder can hand a program to the next server.
                raise SnapshotError("job %d runs and was forked by this server, not by its holder" % job.job_id)
        with self.db:
            for table in WHOLE_TABLES:
                self.db.execute("DELETE FROM %s" % table)
            write_tables(pymux, self.db)
            self._write_jobs(jobs)
            alive = {pane.pane_id for pane in panes}
            for gone in set(self.freezers) - alive:
                del self.freezers[gone]
                for table in _SCREEN_TABLES:
                    self.db.execute("DELETE FROM %s WHERE pane_id = ?" % table, (gone,))
            for pane in panes:
                self._write_program(pane)

    def _write_jobs(self, jobs: list[Job]) -> None:
        db = self.db
        alive = {job.job_id for job in jobs}
        # A running job wrote more since, and its tail is capped, so it
        # is written whole again.
        stale = {job.job_id for job in jobs if not job.is_done}
        for gone in self.jobs_written - (alive - stale):
            for table in _JOB_TABLES:
                db.execute("DELETE FROM %s WHERE job_id = ?" % table, (gone,))
        self.jobs_written &= alive - stale
        for job in jobs:
            if job.job_id in self.jobs_written:
                continue
            self.jobs_written.add(job.job_id)
            _insert_many(
                db,
                "jobs",
                (
                    "job_id",
                    "command",
                    "directory",
                    "pty",
                    "status",
                    "returncode",
                    "error",
                    "started",
                    "finished",
                    "last_active",
                    "env",
                    "stdout_dropped",
                    "stderr_dropped",
                    "program_id",
                    "pid",
                ),
                [
                    (
                        job.job_id,
                        job.command,
                        job.directory,
                        job.pty,
                        job.status,
                        job.returncode,
                        job.error,
                        job.started,
                        job.finished,
                        job.last_active,
                        json.dumps(job.env, sort_keys=True),
                        job.stdout_dropped,
                        job.stderr_dropped,
                        job.program_id,
                        job.pid,
                    )
                ],
            )
            _insert_many(
                db,
                "job_tags",
                ("job_id", "key", "value"),
                [(job.job_id, key, value) for key, value in sorted(job.tags.items())],
            )
            _insert_many(
                db,
                "job_output",
                ("job_id", "stream", "start", "data"),
                [
                    (job.job_id, stream, start, data)
                    for stream, chunks in (("stdout", job.stdout_chunks), ("stderr", job.stderr_chunks))
                    for start, data in chunks
                ],
            )

    def _write_program(self, pane: Pane) -> None:
        control = _terminal(pane).terminal_control
        process = control.process
        backend = process.backend
        assert isinstance(backend, HeldBackend)  # `write` refused anything else
        freezer = self.freezers.get(pane.pane_id)
        if freezer is None:
            freezer = self.freezers[pane.pane_id] = Freezer()
        screen = freezer.freeze(control.screen)
        db = self.db
        _insert_many(
            db,
            "pane_programs",
            ("pane_id", "pane_pid", "pane_revision", "screen", "stream", "process", "program_id"),
            [
                (
                    pane.pane_id,
                    backend.pid,
                    # Output moves it, so it lives with the screen.
                    pane.revision,
                    json.dumps(screen.root),
                    json.dumps(freeze(control.stream).root),
                    json.dumps(freeze(process).root),
                    backend.program_id,
                )
            ],
            replace=True,
        )
        first = len(freezer.appearance_index) - len(screen.appearances)
        _insert_many(
            db,
            "screen_appearances",
            ("pane_id", "appearance", "value"),
            [(pane.pane_id, first + index, json.dumps(value)) for index, value in enumerate(screen.appearances)],
        )
        for buffer in screen.whole:
            db.execute("DELETE FROM screen_rows WHERE pane_id = ? AND buffer = ?", (pane.pane_id, buffer))
        for buffer, numbers in screen.removed.items():
            db.executemany(
                "DELETE FROM screen_rows WHERE pane_id = ? AND buffer = ? AND number = ?",
                [(pane.pane_id, buffer, number) for number in numbers],
            )
        _insert_many(
            db,
            "screen_rows",
            ("pane_id", "buffer", "number", "wrapped", "text", "cells"),
            [
                (pane.pane_id, buffer, number, wrapped, _text_of(cells), json.dumps(cells))
                for buffer, rows in screen.buffers.items()
                for number, wrapped, cells in rows
            ],
            replace=True,
        )

    def finish(self) -> None:
        "Make the file appear at `path`, whole."
        self.db.close()
        with open(self.partial, "rb") as written:
            os.fsync(written.fileno())
        os.replace(self.partial, self.path)
        directory = os.open(self.path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def abandon(self) -> None:
        "Throw the half-written file away."
        self.db.close()
        self.partial.unlink(missing_ok=True)


def _panes_of(pymux: Pymux) -> list[Pane]:
    "Every pane a snapshot holds, in the order the tables write them."
    panes = []
    for session in pymux.sessions:
        arrangement = session.arrangement
        for window in [*arrangement.windows, *arrangement._unlinked_windows]:
            panes.extend(window.panes)
        if session.overlay_pane is not None:
            panes.append(session.overlay_pane)
    return panes


def _terminal(pane: Pane) -> Terminal:
    """
    The terminal of a pane the snapshot holds. A Wayland window lives on
    the server's compositor, which no handover carries, and the upgrade
    closes it before it writes. Lillecarl/pymux#620.
    """
    terminal = pane.terminal
    if not isinstance(terminal, Terminal):
        raise SnapshotError("pane %%%d shows a Wayland window, which no handover carries" % pane.pane_id)
    return terminal


def _refuse_what_cannot_be_saved(pane: Pane) -> None:
    terminal = _terminal(pane)
    backend = getattr(terminal.terminal_control.process, "backend", None)
    if not isinstance(backend, PosixBackend):
        raise SnapshotError("pane %%%d runs no program on a pty, such as a job viewer" % pane.pane_id)
    if not isinstance(backend, HeldBackend):
        # Only the holder can hand a program to the next server.
        raise SnapshotError("pane %%%d was forked by this server and not by its holder" % pane.pane_id)


def _text_of(cells: list) -> str:
    "A frozen row as text, for a question to search. A gap reads as a space."
    chars = {column: char for column, char, _, _ in cells}
    if not chars:
        return ""
    return "".join(chars.get(column, " ") for column in range(max(chars) + 1)).rstrip()


def _refuse_the_server(pymux: Pymux) -> None:
    "What no handover carries. The live store writes such a server all the same."
    listener = pymux.listener
    if listener is not None and not isinstance(listener, PosixSocketListener):
        raise SnapshotError("the server listens on a named pipe, which no handover carries")
    for name, channel in pymux.wait_channels.items():
        # A waiter is a command connection held open in this process,
        # and this process ends: the script that waits would fail.
        if channel.waiters or channel.lockers:
            raise SnapshotError("a command waits on channel %s; signal it first" % name)


def write_tables(pymux: Pymux, db: sqlite3.Connection) -> None:
    "Every table of `LIVE_SCHEMA`, from what `pymux` holds now, into empty ones."
    listener = pymux.listener
    clipboard = pymux.clipboard.get_data()
    _insert_many(
        db,
        "server",
        ("name", "value"),
        [
            ("created", pymux.created),
            ("socket_name", pymux.socket_name),
            # The new server gets it under this number (`upgrade._hand_over`).
            (
                "listener_fd",
                listener.socket.fileno() if isinstance(listener, PosixSocketListener) else None,
            ),
            ("original_cwd", pymux.original_cwd),
            ("source_file", pymux.source_file),
            ("startup_done", pymux._startup_done),
            ("clipboard", clipboard.text),
            ("clipboard_type", clipboard.type.value),
            ("startup_command", pymux.startup_command),
        ],
    )
    _insert_many(db, "named_buffers", ("name", "text"), sorted(pymux.named_buffers.items()))
    _insert_many(
        db,
        "server_lists",
        ("list", "position", "text"),
        [
            *(("prompt_history", n, text) for n, text in enumerate(pymux.prompt_history.get_strings())),
            *(("message_log", n, text) for n, text in enumerate(pymux.message_log)),
        ],
    )
    _insert_many(
        db,
        "hooks",
        ("hook", "position", "command"),
        [(hook, n, command) for hook, commands in sorted(pymux.hooks.items()) for n, command in enumerate(commands)],
    )
    _insert_many(
        db,
        "key_bindings",
        ("key_table", "written", "command", "arguments"),
        sorted(
            (table, binding.written, binding.command, json.dumps(binding.arguments))
            for (table, _keys), binding in pymux.key_bindings_manager.custom_bindings.items()
        ),
    )
    # A finished record has its place in the hub; one still assembling
    # has the identifier its chunks name instead.
    center = pymux.notification_center
    _insert_many(
        db,
        "notifications",
        ("notification_id", "title", "body", "urgency", "pane_id", "at", "position", "identifier"),
        [
            *((*record, position, None) for position, record in enumerate(center._records)),
            *((*record, None, identifier) for (_pane_id, identifier), record in center._pending.items()),
        ],
    )
    _insert_many(
        db,
        "saved_queries",
        ("name", "sql", "description"),
        [(name, sql, description) for name, (sql, description) in sorted(pymux.jobs.saved_queries.items())],
    )
    _insert_many(
        db,
        "wait_channels",
        ("name", "woken", "locked"),
        [(name, channel.woken, channel.locked) for name, channel in sorted(pymux.wait_channels.items())],
    )
    _insert_many(
        db,
        "notification_routes",
        ("ours", "pane_id", "identifier", "position"),
        [
            (ours, pane_id, identifier, position)
            for position, (ours, (pane_id, identifier)) in enumerate(pymux.notifications._incoming.items())
        ],
    )
    _insert_many(
        db,
        "counters",
        ("name", "value"),
        [
            ("pane", Pane._pane_counter),
            ("window", Window._window_counter),
            ("split", _Split._split_counter),
            ("session", pymux._session_counter),
            ("uses", pymux._uses),
            ("job", pymux.jobs.last_id),
            ("notification", pymux.notification_center._next),
            ("notification_route", pymux.notifications._next),
        ],
    )
    _insert_many(
        db,
        "options",
        ("name", "value"),
        [(name, _value(getattr(pymux, attribute))) for name, attribute in sorted(_server_options().items())],
    )
    _insert_many(db, "global_environment", ("name", "value"), sorted(pymux.global_environment.items()))

    window_option_names = _window_option_names()
    for position, session in enumerate(pymux.sessions):
        arrangement = session.arrangement
        _insert_many(
            db,
            "sessions",
            (
                "session_id",
                "session_name",
                "position",
                "session_created",
                "last_used",
                "default_height",
                "default_width",
                "base_index",
                "renumber_windows",
                "last_active_window_id",
                "overlay_pane_id",
                "overlay_title",
                "overlay_width",
                "overlay_height",
            ),
            [
                (
                    session.session_id,
                    session.name,
                    position,
                    session.created,
                    session.last_used,
                    session.default_size.rows,
                    session.default_size.columns,
                    arrangement.base_index,
                    arrangement.renumber_windows,
                    last.window_id if (last := arrangement._last_active_window) is not None else None,
                    _optional_id(session.overlay_pane),
                    session.overlay_title,
                    session.overlay_width,
                    session.overlay_height,
                )
            ],
        )
        _insert_many(
            db,
            "session_environment",
            ("session_id", "name", "value"),
            [(session.session_id, name, value) for name, value in sorted(session.environment.items())],
        )
        _insert_many(
            db,
            "window_defaults",
            ("session_id", "name", "value"),
            [
                (session.session_id, window_option_names.get(attribute, attribute), _value(value))
                for attribute, value in sorted(arrangement.window_defaults.items())
            ],
        )
        for linked, windows in ((True, arrangement.windows), (False, arrangement._unlinked_windows)):
            for place, window in enumerate(windows):
                _write_window(db, session, window, linked, place)
        if session.overlay_pane is not None:
            _write_pane(db, session.overlay_pane, None, None, None)

    _write_clients(pymux, db)


def _write_window(db: sqlite3.Connection, session: Session, window: Window, linked: bool, place: int) -> None:
    # The newest of the history: a restored window can go back one step.
    previous = window.previous_active_pane
    manual = window.manual_size
    _insert(
        db,
        WindowRow(
            window_id=window.window_id,
            session_id=session.session_id,
            linked=linked,
            position=place,
            window_index=int(window.index),
            window_name=window.chosen_name,
            root_split_id=window.root.split_id,
            active_pane_id=_optional_id(window._active_pane),
            previous_active_pane_id=_optional_id(previous),
            previous_layout=_value(window.previous_selected_layout),
            strip=window._strip,
            window_size=_value(window.window_size),
            manual_height=manual.rows if manual is not None else None,
            manual_width=manual.columns if manual is not None else None,
            zoom=window.zoom,
            synchronize_panes=window.synchronize_panes,
            frame_rate=window.frame_rate,
            frame_rate_set=window.frame_rate_set,
            wayland_scale=window.wayland_scale,
            window_codec=_value(window.window_codec),
        ),
    )
    _insert_many(
        db,
        "column_widths",
        ("window_id", "kind", "id", "width"),
        [(window.window_id, kind, id, width) for (kind, id), width in sorted(window.column_widths.items())],
    )
    _write_split(db, window, window.root, None, None)


def _write_split(db: sqlite3.Connection, window: Window, split: _Split, parent: _Split | None, place: int | None):
    _insert(
        db,
        SplitRow(
            split_id=split.split_id,
            window_id=window.window_id,
            kind=_SPLIT_KINDS[type(split)],
            parent_split_id=parent.split_id if parent is not None else None,
            position=place,
        ),
    )
    _insert_many(
        db,
        "split_weights",
        ("split_id", "kind", "id", "weight"),
        [(split.split_id, kind, id, weight) for (kind, id), weight in sorted(split.weights.items())],
    )
    for position, child in enumerate(split):
        if isinstance(child, Pane):
            _write_pane(db, child, window, split, position)
        else:
            _write_split(db, window, child, split, position)


def _write_pane(
    db: sqlite3.Connection, pane: Pane, window: Window | None, parent: _Split | None, place: int | None
) -> None:
    _insert(
        db,
        PaneRow(
            pane_id=pane.pane_id,
            window_id=window.window_id if window is not None else None,
            parent_split_id=parent.split_id if parent is not None else None,
            position=place,
            pane_name=pane.chosen_name,
            clock_mode=pane.clock_mode,
            pane_current_path=pane.current_directory,
            current_host=pane.current_host,
            command_zone=pane.command_zone,
            last_exit_status=pane.last_exit_status,
        ),
    )
    _insert_many(
        db,
        "pane_marks",
        ("pane_id", "position", "row"),
        [(pane.pane_id, position, row) for position, row in enumerate(pane.marks)],
    )
    _insert_many(
        db,
        "pane_user_vars",
        ("pane_id", "name", "value"),
        [(pane.pane_id, name, value) for name, value in sorted(pane.user_vars.items())],
    )


def _write_clients(pymux: Pymux, db: sqlite3.Connection) -> None:
    """
    Each client by the id it keeps across reattaches, and the windows it
    was on in each session. A client the server loaded and that has not
    come back yet is written again as it was read.
    """
    clients: dict[str, dict] = {key: dict(value) for key, value in pymux.returning_clients.items()}
    for connection in pymux.connections:
        state: ClientState | None = connection.client_state
        if state is None or not connection.client_id:
            continue
        windows = {}
        for session in pymux.sessions:
            arrangement = session.arrangement
            active = arrangement._active_window_for_cli.get(state.app)
            previous = arrangement.previous_window_for(state.app)
            if active is not None or previous is not None:
                windows[session.session_id] = (
                    active.window_id if active is not None else None,
                    previous.window_id if previous is not None else None,
                )
        clients[connection.client_id] = {
            "session_id": state.session.session_id if state.session is not None else None,
            "previous_session_id": state.previous_session.session_id if state.previous_session is not None else None,
            "windows": windows,
            "settings": {name: _value(getattr(state, name)) for name in CLIENT_SETTINGS},
            "modes": state.modes(),
            "connected": connection.created,
        }
    for client_id, client in sorted(clients.items()):
        settings = client["settings"]
        modes = client.get("modes")
        _insert_many(
            db,
            "clients",
            CLIENT_COLUMNS,
            [
                (
                    client_id,
                    client["session_id"],
                    client["previous_session_id"],
                    *(settings.get(name) for name in CLIENT_SETTINGS),
                    json.dumps(modes) if modes is not None else None,
                    client.get("connected"),
                )
            ],
        )
        _insert_many(
            db,
            "client_windows",
            ("client_id", "session_id", "active_window_id", "previous_window_id"),
            [(client_id, session_id, *pair) for session_id, pair in sorted(client["windows"].items())],
        )


class SnapshotError(Exception):
    "A snapshot this build cannot load."


class Program(NamedTuple):
    "What a snapshot holds of one pane's program, for `load` to adopt it."

    pane_id: PaneId
    pid: int
    #: The holder's name for the program.
    program_id: int
    screen: Frozen
    stream: Frozen
    process: Frozen


def _adopted(pymux: Pymux, masters: dict[int, int], program: Program, on_done: Callable[[], None] | None) -> Pane:
    """
    A pane built around a program the snapshot names, which still runs.

    The holder keeps it and handed this server its master, which
    `masters` holds by the holder's id. The screen, the parser and the
    process thaw over what `_build_pane` made. Nothing starts until
    `start`.
    """
    if pymux.holding is None:
        raise SnapshotError("this server has no holder to take pane %d from" % (program.pane_id,))
    if program.program_id not in masters:
        raise SnapshotError("the holder keeps no program for pane %d" % (program.pane_id,))
    backend = HeldBackend.adopt_held(pymux.holding, program.program_id, program.pid, masters[program.program_id])
    return _thawed(pymux, program, pymux._build_pane(backend=backend, on_done=on_done))


def _thawed(pymux: Pymux, program: Program, pane: Pane) -> Pane:
    pane.pane_id = program.pane_id
    control = _terminal(pane).terminal_control
    thaw(program.process, control.process)
    thaw(program.screen, control.screen)
    # After the screen: the replay of an open sequence reaches it.
    thaw(program.stream, control.stream)
    pymux._register_pane(pane)
    return pane


async def start(pymux: Pymux) -> None:
    "Start pumping and reaping every loaded program, in the server's scope."
    if pymux.tasks is None:
        raise RuntimeError("start the loaded programs inside `Pymux.running`")
    await pymux.jobs.restore()
    for job in pymux.jobs.listing():
        if not job.is_done:
            pymux.tasks.start_soon(pymux.jobs.supervise, job)
    for pane in _panes_of(pymux):
        control = _terminal(pane).terminal_control
        if not control._running:
            await control.start(pymux.tasks)


def _open(path: str | os.PathLike[str]) -> sqlite3.Connection:
    "The snapshot at `path`, read only, refused if another build wrote it."
    db = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    version = db.execute("PRAGMA user_version").fetchone()[0]
    if version == SNAPSHOT_VERSION:
        return db
    if version != SNAPSHOT_VERSION - 1 or version not in STEPS:
        db.close()
        raise SnapshotError("snapshot version %d, and this build reads %d" % (version, SNAPSHOT_VERSION))
    # The file stays as the old server wrote it: it serves on if this fails.
    stepped = sqlite3.connect(":memory:")
    db.backup(stepped)
    db.close()
    stepped.executescript(STEPS[version])
    return stepped


def read_server(path: str | os.PathLike[str]) -> dict[str, Any]:
    "The `server` table alone, for what has to be known before `load`."
    db = _open(path)
    try:
        return {row["name"]: row["value"] for row in _rows(db, "SELECT * FROM server")}
    finally:
        db.close()


def program_ids(path: str | os.PathLike[str]) -> list[int]:
    "The holder's id of every program the snapshot names: the masters `load` needs."
    db = _open(path)
    try:
        return [row["program_id"] for row in _rows(db, "SELECT program_id FROM pane_programs")] + [
            row["program_id"]
            for row in _rows(db, "SELECT program_id FROM jobs WHERE status = 'running' AND program_id IS NOT NULL")
        ]
    finally:
        db.close()


def load(pymux: Pymux, path: str | os.PathLike[str], masters: dict[int, int] | None = None) -> int | None:
    """
    Replace what a fresh `pymux` holds with a snapshot's.

    `masters` are the ones the holder handed over, by the holder's id,
    one for each of `program_ids`. None is a server with no pane.

    Returns the fd of the listening socket, or None when the server had
    none. The old server passed it to the new one under the same number,
    and adopting it is the caller's: `Pymux.adopt_listener`.
    """
    db = _open(path)
    try:
        return _read(pymux, db, masters or {})
    finally:
        db.close()


def _program(db: sqlite3.Connection, pane_id: PaneId) -> Program:
    (row,) = _rows(db, "SELECT * FROM pane_programs WHERE pane_id = ?", pane_id)
    appearances = [
        json.loads(entry["value"])
        for entry in _rows(db, "SELECT value FROM screen_appearances WHERE pane_id = ? ORDER BY appearance", pane_id)
    ]
    buffers: dict[str, list] = {}
    for entry in _rows(
        db, "SELECT buffer, number, wrapped, cells FROM screen_rows WHERE pane_id = ? ORDER BY buffer, number", pane_id
    ):
        buffers.setdefault(entry["buffer"], []).append(
            [entry["number"], bool(entry["wrapped"]), json.loads(entry["cells"])]
        )
    return Program(
        pane_id=pane_id,
        pid=row["pane_pid"],
        program_id=row["program_id"],
        screen=Frozen(json.loads(row["screen"]), buffers, appearances, sorted(buffers), {}),
        stream=Frozen(json.loads(row["stream"]), {}, [], [], {}),
        process=Frozen(json.loads(row["process"]), {}, [], [], {}),
    )


def _rows(db: sqlite3.Connection, sql: str, *params) -> list[sqlite3.Row]:
    db.row_factory = sqlite3.Row
    return db.execute(sql, params).fetchall()


def _read_bindings(pymux: Pymux, db: sqlite3.Connection) -> None:
    """
    The hooks and key bindings, whole, over what the configuration set:
    a person's `bind-key` and `set-hook` since the server started are in
    these and not in it.
    """
    pymux.hooks = {}
    for row in _rows(db, "SELECT * FROM hooks ORDER BY hook, position"):
        pymux.hooks.setdefault(row["hook"], []).append(row["command"])
    bindings = pymux.key_bindings_manager
    for table, keys in list(bindings.custom_bindings):
        bindings.remove_custom_binding(bindings.custom_bindings[table, keys].written, table=table)
    for row in _rows(db, "SELECT * FROM key_bindings"):
        bindings.add_custom_binding(
            row["written"], row["command"], json.loads(row["arguments"]), table=row["key_table"]
        )


def _read_notifications(pymux: Pymux, db: sqlite3.Connection, counters: dict[str, int]) -> None:
    "The hub and its routes."
    center = pymux.notification_center
    center._next = counters["notification"]
    for row in _rows(db, "SELECT * FROM notifications ORDER BY position"):
        pane_id = PaneId(row["pane_id"]) if row["pane_id"] is not None else None
        record = Notification(row["notification_id"], row["title"], row["body"], row["urgency"], pane_id, row["at"])
        if row["identifier"] is None:
            center._records.append(record)
        else:
            center._pending[pane_id, row["identifier"]] = record
    routes = pymux.notifications
    routes._next = counters["notification_route"]
    for row in _rows(db, "SELECT * FROM notification_routes ORDER BY position"):
        key = (PaneId(row["pane_id"]), row["identifier"])
        routes._outgoing[key] = row["ours"]
        routes._incoming[row["ours"]] = key


def _read(pymux: Pymux, db: sqlite3.Connection, masters: dict[int, int]) -> int | None:
    server = {row["name"]: row["value"] for row in _rows(db, "SELECT * FROM server")}
    pymux.created = server["created"]
    pymux.socket_name = server["socket_name"]
    pymux.original_cwd = server["original_cwd"]
    pymux.source_file = server["source_file"]
    pymux._startup_done = bool(server["startup_done"])
    pymux.clipboard.set_data(ClipboardData(server["clipboard"], SelectionType(server["clipboard_type"])))
    pymux.named_buffers = {row["name"]: row["text"] for row in _rows(db, "SELECT * FROM named_buffers")}
    for row in _rows(db, "SELECT text FROM server_lists WHERE list = 'prompt_history' ORDER BY position"):
        pymux.prompt_history.append_string(row["text"])
    pymux.message_log.extend(
        row["text"] for row in _rows(db, "SELECT text FROM server_lists WHERE list = 'message_log' ORDER BY position")
    )
    _read_bindings(pymux, db)
    pymux.startup_command = server["startup_command"]
    pymux.wait_channels = {}
    for row in _rows(db, "SELECT * FROM wait_channels"):
        channel = WaitChannel()
        channel.woken = bool(row["woken"])
        channel.locked = bool(row["locked"])
        pymux.wait_channels[row["name"]] = channel

    counters = {row["name"]: row["value"] for row in _rows(db, "SELECT * FROM counters")}

    server_options = _server_options()
    for row in _rows(db, "SELECT * FROM options"):
        attribute = server_options.get(row["name"])
        # An option this build dropped is not an error: it has nothing to hold it.
        if attribute is not None:
            setattr(pymux, attribute, _like(getattr(pymux, attribute), row["value"]))
    pymux.global_environment = {row["name"]: row["value"] for row in _rows(db, "SELECT * FROM global_environment")}

    panes: dict[int | None, Pane] = {}

    def pane_of(row: PaneRow, on_done: Callable[[], None] | None = None) -> Pane:
        pane = _adopted(pymux, masters, _program(db, PaneId(row.pane_id)), on_done)
        pane.pane_id = PaneId(row.pane_id)
        pane.chosen_name = row.pane_name
        pane.clock_mode = bool(row.clock_mode)
        pane.current_directory = row.pane_current_path
        pane.current_host = row.current_host
        pane.command_zone = row.command_zone
        pane.last_exit_status = row.last_exit_status
        ((pane.revision,),) = _rows(db, "SELECT pane_revision FROM pane_programs WHERE pane_id = ?", pane.pane_id)
        pane.marks = [
            mark["row"]
            for mark in _rows(db, "SELECT row FROM pane_marks WHERE pane_id = ? ORDER BY position", pane.pane_id)
        ]
        pane.user_vars = {
            var["name"]: var["value"]
            for var in _rows(db, "SELECT * FROM pane_user_vars WHERE pane_id = ?", pane.pane_id)
        }
        panes[pane.pane_id] = pane
        pymux.panes_by_id[pane.pane_id] = pane
        return pane

    def split_of(row: SplitRow) -> _Split:
        split = (HSplit if row.kind == "hsplit" else VSplit)()
        split.split_id = row.split_id
        split.weights = {
            (weight["kind"], weight["id"]): weight["weight"]
            for weight in _rows(db, "SELECT * FROM split_weights WHERE split_id = ?", split.split_id)
        }
        children = [
            *[(r.position, split_of(r)) for r in _select(db, SplitRow, "WHERE parent_split_id = ?", split.split_id)],
            *[(r.position, pane_of(r)) for r in _select(db, PaneRow, "WHERE parent_split_id = ?", split.split_id)],
        ]
        # Rows with a parent always carry a position: the writer
        # numbers every child and leaves None only for parentless
        # rows, which this query excludes.
        split.extend(child for _, child in sorted(children, key=lambda pair: cast(int, pair[0])))
        return split

    windows: dict[int, Window] = {}
    window_attributes = {name: attribute for attribute, name in _window_option_names().items()}
    sessions = []
    for row in _rows(db, "SELECT * FROM sessions ORDER BY position"):
        session = Session(SessionId(row["session_id"]), row["session_name"])
        session.created = row["session_created"]
        session.last_used = row["last_used"]
        session.default_size = Size(rows=row["default_height"], columns=row["default_width"])
        session.overlay_title = row["overlay_title"]
        session.overlay_width = row["overlay_width"]
        session.overlay_height = row["overlay_height"]
        session.environment = {
            env["name"]: env["value"]
            for env in _rows(db, "SELECT * FROM session_environment WHERE session_id = ?", session.session_id)
        }
        arrangement = session.arrangement
        arrangement.base_index = row["base_index"]
        arrangement.renumber_windows = bool(row["renumber_windows"])
        for default in _rows(db, "SELECT * FROM window_defaults WHERE session_id = ?", session.session_id):
            attribute = window_attributes.get(default["name"], default["name"])
            fresh = Window()
            if hasattr(fresh, attribute):
                arrangement.window_defaults[attribute] = _like(getattr(fresh, attribute), default["value"])

        for window_row in _select(
            db, WindowRow, "WHERE session_id = ? ORDER BY linked DESC, position", session.session_id
        ):
            window = Window(WindowIndex(window_row.window_index))
            window.window_id = WindowId(window_row.window_id)
            window.chosen_name = window_row.window_name
            (root_row,) = _select(db, SplitRow, "WHERE split_id = ?", window_row.root_split_id)
            window.root = split_of(root_row)  # type: ignore[assignment]
            window._active_pane = panes.get(window_row.active_pane_id)
            previous = panes.get(window_row.previous_active_pane_id)
            window._pane_history = [ref(previous)] if previous is not None else []
            layout = window_row.previous_layout
            window.previous_selected_layout = LayoutTypes(layout) if layout is not None else None
            window._strip = bool(window_row.strip)
            window.window_size = type(window.window_size)(window_row.window_size)
            if window_row.manual_height is not None and window_row.manual_width is not None:
                window.manual_size = Size(rows=window_row.manual_height, columns=window_row.manual_width)
            window.zoom = bool(window_row.zoom)
            window.synchronize_panes = bool(window_row.synchronize_panes)
            window.frame_rate = window_row.frame_rate
            window.frame_rate_set = bool(window_row.frame_rate_set)
            window.wayland_scale = window_row.wayland_scale
            window.window_codec = type(window.window_codec)(window_row.window_codec)
            window.column_widths = {
                (width["kind"], width["id"]): width["width"]
                for width in _rows(db, "SELECT * FROM column_widths WHERE window_id = ?", window.window_id)
            }
            (arrangement.windows if window_row.linked else arrangement._unlinked_windows).append(window)
            windows[window.window_id] = window

        arrangement._last_active_window = windows.get(row["last_active_window_id"])
        if row["overlay_pane_id"] is not None:
            (overlay_row,) = _select(db, PaneRow, "WHERE pane_id = ?", row["overlay_pane_id"])
            overlay: list[Pane] = []
            session.overlay_pane = pane_of(
                overlay_row,
                lambda session=session: pymux.overlay_ended(session, overlay[0] if overlay else None),
            )
            overlay.append(session.overlay_pane)
        sessions.append(session)

    pymux.sessions = sessions

    pymux.returning_clients = {}
    for row in _rows(db, "SELECT * FROM clients"):
        pymux.returning_clients[row["client_id"]] = {
            "session_id": row["session_id"],
            "previous_session_id": row["previous_session_id"],
            "windows": {
                window["session_id"]: (window["active_window_id"], window["previous_window_id"])
                for window in _rows(db, "SELECT * FROM client_windows WHERE client_id = ?", row["client_id"])
            },
            "settings": {
                name: _CLIENT_SETTING_TYPES[name](row[name]) for name in CLIENT_SETTINGS if row[name] is not None
            },
            "modes": json.loads(row["modes"]) if row["modes"] is not None else None,
            "connected": row["connected"],
        }

    Pane._pane_counter = counters["pane"]
    Window._window_counter = counters["window"]
    _Split._split_counter = counters["split"]
    pymux._session_counter = counters["session"]
    pymux._uses = counters["uses"]
    pymux.jobs.last_id = counters["job"]
    pymux.jobs.saved_queries = {
        row["name"]: (row["sql"], row["description"]) for row in _rows(db, "SELECT * FROM saved_queries")
    }
    _read_notifications(pymux, db, counters)
    for job in _read_jobs(pymux, db, masters):
        pymux.jobs._jobs[job.job_id] = job
    return server["listener_fd"]


def _read_jobs(pymux: Pymux, db: sqlite3.Connection, masters: dict[int, int]) -> list[Job]:
    """
    The jobs, for `start` to write into the jobs database. A running one
    takes its terminal from `masters`, and `start` supervises it again.
    """
    jobs = []
    for row in _rows(db, "SELECT * FROM jobs ORDER BY job_id"):
        job = Job(
            JobId(row["job_id"]),
            row["command"],
            row["directory"],
            row["started"],
            [(tag["key"], tag["value"]) for tag in _rows(db, "SELECT * FROM job_tags WHERE job_id = ?", row["job_id"])],
            json.loads(row["env"]),
            bool(row["pty"]),
        )
        job.status = row["status"]
        job.returncode = row["returncode"]
        job.error = row["error"]
        job.finished = row["finished"]
        job.last_active = row["last_active"]
        job.stdout_dropped = row["stdout_dropped"]
        job.stderr_dropped = row["stderr_dropped"]
        for chunk in _rows(db, "SELECT * FROM job_output WHERE job_id = ? ORDER BY stream, start", job.job_id):
            chunks = job.stdout_chunks if chunk["stream"] == "stdout" else job.stderr_chunks
            chunks.append((chunk["start"], chunk["data"]))
        job.stdout_kept = sum(len(data) for _, data in job.stdout_chunks)
        job.stderr_kept = sum(len(data) for _, data in job.stderr_chunks)
        job.program_id = row["program_id"]
        job.pid = row["pid"]
        if not job.is_done:
            if pymux.holding is None:
                raise SnapshotError("this server has no holder to take job %d from" % (job.job_id,))
            if job.program_id not in masters:
                raise SnapshotError("the holder keeps no program for job %d" % (job.job_id,))
            job.backend = HeldBackend.adopt_held(pymux.holding, job.program_id, job.pid, masters[job.program_id])
        jobs.append(job)
    return jobs
