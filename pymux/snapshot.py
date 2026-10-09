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
build. When the window-management tables become the live store
(step 5 of Lillecarl/pymux#399), those three stay here: a store that
held every pane's cells would serialize them on every write.

What each class writes is declared in `WRITES`, and what it does not
write yet in `LATER`. `tests/test_snapshot.py` holds the two against
every `Keep.SAVED` in the classes' `KEEP`, so a saved field that no
table holds fails a test instead of vanishing in an upgrade.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Callable
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple
from weakref import ref

from prompt_toolkit.data_structures import Size
from ptyhost.backends.posix import PosixBackend
from ptyhost.backends.posix_utils import PtyReader
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
from .ids import PaneId, SessionId, WindowId, WindowIndex
from .jobs import Job, JobId
from .options import ALL_OPTIONS
from .pipes.posix import PosixSocketListener
from .session import Session

if TYPE_CHECKING:
    from .main import ClientState, Pymux

__all__ = ["SNAPSHOT_VERSION", "Snapshot", "adopting", "checking", "load", "save", "start"]

#: The shape this build writes, in `PRAGMA user_version`. A load
#: takes an older one up through `STEPS`, and refuses any other.
SNAPSHOT_VERSION = 2

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
}
CLIENT_SETTINGS = tuple(_CLIENT_SETTING_TYPES)

#: What takes a snapshot of each older version one version up. A
#: change of shape adds the one step from the version before it: the
#: server being upgraded wrote the old shape.
STEPS: dict[int, str] = {
    # NULL is "not recorded": a returning client keeps what it announced.
    1: "".join(
        "ALTER TABLE clients ADD COLUMN %s %s;" % (name, "TEXT" if kind is str else "INTEGER")
        for name, kind in _CLIENT_SETTING_TYPES.items()
    ),
}

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
  frame_rate INTEGER NOT NULL
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
  last_exit_status INTEGER,
  pane_revision INTEGER NOT NULL
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
  message TEXT
);
CREATE TABLE client_windows(
  client_id TEXT NOT NULL REFERENCES clients(client_id),
  session_id INTEGER NOT NULL REFERENCES sessions(session_id),
  active_window_id INTEGER REFERENCES windows(window_id),
  previous_window_id INTEGER REFERENCES windows(window_id),
  PRIMARY KEY (client_id, session_id)
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
  stderr_dropped INTEGER NOT NULL
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
  master_fd INTEGER NOT NULL,
  slave_fd INTEGER,
  pane_width INTEGER NOT NULL,
  pane_height INTEGER NOT NULL,
  screen TEXT NOT NULL,
  stream TEXT NOT NULL,
  process TEXT NOT NULL
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
    "jobs",
    "job_tags",
    "job_output",
    "pane_programs",
    "screen_appearances",
    "screen_rows",
)

#: The tables a later write replaces whole. A finished job never
#: changes, and the screens' tables take only what changed since the
#: write before.
WHOLE_TABLES = TABLES[: TABLES.index("jobs")]

_JOB_TABLES = ("jobs", "job_tags", "job_output")

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
#: this equals the class's `Keep.SAVED` fields, less `LATER`.
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
            "_prev_active_window_for_cli",
        }
    ),
    "pymux.arrangement.Window": frozenset(Window.KEEP),
    "pymux.arrangement._Split": frozenset(_Split.KEEP),
    "pymux.arrangement.Pane": frozenset(Pane.KEEP),
    "pymux.main.ClientState": frozenset({"session", "previous_session", *CLIENT_SETTINGS}),
    "pymux.jobs.JobTable": frozenset({"_jobs", "last_id"}),
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
            PtyReader,
        )
    },
    "pymux.main.Pymux": frozenset(
        {"sessions", "_session_counter", "_uses", "global_environment", "returning_clients", "jobs"}
        | {"created", "socket_name", "listener", "original_cwd", "source_file", "_startup_done"}
        | set(_server_options().values())
    ),
}

#: Saved fields no table holds yet. Each is one step of
#: Lillecarl/pymux#399 still to come.
LATER: dict[str, frozenset[str]] = {
    # Copy mode. A snapshot is refused while a pane is in it.
    "ptterm.terminal.Terminal": frozenset({"is_copying", "copy_buffer", "copy_reverse_video"}),
    # What a person was in the middle of, and how the client is set.
    "pymux.main.ClientState": frozenset(
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
            "choose_window",
            "choose_window_index",
            "choose_window_filter",
            "choose_window_command",
            "choose_buffer",
            "choose_job",
            "choose_notifications",
            "choose_options",
        }
    ),
    # The server's own records, and what the next process is started with.
    "pymux.main.Pymux": frozenset(
        {
            "startup_command",
            "startup_errors",
            "_runs_standalone",
            "_serves_one_terminal",
            "clipboard",
            "named_buffers",
            "prompt_history",
            "hooks",
            "wait_channels",
            "message_log",
            "notifications",
            "notification_center",
            "key_bindings_manager",
        }
    ),
}

#: Classes with saved fields that no table holds any of yet.
LATER_CLASSES: frozenset[str] = frozenset(
    {
        # The client's connection: its record and the images on its terminal.
        "pymux.server.ServerConnection",
    }
)


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
        # Here and not in `finish`: an upgrade's dry run loads the
        # partial file between the two writes.
        self.db.execute("PRAGMA user_version = %d" % SNAPSHOT_VERSION)
        self.freezers: dict[int, Freezer] = {}
        self.jobs_written: set[int] = set()

    def write(self, pymux: Pymux) -> None:
        "Write what `pymux` holds now over what the write before held."
        panes = _panes_of(pymux)
        for pane in panes:
            _refuse_what_cannot_be_saved(pane)
        jobs = pymux.jobs.listing()
        for job in jobs:
            if not job.is_done:
                raise SnapshotError("job %d is still running" % job.job_id)
        with self.db:
            for table in WHOLE_TABLES:
                self.db.execute("DELETE FROM %s" % table)
            _write(pymux, self.db)
            self._write_jobs(jobs)
            alive = {pane.pane_id for pane in panes}
            for gone in set(self.freezers) - alive:
                del self.freezers[gone]
                for table in ("pane_programs", "screen_appearances", "screen_rows"):
                    self.db.execute("DELETE FROM %s WHERE pane_id = ?" % table, (gone,))
            for pane in panes:
                self._write_program(pane)

    def _write_jobs(self, jobs: list[Job]) -> None:
        db = self.db
        alive = {job.job_id for job in jobs}
        for gone in self.jobs_written - alive:
            for table in _JOB_TABLES:
                db.execute("DELETE FROM %s WHERE job_id = ?" % table, (gone,))
        self.jobs_written &= alive
        for job in jobs:
            if job.job_id in self.jobs_written:
                continue
            self.jobs_written.add(job.job_id)
            db.execute(
                "INSERT INTO jobs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                ),
            )
            db.executemany(
                "INSERT INTO job_tags VALUES (?, ?, ?)",
                [(job.job_id, key, value) for key, value in sorted(job.tags.items())],
            )
            db.executemany(
                "INSERT INTO job_output VALUES (?, ?, ?, ?)",
                [
                    (job.job_id, stream, start, data)
                    for stream, chunks in (("stdout", job.stdout_chunks), ("stderr", job.stderr_chunks))
                    for start, data in chunks
                ],
            )

    def _write_program(self, pane: Pane) -> None:
        control = pane.terminal.terminal_control
        process = control.process
        backend = process.backend
        assert isinstance(backend, PosixBackend)  # `write` refused anything else
        freezer = self.freezers.get(pane.pane_id)
        if freezer is None:
            freezer = self.freezers[pane.pane_id] = Freezer()
        screen = freezer.freeze(control.screen)
        db = self.db
        db.execute(
            "INSERT OR REPLACE INTO pane_programs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                pane.pane_id,
                backend.pid,
                backend.master,
                backend.slave,
                process.sx,
                process.sy,
                json.dumps(screen.root),
                json.dumps(freeze(control.stream).root),
                json.dumps(freeze(process).root),
            ),
        )
        first = len(freezer.appearance_index) - len(screen.appearances)
        db.executemany(
            "INSERT INTO screen_appearances VALUES (?, ?, ?)",
            [(pane.pane_id, first + index, json.dumps(value)) for index, value in enumerate(screen.appearances)],
        )
        for buffer in screen.whole:
            db.execute("DELETE FROM screen_rows WHERE pane_id = ? AND buffer = ?", (pane.pane_id, buffer))
        for buffer, numbers in screen.removed.items():
            db.executemany(
                "DELETE FROM screen_rows WHERE pane_id = ? AND buffer = ? AND number = ?",
                [(pane.pane_id, buffer, number) for number in numbers],
            )
        db.executemany(
            "INSERT OR REPLACE INTO screen_rows VALUES (?, ?, ?, ?, ?, ?)",
            [
                (pane.pane_id, buffer, number, wrapped, _text_of(cells), json.dumps(cells))
                for buffer, rows in screen.buffers.items()
                for number, wrapped, cells in rows
            ],
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


def _refuse_what_cannot_be_saved(pane: Pane) -> None:
    terminal = pane.terminal
    if terminal.is_copying:
        raise SnapshotError("pane %%%d is in copy mode; leave it first" % pane.pane_id)
    if not isinstance(getattr(terminal.terminal_control.process, "backend", None), PosixBackend):
        raise SnapshotError("pane %%%d runs no program on a pty, such as a job viewer" % pane.pane_id)


def _text_of(cells: list) -> str:
    "A frozen row as text, for a question to search. A gap reads as a space."
    chars = {column: char for column, char, _, _ in cells}
    if not chars:
        return ""
    return "".join(chars.get(column, " ") for column in range(max(chars) + 1)).rstrip()


def _write(pymux: Pymux, db: sqlite3.Connection) -> None:
    listener = pymux.listener
    if listener is not None and not isinstance(listener, PosixSocketListener):
        raise SnapshotError("the server listens on a named pipe, which no `execve` carries")
    db.executemany(
        "INSERT INTO server VALUES (?, ?)",
        [
            ("created", pymux.created),
            ("socket_name", pymux.socket_name),
            # Inherited across `execve`, so its number is the listener.
            ("listener_fd", listener.socket.fileno() if listener is not None else None),
            ("original_cwd", pymux.original_cwd),
            ("source_file", pymux.source_file),
            ("startup_done", pymux._startup_done),
        ],
    )
    db.executemany(
        "INSERT INTO counters VALUES (?, ?)",
        [
            ("pane", Pane._pane_counter),
            ("window", Window._window_counter),
            ("split", _Split._split_counter),
            ("session", pymux._session_counter),
            ("uses", pymux._uses),
            ("job", pymux.jobs.last_id),
        ],
    )
    db.executemany(
        "INSERT INTO options VALUES (?, ?)",
        [(name, _value(getattr(pymux, attribute))) for name, attribute in sorted(_server_options().items())],
    )
    db.executemany("INSERT INTO global_environment VALUES (?, ?)", sorted(pymux.global_environment.items()))

    window_option_names = _window_option_names()
    for position, session in enumerate(pymux.sessions):
        arrangement = session.arrangement
        db.execute(
            "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
            ),
        )
        db.executemany(
            "INSERT INTO session_environment VALUES (?, ?, ?)",
            [(session.session_id, name, value) for name, value in sorted(session.environment.items())],
        )
        db.executemany(
            "INSERT INTO window_defaults VALUES (?, ?, ?)",
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
    previous = window._prev_active_pane() if window._prev_active_pane is not None else None
    manual = window.manual_size
    db.execute(
        "INSERT INTO windows VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            window.window_id,
            session.session_id,
            linked,
            place,
            int(window.index),
            window.chosen_name,
            window.root.split_id,
            _optional_id(window._active_pane),
            _optional_id(previous),
            _value(window.previous_selected_layout),
            window._strip,
            _value(window.window_size),
            manual.rows if manual is not None else None,
            manual.columns if manual is not None else None,
            window.zoom,
            window.synchronize_panes,
            window.frame_rate,
        ),
    )
    db.executemany(
        "INSERT INTO column_widths VALUES (?, ?, ?, ?)",
        [(window.window_id, kind, id, width) for (kind, id), width in sorted(window.column_widths.items())],
    )
    _write_split(db, window, window.root, None, None)


def _write_split(db: sqlite3.Connection, window: Window, split: _Split, parent: _Split | None, place: int | None):
    db.execute(
        "INSERT INTO splits VALUES (?, ?, ?, ?, ?)",
        (
            split.split_id,
            window.window_id,
            _SPLIT_KINDS[type(split)],
            parent.split_id if parent is not None else None,
            place,
        ),
    )
    db.executemany(
        "INSERT INTO split_weights VALUES (?, ?, ?, ?)",
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
    db.execute(
        "INSERT INTO panes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            pane.pane_id,
            window.window_id if window is not None else None,
            parent.split_id if parent is not None else None,
            place,
            pane.chosen_name,
            pane.clock_mode,
            pane.current_directory,
            pane.current_host,
            pane.command_zone,
            pane.last_exit_status,
            pane.revision,
        ),
    )
    db.executemany(
        "INSERT INTO pane_marks VALUES (?, ?, ?)",
        [(pane.pane_id, position, row) for position, row in enumerate(pane.marks)],
    )
    db.executemany(
        "INSERT INTO pane_user_vars VALUES (?, ?, ?)",
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
            previous = arrangement._prev_active_window_for_cli.get(state.app)
            if active is not None or previous is not None:
                windows[session.session_id] = (
                    active.window_id if active is not None else None,
                    previous.window_id if previous is not None else None,
                )
        clients[connection.client_id] = {
            "session_id": state.session.session_id if state.session is not None else None,
            "previous_session_id": state.previous_session.session_id if state.previous_session is not None else None,
            "windows": windows,
            "settings": {name: getattr(state, name) for name in CLIENT_SETTINGS},
        }
    for client_id, client in sorted(clients.items()):
        settings = client["settings"]
        db.execute(
            "INSERT INTO clients VALUES (%s)" % ", ".join("?" * (3 + len(CLIENT_SETTINGS))),
            (
                client_id,
                client["session_id"],
                client["previous_session_id"],
                *(settings.get(name) for name in CLIENT_SETTINGS),
            ),
        )
        db.executemany(
            "INSERT INTO client_windows VALUES (?, ?, ?, ?)",
            [(client_id, session_id, *pair) for session_id, pair in sorted(client["windows"].items())],
        )


class SnapshotError(Exception):
    "A snapshot this build cannot load."


class Program(NamedTuple):
    "What a snapshot holds of one pane's program, for `load` to adopt it."

    pane_id: PaneId
    pid: int
    master: int
    slave: int | None
    screen: Frozen
    stream: Frozen
    process: Frozen


#: Builds a loaded pane: from its program, with what runs when it ends.
MakePane = Callable[[Program, Callable[[], None] | None], Pane]


def adopting(pymux: Pymux) -> MakePane:
    """
    Panes built around the programs a snapshot names, which still run.

    The pty and the pid survive `execve`, so the new build adopts them,
    and the screen, the parser and the process thaw over what
    `_build_pane` made. Nothing starts until `start`.
    """

    def make(program: Program, on_done: Callable[[], None] | None) -> Pane:
        backend = PosixBackend.adopt(program.master, program.slave, program.pid)
        return _thawed(pymux, program, pymux._build_pane(backend=backend, on_done=on_done))

    return make


def checking(pymux: Pymux) -> MakePane:
    """
    Panes that thaw a snapshot's screens over a pty nobody runs on.

    An upgrade's dry run loads the snapshot this way in a process of the
    new build, which holds none of the fds. It proves the new build
    reads every table and thaws every screen, and adopts nothing.

    **Start nothing it built.** The thaw writes the old owner's fd
    numbers over the pty `_build_pane` opened, and in this process
    those numbers are other files.
    """

    def make(program: Program, on_done: Callable[[], None] | None) -> Pane:
        return _thawed(pymux, program, pymux._build_pane(command=["true"], on_done=on_done))

    return make


def _thawed(pymux: Pymux, program: Program, pane: Pane) -> Pane:
    pane.pane_id = program.pane_id
    control = pane.terminal.terminal_control
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
    for pane in _panes_of(pymux):
        control = pane.terminal.terminal_control
        if not control._running:
            await control.start(pymux.tasks)


def _open(path: str | os.PathLike[str]) -> sqlite3.Connection:
    "The snapshot at `path`, read only, refused if another build wrote it."
    db = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    version = db.execute("PRAGMA user_version").fetchone()[0]
    if version == SNAPSHOT_VERSION:
        return db
    if version not in STEPS:
        db.close()
        raise SnapshotError("snapshot version %d, and this build reads %d" % (version, SNAPSHOT_VERSION))
    # The file stays as the old server wrote it: the fallback reads it.
    stepped = sqlite3.connect(":memory:")
    db.backup(stepped)
    db.close()
    while version < SNAPSHOT_VERSION:
        stepped.executescript(STEPS[version])
        version += 1
    return stepped


def read_server(path: str | os.PathLike[str]) -> dict[str, Any]:
    "The `server` table alone, for what has to be known before `load`."
    db = _open(path)
    try:
        return {row["name"]: row["value"] for row in _rows(db, "SELECT * FROM server")}
    finally:
        db.close()


def load(pymux: Pymux, path: str | os.PathLike[str], make_pane: MakePane | None = None) -> int | None:
    """
    Replace what a fresh `pymux` holds with a snapshot's.

    `make_pane` builds each pane; `adopting` is the default, and
    `checking` is the dry run's.

    Returns the fd of the listening socket, or None when the server had
    none. Only the process the old server exec'd into holds it, so
    adopting it is the caller's: `Pymux.adopt_listener`.
    """
    db = _open(path)
    try:
        return _read(pymux, db, make_pane or adopting(pymux))
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
        master=row["master_fd"],
        slave=row["slave_fd"],
        screen=Frozen(json.loads(row["screen"]), buffers, appearances, sorted(buffers), {}),
        stream=Frozen(json.loads(row["stream"]), {}, [], [], {}),
        process=Frozen(json.loads(row["process"]), {}, [], [], {}),
    )


def _rows(db: sqlite3.Connection, sql: str, *params) -> list[sqlite3.Row]:
    db.row_factory = sqlite3.Row
    return db.execute(sql, params).fetchall()


def _read(pymux: Pymux, db: sqlite3.Connection, make_pane: MakePane) -> int | None:
    server = {row["name"]: row["value"] for row in _rows(db, "SELECT * FROM server")}
    pymux.created = server["created"]
    pymux.socket_name = server["socket_name"]
    pymux.original_cwd = server["original_cwd"]
    pymux.source_file = server["source_file"]
    pymux._startup_done = bool(server["startup_done"])

    counters = {row["name"]: row["value"] for row in _rows(db, "SELECT * FROM counters")}

    server_options = _server_options()
    for row in _rows(db, "SELECT * FROM options"):
        attribute = server_options.get(row["name"])
        # An option this build dropped is not an error: it has nothing to hold it.
        if attribute is not None:
            setattr(pymux, attribute, _like(getattr(pymux, attribute), row["value"]))
    pymux.global_environment = {row["name"]: row["value"] for row in _rows(db, "SELECT * FROM global_environment")}

    panes: dict[int, Pane] = {}

    def pane_of(row: sqlite3.Row, on_done: Callable[[], None] | None = None) -> Pane:
        pane = make_pane(_program(db, PaneId(row["pane_id"])), on_done)
        pane.pane_id = PaneId(row["pane_id"])
        pane.chosen_name = row["pane_name"]
        pane.clock_mode = bool(row["clock_mode"])
        pane.current_directory = row["pane_current_path"]
        pane.current_host = row["current_host"]
        pane.command_zone = row["command_zone"]
        pane.last_exit_status = row["last_exit_status"]
        pane.revision = row["pane_revision"]
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

    def split_of(row: sqlite3.Row) -> _Split:
        split = (HSplit if row["kind"] == "hsplit" else VSplit)()
        split.split_id = row["split_id"]
        split.weights = {
            (weight["kind"], weight["id"]): weight["weight"]
            for weight in _rows(db, "SELECT * FROM split_weights WHERE split_id = ?", split.split_id)
        }
        children = [
            *[
                (r["position"], split_of(r))
                for r in _rows(db, "SELECT * FROM splits WHERE parent_split_id = ?", split.split_id)
            ],
            *[
                (r["position"], pane_of(r))
                for r in _rows(db, "SELECT * FROM panes WHERE parent_split_id = ?", split.split_id)
            ],
        ]
        split.extend(child for _, child in sorted(children, key=lambda pair: pair[0]))
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

        for window_row in _rows(
            db, "SELECT * FROM windows WHERE session_id = ? ORDER BY linked DESC, position", session.session_id
        ):
            window = Window(WindowIndex(window_row["window_index"]))
            window.window_id = WindowId(window_row["window_id"])
            window.chosen_name = window_row["window_name"]
            (root_row,) = _rows(db, "SELECT * FROM splits WHERE split_id = ?", window_row["root_split_id"])
            window.root = split_of(root_row)  # type: ignore[assignment]
            window._active_pane = panes.get(window_row["active_pane_id"])
            previous = panes.get(window_row["previous_active_pane_id"])
            window._prev_active_pane = ref(previous) if previous is not None else None
            layout = window_row["previous_layout"]
            window.previous_selected_layout = LayoutTypes(layout) if layout is not None else None
            window._strip = bool(window_row["strip"])
            window.window_size = type(window.window_size)(window_row["window_size"])
            if window_row["manual_height"] is not None:
                window.manual_size = Size(rows=window_row["manual_height"], columns=window_row["manual_width"])
            window.zoom = bool(window_row["zoom"])
            window.synchronize_panes = bool(window_row["synchronize_panes"])
            window.frame_rate = window_row["frame_rate"]
            window.column_widths = {
                (width["kind"], width["id"]): width["width"]
                for width in _rows(db, "SELECT * FROM column_widths WHERE window_id = ?", window.window_id)
            }
            (arrangement.windows if window_row["linked"] else arrangement._unlinked_windows).append(window)
            windows[window.window_id] = window

        arrangement._last_active_window = windows.get(row["last_active_window_id"])
        if row["overlay_pane_id"] is not None:
            (overlay_row,) = _rows(db, "SELECT * FROM panes WHERE pane_id = ?", row["overlay_pane_id"])
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
        }

    Pane._pane_counter = counters["pane"]
    Window._window_counter = counters["window"]
    _Split._split_counter = counters["split"]
    pymux._session_counter = counters["session"]
    pymux._uses = counters["uses"]
    pymux.jobs.last_id = counters["job"]
    for job in _read_jobs(db):
        pymux.jobs._jobs[job.job_id] = job
    return server["listener_fd"]


def _read_jobs(db: sqlite3.Connection) -> list[Job]:
    "The finished jobs, for `start` to write into the jobs database."
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
        jobs.append(job)
    return jobs
