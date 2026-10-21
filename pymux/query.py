"""
SQL over the server's own objects, for the `sql` command.

**Nothing is stored.** A question opens a private in-memory connection,
and every table in it is a virtual one (apsw's `make_virtual_module`)
over rows built from the objects as the question starts: the jobs, the
tags and the kept questions in `main`, and sessions, windows, panes,
clients and the server's records in `live`, by the snapshot's names
(`tables.LIVE_ROWS`). The connection closes with the answer. So typing,
a frame or a pane's output never touches SQLite, and two questions
never lock each other. Lillecarl/pymux#722.

**The rows are built on the loop, and SQLite runs on a worker.** An
async apsw connection runs SQLite in a thread of its own, which is
what keeps a slow question off the loop. The rows are frozen
dataclasses made before the question runs, so that thread only reads
values that cannot change, and every table describes the same instant.
"""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import nullcontext
from dataclasses import dataclass, fields
from typing import TYPE_CHECKING, Any, NamedTuple, cast, get_args, get_type_hints

import anyio
import apsw
import apsw.ext

from .tables import LIVE_ROWS, JobTagRow, Row, SavedQueryRow

if TYPE_CHECKING:
    from .main import Pymux

__all__ = ["SCHEMA_VERSION", "SEEDS", "Answer", "QueryError", "ask", "is_read_only_query"]

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
#: were told. 7: `live.clients` has no `modes`.
SCHEMA_VERSION = 7

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
]


@dataclass(frozen=True, slots=True)
class JobView:
    """
    A job, as a question reads it. The snapshot's `JobRow` holds more --
    the environment, the holder's ids -- which no question should read.
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


class Answer(NamedTuple):
    "The columns, up to the limit of rows, and whether rows were left over."

    columns: list[str]
    rows: list[tuple[Any, ...]]
    truncated: bool


class QueryError(Exception):
    "A question SQLite refused or could not finish, in its words."


def _tables_now(pymux: Pymux) -> dict[tuple[str, str], tuple[type, list]]:
    "Every table a question sees, by schema and name, with its rows as of now."
    # Here and not at the top: the snapshot reaches `pymux.main`.
    from .snapshot import rows_of

    jobs = pymux.jobs.listing()
    tables: dict[tuple[str, str], tuple[type, list]] = {
        ("main", "jobs"): (
            JobView,
            [
                JobView(
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
                for job in jobs
            ],
        ),
        ("main", "job_tags"): (
            JobTagRow,
            [JobTagRow(job.job_id, key, value) for job in jobs for key, value in job.tags.items()],
        ),
        ("main", "saved_queries"): (
            SavedQueryRow,
            [
                SavedQueryRow(name, sql, description)
                for name, (sql, description) in sorted(pymux.jobs.saved_queries.items())
            ],
        ),
    }
    for kind in LIVE_ROWS:
        tables["live", kind.TABLE] = (kind, [])
    for row in rows_of(pymux):
        tables["live", row.TABLE][1].append(row)
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


class _Rows:
    "A read-only virtual table over a list of rows: apsw's module, table and cursor in one."

    def __init__(self, kind: type, rows: list[Row]) -> None:
        self.schema = _declared(kind)
        self.names = [field.name for field in fields(kind)]
        self.rows = rows

    def Create(
        self, connection: apsw.Connection, modulename: str, databasename: str, tablename: str, *args: apsw.SQLiteValue
    ) -> tuple[str, _Rows]:
        return self.schema, self

    Connect = Create

    def BestIndex(self, constraints: Sequence[tuple[int, int]], orderbys: Sequence[tuple[int, int]]) -> None:
        return None

    def Open(self) -> _Cursor:
        return _Cursor(self)

    def Disconnect(self) -> None:
        pass

    Destroy = Disconnect


class _Cursor:
    def __init__(self, table: _Rows) -> None:
        self._table = table
        self._at = 0

    def Filter(self, indexnum: int, indexname: str, constraintargs: tuple | None) -> None:
        self._at = 0

    def Eof(self) -> bool:
        return self._at >= len(self._table.rows)

    def Column(self, number: int) -> apsw.SQLiteValue:
        if number == -1:
            return self._at
        return getattr(self._table.rows[self._at], self._table.names[number])

    def Next(self) -> None:
        self._at += 1

    def Rowid(self) -> int:
        return self._at

    def Close(self) -> None:
        pass


def _register(db: apsw.Connection, tables: dict[tuple[str, str], tuple[type, list]]) -> None:
    db.execute("ATTACH ':memory:' AS live")
    for (schema, name), (kind, rows) in tables.items():
        module = "%s_%s" % (schema, name)
        # A cast, because apsw's stubs make every optional method of a
        # virtual table -- `ShadowName`, `UpdateInsertRow`, `Savepoint`
        # -- a member of the protocol, which no read-only table has.
        db.create_module(module, cast("apsw.VTModule", _Rows(kind, rows)))
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
    tables = _tables_now(pymux)
    db = await apsw.Connection.as_async(":memory:")
    try:
        await db.async_run(_register, db, tables)
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
