"""
The tables a snapshot holds and `sql` reads, and one dataclass per
table for its row.

`SCHEMA` is the authority. A row class is the shape of one row and
nothing more: its fields are the table's columns in order, and `KEY` is
the table's primary key. `tests/test_snapshot.py` holds both against
what sqlite reads out of `SCHEMA`. The SQL stays hand-written; the
classes only name the columns, so that a writer and a reader cannot
disagree about them. Lillecarl/pymux#554.
"""

from __future__ import annotations

import functools
from collections.abc import Iterable
from dataclasses import Field, dataclass, field, fields
from typing import Any, ClassVar, Final, Protocol

import apsw

__all__ = [
    "LIVE_ROWS",
    "LIVE_TABLES",
    "ROWS",
    "SCHEMA",
    "SCREEN_TABLES",
    "TABLES",
    "ChangedRows",
    "insert",
    "select",
]

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
CREATE TABLE session_tags(
  session_id INTEGER NOT NULL REFERENCES sessions(session_id),
  key TEXT NOT NULL,
  value TEXT,
  PRIMARY KEY (session_id, key)
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
  window_codec TEXT NOT NULL,
  monitor_bell INTEGER NOT NULL DEFAULT 1,
  bell INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE window_tags(
  window_id INTEGER NOT NULL REFERENCES windows(window_id),
  key TEXT NOT NULL,
  value TEXT,
  PRIMARY KEY (window_id, key)
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
  bell INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE pane_user_vars(
  pane_id INTEGER NOT NULL REFERENCES panes(pane_id),
  name TEXT NOT NULL,
  value TEXT NOT NULL,
  PRIMARY KEY (pane_id, name)
);
CREATE TABLE pane_tags(
  pane_id INTEGER NOT NULL REFERENCES panes(pane_id),
  key TEXT NOT NULL,
  value TEXT,
  PRIMARY KEY (pane_id, key)
);
CREATE TABLE pane_viewers(
  pane_id INTEGER PRIMARY KEY REFERENCES panes(pane_id),
  job_id INTEGER NOT NULL REFERENCES jobs(job_id),
  pane_width INTEGER NOT NULL,
  pane_height INTEGER NOT NULL
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
  connected REAL,
  window_scroll TEXT,
  browser_program TEXT,
  browser_profile TEXT,
  audio TEXT,
  audio_rtt_multiplier REAL,
  audio_frame TEXT,
  audio_speaker_bitrate INTEGER,
  audio_microphone_bitrate INTEGER,
  file_transfer TEXT
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
  pid INTEGER,
  fds TEXT,
  parent INTEGER
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
  master_fd INTEGER,
  parent INTEGER
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
  marks INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (pane_id, buffer, number)
);
CREATE TABLE client_modes(
  client_id TEXT PRIMARY KEY REFERENCES clients(client_id),
  modes TEXT NOT NULL
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
    "session_tags",
    "window_defaults",
    "windows",
    "window_tags",
    "splits",
    "panes",
    "pane_user_vars",
    "pane_tags",
    "pane_viewers",
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
    "client_modes",
)

JOB_TABLES = ("jobs", "job_tags", "job_output")

#: A pane's program and its screen, which only the transition holds.
SCREEN_TABLES = ("pane_programs", "screen_appearances", "screen_rows")

#: What a client is in the middle of -- a held prefix, a key table, a
#: prompt -- which typing changes, so only the transition holds it too.
CLIENT_MODE_TABLES = ("client_modes",)

#: The tables of window management, which `sql` reads under `live`. A
#: snapshot holds the jobs, the screens and the client modes beside them.
LIVE_TABLES = tuple(table for table in TABLES if table not in JOB_TABLES + SCREEN_TABLES + CLIENT_MODE_TABLES)


#: Marks a column worked out from the live objects -- what a person
#: sees, such as a pane's title -- which `sql` reads and the snapshot
#: neither writes nor compares: a new build works it out again.
SHOWN: Final = {"shown": True}


def shown(default: Any = None) -> Any:
    "A column `sql` reads and the snapshot leaves out. It goes after every stored one."
    return field(default=default, compare=False, metadata=SHOWN)


def stored_fields(kind: type) -> tuple[Field[Any], ...]:
    "The columns of `kind` the snapshot holds."
    return tuple(each for each in fields(kind) if not each.metadata.get("shown"))


@dataclass(frozen=True, slots=True)
class ServerRow:
    TABLE: ClassVar[str] = "server"
    KEY: ClassVar[tuple[str, ...]] = ("name",)
    name: str
    value: Any


@dataclass(frozen=True, slots=True)
class CounterRow:
    TABLE: ClassVar[str] = "counters"
    KEY: ClassVar[tuple[str, ...]] = ("name",)
    name: str
    value: int


@dataclass(frozen=True, slots=True)
class OptionRow:
    TABLE: ClassVar[str] = "options"
    KEY: ClassVar[tuple[str, ...]] = ("name",)
    name: str
    value: Any


@dataclass(frozen=True, slots=True)
class GlobalEnvironmentRow:
    TABLE: ClassVar[str] = "global_environment"
    KEY: ClassVar[tuple[str, ...]] = ("name",)
    name: str
    value: str | None


@dataclass(frozen=True, slots=True)
class SessionRow:
    TABLE: ClassVar[str] = "sessions"
    KEY: ClassVar[tuple[str, ...]] = ("session_id",)
    session_id: int
    session_name: str
    position: int
    session_created: float
    last_used: int
    default_height: int
    default_width: int
    base_index: int
    renumber_windows: int
    last_active_window_id: int | None
    overlay_pane_id: int | None
    overlay_title: str
    overlay_width: str | None
    overlay_height: str | None


@dataclass(frozen=True, slots=True)
class SessionEnvironmentRow:
    TABLE: ClassVar[str] = "session_environment"
    KEY: ClassVar[tuple[str, ...]] = ("session_id", "name")
    session_id: int
    name: str
    value: str | None


@dataclass(frozen=True, slots=True)
class SessionTagRow:
    TABLE: ClassVar[str] = "session_tags"
    KEY: ClassVar[tuple[str, ...]] = ("session_id", "key")
    session_id: int
    key: str
    value: str | None


@dataclass(frozen=True, slots=True)
class WindowDefaultRow:
    TABLE: ClassVar[str] = "window_defaults"
    KEY: ClassVar[tuple[str, ...]] = ("session_id", "name")
    session_id: int
    name: str
    value: Any


@dataclass(frozen=True, slots=True)
class WindowRow:
    TABLE: ClassVar[str] = "windows"
    KEY: ClassVar[tuple[str, ...]] = ("window_id",)
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
    monitor_bell: int
    bell: int
    shown_name: str | None = shown()


@dataclass(frozen=True, slots=True)
class WindowTagRow:
    TABLE: ClassVar[str] = "window_tags"
    KEY: ClassVar[tuple[str, ...]] = ("window_id", "key")
    window_id: int
    key: str
    value: str | None


@dataclass(frozen=True, slots=True)
class SplitRow:
    TABLE: ClassVar[str] = "splits"
    KEY: ClassVar[tuple[str, ...]] = ("split_id",)
    split_id: int
    window_id: int
    kind: str
    parent_split_id: int | None
    position: int | None


@dataclass(frozen=True, slots=True)
class PaneRow:
    TABLE: ClassVar[str] = "panes"
    KEY: ClassVar[tuple[str, ...]] = ("pane_id",)
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
    bell: int
    shown_name: str | None = shown()
    title: str | None = shown()
    current_command: str | None = shown()


@dataclass(frozen=True, slots=True)
class PaneUserVarRow:
    TABLE: ClassVar[str] = "pane_user_vars"
    KEY: ClassVar[tuple[str, ...]] = ("pane_id", "name")
    pane_id: int
    name: str
    value: str


@dataclass(frozen=True, slots=True)
class PaneTagRow:
    TABLE: ClassVar[str] = "pane_tags"
    KEY: ClassVar[tuple[str, ...]] = ("pane_id", "key")
    pane_id: int
    key: str
    value: str | None


@dataclass(frozen=True, slots=True)
class ViewerRow:
    TABLE: ClassVar[str] = "pane_viewers"
    KEY: ClassVar[tuple[str, ...]] = ("pane_id",)
    pane_id: int
    job_id: int
    pane_width: int
    pane_height: int


@dataclass(frozen=True, slots=True)
class SplitWeightRow:
    TABLE: ClassVar[str] = "split_weights"
    KEY: ClassVar[tuple[str, ...]] = ("split_id", "kind", "id")
    split_id: int
    kind: str
    id: int
    weight: int


@dataclass(frozen=True, slots=True)
class ColumnWidthRow:
    TABLE: ClassVar[str] = "column_widths"
    KEY: ClassVar[tuple[str, ...]] = ("window_id", "kind", "id")
    window_id: int
    kind: str
    id: int
    width: float


@dataclass(frozen=True, slots=True)
class ClientRow:
    TABLE: ClassVar[str] = "clients"
    KEY: ClassVar[tuple[str, ...]] = ("client_id",)
    client_id: str
    session_id: int | None
    previous_session_id: int | None
    name: str | None
    last_used: int | None
    read_only: int | None
    ignore_size: int | None
    full_screen: int | None
    theme: str | None
    swap_dark_and_light: int | None
    message: str | None
    connected: float | None
    window_scroll: str | None
    browser_program: str | None
    browser_profile: str | None
    audio: str | None
    audio_rtt_multiplier: float | None
    audio_frame: str | None
    audio_speaker_bitrate: int | None
    audio_microphone_bitrate: int | None
    file_transfer: str | None


@dataclass(frozen=True, slots=True)
class ClientWindowRow:
    TABLE: ClassVar[str] = "client_windows"
    KEY: ClassVar[tuple[str, ...]] = ("client_id", "session_id")
    client_id: str
    session_id: int
    active_window_id: int | None
    previous_window_id: int | None


@dataclass(frozen=True, slots=True)
class NamedBufferRow:
    TABLE: ClassVar[str] = "named_buffers"
    KEY: ClassVar[tuple[str, ...]] = ("name",)
    name: str
    text: str


@dataclass(frozen=True, slots=True)
class ServerListRow:
    TABLE: ClassVar[str] = "server_lists"
    KEY: ClassVar[tuple[str, ...]] = ("list", "position")
    list: str
    position: int
    text: str


@dataclass(frozen=True, slots=True)
class HookRow:
    TABLE: ClassVar[str] = "hooks"
    KEY: ClassVar[tuple[str, ...]] = ("hook", "position")
    hook: str
    position: int
    command: str


@dataclass(frozen=True, slots=True)
class KeyBindingRow:
    TABLE: ClassVar[str] = "key_bindings"
    KEY: ClassVar[tuple[str, ...]] = ("key_table", "written")
    key_table: str
    written: str
    command: str
    arguments: str


@dataclass(frozen=True, slots=True)
class NotificationRow:
    TABLE: ClassVar[str] = "notifications"
    KEY: ClassVar[tuple[str, ...]] = ("notification_id",)
    notification_id: int
    title: str
    body: str
    urgency: int
    pane_id: int | None
    at: float
    position: int | None
    identifier: str | None


@dataclass(frozen=True, slots=True)
class NotificationRouteRow:
    TABLE: ClassVar[str] = "notification_routes"
    KEY: ClassVar[tuple[str, ...]] = ("ours",)
    ours: str
    pane_id: int
    identifier: str
    position: int


@dataclass(frozen=True, slots=True)
class WaitChannelRow:
    TABLE: ClassVar[str] = "wait_channels"
    KEY: ClassVar[tuple[str, ...]] = ("name",)
    name: str
    woken: int
    locked: int


@dataclass(frozen=True, slots=True)
class SavedQueryRow:
    TABLE: ClassVar[str] = "saved_queries"
    KEY: ClassVar[tuple[str, ...]] = ("name",)
    name: str
    sql: str
    description: str | None


@dataclass(frozen=True, slots=True)
class JobRow:
    TABLE: ClassVar[str] = "jobs"
    KEY: ClassVar[tuple[str, ...]] = ("job_id",)
    job_id: int
    command: str
    directory: str | None
    pty: int
    status: str
    returncode: int | None
    error: str | None
    started: float
    finished: float | None
    last_active: float
    env: str
    stdout_dropped: int
    stderr_dropped: int
    pid: int | None
    #: A running job's master, or its stdout and stderr, as a JSON list:
    #: the fds an upgrade's exec carries. Lillecarl/pymux#736.
    fds: str | None
    #: Whether the server is its parent, so the next image can wait for it.
    parent: int | None


@dataclass(frozen=True, slots=True)
class JobTagRow:
    TABLE: ClassVar[str] = "job_tags"
    KEY: ClassVar[tuple[str, ...]] = ("job_id", "key")
    job_id: int
    key: str
    value: str | None


@dataclass(frozen=True, slots=True)
class JobOutputRow:
    TABLE: ClassVar[str] = "job_output"
    KEY: ClassVar[tuple[str, ...]] = ("job_id", "stream", "start")
    job_id: int
    stream: str
    start: int
    data: bytes


@dataclass(frozen=True, slots=True)
class ProgramRow:
    TABLE: ClassVar[str] = "pane_programs"
    KEY: ClassVar[tuple[str, ...]] = ("pane_id",)
    pane_id: int
    pane_pid: int
    pane_revision: int
    screen: str
    stream: str
    process: str
    #: The master, which an upgrade's exec carries under this number.
    master_fd: int | None
    #: Whether the server is its parent, so the next image can wait for it.
    parent: int | None


@dataclass(frozen=True, slots=True)
class AppearanceRow:
    TABLE: ClassVar[str] = "screen_appearances"
    KEY: ClassVar[tuple[str, ...]] = ("pane_id", "appearance")
    pane_id: int
    appearance: int
    value: str


@dataclass(frozen=True, slots=True)
class ScreenRow:
    "One row of a pane's screen or history, as the hand-over file holds it."

    TABLE: ClassVar[str] = "screen_rows"
    KEY: ClassVar[tuple[str, ...]] = ("pane_id", "buffer", "number")
    pane_id: int
    buffer: str
    number: int
    wrapped: int
    text: str
    cells: str
    marks: int


@dataclass(frozen=True, slots=True)
class ClientModesRow:
    TABLE: ClassVar[str] = "client_modes"
    KEY: ClassVar[tuple[str, ...]] = ("client_id",)
    client_id: str
    modes: str


#: One class per table, in the order of `TABLES`.
ROWS = (
    ServerRow,
    CounterRow,
    OptionRow,
    GlobalEnvironmentRow,
    SessionRow,
    SessionEnvironmentRow,
    SessionTagRow,
    WindowDefaultRow,
    WindowRow,
    WindowTagRow,
    SplitRow,
    PaneRow,
    PaneUserVarRow,
    PaneTagRow,
    ViewerRow,
    SplitWeightRow,
    ColumnWidthRow,
    ClientRow,
    ClientWindowRow,
    NamedBufferRow,
    ServerListRow,
    HookRow,
    KeyBindingRow,
    NotificationRow,
    NotificationRouteRow,
    WaitChannelRow,
    SavedQueryRow,
    JobRow,
    JobTagRow,
    JobOutputRow,
    ProgramRow,
    AppearanceRow,
    ScreenRow,
    ClientModesRow,
)


#: The rows of `LIVE_TABLES`.
LIVE_ROWS = tuple(row for row in ROWS if row.TABLE in LIVE_TABLES)


class Row(Protocol):
    TABLE: ClassVar[str]
    KEY: ClassVar[tuple[str, ...]]
    __dataclass_fields__: ClassVar[dict[str, Field[Any]]]


@functools.cache
def _insert_of(kind: type[Row], replace: bool) -> tuple[str, tuple[str, ...]]:
    """
    The statement that inserts a row of `kind`, and its columns in order.
    Built once per kind: a snapshot inserts thousands of rows on a big
    server, and building it per row was a third of a write.
    Lillecarl/pymux#557.
    """
    names = tuple(each.name for each in stored_fields(kind))
    statement = "INSERT %sINTO %s (%s) VALUES (%s)" % (
        "OR REPLACE " if replace else "",
        kind.TABLE,
        ", ".join(names),
        ", ".join("?" * len(names)),
    )
    return statement, names


def insert(db: apsw.Connection, rows: Iterable[Row], replace: bool = False) -> None:
    """
    Rows into their tables, one statement per table.

    The columns are named and never the table's own order: a column that
    moves in `SCHEMA` would take another column's value without a word.
    """
    by_kind: dict[type[Row], list[Row]] = {}
    for row in rows:
        by_kind.setdefault(type(row), []).append(row)
    for kind, batch in by_kind.items():
        statement, names = _insert_of(kind, replace)
        db.executemany(statement, [[getattr(row, name) for name in names] for row in batch])


#: Each table's rows by key, as a `ChangedRows` last wrote them.
Written = dict[type[Row], dict[tuple, Row]]


@functools.cache
def _delete_of(kind: type[Row]) -> str:
    return "DELETE FROM %s WHERE %s" % (kind.TABLE, " AND ".join("%s = ?" % name for name in kind.KEY))


def key_of(row: Row) -> tuple:
    return tuple(getattr(row, name) for name in row.KEY)


class ChangedRows:
    """
    Writes the rows of some tables, and after the first time only the
    rows that differ from what it wrote last: a row whose key is gone is
    deleted, and a new or changed one inserted. An upgrade's snapshot
    writes twice, and the second time, in the pause, is this.
    Lillecarl/pymux#557.

    **A changed row is deleted and inserted, never replaced.**
    `notifications` and `notification_routes` hold a `position` that is
    unique beyond the key, and `INSERT OR REPLACE` would delete whatever
    neighbour holds that position now. Every delete runs before every
    insert, so the positions are free; a row that takes another's
    position has changed too, so it is in the inserts.

    **`written` moves only after the caller's transaction commits.** A
    write that rolls back and moved it anyway would make the retry see
    nothing to do.
    """

    def __init__(self, kinds: Iterable[type[Row]]) -> None:
        self.kinds = tuple(kinds)
        self.written: Written = {kind: {} for kind in self.kinds}

    def write(self, db: apsw.Connection, rows: Iterable[Row]) -> Written:
        "Write the difference, inside the caller's transaction, and answer what the tables hold then."
        now: Written = {kind: {} for kind in self.kinds}
        for row in rows:
            now[type(row)][key_of(row)] = row
        inserts: list[Row] = []
        for kind in self.kinds:
            before, after = self.written[kind], now[kind]
            stale = [key for key, row in before.items() if after.get(key) != row]
            if stale:
                db.executemany(_delete_of(kind), stale)
            inserts.extend(row for key, row in after.items() if before.get(key) != row)
        insert(db, inserts)
        return now


def select[R: Row](db: apsw.Connection, kind: type[R], where: str = "", *params) -> list[R]:
    """
    `kind` for each row of its table that `where` picks.

    The columns are named from the class and not read off the cursor:
    a question that answers no rows has finished before its cursor
    could name them.
    """
    names = [each.name for each in stored_fields(kind)]
    query = "SELECT %s FROM %s %s" % (", ".join(names), kind.TABLE, where)
    return [kind(*values) for values in db.execute(query, params)]
