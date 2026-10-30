"""
The operations, each over libpymux.

Ids are pymux's own: a session is `$0`, a window `@1`, a pane `%5`, a
job a number. `command` runs anything else pymux's command line takes,
with those ids as its targets.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Awaitable, Callable, Sequence
from typing import Annotated, Any, Final, Literal

import anyio
from libpymux import CommandError, Job, Pane, Server, Session, Window
from libpymux.operations import WaitFor
from pydantic import BaseModel, ConfigDict, Field

from . import browser, cdp
from .fonts import bundled
from .registry import Registry

__all__ = ["OPERATIONS", "Png"]

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

#: The longest edge of a PNG, in pixels: what Claude reads without
#: scaling it down, which costs tokens and blurs it both.
PNG_FIT: Final = 1568

#: What pymux says when it has no font to draw a PNG with, and the
#: bundled one is offered instead.
NO_FONT: Final = "no font to draw with"


@dataclasses.dataclass(frozen=True, slots=True)
class Png:
    "A picture an operation answers: `server.render` sends it as image content."

    data: bytes


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
    names its targets by. `here` marks the pane the agent itself runs
    in, when it runs in one of this server's. `tags` are what `tag.set`
    put on each.
    """
    enclosing = Server.enclosing()
    here = enclosing[1].id if enclosing is not None and enclosing[0].socket_path == server.socket_path else None
    tags = await server.tags()
    by_window: dict[str, list[Json]] = {}
    for pane in await server.panes():
        by_window.setdefault(pane.window_id, []).append(
            {**pane_json(pane), "here": pane.id == here, "tags": tags.get(pane.id, {})}
        )
    found = []
    for session in await server.sessions():
        windows = [
            {**window_json(window), "tags": tags.get(window.id, {}), "panes": by_window.get(window.id, [])}
            for window in await session.windows()
        ]
        found.append({**session_json(session), "tags": tags.get(session.id, {}), "windows": windows})
    return found


@operation("sql")
async def sql(server: Server, query: str, params: dict[str, str | int | float] | None = None) -> list[Json]:
    """
    Every row of one read-only SQL question about the server.

    Jobs are the tables `jobs` and `job_tags`; sessions, windows, panes
    and clients are under `live`, as `live.windows`, with their tags in
    `live.session_tags`, `live.window_tags` and `live.pane_tags`. `:name` in the
    query reads `params`. A question that writes is refused. Ids there
    are bare numbers: `window_id = 1` is the window other operations
    call `@1`, and `pane_id = 5` the pane `%5`.
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


#: What a `.new` operation's `tags` says.
NewTags = Annotated[Tags | None, Field(description="Tags to find it by later; null makes a bare key.")]


@operation("session.new")
async def new_session(server: Server, name: str, tags: NewTags = None) -> Json:
    "Make a session that nobody is attached to, and answer it."
    session = await server.new_session(name, tags=tags)
    if session is None:
        raise LookupError("new-session answered nothing")
    return {**session_json(session), "tags": tags or {}}


@operation("window.new")
async def new_window(
    server: Server,
    session: Annotated[str | None, Field(description="The session's name; the oldest session when left out.")] = None,
    name: str | None = None,
    command: Annotated[str | None, Field(description="What to run in it; the default shell when left out.")] = None,
    directory: str | None = None,
    select: Annotated[bool, Field(description="Show it to whoever looks at the session.")] = False,
    tags: NewTags = None,
) -> Json:
    "Make a window in a session, and answer it with its one pane."
    found = await (server.session() if session is None else server.session_named(session))
    if found is None:
        raise LookupError("no session named %r" % (session,))
    window = await found.new_window(command=command, name=name, start_directory=directory, select=select, tags=tags)
    if window is None:
        raise LookupError("new-window answered nothing")
    return {**window_json(window), "tags": tags or {}, "panes": [pane_json(pane) for pane in await window.panes()]}


@operation("window.split")
async def split_window(
    server: Server,
    window: Annotated[str, Field(description="The window's id: `@1`.")],
    command: str | None = None,
    below: Annotated[bool, Field(description="Put the new pane below; false puts it to the right.")] = True,
    directory: str | None = None,
    select: Annotated[bool, Field(description="Give the new pane the keyboard.")] = False,
    tags: NewTags = None,
) -> Json:
    "Split a window, and answer the pane that appears."
    pane = await Window(server, {"window_id": window}).split(
        command=command, vertical=below, start_directory=directory, select=select, tags=tags
    )
    if pane is None:
        raise LookupError("split-window answered nothing")
    return {**pane_json(pane), "tags": tags or {}}


@operation("pane.capture")
async def capture(
    server: Server,
    pane: Annotated[str, Field(description="The pane's id: `%5`.")],
    start: Annotated[
        int | None, Field(description="First line; 0 is the top of the screen, negative is history.")
    ] = None,
    end: Annotated[int | None, Field(description="Last line, counted the same way.")] = None,
    format: Annotated[
        Literal["text", "html", "png"],
        Field(description="`html` adds colours, bold, underline and links; `png` is a picture of the pane."),
    ] = "text",
    cell: Annotated[
        tuple[int, int] | None,
        Field(description="For `png`: one cell's width and height in pixels; the attached terminal's when left out."),
    ] = None,
    fit: Annotated[
        int | None,
        Field(gt=0, description="For `png`: the longest edge in pixels; the cell shrinks to fit. null for none."),
    ] = PNG_FIT,
) -> str | Png:
    """
    The text a pane shows, or the lines between `start` and `end`.

    `html` is one `pre`: colours as `#rrggbb` on spans, `b` `i` `u` `s`
    for renditions, and the pane's own colours on the `pre`. Ask for it
    when the look carries meaning, as a highlighted menu entry or a red
    status line does. `png` draws the pane in its own colours, cursor
    and all, for an agent that reads images: layout and box drawing at
    a glance, where exact text reads better from `text`.
    """
    target = Pane(server, {"pane_id": pane})
    if format == "html":
        return await target.capture_html(start, end, compact=True)
    if format == "png":
        return await _drawn(lambda faces, fallbacks: target.capture_png(start, end, faces, fallbacks, cell, fit))
    return await target.capture(start, end)


async def _drawn(capture: Callable[[Sequence[str], Sequence[str]], Awaitable[bytes]]) -> Png:
    """
    A picture drawn with the system's font and the bundled fallbacks, or
    with the bundled font when pymux finds none of its own.
    """
    fonts = bundled()
    try:
        return Png(await capture((), fonts.fallbacks))
    except CommandError as error:
        if NO_FONT not in str(error) or not fonts.faces:
            raise
        return Png(await capture(fonts.faces, fonts.fallbacks))


@operation("client.capture")
async def capture_client(
    server: Server,
    client: Annotated[
        str | None,
        Field(description="The client's name, as `list-clients` prints it; the one a person used last when left out."),
    ] = None,
    format: Annotated[
        Literal["text", "png"],
        Field(description="`png` is a picture of everything the client shows, in its own colours."),
    ] = "text",
    cell: Annotated[
        tuple[int, int] | None,
        Field(description="For `png`: one cell's width and height in pixels; the client terminal's when left out."),
    ] = None,
    fit: Annotated[
        int | None,
        Field(gt=0, description="For `png`: the longest edge in pixels; the cell shrinks to fit. null for none."),
    ] = PNG_FIT,
) -> str | Png:
    """
    Everything a person attached to pymux sees: the status line, the
    borders, every pane, and the popups, menus and modes over them.

    It is the frame pymux last sent that client, so a change shows once
    pymux has drawn it; `pane.wait` on a pane that changes, or a short
    pause, comes first. Use it to see pymux's own interface, and
    `pane.capture` to read one program.
    """
    if format == "png":
        return await _drawn(lambda faces, fallbacks: server.capture_client_png(client, faces, fallbacks, cell, fit))
    return await server.capture_client(client)


@operation("window.capture")
async def capture_window(
    server: Server,
    window: Annotated[str, Field(description="The window's id: `@3`.")],
    format: Annotated[
        Literal["text", "png"],
        Field(description="`png` is a picture of the window's panes and borders, in their own colours."),
    ] = "text",
    cell: Annotated[
        tuple[int, int] | None,
        Field(description="For `png`: one cell's width and height in pixels; the attached terminal's when left out."),
    ] = None,
    fit: Annotated[
        int | None,
        Field(gt=0, description="For `png`: the longest edge in pixels; the cell shrinks to fit. null for none."),
    ] = PNG_FIT,
) -> str | Png:
    """
    A window as its layout lays it out: every pane at its place and the
    borders between them, whether a person looks at it or not.

    Use it on a window in a session of your own, where `client.capture`
    has nobody to ask. It has no status line, which is a client's.
    """
    target = Window(server, {"window_id": window})
    if format == "png":
        return await _drawn(lambda faces, fallbacks: target.capture_png(faces, fallbacks, cell, fit))
    return await target.capture()


class SendWait(BaseModel):
    "What `pane.send` waits for after its last key: one condition, and a timeout."

    model_config = ConfigDict(extra="forbid")

    timeout: Annotated[float, Field(gt=0, description="How long to wait, in seconds.")]
    quiet: Annotated[float | None, Field(gt=0, description="Until nothing changed for this many seconds.")] = None
    text: Annotated[str | None, Field(description="Until a visible line matches this regular expression.")] = None
    next_text: Annotated[
        str | None,
        Field(description="Until a line written after the send matches; the echo of the keys counts, so anchor it."),
    ] = None
    exit: Annotated[bool, Field(description="Until the program in the pane ends.")] = False

    def condition(self) -> WaitFor:
        return WaitFor(self.quiet, self.text, self.next_text, self.exit, self.timeout)


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
    wait: Annotated[
        SendWait | None,
        Field(description="Hold after the last key until this holds, armed before that key goes out."),
    ] = None,
) -> Json | None:
    """
    Type into a pane: text, then named keys, then Return when asked.

    The text arrives in one burst, which some programs read as a paste,
    and a Return in the same burst then joins the paste. `no_paste`
    sends the Return apart from it.

    With `wait` it answers what `pane.wait_until` answers, and nothing
    the program writes in between is lost: send and wait in one call
    instead of sleeping.
    """
    target = Pane(server, {"pane_id": pane})
    # Each word, and whether it is text rather than a key name.
    words = ([(text, True)] if text else []) + [(key, False) for key in keys or []]
    if enter:
        words.append(("Enter", False))
    if wait is not None and not words:
        raise ValueError("a wait after a send needs something to send: text, keys or enter")
    for index, (word, literal) in enumerate(words):
        last = index == len(words) - 1
        if last and enter and no_paste:
            await anyio.sleep(NO_PASTE_DELAY)
        if last and wait is not None:
            waited = await target.send_and_wait(word, wait.condition(), literal=literal)
            return {"met": waited.met, "text": waited.text}
        if literal:
            await target.send_keys(word, enter=False)
        else:
            await target.send_key(word)
    return None


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


@operation("pane.wait_until")
async def wait_until(
    server: Server,
    pane: Annotated[str, Field(description="The pane's id: `%5`.")],
    timeout: Annotated[float, Field(gt=0, description="How long to wait, in seconds.")],
    quiet: Annotated[
        float | None, Field(gt=0, description="Until nothing changed in the pane for this many seconds.")
    ] = None,
    text: Annotated[str | None, Field(description="Until a visible line matches this regular expression.")] = None,
    next_text: Annotated[
        str | None,
        Field(
            description=(
                "Until a line written after the wait began matches this regular expression; what the screen "
                "held before does not. The echo of keys typed afterwards is new too, so anchor the pattern."
            )
        ),
    ] = None,
    exit: Annotated[bool, Field(description="Until the program in the pane ends.")] = False,
) -> Json:
    """
    Hold until a pane meets one condition, or `timeout` passes.

    Instead of sleeping after `pane.send`: `quiet` for output that
    stops, `text` for a line on the screen, `exit` for the program's
    end. Answers `met`, and `text`: the line that matched, or the screen
    when the wait ran out.
    """
    waited = await Pane(server, {"pane_id": pane}).wait_until(
        quiet=quiet, text=text, next_text=next_text, exit=exit, timeout=timeout
    )
    return {"met": waited.met, "text": waited.text}


@operation("pane.kill")
async def kill_pane(server: Server, pane: Annotated[str, Field(description="The pane's id: `%5`.")]) -> None:
    "End the program in a pane, and take the pane away."
    await Pane(server, {"pane_id": pane}).kill()


# -- tags ------------------------------------------------------------------

TagTarget = Annotated[
    str | int, Field(description="A session `$0`, window `@1` or pane `%5` by its id, or a job by its number.")
]


@operation("tag.set")
async def set_tags(
    server: Server,
    target: TagTarget,
    tags: Annotated[
        Tags, Field(description="Tags to set; null makes a bare key, and a key there takes the new value.")
    ],
) -> None:
    """
    Tag a session, window, pane or job, to find it by later.

    Tag what you make, and `tag.find` answers it again without an id to
    remember. Tags survive a server upgrade.
    """
    await server.set_tags(target, tags)


@operation("tag.unset")
async def unset_tags(server: Server, target: TagTarget, keys: list[str]) -> None:
    "Take tags off a session, window, pane or job. A key that is not there is no error."
    await server.unset_tags(target, *keys)


@operation("tag.find")
async def find_tagged(
    server: Server,
    tags: Annotated[Tags, Field(description="What to find; a null value takes any value.")],
) -> Json:
    """
    Every session, window, pane and job carrying every tag.

    A tag with a value matches that value alone, and one with null any
    value the key holds.
    """
    if not tags:
        raise ValueError("name a tag to find by; tree shows everything")
    found = await server.tags()
    return {
        "sessions": [{**session_json(one), "tags": found.get(one.id, {})} for one in await server.sessions(tags)],
        "windows": [{**window_json(one), "tags": found.get(one.id, {})} for one in await server.windows(tags)],
        "panes": [{**pane_json(one), "tags": found.get(one.id, {})} for one in await server.panes(tags)],
        "jobs": [job_json(job) for job in await server.jobs(tags)],
    }


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


# -- browsers --------------------------------------------------------------

BrowserName = Annotated[str, Field(description="The browser's name, as `browser.open` was given it.")]
Tab = Annotated[str | None, Field(description="A tab's target id from `browser.tabs`; the first tab when left out.")]
Ref = Annotated[str | None, Field(description="A ref from `browser.snapshot`: `e12`.")]
Selector = Annotated[str | None, Field(description="A CSS selector, for an element no snapshot named.")]


@operation("browser.open")
async def open_browser(
    server: Server,
    name: Annotated[str, Field(description="What the other browser operations call it.")] = "browser",
    seat: Annotated[
        browser.Seat,
        Field(
            description=(
                "`pane`: a window of this server that a person attached can watch. `headless`: nothing to watch."
            )
        ),
    ] = browser.Seat.PANE,
    url: str = "about:blank",
    session: Annotated[
        str | None, Field(description="For `pane`: the session's name; the oldest session when left out.")
    ] = None,
    executable: Annotated[
        str | None, Field(description="Chromium to run; PYMUX_MCP_CHROMIUM or `chromium` on the PATH when left out.")
    ] = None,
    arguments: Annotated[list[str] | None, Field(description="More command line for Chromium.")] = None,
    idle: Annotated[
        float,
        Field(ge=0, description="Close it after this many seconds unused by you or a person; 0 keeps it."),
    ] = browser.DEFAULT_IDLE,
) -> Json:
    """
    Start a Chromium to drive, and answer it once it listens.

    It runs across calls until `browser.close`, or until nobody used it
    for `idle` seconds: no browser operation, and no key or mouse from a
    person looking at its window. pymux holds it, as a window pane or a
    job. A `pane` browser opens in a window that is not in front.
    """
    opened = await browser.open_browser(server, name, seat, url, session, executable, arguments or (), idle)
    return opened.json()


@operation("browser.list")
async def list_browsers(server: Server) -> list[Json]:
    "Every browser open, with what holds it."
    return [one.json() for one in await browser.every(server)]


@operation("browser.close")
async def close_browser(server: Server, name: BrowserName = "browser") -> None:
    "Stop a browser, and take its window or job and its profile away."
    await browser.close(await browser.find(server, name))


@operation("browser.tabs")
async def browser_tabs(server: Server, name: BrowserName = "browser") -> list[Json]:
    "A browser's tabs: each one's target id, title and address."
    found = await browser.find(server, name)
    endpoint = found.endpoint()
    if endpoint is None:
        raise browser.BrowserGone("the browser %r is not listening" % (name,))
    async with cdp.connected(endpoint) as connection:
        return [
            {"tab": one["targetId"], "title": one["title"], "url": one["url"]} for one in await browser.tabs(connection)
        ]


@operation("browser.goto")
async def browser_goto(
    server: Server, url: str, name: BrowserName = "browser", tab: Tab = None, timeout: Seconds = 30.0
) -> Json:
    "Load an address in a tab, and wait for it to load or `timeout`."
    async with browser.tab(server, name, tab) as page:
        return await browser.goto(page, url, timeout)


@operation("browser.snapshot")
async def browser_snapshot(
    server: Server,
    name: BrowserName = "browser",
    tab: Tab = None,
    limit: Annotated[int, Field(gt=0, description="The most lines to answer.")] = 400,
) -> str:
    """
    What a tab shows, as its accessibility tree: one line a node.

    Each node an operation can act on carries a ref, `[ref=e12]`, which
    `browser.click` and `browser.type` take. A ref stays good until the
    page drops that element.
    """
    async with browser.tab(server, name, tab) as page:
        return await browser.snapshot(page, limit)


@operation("browser.click")
async def browser_click(
    server: Server, name: BrowserName = "browser", ref: Ref = None, selector: Selector = None, tab: Tab = None
) -> Json:
    "Click an element, named by a ref or a CSS selector, at its centre."
    async with browser.tab(server, name, tab) as page:
        return await browser.click(page, ref, selector)


@operation("browser.type")
async def browser_type(
    server: Server,
    text: str,
    name: BrowserName = "browser",
    ref: Ref = None,
    selector: Selector = None,
    clear: Annotated[bool, Field(description="Empty the element first.")] = False,
    enter: Annotated[bool, Field(description="Press Enter after the text.")] = False,
    tab: Tab = None,
) -> None:
    "Type text into an element, or where the focus is when none is named."
    async with browser.tab(server, name, tab) as page:
        await browser.type_text(page, text, ref, selector, clear)
        if enter:
            await browser.press(page, "Enter")


@operation("browser.press")
async def browser_press(
    server: Server,
    key: Annotated[str, Field(description='A character, or a key by its DOM name: "Enter", "Escape", "ArrowDown".')],
    name: BrowserName = "browser",
    tab: Tab = None,
) -> None:
    "Press one key where the focus is."
    async with browser.tab(server, name, tab) as page:
        await browser.press(page, key)


@operation("browser.evaluate")
async def browser_evaluate(
    server: Server,
    expression: Annotated[str, Field(description="JavaScript; a promise is awaited.")],
    name: BrowserName = "browser",
    tab: Tab = None,
) -> Any:
    "Run JavaScript in a tab, and answer its value as JSON."
    async with browser.tab(server, name, tab) as page:
        return await browser.evaluate(page, expression)


@operation("browser.screenshot")
async def browser_screenshot(
    server: Server,
    name: BrowserName = "browser",
    tab: Tab = None,
    full_page: Annotated[bool, Field(description="The whole page, not only what the window shows.")] = False,
    fit: Annotated[
        int | None,
        Field(gt=0, description="The longest edge in pixels; the page is drawn smaller to fit. null for none."),
    ] = PNG_FIT,
) -> Png:
    "A picture of a tab."
    async with browser.tab(server, name, tab) as page:
        return Png(await browser.screenshot(page, full_page, fit))


@operation("browser.console")
async def browser_console(
    server: Server,
    name: BrowserName = "browser",
    tab: Tab = None,
    limit: Annotated[int, Field(gt=0, description="The most messages to answer, the latest ones.")] = 50,
) -> list[Json]:
    "What a tab logged to its console, and the exceptions it threw, oldest first."
    async with browser.tab(server, name, tab) as page:
        return await browser.console(page, limit)
