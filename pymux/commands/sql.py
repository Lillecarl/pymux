from __future__ import annotations

import argparse
import json
import sqlite3
from contextlib import nullcontext
from typing import TYPE_CHECKING, Any

import anyio
from aiosqlite import Connection

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import CommandException, CommandParser, add_command
from pymux.commands.common import answer
from pymux.jobstore import is_read_only_query

#: Rows a question answers before it says it stopped counting. 0 asks
#: for everything, which is how a caller that means it opts out.
DEFAULT_LIMIT = 100

#: Seconds a question may run. 0 asks without a bound.
DEFAULT_TIMEOUT = 10.0


def sql(pymux: Pymux, args: argparse.Namespace):
    """
    Ask the jobs database a question, in SQL.

    Free-form: `sql SELECT id, command FROM jobs WHERE status =
    'running'` answers with a table, `--json` with objects. `sql
    --save NAME` keeps the question on the server under a name, with
    `:named` parameters filled by `--param` when it or another caller
    runs it back with `sql --run NAME`. `sql --list` shows the kept
    questions, `sql --delete NAME` forgets one. Only questions that
    read run; anything else is refused before sqlite sees it.
    """
    if args.list:
        if args.save is not None or args.run is not None or args.param:
            raise CommandException("--list shows questions; it takes no other question flags")
        return _list(pymux, args)
    if args.delete is not None:
        if args.save is not None or args.run is not None or args.query or args.param:
            raise CommandException("--delete forgets one question; it takes no other question flags")
        return _delete(pymux, args.delete)
    if args.save is not None and args.run is not None:
        raise CommandException("give --save or --run, not both")
    if args.lines < 0:
        raise CommandException("-n counts rows from 0")
    if args.timeout < 0:
        raise CommandException("--timeout counts seconds from 0")
    if args.run is not None:
        if args.query:
            raise CommandException("give a query or --run, not both")
        return _run_saved(pymux, args, args.run)
    query = " ".join(args.query).strip()
    if not query:
        raise CommandException("no query given; sql --list shows the kept ones")
    if args.save is not None:
        if args.param:
            raise CommandException("--param fills a run, not a save")
        return _save(pymux, args, args.save, query)
    return _ask(pymux, args, query, _params(args))


async def _list(pymux: Pymux, args: argparse.Namespace) -> None:
    async with pymux.jobs.store.reader() as conn:
        cursor = await conn.execute("SELECT name, sql, description FROM saved_queries ORDER BY name")
        kept = await cursor.fetchall()
    if args.json:
        kept_json = [
            {"name": name, "sql": kept_sql, "description": description} for name, kept_sql, description in kept
        ]
        answer(pymux, json.dumps(kept_json))
        return
    if not kept:
        answer(pymux, "(no saved queries)")
        return
    lines = []
    for name, sql, description in kept:
        lines.append("%s: %s" % (name, description or "no description"))
        lines.append("  %s" % sql)
    answer(pymux, "\n".join(lines))


async def _save(pymux: Pymux, args: argparse.Namespace, name: str, query: str) -> None:
    if not is_read_only_query(query):
        raise CommandException("only questions that read can be kept")
    await pymux.jobs.store.write(
        "INSERT OR REPLACE INTO saved_queries(name, sql, description) VALUES (?, ?, ?)",
        (name, query, args.description),
    )
    answer(pymux, "saved %s" % name)


async def _delete(pymux: Pymux, name: str) -> None:
    cursor = await pymux.jobs.store.write("DELETE FROM saved_queries WHERE name = ?", (name,))
    if not cursor.rowcount:
        raise CommandException("no saved query %s; sql --list shows the kept ones" % name)
    answer(pymux, "deleted %s" % name)


async def _run_saved(pymux: Pymux, args: argparse.Namespace, name: str) -> None:
    async with pymux.jobs.store.reader() as conn:
        cursor = await conn.execute("SELECT sql FROM saved_queries WHERE name = ?", (name,))
        found = await cursor.fetchone()
    if found is None:
        raise CommandException("no saved query %s; sql --list shows the kept ones" % name)
    await _ask(pymux, args, found[0], _params(args))


def _params(args: argparse.Namespace) -> dict[str, str]:
    params = {}
    for item in args.param:
        key, separator, value = item.partition("=")
        if not separator or not key:
            raise CommandException("param must be KEY=VALUE: %r" % item)
        params[key] = value
    return params


async def _ask(pymux: Pymux, args: argparse.Namespace, query: str, params: dict[str, str]) -> None:
    """
    Run one question and answer with its rows.

    The rows come back oldest first only when the question says ORDER
    BY; sqlite answers in whatever order the plan finds, and a limit
    without an order names no promise. What is answered is answered
    whole or not at all: a question that outlives its timeout fails
    instead of answering half its rows.
    """
    if not is_read_only_query(query):
        raise CommandException("only SELECT, WITH, EXPLAIN, PRAGMA and VALUES questions read")
    limit = args.lines
    bound = args.timeout
    scope = anyio.fail_after(bound) if bound else nullcontext()
    try:
        async with pymux.jobs.store.reader() as conn:
            try:
                with scope:
                    columns, rows, truncated = await _run(conn, query, params, limit)
            except TimeoutError:
                # The worker thread still steps; interrupt it before
                # the connection goes back to the pool, or the next
                # checkout inherits a query that never ends.
                await conn.interrupt()
                raise CommandException("sql timed out after %gs; narrow it or raise --timeout" % bound) from None
    except CommandException:
        raise
    except sqlite3.Error as e:
        raise CommandException("sql failed: %s" % e) from None
    if args.json:
        answer(
            pymux,
            json.dumps(
                {
                    "columns": columns,
                    "rows": [dict(zip(columns, row)) for row in rows],
                    "truncated": truncated,
                },
                default=str,
            ),
        )
        return
    lines = [_table(columns, rows)]
    if truncated:
        lines.append("... truncated at %d rows; -n raises the cap" % limit)
    answer(pymux, "\n".join(lines))


async def _run(
    conn: Connection, query: str, params: dict[str, str], limit: int
) -> tuple[list[str], list[tuple[Any, ...]], bool]:
    """
    The columns, up to `limit` rows, and whether rows were left over.

    One row past the cap is read to learn that: the cap names how
    much is answered, not how much is looked at.
    """
    take = None if limit == 0 else limit + 1
    cursor = await conn.execute(query, params)
    columns = [part[0] for part in cursor.description or []]
    rows = [tuple(row) for row in (await cursor.fetchmany(take) if take is not None else await cursor.fetchall())]
    truncated = take is not None and len(rows) > limit
    return columns, rows[:limit] if truncated else rows, truncated


def _table(columns: list[str], rows: list[tuple[Any, ...]]) -> str:
    "Columns and rows, aligned. NULLs say so; machines want --json."
    cells = [[column for column in columns]]
    for row in rows:
        cells.append([str(value) if value is not None else "NULL" for value in row])
    widths = [max(len(line[index]) for line in cells) for index in range(len(columns))]
    return "\n".join(
        "  ".join(line[index].ljust(widths[index]) for index in range(len(columns))).rstrip() for line in cells
    )


def register(subparsers: argparse._SubParsersAction[CommandParser]):
    parser = add_command(subparsers, sql, name="sql", read_only=True)
    parser.add_argument(
        "--save",
        metavar="<name>",
        default=None,
        help="Keep the question on the server under this name, instead of running it.",
    )
    parser.add_argument(
        "--run",
        metavar="<name>",
        default=None,
        help="Run a kept question. :named parameters come from --param.",
    )
    parser.add_argument("--list", action="store_true", help="Show the kept questions and their SQL.")
    parser.add_argument("--delete", metavar="<name>", default=None, help="Forget a kept question.")
    parser.add_argument("--description", metavar="<text>", default=None, help="What a kept question is for.")
    parser.add_argument(
        "--param",
        action="append",
        default=[],
        metavar="<key=value>",
        help="A :named parameter. Repeat for several.",
    )
    parser.add_argument(
        "-n",
        "--lines",
        type=int,
        default=DEFAULT_LIMIT,
        metavar="<n>",
        help="Answer this many rows (default: %d, 0 for everything)." % DEFAULT_LIMIT,
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        metavar="<seconds>",
        help="Fail when the question outlives this (default: %g, 0 for no bound)." % DEFAULT_TIMEOUT,
    )
    parser.add_argument("--json", action="store_true", help="Answer with objects, not a table.")
    parser.add_argument(
        "query",
        nargs=argparse.REMAINDER,
        metavar="<query>",
        help="The question, in SQL.",
    )
