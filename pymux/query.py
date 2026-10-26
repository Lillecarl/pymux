"""
SQL over the server's own objects, for the `sql` command.

**Nothing is stored.** A question opens a private in-memory connection,
and every table in it is a virtual one over rows built from the
objects when the question reads that table: the jobs, the tags and the
kept questions in `main`, and sessions, windows, panes, clients and the
server's records in `live`, by the snapshot's names
(`tables.LIVE_ROWS`), with the columns a person sees beside them
(`tables.shown`). The connection closes with the answer. So typing, a
frame or a pane's output never touches SQLite, and two questions never
lock each other. Lillecarl/pymux#722.

**The rows are built on the loop, and SQLite runs on a worker.** An
async apsw connection runs SQLite in a thread of its own, which is
what keeps a slow question off the loop. The rows are frozen
dataclasses, so that thread only reads values that cannot change.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import nullcontext
from dataclasses import dataclass, fields
from functools import partial
from typing import TYPE_CHECKING, Any, NamedTuple, cast, get_args, get_type_hints

import anyio
import apsw
import apsw.ext

from .tables import LIVE_ROWS, JobTagRow, SavedQueryRow

if TYPE_CHECKING:
    from .main import Pymux

__all__ = [
    "SCHEMA_VERSION",
    "SEEDS",
    "Answer",
    "QueryError",
    "ask",
    "is_read_only_query",
    "register",
    "tables_of",
]

# **The SQLite inside apsw is the one of its own release.** apsw builds
# its own SQLite in, with every symbol `static`, so a second SQLite in
# the process cannot answer for it; the versions agree to the patch, or
# this build is not the one `nix/python-pins.nix` makes.
if not apsw.using_amalgamation or not apsw.apsw_version().startswith(apsw.sqlite_lib_version() + "."):
    raise ImportError(
        "apsw %s runs on SQLite %s, %s"
        % (
            apsw.apsw_version(),
            apsw.sqlite_lib_version(),
            "compiled in" if apsw.using_amalgamation else "a shared library",
        )
    )

#: What this server answers with. Agents read it with `PRAGMA
#: user_version` and re-read the tables when it differs from what they
#: were told. 11: `live` has `forgotten_seeds`, and `live.saved_queries`
#: holds only what was saved; `main.saved_queries` has the seeds too.
SCHEMA_VERSION = 11

#: The questions every boot starts with. A name an agent saves later
#: replaces one of these for the rest of the boot; the next boot seeds
#: again.
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
    *(
        ("complete-%s" % kind, sql, "What Tab offers for a %s: the word, and what it says beside it." % kind)
        for kind, sql in (
            (
                "session",
                (
                    "SELECT s.session_name AS word,"
                    " (SELECT count(*) FROM live.windows w WHERE w.session_id = s.session_id AND w.linked)"
                    " || ' windows'"
                    " || CASE WHEN EXISTS (SELECT 1 FROM live.clients c"
                    " WHERE c.session_id = s.session_id AND c.terminal_name IS NOT NULL)"
                    " THEN ' (attached)' ELSE '' END AS says"
                    " FROM live.sessions s ORDER BY s.position"
                ),
            ),
            (
                "session-end",
                (
                    "SELECT session_name || ':' AS word, 'after the last window' AS says"
                    " FROM live.sessions ORDER BY position"
                ),
            ),
            (
                "window",
                (
                    "SELECT s.session_name || ':' || w.window_index AS word,"
                    " w.shown_name || ', ' || (SELECT count(*) FROM live.panes p WHERE p.window_id = w.window_id)"
                    " || ' panes' AS says"
                    " FROM live.windows w JOIN live.sessions s USING (session_id)"
                    " WHERE w.linked ORDER BY s.position, w.window_index"
                ),
            ),
            (
                "pane",
                (
                    "SELECT '%' || p.pane_id AS word,"
                    " s.session_name || ':' || w.window_index || '.' || p.pane_index"
                    " || ' ' || coalesce(p.current_command, '') || ' ' || coalesce(p.title, '') AS says"
                    " FROM live.panes p JOIN live.windows w USING (window_id) JOIN live.sessions s USING (session_id)"
                    " WHERE w.linked ORDER BY s.position, w.window_index, p.pane_index"
                ),
            ),
            (
                "client",
                (
                    "SELECT coalesce(nullif(c.name, ''), c.terminal_name) AS word, s.session_name AS says"
                    " FROM live.clients c LEFT JOIN live.sessions s USING (session_id)"
                    " WHERE c.terminal_name IS NOT NULL ORDER BY c.connected"
                ),
            ),
            ("job", "SELECT id AS word, command AS says FROM jobs ORDER BY id"),
            ("option", "SELECT name AS word, NULL AS says FROM known_options WHERE scope = :scope"),
            (
                "option-value",
                "SELECT value AS word, NULL AS says FROM option_choices WHERE scope = :scope AND name = :option",
            ),
            ("layout", "SELECT name AS word, NULL AS says FROM layouts"),
            # A third column: the chord grammar reads where each word may stand.
            ("key", "SELECT name AS word, says, kind FROM key_names"),
            (
                "forward",
                (
                    "SELECT listening AS word, dest AS says FROM forwards"
                    " WHERE client_id = :client AND direction = :direction"
                ),
            ),
        )
    ),
]


@dataclass(frozen=True, slots=True)
class JobView:
    """
    A job, as a question reads it. The snapshot's `JobRow` holds more --
    the environment, the fds -- which no question should read.
    """

    id: int
    command: str
    directory: str | None
    status: str
    returncode: int | None
    error: str | None
    started: float
    finished: float | None
    pty: int


@dataclass(frozen=True, slots=True)
class KnownOptionView:
    "An option a command can set, and its scope: server, session, window or client."

    scope: str
    name: str


@dataclass(frozen=True, slots=True)
class OptionChoiceView:
    "One value an option takes, of those it can name: a free one names none."

    scope: str
    name: str
    value: str


@dataclass(frozen=True, slots=True)
class LayoutView:
    name: str


@dataclass(frozen=True, slots=True)
class KeyNameView:
    "A word a key is written with, and whether it is a key, a modifier or the prefix."

    name: str
    says: str
    kind: str


@dataclass(frozen=True, slots=True)
class ForwardView:
    "A port a client forwards, by the listening end `list-forwards` shows."

    client_id: str
    direction: str
    listening: str
    dest: str


class Answer(NamedTuple):
    "The columns, up to the limit of rows, and whether rows were left over."

    columns: list[str]
    rows: list[tuple[Any, ...]]
    truncated: bool


class QueryError(Exception):
    "A question SQLite refused or could not finish, in its words."


#: What gives a table its rows, called when a question first reads it.
type Source = Callable[..., Iterable[Any]]


def _jobs(pymux: Pymux) -> Iterator[JobView]:
    for job in pymux.jobs.listing():
        yield JobView(
            job.job_id,
            job.command,
            job.directory,
            job.status,
            job.returncode,
            job.error,
            job.started,
            job.finished,
            1 if job.pty else 0,
        )


def _job_tags(pymux: Pymux) -> Iterator[JobTagRow]:
    for job in pymux.jobs.listing():
        for key, value in job.tags.items():
            yield JobTagRow(job.job_id, key, value)


def _saved_queries(pymux: Pymux) -> Iterator[SavedQueryRow]:
    for name, (sql, description) in sorted(pymux.jobs.queries.items()):
        yield SavedQueryRow(name, sql, description)


def _scoped_options(pymux: Pymux) -> Iterator[tuple[str, str, Any]]:
    for scope, options in pymux.option_tables.items():
        for name, option in sorted(options.items()):
            yield scope.value, name, option


def _known_options(pymux: Pymux) -> Iterator[KnownOptionView]:
    for scope, name, _option in _scoped_options(pymux):
        yield KnownOptionView(scope, name)


def _option_choices(pymux: Pymux, scope: object = None, name: object = None) -> Iterator[OptionChoiceView]:
    for each_scope, each_name, option in _scoped_options(pymux):
        if (scope is not None and each_scope != scope) or (name is not None and each_name != name):
            continue
        for value in sorted(option.get_all_values(pymux)):
            yield OptionChoiceView(each_scope, each_name, value)


def _layouts(pymux: Pymux) -> Iterator[LayoutView]:
    from .arrangement import LayoutTypes

    for layout in sorted(each.value for each in LayoutTypes):
        yield LayoutView(layout)


def _key_names(pymux: Pymux) -> Iterator[KeyNameView]:
    from .key_spelling import key_words

    for name, says, kind in key_words():
        yield KeyNameView(name, says, kind.value)


def _forwards(pymux: Pymux) -> Iterator[ForwardView]:
    for connection in pymux.connections:
        for one in connection.forwards:
            yield ForwardView(
                connection.client_id or "",
                one.get("direction", ""),
                # The port really bound, which is what `list-forwards`
                # shows and so what a person can name. Lillecarl/pymux#441.
                "%s:%s" % (one.get("listen_host", ""), one.get("port", "")),
                one.get("dest", ""),
            )


def _live(pymux: Pymux, table: str) -> Iterator[Any]:
    # Here and not at the top: the snapshot reaches `pymux.main`.
    from .snapshot import rows_of

    return (row for row in rows_of(pymux) if row.TABLE == table)


class Table(NamedTuple):
    "A table's rows, what gives them, and the columns an `=` can narrow the giving by."

    kind: type
    source: Source
    narrows: tuple[str, ...] = ()


def tables_of(pymux: Pymux) -> dict[tuple[str, str], Table]:
    "Every table a question sees, by schema and name, with what gives its rows."
    tables = {
        ("main", "jobs"): Table(JobView, partial(_jobs, pymux)),
        ("main", "job_tags"): Table(JobTagRow, partial(_job_tags, pymux)),
        ("main", "saved_queries"): Table(SavedQueryRow, partial(_saved_queries, pymux)),
        ("main", "known_options"): Table(KnownOptionView, partial(_known_options, pymux)),
        # Narrowed, because listing a value can be slow: `theme` reads
        # every theme it knows, which measured 65 ms.
        ("main", "option_choices"): Table(OptionChoiceView, partial(_option_choices, pymux), ("scope", "name")),
        ("main", "layouts"): Table(LayoutView, partial(_layouts, pymux)),
        ("main", "key_names"): Table(KeyNameView, partial(_key_names, pymux)),
        ("main", "forwards"): Table(ForwardView, partial(_forwards, pymux)),
    }
    for kind in LIVE_ROWS:
        tables["live", kind.TABLE] = Table(kind, partial(_live, pymux, kind.TABLE))
    return tables


#: The column type each Python type declares, so that a `--param`,
#: which arrives as text, compares with a number the way it does with a
#: real table's column.
_AFFINITY = {int: "INTEGER", float: "REAL", str: "TEXT", bytes: "BLOB"}


def _declared(kind: type) -> str:
    "The columns of `kind`, each with the type its field holds; untyped for `Any`."
    hints = get_type_hints(kind)
    columns = []
    for field in fields(kind):
        types = [each for each in get_args(hints[field.name]) or (hints[field.name],) if each is not type(None)]
        affinity = _AFFINITY.get(types[0], "") if len(types) == 1 else ""
        columns.append(('"%s" %s' % (field.name, affinity)).rstrip())
    return "CREATE TABLE x(%s)" % ", ".join(columns)


async def _on_loop(source: Source) -> list[Any]:
    return list(source())


class _Rows:
    """
    A read-only virtual table: apsw's module, table and cursor in one.

    **The rows are built when a question reads the table, and only
    then.** On an async connection SQLite runs on a worker, and the
    objects belong to the loop, so the worker hands the build to the
    loop (`apsw.async_run_coro`) and waits. Two tables of one answer can
    each show a slightly different moment; nothing here needs more.
    """

    def __init__(self, table: Table, on_loop: bool) -> None:
        self.schema = _declared(table.kind)
        self.names = [field.name for field in fields(table.kind)]
        self.source = table.source
        self.narrows = table.narrows
        self.on_loop = on_loop

    def build(self, **narrowed: apsw.SQLiteValue) -> list[Any]:
        source = partial(self.source, **narrowed)
        if self.on_loop:
            return apsw.async_run_coro(_on_loop(source))
        return list(source())

    def Create(
        self, connection: apsw.Connection, modulename: str, databasename: str, tablename: str, *args: apsw.SQLiteValue
    ) -> tuple[str, _Rows]:
        return self.schema, self

    Connect = Create

    def BestIndex(self, constraints: Sequence[tuple[int, int]], orderbys: Sequence[tuple[int, int]]) -> Any:
        """
        Hand an `=` on a column the source narrows by to the source, so
        it builds only those rows. SQLite still checks every row, so a
        source that narrows less than it was asked is only slower.
        """
        used: list[int | None] = []
        columns: list[str] = []
        for column, op in constraints:
            name = self.names[column] if 0 <= column < len(self.names) else None
            if op == apsw.SQLITE_INDEX_CONSTRAINT_EQ and name in self.narrows and name not in columns:
                used.append(len(columns))
                columns.append(name)
            else:
                used.append(None)
        if not columns:
            return None
        return used, 0, ",".join(columns), False, 10.0

    def Open(self) -> _Cursor:
        return _Cursor(self)

    def Disconnect(self) -> None:
        pass

    Destroy = Disconnect


class _Cursor:
    def __init__(self, table: _Rows) -> None:
        self._table = table
        self._rows: list[Any] = []
        self._at = 0

    def Filter(self, indexnum: int, indexname: str | None, constraintargs: tuple | None) -> None:
        narrowed = dict(zip(indexname.split(","), constraintargs or (), strict=True)) if indexname else {}
        self._rows = self._table.build(**narrowed)
        self._at = 0

    def Eof(self) -> bool:
        return self._at >= len(self._rows)

    def Column(self, number: int) -> apsw.SQLiteValue:
        if number == -1:
            return self._at
        return getattr(self._rows[self._at], self._table.names[number])

    def Next(self) -> None:
        self._at += 1

    def Rowid(self) -> int:
        return self._at

    def Close(self) -> None:
        pass


def register(db: apsw.Connection, tables: dict[tuple[str, str], Table], on_loop: bool) -> None:
    "The tables, on a connection that runs on the loop or, with `on_loop`, on a worker that hands back to it."
    db.execute("ATTACH ':memory:' AS live")
    for (schema, name), table in tables.items():
        module = "%s_%s" % (schema, name)
        # A cast, because apsw's stubs make every optional method of a
        # virtual table -- `ShadowName`, `UpdateInsertRow`, `Savepoint`
        # -- a member of the protocol, which no read-only table has.
        db.create_module(module, cast("apsw.VTModule", _Rows(table, on_loop)))
        db.execute("CREATE VIRTUAL TABLE %s.%s USING %s()" % (schema, name, module))
    db.execute("PRAGMA user_version = %d" % SCHEMA_VERSION)
    db.execute("PRAGMA query_only = ON")


def _run(db: apsw.Connection, query: str, params: dict[str, str], limit: int) -> Answer:
    """
    One row past the cap is read to learn whether rows were left: the
    cap names how much is answered, not how much is looked at. The
    columns come from preparing the question, because a question that
    answers no rows has finished before its cursor could name them.
    """
    info = apsw.ext.query_info(db, query, params)
    if info.query_remaining and info.query_remaining.strip():
        raise QueryError("one question at a time")
    columns = [name for name, _type in info.description]
    take = None if limit == 0 else limit + 1
    rows: list[tuple[Any, ...]] = []
    for row in db.execute(query, params):
        rows.append(tuple(row))
        if take is not None and len(rows) == take:
            break
    truncated = take is not None and len(rows) > limit
    return Answer(columns, rows[:limit] if truncated else rows, truncated)


async def ask(pymux: Pymux, query: str, params: dict[str, str], limit: int, timeout: float) -> Answer:
    """
    Answer one question. A question that outlives `timeout` seconds
    (0: no bound) is interrupted and raises `TimeoutError`.
    """
    db = await apsw.Connection.as_async(":memory:")
    try:
        await db.async_run(register, db, tables_of(pymux), True)
        bound = anyio.fail_after(timeout) if timeout else nullcontext()
        with bound:
            try:
                return await db.async_run(_run, db, query, params, limit)
            except anyio.get_cancelled_exc_class():
                # The worker still steps the question; it stops at the
                # next row SQLite looks at.
                db.interrupt()
                raise
            except apsw.Error as error:
                raise QueryError(str(error)) from None
            except KeyError as error:
                # apsw names a `:name` nothing fills with a KeyError.
                raise QueryError("nothing fills :%s" % error.args[0]) from None
    finally:
        with anyio.CancelScope(shield=True):
            await db.aclose()


#: What a question may start with, after comments and whitespace are
#: gone. Everything else -- INSERT, UPDATE, DELETE, DROP, ATTACH, and
#: the pragmas that write -- is refused before SQLite ever sees it;
#: `query_only` and the read-only virtual tables stand behind this.
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
