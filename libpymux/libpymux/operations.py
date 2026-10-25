"""
What the objects of libpymux ask a server, with no I/O.

An operation is a generator. It yields each command it needs as an
`Ask`, receives the `CommandResult`, and returns its answer. `drive`
runs one over a connection, so `objects.py` spells no command and parses
no row itself, and a test runs an operation with no socket at all.

An operation answers with rows, as dictionaries of text. `objects.py`
makes its objects of them, because an object holds the server it came
from.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Generator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, NamedTuple

from .connection import CommandResult

__all__ = [
    "PANE_FIELDS",
    "SEPARATOR",
    "SERVER_FIELDS",
    "SESSION_FIELDS",
    "WINDOW_FIELDS",
    "Ask",
    "JobRecord",
    "Operation",
    "Row",
    "SqlRow",
    "Tail",
    "drive",
    "format_string",
    "rows",
]

#: What separates two fields of one row. A format string carries it
#: between the variables, and nothing a terminal writes holds it.
SEPARATOR = "\x1f"

SERVER_FIELDS = ("socket_path", "pid", "version", "host", "start_time")

SESSION_FIELDS = (
    "session_id",
    "session_name",
    "session_attached",
    "session_windows",
    "session_path",
    "session_created",
)

WINDOW_FIELDS = (
    "window_id",
    "window_index",
    "window_name",
    "window_active",
    "window_flags",
    "window_panes",
    "window_width",
    "window_height",
)

PANE_FIELDS = (
    "pane_id",
    "pane_index",
    "window_id",
    "window_index",
    "pane_active",
    "pane_width",
    "pane_height",
    "pane_title",
    "pane_pid",
    "pane_current_command",
    "pane_current_path",
    "pane_dead",
    "pane_in_mode",
    "pane_mode",
    "pane_revision",
    "history_size",
    "history_limit",
)

#: One row of a `-F` listing, by field name. The keys are the names of
#: format variables, which is data here and not a fixed shape.
type Row = dict[str, str]


class Ask(NamedTuple):
    "One command an operation needs run."

    arguments: list[str]
    #: False for a command that answers with its exit code.
    check: bool = True


type Operation[T] = Generator[Ask, CommandResult, T]


async def drive[T](operation: Operation[T], run: Callable[[Ask], Awaitable[CommandResult]]) -> T:
    "Run `operation`, with `run` carrying each command to the server."
    try:
        ask = next(operation)
        while True:
            ask = operation.send(await run(ask))
    except StopIteration as done:
        return done.value


def format_string(fields: Sequence[str]) -> str:
    "The format string that asks for these fields, in this order."
    return SEPARATOR.join("#{%s}" % name for name in fields)


def rows(result: CommandResult, fields: Sequence[str]) -> list[Row]:
    "One dictionary for each line that the server printed."
    found = []
    for line in result.stdout.splitlines():
        if not line:
            continue
        values = line.split(SEPARATOR)
        # A field that the server does not know comes back empty, and a
        # short row means the server is older than this library.
        values += [""] * (len(fields) - len(values))
        found.append(dict(zip(fields, values, strict=False)))
    return found


def as_int(value: str, fallback: int = 0) -> int:
    try:
        return int(value)
    except TypeError, ValueError:
        return fallback


def first[T](found: list[T]) -> T | None:
    return found[0] if found else None


def having(found: list[Row], field: str, value: str) -> Row | None:
    "The first row whose `field` is `value`."
    for row in found:
        if row.get(field) == value:
            return row
    return None


def _lines(start: int | None, end: int | None) -> list[str]:
    arguments = []
    if start is not None:
        arguments += ["-S", str(start)]
    if end is not None:
        arguments += ["-E", str(end)]
    return arguments


# -- reading -----------------------------------------------------------


def query(command: Sequence[str], fields: Sequence[str]) -> Operation[list[Row]]:
    result = yield Ask([*command, "-F", format_string(fields)])
    return rows(result, fields)


def server_info() -> Operation[Row]:
    return first((yield from query(["list-sessions"], SERVER_FIELDS))) or {}


def sessions() -> Operation[list[Row]]:
    return (yield from query(["list-sessions"], SESSION_FIELDS))


def session_windows(session_id: str) -> Operation[list[Row]]:
    return (yield from query(["list-windows", "-t", session_id], WINDOW_FIELDS))


def windows() -> Operation[list[Row]]:
    "Every window of the session a client last looked at."
    return (yield from query(["list-windows"], WINDOW_FIELDS))


def panes() -> Operation[list[Row]]:
    "Every pane of every window."
    return (yield from query(["list-panes", "-a"], PANE_FIELDS))


def window_panes(window_id: str) -> Operation[list[Row]]:
    return [row for row in (yield from panes()) if row.get("window_id") == window_id]


def session_panes(session_id: str) -> Operation[list[Row]]:
    found = []
    for window in (yield from session_windows(session_id)):
        found += yield from window_panes(window["window_id"])
    return found


def pane(pane_id: str) -> Operation[Row | None]:
    return having((yield from panes()), "pane_id", pane_id)


def window(window_id: str) -> Operation[Row | None]:
    return having((yield from windows()), "window_id", window_id)


def window_at(index: int) -> Operation[Row | None]:
    return having((yield from windows()), "window_index", str(index))


def session(session_id: str) -> Operation[Row | None]:
    return having((yield from sessions()), "session_id", session_id)


def session_named(name: str) -> Operation[Row | None]:
    return having((yield from sessions()), "session_name", name)


def has_session(name: str = "") -> Operation[bool]:
    arguments = ["has-session"]
    if name:
        arguments += ["-t", name]
    return (yield Ask(arguments, check=False)).ok


def html_stylesheet(pane_id: str | None = None) -> Operation[str]:
    arguments = ["show-html-stylesheet"]
    if pane_id is not None:
        arguments += ["-t", pane_id]
    return (yield Ask(arguments)).stdout


# -- a pane ------------------------------------------------------------


def current_revision(pane_id: str) -> Operation[int]:
    row = yield from pane(pane_id)
    if row is None:
        raise LookupError("the pane %s is gone" % (pane_id,))
    return as_int(row.get("pane_revision", ""), -1)


def send_keys(pane_id: str, text: str, enter: bool, literal: bool) -> Operation[None]:
    if text:
        arguments = ["send-keys", "-t", pane_id]
        if literal:
            arguments.append("-l")
        arguments.append(text)
        yield Ask(arguments)
    if enter:
        yield Ask(["send-keys", "-t", pane_id, "Enter"])


def send_key(pane_id: str, name: str) -> Operation[None]:
    yield Ask(["send-keys", "-t", pane_id, name])


def capture(pane_id: str, start: int | None, end: int | None, html: bool = False) -> Operation[str]:
    arguments = ["capture-pane", "-p"]
    if html:
        arguments.append("-H")
    arguments += ["-t", pane_id, *_lines(start, end)]
    return (yield Ask(arguments)).stdout


def wait_for_change(pane_id: str, since: int | None, timeout: float | None) -> Operation[int]:
    arguments = ["wait-pane-change", "-t", pane_id]
    if since is not None:
        arguments += ["--since", str(since)]
    if timeout is not None:
        arguments += ["--timeout", str(timeout)]
    return as_int((yield Ask(arguments)).stdout.strip(), -1)


def clear_history(pane_id: str) -> Operation[None]:
    yield Ask(["clear-history", "-t", pane_id])


def select_pane(pane_id: str) -> Operation[None]:
    yield Ask(["select-pane", "-t", pane_id])


def kill_pane(pane_id: str) -> Operation[None]:
    yield Ask(["kill-pane", "-t", pane_id])


def rename_pane(pane_id: str, name: str) -> Operation[None]:
    yield Ask(["rename-pane", "-t", pane_id, name])


# -- a window ----------------------------------------------------------


def split_window(
    window_id: str,
    command: str | None,
    vertical: bool,
    start_directory: str | None,
    select: bool,
) -> Operation[Row | None]:
    arguments = ["split-window", "-t", window_id, "-v" if vertical else "-h"]
    if start_directory is not None:
        arguments += ["-c", start_directory]
    if not select:
        arguments.append("-d")
    arguments += ["-P", "-F", format_string(PANE_FIELDS)]
    if command is not None:
        arguments.append(command)
    return first(rows((yield Ask(arguments)), PANE_FIELDS))


def select_window(index: int) -> Operation[None]:
    yield Ask(["select-window", "-t", str(index)])


def rename_window(index: int, name: str) -> Operation[None]:
    yield Ask(["rename-window", "-t", str(index), name])


def kill_window(index: int) -> Operation[None]:
    yield Ask(["kill-window", "-t", str(index)])


# -- a session ---------------------------------------------------------


def new_window(
    session_name: str,
    command: str | None,
    name: str | None,
    start_directory: str | None,
    select: bool,
) -> Operation[Row | None]:
    arguments = ["new-window", "-t", "%s:" % (session_name,)]
    if name is not None:
        arguments += ["-n", name]
    if start_directory is not None:
        arguments += ["-c", start_directory]
    if not select:
        arguments.append("-d")
    arguments += ["-P", "-F", format_string(WINDOW_FIELDS)]
    if command is not None:
        arguments.append(command)
    return first(rows((yield Ask(arguments)), WINDOW_FIELDS))


def rename_session(session_id: str, name: str) -> Operation[None]:
    yield Ask(["rename-session", "-t", session_id, name])


def kill_session(session_id: str) -> Operation[None]:
    yield Ask(["kill-session", "-t", session_id])


# -- the server --------------------------------------------------------


def new_session(name: str, attach: bool) -> Operation[Row | None]:
    arguments = ["new-session", "-s", name]
    if not attach:
        arguments.append("-d")
    arguments += ["-P", "-F", format_string(SESSION_FIELDS)]
    return first(rows((yield Ask(arguments)), SESSION_FIELDS))


def kill_server() -> Operation[None]:
    yield Ask(["kill-server"], check=False)


# -- jobs ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class JobRecord:
    """
    One job, as the server's `jobs` and `job_tags` tables gave it.

    A tag with no value is `None`, so `key` and `key=` stay two tags,
    as they are on the command line.
    """

    id: int
    command: str
    directory: str | None
    #: "running" or "done".
    status: str
    returncode: int | None
    #: Why the job never started, when it did not.
    error: str | None
    started: float
    finished: float | None
    pty: bool
    tags: dict[str, str | None]

    @property
    def running(self) -> bool:
        return self.status == "running"


class Tail(NamedTuple):
    "What a tail read, and the job as it stood when the tail stopped."

    text: str
    job: JobRecord


_JOB_COLUMNS = "id, command, directory, status, returncode, error, started, finished, pty"


#: One row `sql --json` answers: a column name to a value of whatever
#: type the column holds. The columns are the question's, so no fixed
#: shape fits them.
type SqlRow = dict[str, Any]


def sql(query: str, params: Mapping[str, object] | None = None) -> Operation[list[SqlRow]]:
    "The rows of one read-only question, every one of them. `:name` in the query reads `params`."
    arguments = ["sql", "--json", "-n", "0"]
    for name, value in (params or {}).items():
        arguments += ["--param", "%s=%s" % (name, value)]
    return json.loads((yield Ask([*arguments, query])).stdout)["rows"]


def _job_records(found: list[dict[str, Any]], tagged: list[dict[str, Any]]) -> list[JobRecord]:
    tags: dict[int, dict[str, str | None]] = {}
    for row in tagged:
        tags.setdefault(row["job_id"], {})[row["key"]] = row["value"]
    return [
        JobRecord(
            id=row["id"],
            command=row["command"],
            directory=row["directory"],
            status=row["status"],
            returncode=row["returncode"],
            error=row["error"],
            started=row["started"],
            finished=row["finished"],
            pty=bool(row["pty"]),
            tags=tags.get(row["id"], {}),
        )
        for row in found
    ]


def carries(record: JobRecord, wanted: Mapping[str, str | None]) -> bool:
    "Whether the job carries every tag: a bare key takes any value, as `pymux tail --tag` reads one."
    for key, value in wanted.items():
        if key not in record.tags:
            return False
        if value is not None and record.tags[key] != value:
            return False
    return True


def jobs(tags: Mapping[str, str | None] | None = None) -> Operation[list[JobRecord]]:
    "Every job the server remembers, oldest first, or those carrying every tag."
    found = yield from sql("SELECT %s FROM jobs ORDER BY id" % (_JOB_COLUMNS,))
    tagged = yield from sql("SELECT job_id, key, value FROM job_tags")
    return [record for record in _job_records(found, tagged) if carries(record, tags or {})]


def job(job_id: int) -> Operation[JobRecord | None]:
    found = yield from sql("SELECT %s FROM jobs WHERE id = :id" % (_JOB_COLUMNS,), {"id": job_id})
    tagged = yield from sql("SELECT job_id, key, value FROM job_tags WHERE job_id = :id", {"id": job_id})
    return first(_job_records(found, tagged))


def _tag_word(key: str, value: str | None) -> str:
    return key if value is None else "%s=%s" % (key, value)


def run_job(
    command: str,
    tags: Mapping[str, str | None],
    directory: str | None,
    env: Mapping[str, str],
    pty: bool,
) -> Operation[JobRecord]:
    """
    Start `command` through the shell, as a job, and answer it.

    The command goes as one word, which `run` reads as a whole command
    line, so `echo a; echo b` stays two commands of one shell.
    """
    arguments = ["run"]
    for key, value in tags.items():
        arguments += ["--tag", _tag_word(key, value)]
    if directory is not None:
        arguments += ["-d", directory]
    for key, value in env.items():
        arguments += ["--env", "%s=%s" % (key, value)]
    if pty:
        arguments.append("--pty")
    job_id = int((yield Ask([*arguments, "--", command])).stdout.strip())
    record = yield from job(job_id)
    if record is None:
        raise LookupError("job %d is gone as soon as it started" % (job_id,))
    return record


def tail_job(job_id: int, lines: int, timeout: float) -> Operation[Tail]:
    """
    Replay the last `lines` of stdout and follow both streams for up to
    `timeout` seconds, or until the job ends.

    **The job is read back after the tail, and the tail's exit code is
    not trusted.** `pymux tail` answers 124 for a job that still runs
    and 4 for no job, and a job may exit with 124 or 4 itself, so the
    code alone cannot say which happened.
    """
    result = yield Ask(["tail", str(job_id), "--lines", str(lines), "--timeout", str(timeout)], check=False)
    record = yield from job(job_id)
    if record is None:
        raise LookupError("no job %d: %s" % (job_id, result.stderr.strip() or "the server forgot it"))
    return Tail(result.stdout, record)


def job_output(job_id: int, stderr: bool) -> Operation[str]:
    "The kept tail of one stream, with a note on how much fell off past the cap."
    arguments = ["show-job"]
    if stderr:
        arguments.append("-e")
    return (yield Ask([*arguments, str(job_id)])).stdout


def kill_job(job_id: int) -> Operation[None]:
    "Send the job SIGTERM. A job that already ended raises `CommandError`."
    yield Ask(["kill-job", str(job_id)])
