"""
The operations, each over libpymux.

Ids are pymux's own: a session is `$0`, a window `@1`, a pane `%5`, a
job a number. `command` runs anything else pymux's command line takes,
with those ids as its targets.
"""

from __future__ import annotations

import dataclasses
from typing import Annotated, Any, Final

import anyio
from libpymux import Job, Pane, Server, Session, Window
from pydantic import Field

from .registry import Registry

__all__ = ["OPERATIONS"]

#: Every operation below. `registry.py` says why it is read from here.
OPERATIONS = Registry()
operation = OPERATIONS.operation

#: A tag: a key to its value, or to null for a bare key.
type Tags = dict[str, str | None]

#: A JSON object an operation answers. Each one picks its own fields.
type Json = dict[str, Any]

Seconds = Annotated[float, Field(ge=0, description="How long to wait, in seconds.")]
JobId = Annotated[int, Field(description="The job's id, as `job.run` and `job.list` answer it.")]

#: Seconds between the text and the Return of a `no_paste` send.
NO_PASTE_DELAY: Final = 0.5


def pane_json(pane: Pane) -> Json:
    return {
        "id": pane.id,
        "window": pane.window_id,
        "index": pane.index,
        "active": pane.active,
        "width": pane.width,
        "height": pane.height,
        "title": pane.title,
        "command": pane.current_command,
        "path": pane.current_path,
        "dead": pane.dead,
        "mode": pane.mode,
    }


def window_json(window: Window) -> Json:
    return {
        "id": window.id,
        "index": window.index,
        "name": window.name,
        "active": window.active,
        "width": window.width,
        "height": window.height,
    }


def session_json(session: Session) -> Json:
    return {"id": session.id, "name": session.name, "attached": session.attached}


def job_json(job: Job) -> Json:
    return dataclasses.asdict(job.record)


# -- what there is ---------------------------------------------------------


@operation("tree")
async def tree(server: Server) -> list[Json]:
    """
    Every session, with its windows, and each window with its panes.

    Start here: the ids in the answer are what every other operation
    names its targets by.
    """
    by_window: dict[str, list[Json]] = {}
    for pane in await server.panes():
        by_window.setdefault(pane.window_id, []).append(pane_json(pane))
    found = []
    for session in await server.sessions():
        windows = [{**window_json(window), "panes": by_window.get(window.id, [])} for window in await session.windows()]
        found.append({**session_json(session), "windows": windows})
    return found


@operation("sql")
async def sql(server: Server, query: str, params: dict[str, str | int | float] | None = None) -> list[Json]:
    """
    Every row of one read-only SQL question about the server.

    Jobs are the tables `jobs` and `job_tags`; sessions, windows, panes
    and clients are under `live`, as `live.windows`. `:name` in the
    query reads `params`. A question that writes is refused.
    """
    return await server.sql(query, params)


@operation("command")
async def command(server: Server, command: str) -> Json:
    """
    Run any pymux command line, as `pymux <command>` would.

    For what no other operation covers: `rename-window -t @1 logs`,
    `display-message -p '#{pane_current_path}'`. Answers what it wrote
    and its exit code, and never raises on a non-zero one.
    """
    result = await server.cmd(command, check=False)
    return {"stdout": result.stdout, "stderr": result.stderr, "exit_code": result.exit_code}


# -- sessions, windows, panes ----------------------------------------------


@operation("session.new")
async def new_session(server: Server, name: str) -> Json:
    "Make a session that nobody is attached to, and answer it."
    session = await server.new_session(name)
    if session is None:
        raise LookupError("new-session answered nothing")
    return session_json(session)


@operation("window.new")
async def new_window(
    server: Server,
    session: Annotated[str | None, Field(description="The session's name; the oldest session when left out.")] = None,
    name: str | None = None,
    command: Annotated[str | None, Field(description="What to run in it; the default shell when left out.")] = None,
    directory: str | None = None,
    select: Annotated[bool, Field(description="Show it to whoever looks at the session.")] = False,
) -> Json:
    "Make a window in a session, and answer it with its one pane."
    found = await (server.session() if session is None else server.session_named(session))
    if found is None:
        raise LookupError("no session named %r" % (session,))
    window = await found.new_window(command=command, name=name, start_directory=directory, select=select)
    if window is None:
        raise LookupError("new-window answered nothing")
    return {**window_json(window), "panes": [pane_json(pane) for pane in await window.panes()]}


@operation("window.split")
async def split_window(
    server: Server,
    window: Annotated[str, Field(description="The window's id: `@1`.")],
    command: str | None = None,
    below: Annotated[bool, Field(description="Put the new pane below; false puts it to the right.")] = True,
    directory: str | None = None,
    select: Annotated[bool, Field(description="Give the new pane the keyboard.")] = False,
) -> Json:
    "Split a window, and answer the pane that appears."
    pane = await Window(server, {"window_id": window}).split(
        command=command, vertical=below, start_directory=directory, select=select
    )
    if pane is None:
        raise LookupError("split-window answered nothing")
    return pane_json(pane)


@operation("pane.capture")
async def capture(
    server: Server,
    pane: Annotated[str, Field(description="The pane's id: `%5`.")],
    start: Annotated[
        int | None, Field(description="First line; 0 is the top of the screen, negative is history.")
    ] = None,
    end: Annotated[int | None, Field(description="Last line, counted the same way.")] = None,
) -> str:
    "The text a pane shows, or the lines between `start` and `end`."
    return await Pane(server, {"pane_id": pane}).capture(start, end)


@operation("pane.send")
async def send(
    server: Server,
    pane: Annotated[str, Field(description="The pane's id: `%5`.")],
    text: Annotated[str, Field(description="Typed as it stands.")] = "",
    keys: Annotated[list[str] | None, Field(description='Named keys, after the text: "C-c", "Escape", "Up".')] = None,
    enter: Annotated[bool, Field(description="Press Return at the end.")] = False,
    no_paste: Annotated[
        bool,
        Field(
            description=(
                "Wait a moment before Return, so a program that tells a paste by its speed, as Claude Code "
                "does, reads Return as Enter and not as a newline inside the pasted text."
            )
        ),
    ] = False,
) -> None:
    """
    Type into a pane: text, then named keys, then Return when asked.

    The text arrives in one burst, which some programs read as a paste,
    and a Return in the same burst then joins the paste. `no_paste`
    sends the Return apart from it.
    """
    target = Pane(server, {"pane_id": pane})
    if text:
        await target.send_keys(text, enter=False)
    for key in keys or []:
        await target.send_key(key)
    if enter:
        if no_paste:
            await anyio.sleep(NO_PASTE_DELAY)
        await target.send_key("Enter")


@operation("pane.wait")
async def wait(
    server: Server,
    pane: Annotated[str, Field(description="The pane's id: `%5`.")],
    # pymux refuses a wait of no time at all.
    timeout: Annotated[float, Field(gt=0, description="How long to wait, in seconds.")],
    since: Annotated[
        int | None, Field(description="The revision last seen; the one the pane holds now when left out.")
    ] = None,
) -> Json:
    """
    Hold until a pane shows something new, or `timeout` passes.

    Answers the pane's revision and whether it changed. Pass that
    revision back as `since` next time, and nothing that happened
    between two waits is missed.
    """
    target = Pane(server, {"pane_id": pane})
    if since is None:
        since = await target.current_revision()
    revision = await target.wait_for_change(since=since, timeout=timeout)
    return {"revision": revision, "changed": revision != since}


@operation("pane.kill")
async def kill_pane(server: Server, pane: Annotated[str, Field(description="The pane's id: `%5`.")]) -> None:
    "End the program in a pane, and take the pane away."
    await Pane(server, {"pane_id": pane}).kill()


# -- jobs ------------------------------------------------------------------


@operation("job.run")
async def run_job(
    server: Server,
    command: Annotated[str, Field(description="A shell command line.")],
    tags: Annotated[Tags | None, Field(description="Tags to find it by later; null makes a bare key.")] = None,
    directory: str | None = None,
    env: dict[str, str] | None = None,
    pty: Annotated[bool, Field(description="Run on a terminal instead of pipes.")] = False,
    timeout: Annotated[float, Field(ge=0, description="Follow its output this long; 0 answers at once.")] = 0,
    lines: int = 10,
) -> Json:
    """
    Run a shell command as a job, with no pane, and answer the job.

    With a `timeout` it also follows the output that long, as
    `job.tail` does; the job keeps running past it. Tag a job you will
    look for again.
    """
    job = await server.run_job(command, tags=tags, directory=directory, env=env, pty=pty)
    output = await job.tail(timeout=timeout, lines=lines) if timeout else ""
    return {"job": job_json(job), "output": output}


@operation("job.tail")
async def tail_job(server: Server, job: JobId, timeout: Seconds, lines: int = 10) -> Json:
    """
    Replay a job's last `lines` of stdout, then follow both streams until
    it ends or `timeout` passes.

    `job.running` in the answer says which. Tailing again picks up where
    this stopped; `lines: 0` follows without the replay.
    """
    found = await _job(server, job)
    output = await found.tail(timeout=timeout, lines=lines)
    return {"job": job_json(found), "output": output}


@operation("job.list")
async def list_jobs(
    server: Server,
    tags: Annotated[
        Tags | None, Field(description="Only jobs carrying every tag; a null value takes any value.")
    ] = None,
) -> list[Json]:
    "Every job the server remembers, oldest first."
    return [job_json(job) for job in await server.jobs(tags)]


@operation("job.output")
async def job_output(server: Server, job: JobId, stderr: bool = False) -> str:
    "What a job wrote to stdout, or to stderr, so far. It does not wait."
    return await (await _job(server, job)).output(stderr=stderr)


@operation("job.view")
async def view_job(
    server: Server,
    job: JobId,
    session: Annotated[str | None, Field(description="The session's name; the oldest session when left out.")] = None,
    name: Annotated[str | None, Field(description='The window\'s name; "job <id>" when left out.')] = None,
) -> Json:
    """
    Show a job to a person: a new window, in front, that follows the job.

    The window is read-only and outlives the job; closing it leaves the
    job running. Answers the window with its one pane.
    """
    found = await _job(server, job)
    window = await new_window(server, session, name or "job %d" % (job,), select=True)
    (pane,) = window["panes"]
    await found.view(Pane(server, {"pane_id": pane["id"]}), kill=True)
    return {**window, "panes": [pane_json(pane) for pane in await Window(server, {"window_id": window["id"]}).panes()]}


@operation("job.kill")
async def kill_job(server: Server, job: JobId) -> None:
    "Send a running job SIGTERM."
    await (await _job(server, job)).kill()


async def _job(server: Server, job_id: int) -> Job:
    found = await server.job(job_id)
    if found is None:
        raise LookupError("no job %d; job.list shows the ones there are" % (job_id,))
    return found
