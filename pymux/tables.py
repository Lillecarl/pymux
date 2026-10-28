"""
The tables a snapshot holds and `sql` reads, one dataclass per table.

A row class is the declaration, and `create_table` writes its SQL: a
field is a column, its annotation the type and `NOT NULL` unless it
takes None, its default the column's, and `KEY` the primary key.
`column` adds what an annotation cannot say. A load adds a table or a
column the snapshot lacks, so an addition needs no step.
Lillecarl/pymux#771.

A scope's options declare their own columns, and `options_table` makes
the row class from that declaration. Lillecarl/pymux#769.
"""

from __future__ import annotations

import functools
from collections.abc import Iterable
from dataclasses import MISSING, Field, dataclass, field, fields, make_dataclass
from typing import Any, ClassVar, Final, Protocol, get_args, get_type_hints

import apsw
from pyte.keep import Keep

from .client_options import ClientOptions
from .option_fields import Options
from .server_options import ServerOptions
from .session_options import SessionOptions
from .window_options import WindowOptions

__all__ = [
    "LIVE_ROWS",
    "LIVE_TABLES",
    "ROWS",
    "SCHEMA",
    "SCREEN_TABLES",
    "TABLES",
    "ChangedRows",
    "affinity",
    "column",
    "columns_of",
    "create_table",
    "insert",
    "literal",
    "missing_from",
    "select",
]


def literal(value: Any) -> str:
    "A value as SQL writes it, for a column's DEFAULT."
    if isinstance(value, bool):
        return str(int(value))
    if isinstance(value, (int, float)):
        return repr(value)
    return "'%s'" % str(value).replace("'", "''")


_AFFINITY: Final = {int: "INTEGER", bool: "INTEGER", float: "REAL", str: "TEXT", bytes: "BLOB"}


def affinity(hint: Any) -> str:
    "The column type of a field annotated `hint`; none for `Any` or a union of two types."
    types = [each for each in get_args(hint) or (hint,) if each is not type(None)]
    return _AFFINITY.get(types[0], "") if len(types) == 1 else ""


def column(
    *,
    references: str | None = None,
    check: str | None = None,
    unique: bool = False,
    affinity: str | None = None,
) -> dict[str, Any]:
    """
    A field's metadata for what its annotation cannot say; `affinity`
    overrides the annotation's. Not a field itself: a checker reads any
    call but `field` as a default.
    """
    metadata = {"references": references, "check": check, "unique": unique, "affinity": affinity}
    return {name: value for name, value in metadata.items() if value}


@functools.cache
def columns_of(kind: type[Row]) -> dict[str, str]:
    "Each stored column of `kind` by name, as `CREATE TABLE` and `ADD COLUMN` write it."
    hints = get_type_hints(kind)
    single = kind.KEY if len(kind.KEY) == 1 else ()
    columns = {}
    for each in stored_fields(kind):
        hint = hints[each.name]
        words = [each.name, each.metadata.get("affinity") or affinity(hint)]
        if each.name in single:
            words.append("PRIMARY KEY")
        elif hint is not Any and type(None) not in get_args(hint):
            words.append("NOT NULL")
        if each.metadata.get("unique"):
            words.append("UNIQUE")
        if each.default not in (MISSING, None):
            words.append("DEFAULT " + literal(each.default))
        if references := each.metadata.get("references"):
            words.append("REFERENCES " + references)
        if check := each.metadata.get("check"):
            words.append("CHECK (%s)" % check)
        columns[each.name] = " ".join(word for word in words if word)
    return columns


@functools.cache
def create_table(kind: type[Row]) -> str:
    "The `CREATE TABLE` of `kind`."
    lines = list(columns_of(kind).values())
    if len(kind.KEY) > 1:
        lines.append("PRIMARY KEY (%s)" % ", ".join(kind.KEY))
    lines.extend("CHECK (%s)" % check for check in getattr(kind, "CHECKS", ()))
    return "CREATE TABLE %s(\n  %s\n);\n" % (kind.TABLE, ",\n  ".join(lines))


def options_table(
    scope: type[Options],
    name: str,
    table: str,
    key: str,
    references: str | None,
    *,
    defaults: bool,
    key_type: type = int,
) -> type:
    """
    The row class of one scope's options, one column per option.

    `defaults` makes the table of what a new owner starts with: a
    column is NULL until somebody set it, which `show-options -g` reads
    as not set.
    """
    specs: list[Any] = [(key, key_type, field(metadata=column(references=references)))]
    for declared in scope.DECLARED.values():
        if declared.keep is not Keep.SAVED:
            continue
        kind = declared.python_type()
        if defaults:
            specs.append(
                (declared.attribute, kind | None, field(default=None, metadata=column(affinity=declared.column_type())))
            )
        else:
            stored = declared.stored(declared.default)
            specs.append(
                (declared.attribute, kind, field(default=stored, metadata=column(affinity=declared.column_type())))
            )
    namespace = {"TABLE": table, "KEY": (key,)}
    return make_dataclass(name, specs, frozen=True, slots=True, namespace=namespace, module=__name__)


#: What every new window of a session starts with, as `set-window-option -g` said.
WindowDefaultRow = options_table(
    WindowOptions, "WindowDefaultRow", "window_defaults", "session_id", "sessions(session_id)", defaults=True
)
#: One window's options.
WindowOptionRow = options_table(
    WindowOptions, "WindowOptionRow", "window_options", "window_id", "windows(window_id)", defaults=False
)

#: The server's options, in its one row.
ServerOptionRow = options_table(ServerOptions, "ServerOptionRow", "server_options", "id", None, defaults=False)

#: What every session reads unless it set its own, in one row.
SessionDefaultRow = options_table(SessionOptions, "SessionDefaultRow", "session_defaults", "id", None, defaults=False)
#: The values one session set itself. Lillecarl/pymux#756.
SessionOptionRow = options_table(
    SessionOptions, "SessionOptionRow", "session_options", "session_id", "sessions(session_id)", defaults=True
)

#: What one client set, by the id it keeps across reattaches.
ClientOptionRow = options_table(
    ClientOptions, "ClientOptionRow", "client_options", "client_id", "clients(client_id)", defaults=False, key_type=str
)

JOB_TABLES = ("jobs", "job_tags", "job_output")

#: A pane's program and its screen, which only the transition holds.
SCREEN_TABLES = ("pane_programs", "screen_appearances", "screen_rows")

#: What a client is in the middle of -- a held prefix, a key table, a
#: prompt -- which typing changes, so only the transition holds it too.
CLIENT_MODE_TABLES = ("client_modes",)


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
    last_active_window_id: int | None
    overlay_pane_id: int | None
    overlay_title: str
    overlay_width: str | None
    overlay_height: str | None


@dataclass(frozen=True, slots=True)
class SessionEnvironmentRow:
    TABLE: ClassVar[str] = "session_environment"
    KEY: ClassVar[tuple[str, ...]] = ("session_id", "name")
    session_id: int = field(metadata=column(references="sessions(session_id)"))
    name: str
    value: str | None


@dataclass(frozen=True, slots=True)
class SessionTagRow:
    TABLE: ClassVar[str] = "session_tags"
    KEY: ClassVar[tuple[str, ...]] = ("session_id", "key")
    session_id: int = field(metadata=column(references="sessions(session_id)"))
    key: str
    value: str | None


@dataclass(frozen=True, slots=True)
class WindowRow:
    TABLE: ClassVar[str] = "windows"
    KEY: ClassVar[tuple[str, ...]] = ("window_id",)
    window_id: int
    session_id: int = field(metadata=column(references="sessions(session_id)"))
    linked: int
    position: int
    window_index: int
    window_name: str | None
    root_split_id: int
    active_pane_id: int | None
    previous_active_pane_id: int | None
    previous_layout: str | None
    manual_height: int | None
    manual_width: int | None
    zoom: int
    frame_rate_set: int
    bell: int = 0
    activity: int = 0
    silence: int = 0
    notified: int = 0
    shown_name: str | None = shown()


@dataclass(frozen=True, slots=True)
class WindowTagRow:
    TABLE: ClassVar[str] = "window_tags"
    KEY: ClassVar[tuple[str, ...]] = ("window_id", "key")
    window_id: int = field(metadata=column(references="windows(window_id)"))
    key: str
    value: str | None


@dataclass(frozen=True, slots=True)
class SplitRow:
    TABLE: ClassVar[str] = "splits"
    KEY: ClassVar[tuple[str, ...]] = ("split_id",)
    split_id: int
    window_id: int = field(metadata=column(references="windows(window_id)"))
    kind: str = field(metadata=column(check="kind IN ('hsplit', 'vsplit')"))
    parent_split_id: int | None = field(metadata=column(references="splits(split_id)"))
    position: int | None


@dataclass(frozen=True, slots=True)
class PaneRow:
    TABLE: ClassVar[str] = "panes"
    KEY: ClassVar[tuple[str, ...]] = ("pane_id",)
    pane_id: int
    window_id: int | None = field(metadata=column(references="windows(window_id)"))
    parent_split_id: int | None = field(metadata=column(references="splits(split_id)"))
    position: int | None
    pane_name: str | None
    clock_mode: int
    pane_current_path: str | None
    current_host: str | None
    command_zone: str | None
    last_exit_status: int | None
    bell: int = 0
    shown_name: str | None = shown()
    title: str | None = shown()
    current_command: str | None = shown()
    pane_index: int | None = shown()


@dataclass(frozen=True, slots=True)
class PaneUserVarRow:
    TABLE: ClassVar[str] = "pane_user_vars"
    KEY: ClassVar[tuple[str, ...]] = ("pane_id", "name")
    pane_id: int = field(metadata=column(references="panes(pane_id)"))
    name: str
    value: str


@dataclass(frozen=True, slots=True)
class PaneTagRow:
    TABLE: ClassVar[str] = "pane_tags"
    KEY: ClassVar[tuple[str, ...]] = ("pane_id", "key")
    pane_id: int = field(metadata=column(references="panes(pane_id)"))
    key: str
    value: str | None


@dataclass(frozen=True, slots=True)
class ViewerRow:
    TABLE: ClassVar[str] = "pane_viewers"
    KEY: ClassVar[tuple[str, ...]] = ("pane_id",)
    pane_id: int = field(metadata=column(references="panes(pane_id)"))
    job_id: int = field(metadata=column(references="jobs(job_id)"))
    pane_width: int
    pane_height: int


@dataclass(frozen=True, slots=True)
class SplitWeightRow:
    TABLE: ClassVar[str] = "split_weights"
    KEY: ClassVar[tuple[str, ...]] = ("split_id", "kind", "id")
    split_id: int = field(metadata=column(references="splits(split_id)"))
    kind: str = field(metadata=column(check="kind IN ('pane', 'split')"))
    id: int
    weight: int


@dataclass(frozen=True, slots=True)
class ColumnWidthRow:
    TABLE: ClassVar[str] = "column_widths"
    KEY: ClassVar[tuple[str, ...]] = ("window_id", "kind", "id")
    window_id: int = field(metadata=column(references="windows(window_id)"))
    kind: str = field(metadata=column(check="kind IN ('pane', 'split')"))
    id: int
    width: float


@dataclass(frozen=True, slots=True)
class ClientRow:
    TABLE: ClassVar[str] = "clients"
    KEY: ClassVar[tuple[str, ...]] = ("client_id",)
    client_id: str
    session_id: int | None = field(metadata=column(references="sessions(session_id)"))
    previous_session_id: int | None = field(metadata=column(references="sessions(session_id)"))
    last_used: int | None
    read_only: int | None
    ignore_size: int | None
    message: str | None
    connected: float | None
    terminal_name: str | None = shown()


@dataclass(frozen=True, slots=True)
class ClientWindowRow:
    TABLE: ClassVar[str] = "client_windows"
    KEY: ClassVar[tuple[str, ...]] = ("client_id", "session_id")
    client_id: str = field(metadata=column(references="clients(client_id)"))
    session_id: int = field(metadata=column(references="sessions(session_id)"))
    active_window_id: int | None = field(metadata=column(references="windows(window_id)"))
    previous_window_id: int | None = field(metadata=column(references="windows(window_id)"))


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
    list: str = field(metadata=column(check="list IN ('prompt_history', 'message_log')"))
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
    CHECKS: ClassVar[tuple[str, ...]] = ("(position IS NULL) != (identifier IS NULL)",)
    notification_id: int
    title: str
    body: str
    urgency: int
    pane_id: int | None
    at: float
    position: int | None = field(metadata=column(unique=True))
    identifier: str | None
    source: str = ""


@dataclass(frozen=True, slots=True)
class NotificationRouteRow:
    TABLE: ClassVar[str] = "notification_routes"
    KEY: ClassVar[tuple[str, ...]] = ("ours",)
    ours: str
    pane_id: int
    identifier: str
    position: int = field(metadata=column(unique=True))


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
class ForgottenSeedRow:
    "A seed question somebody took away, which an upgrade must not bring back."

    TABLE: ClassVar[str] = "forgotten_seeds"
    KEY: ClassVar[tuple[str, ...]] = ("name",)
    name: str


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
    job_id: int = field(metadata=column(references="jobs(job_id)"))
    key: str
    value: str | None


@dataclass(frozen=True, slots=True)
class JobOutputRow:
    TABLE: ClassVar[str] = "job_output"
    KEY: ClassVar[tuple[str, ...]] = ("job_id", "stream", "start")
    job_id: int = field(metadata=column(references="jobs(job_id)"))
    stream: str = field(metadata=column(check="stream IN ('stdout', 'stderr')"))
    start: int
    data: bytes


@dataclass(frozen=True, slots=True)
class ProgramRow:
    TABLE: ClassVar[str] = "pane_programs"
    KEY: ClassVar[tuple[str, ...]] = ("pane_id",)
    pane_id: int = field(metadata=column(references="panes(pane_id)"))
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
    pane_id: int = field(metadata=column(references="panes(pane_id)"))
    appearance: int
    value: str


@dataclass(frozen=True, slots=True)
class ScreenRow:
    "One row of a pane's screen or history, as the hand-over file holds it."

    TABLE: ClassVar[str] = "screen_rows"
    KEY: ClassVar[tuple[str, ...]] = ("pane_id", "buffer", "number")
    pane_id: int = field(metadata=column(references="panes(pane_id)"))
    buffer: str
    number: int
    wrapped: int
    text: str
    cells: str
    marks: int = 0


@dataclass(frozen=True, slots=True)
class ClientModesRow:
    TABLE: ClassVar[str] = "client_modes"
    KEY: ClassVar[tuple[str, ...]] = ("client_id",)
    client_id: str = field(metadata=column(references="clients(client_id)"))
    modes: str


#: One class per table, in the order a snapshot creates and a dump compares them.
ROWS = (
    ServerRow,
    CounterRow,
    ServerOptionRow,
    SessionDefaultRow,
    GlobalEnvironmentRow,
    SessionRow,
    SessionEnvironmentRow,
    SessionTagRow,
    SessionOptionRow,
    WindowDefaultRow,
    WindowRow,
    WindowOptionRow,
    WindowTagRow,
    SplitRow,
    PaneRow,
    PaneUserVarRow,
    PaneTagRow,
    ViewerRow,
    SplitWeightRow,
    ColumnWidthRow,
    ClientRow,
    ClientOptionRow,
    ClientWindowRow,
    NamedBufferRow,
    ServerListRow,
    HookRow,
    KeyBindingRow,
    NotificationRow,
    NotificationRouteRow,
    WaitChannelRow,
    SavedQueryRow,
    ForgottenSeedRow,
    JobRow,
    JobTagRow,
    JobOutputRow,
    ProgramRow,
    AppearanceRow,
    ScreenRow,
    ClientModesRow,
)


TABLES = tuple(kind.TABLE for kind in ROWS)

SCHEMA = "".join(create_table(kind) for kind in ROWS)

#: The tables of window management, which `sql` reads under `live`. A
#: snapshot holds the jobs, the screens and the client modes beside them.
LIVE_TABLES = tuple(table for table in TABLES if table not in JOB_TABLES + SCREEN_TABLES + CLIENT_MODE_TABLES)


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
type Written = dict[type[Row], dict[tuple[apsw.SQLiteValue, ...], Row]]


@functools.cache
def _delete_of(kind: type[Row]) -> str:
    return "DELETE FROM %s WHERE %s" % (kind.TABLE, " AND ".join("%s = ?" % name for name in kind.KEY))


def key_of(row: Row) -> tuple[apsw.SQLiteValue, ...]:
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


def missing_from(db: apsw.Connection) -> str:
    """
    What gives `db` each table and column `ROWS` declares and it lacks,
    a column at its default: an addition needs no step. sqlite cannot
    add a `UNIQUE` column, or a `NOT NULL` one with no default, so those
    fail here and need one.
    """
    statements = []
    for kind in ROWS:
        have = {each[1] for each in db.execute("PRAGMA table_info(%s)" % (kind.TABLE,))}
        if not have:
            statements.append(create_table(kind))
            continue
        for name, definition in columns_of(kind).items():
            if name not in have:
                statements.append("ALTER TABLE %s ADD COLUMN %s;\n" % (kind.TABLE, definition))
    return "".join(statements)


def select[R: Row](db: apsw.Connection, kind: type[R], where: str = "", *params: apsw.SQLiteValue) -> list[R]:
    """
    `kind` for each row of its table that `where` picks.

    The columns are named from the class and not read off the cursor:
    a question that answers no rows has finished before its cursor
    could name them.
    """
    names = [each.name for each in stored_fields(kind)]
    query = "SELECT %s FROM %s %s" % (", ".join(names), kind.TABLE, where)
    return [kind(*values) for values in db.execute(query, params)]
