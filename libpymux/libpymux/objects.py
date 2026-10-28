"""
A pymux server, and what it holds, as objects.

A server holds sessions, a session holds windows, a window holds panes.
A pymux server holds as many sessions as a person makes
(Lillecarl/pymux#323), so `await server.sessions()` is the list and
`await server.session()` is the first of them.

Every object reads its fields from the server through a format string.
An object holds what it read and does not follow the server on its own.
Call `refresh()` for the fields again, or read the collection again for
the objects.

**A read that asks the server is an awaited method, never a property.**
A field the object already holds, such as `pane.width`, is a property.

What each object asks is in `operations.py`, and the fields it holds
are in `records.py`.
"""

from __future__ import annotations

# `Server.list` hides the builtin inside that class.
import builtins
import os
from collections.abc import Mapping, Sequence

import anyio

from . import operations
from .connection import CommandResult, Connection, ServerNotRunning
from .operations import Ask, JobRecord, Operation, Row, SqlRow, Tags, drive
from .records import PaneFields, SessionFields, WindowFields
from .sockets import socket_paths
from .streams import PaneStream

__all__ = ["Job", "Pane", "Server", "Session", "Window"]


class Pane(PaneFields):
    "One pane, and the program that runs in it."

    def __init__(self, server: Server, values: Row) -> None:
        super().__init__(values)
        self.server = server

    async def current_revision(self) -> int:
        """
        The revision this pane holds now, asked of the server.

        `revision` alone is the snapshot this object was made from. A
        caller that kept its `Pane` and waited on that number would wait
        on one the server left long ago: every wait then answers at once
        and the loop spins.
        """
        return await self.server.run(operations.current_revision(self.id))

    async def window(self) -> Window | None:
        "The window that holds this pane, read again from the server."
        return self.server._window(await self.server.run(operations.window(self.window_id)))

    async def send_keys(self, text: str, enter: bool = True, literal: bool = True) -> None:
        """
        Send text to the program in this pane.

        `literal` sends the text as it stands. Without it the server
        reads a word such as "Enter" or "C-c" as the name of a key,
        which is how you send a key that has no text.

        `enter` adds a Return after the text.
        """
        await self.server.run(operations.send_keys(self.id, text, enter, literal))

    async def send_key(self, name: str) -> None:
        'Send one named key, such as "C-c" or "Escape".'
        await self.server.run(operations.send_key(self.id, name))

    async def capture(self, start: int | None = None, end: int | None = None) -> str:
        """
        Read the content of this pane back as text.

        The line numbers are the ones tmux uses: 0 is the first visible
        line, and a negative number reaches into the history.
        """
        return await self.server.run(operations.capture(self.id, start, end))

    async def capture_html(self, start: int | None = None, end: int | None = None, compact: bool = False) -> str:
        """
        Read the content of this pane back as HTML.

        One `pre` element, holding the colours, the renditions and the
        hyperlinks that `capture` drops. `Server.html_stylesheet` is the
        other half of it, and a caller needs that once. `compact` needs
        no stylesheet: it writes each colour as it looks and each
        rendition as a tag, for a reader that reads and never draws.

        **The default range is the visible pane**, where `capture` reads
        the whole buffer. The line numbers are the same ones: 0 is the
        first visible line and a negative number reaches into the
        history.

        Three things are not in it. There is no cursor. There are no
        images. And what pymux draws over the pane -- copy mode, the
        clock, a popup -- is not on the pane's own screen, so it is not
        here either; `in_mode` says when that is happening and `mode`
        says which one. Lillecarl/pymux#452.
        """
        return await self.server.run(operations.capture(self.id, start, end, html=True, compact=compact))

    async def capture_png(
        self,
        start: int | None = None,
        end: int | None = None,
        fonts: Sequence[str] = (),
        fallbacks: Sequence[str] = (),
        cell: tuple[int, int] | None = None,
        fit: int | None = None,
    ) -> bytes:
        """
        Read this pane back as a picture: PNG bytes, drawn by the server.

        `fonts` are font files, one per face; without them the server
        draws with fontconfig's monospace, and refuses when it finds
        none. `fallbacks` are font files tried in order for a glyph the
        font lacks. `cell` is the width and height of a cell in pixels;
        the attached terminal's without it. `fit` shrinks the cell until
        the longest edge is at most that many pixels. The range is
        `capture_html`'s.
        """
        return await self.server.run(operations.capture_png(self.id, start, end, fonts, fallbacks, cell, fit))

    async def wait_for_change(self, since: int | None = None, timeout: float | None = None) -> int:
        """
        Hold until this pane shows something else, and answer its
        revision.

        `since` is the revision last seen. The server answers at once
        when the pane has already left it, so a caller that draws a
        frame and then waits misses nothing in between -- which is what
        makes this safe to use in a loop:

            revision = await pane.current_revision()
            while True:
                draw(await pane.capture_html())
                revision = await pane.wait_for_change(since=revision)

        **`current_revision()` and not `revision`.** The property is the
        snapshot this object was made from, so a caller that keeps its
        `Pane` seeds the loop with a number the server left long ago:
        every wait then answers at once and the loop spins at full speed
        with nothing happening. Found by the first caller doing exactly
        that.

        Without `since` it waits for the next change, whatever the pane
        holds now.

        The answer is the same revision back when the wait ran out, so a
        caller compares it with what it sent. `timeout` is how long to
        wait; the server has its own answer for how long, and it is
        under a minute. Lillecarl/pymux#387.
        """
        return await self.server.run(operations.wait_for_change(self.id, since, timeout))

    async def wait_until(
        self,
        *,
        quiet: float | None = None,
        text: str | None = None,
        next_text: str | None = None,
        exit: bool = False,
        timeout: float | None = None,
    ) -> operations.Waited:
        """
        Hold until this pane meets one condition, or `timeout` passes.

        `quiet` is seconds with no change. `text` is a regular expression
        a visible line matches; `next_text` one that a line written after
        the wait began matches, so start it before sending the keys it
        waits on. `exit` waits for the program to end.

        `Waited.met` is False when the wait ran out, and `Waited.text`
        is then the screen it ran out on. Lillecarl/pymux#388.
        """
        condition = operations.WaitFor(quiet, text, next_text, exit, timeout)
        return await self.server.run(operations.wait_until(self.id, condition))

    async def send_and_wait(self, word: str, wait: operations.WaitFor, literal: bool = False) -> operations.Waited:
        """
        Send one key name, or one text with `literal`, and hold until
        `wait` holds. The wait is armed before the key goes out, so
        output that comes back at once is not lost; `wait_until` says
        what each condition means. Lillecarl/pymux#388.
        """
        return await self.server.run(operations.send_and_wait(self.id, word, literal, wait))

    def stream(self, writable: bool = False) -> PaneStream:
        """
        A stream of the rows of this pane that change.

        For a caller that draws the pane rather than reading it once:
        `capture_html` gives a whole screen each time, and this gives each
        changed row once. Use it as a context manager:

            async with pane.stream() as stream:
                async for frame in stream:
                    draw(frame)

        `writable` asks for a stream that takes input as well.
        `libpymux.streams` says what a frame holds. Lillecarl/pymux#461.
        """
        return PaneStream(self.server.socket_path, self.id, writable=writable)

    async def clear_history(self) -> None:
        "Throw away the scrollback of this pane."
        await self.server.run(operations.clear_history(self.id))

    async def select(self) -> None:
        "Give this pane the keyboard."
        await self.server.run(operations.select_pane(self.id))

    async def kill(self) -> None:
        "End the program in this pane and take the pane away."
        await self.server.run(operations.kill_pane(self.id))

    async def rename(self, name: str) -> None:
        await self.server.run(operations.rename_pane(self.id, name))

    async def tags(self) -> Tags:
        "This pane's tags, asked of the server. A bare key holds `None`."
        return await self.server.run(operations.tags_of(self.id))

    async def set_tags(self, tags: Mapping[str, str | None]) -> None:
        await self.server.run(operations.set_tags(self.id, tags))

    async def unset_tags(self, *keys: str) -> None:
        await self.server.run(operations.unset_tags(self.id, keys))

    async def refresh(self) -> Pane:
        "Read the fields of this pane again."
        row = await self.server.run(operations.pane(self.id))
        if row is None:
            raise LookupError("the pane %s is gone" % (self.id,))
        self._values = row
        return self


class Window(WindowFields):
    "One window, and the panes it holds."

    def __init__(self, server: Server, values: Row) -> None:
        super().__init__(values)
        self.server = server

    async def panes(self) -> list[Pane]:
        "Every pane of this window."
        return self.server._panes(await self.server.run(operations.window_panes(self.id)))

    async def active_pane(self) -> Pane | None:
        for pane in await self.panes():
            if pane.active:
                return pane
        return None

    async def split(
        self,
        command: str | None = None,
        vertical: bool = True,
        start_directory: str | None = None,
        select: bool = True,
        tags: Mapping[str, str | None] | None = None,
    ) -> Pane | None:
        """
        Split this window and return the pane that appears.

        `vertical` puts the new pane below the old one, which is what
        tmux calls a vertical split. `tags` tags the new pane.
        """
        row = await self.server.run(
            operations.split_window(self.id, command, vertical, start_directory, select, tags or {})
        )
        return None if row is None else Pane(self.server, row)

    async def select(self) -> None:
        "Show this window."
        await self.server.run(operations.select_window(self.index))

    async def rename(self, name: str) -> None:
        await self.server.run(operations.rename_window(self.index, name))

    async def tags(self) -> Tags:
        "This window's tags, asked of the server. A bare key holds `None`."
        return await self.server.run(operations.tags_of(self.id))

    async def set_tags(self, tags: Mapping[str, str | None]) -> None:
        await self.server.run(operations.set_tags(self.id, tags))

    async def unset_tags(self, *keys: str) -> None:
        await self.server.run(operations.unset_tags(self.id, keys))

    async def kill(self) -> None:
        "End every pane of this window and take the window away."
        await self.server.run(operations.kill_window(self.index))

    async def refresh(self) -> Window:
        row = await self.server.run(operations.window(self.id))
        if row is None:
            raise LookupError("the window %s is gone" % (self.id,))
        self._values = row
        return self


class Session(SessionFields):
    """
    One session of a server.

    Everything it reads names itself with `-t`, so two sessions of one
    server answer for themselves and not for whichever one a client is
    on.
    """

    def __init__(self, server: Server, values: Row) -> None:
        super().__init__(values)
        self.server = server

    async def windows(self) -> list[Window]:
        found = await self.server.run(operations.session_windows(self.id))
        return [Window(self.server, row) for row in found]

    async def active_window(self) -> Window | None:
        for window in await self.windows():
            if window.active:
                return window
        return None

    async def panes(self) -> list[Pane]:
        return self.server._panes(await self.server.run(operations.session_panes(self.id)))

    async def new_window(
        self,
        command: str | None = None,
        name: str | None = None,
        start_directory: str | None = None,
        select: bool = True,
        tags: Mapping[str, str | None] | None = None,
    ) -> Window | None:
        "Make a window in this session and return it. `tags` tags the window."
        return self.server._window(
            await self.server.run(operations.new_window(self.name, command, name, start_directory, select, tags or {}))
        )

    async def rename(self, name: str) -> None:
        await self.server.run(operations.rename_session(self.id, name))

    async def tags(self) -> Tags:
        "This session's tags, asked of the server. A bare key holds `None`."
        return await self.server.run(operations.tags_of(self.id))

    async def set_tags(self, tags: Mapping[str, str | None]) -> None:
        await self.server.run(operations.set_tags(self.id, tags))

    async def unset_tags(self, *keys: str) -> None:
        await self.server.run(operations.unset_tags(self.id, keys))

    async def kill(self) -> None:
        "End this session. The last one to go ends the server."
        await self.server.run(operations.kill_session(self.id))

    async def refresh(self) -> Session:
        row = await self.server.run(operations.session(self.id))
        if row is None:
            raise LookupError("the session %s is gone" % (self.id,))
        self._values = row
        return self


class Job:
    """
    One command the server runs without a pane, or remembers having run.

    `record` is the job as it was last read: `refresh()` reads it again,
    and `tail()` reads it again when the tail stops.
    """

    def __init__(self, server: Server, record: JobRecord) -> None:
        self.server = server
        self.record = record

    def __repr__(self) -> str:
        return "Job(%d, %s, command=%r)" % (self.id, self.status, self.command)

    @property
    def id(self) -> int:
        return self.record.id

    @property
    def command(self) -> str:
        return self.record.command

    @property
    def status(self) -> str:
        'What it was last read as: "running" or "done".'
        return self.record.status

    @property
    def running(self) -> bool:
        return self.record.running

    @property
    def returncode(self) -> int | None:
        return self.record.returncode

    @property
    def tags(self) -> dict[str, str | None]:
        return self.record.tags

    async def set_tags(self, tags: Mapping[str, str | None]) -> None:
        "Tag the job, and read it again so `tags` holds them."
        await self.server.run(operations.set_tags(self.id, tags))
        await self.refresh()

    async def unset_tags(self, *keys: str) -> None:
        await self.server.run(operations.unset_tags(self.id, keys))
        await self.refresh()

    async def tail(self, timeout: float, lines: int = 10) -> str:
        """
        Replay the last `lines` of stdout, follow both streams until the
        job ends or `timeout` seconds pass, and answer what was read.

        `running` and `returncode` then say which of the two happened. A
        timeout leaves the job running, and tailing again picks up where
        this stopped; `lines=0` follows without the replay.
        """
        tail = await self.server.run(operations.tail_job(self.id, lines, timeout))
        self.record = tail.job
        return tail.text

    async def output(self, stderr: bool = False) -> str:
        "The kept tail of stdout, or of stderr, without waiting."
        return await self.server.run(operations.job_output(self.id, stderr))

    async def kill(self) -> None:
        "Send the job SIGTERM. A job that already ended raises `CommandError`."
        await self.server.run(operations.kill_job(self.id))

    async def view(self, pane: Pane, kill: bool = False) -> None:
        """
        Replace `pane` with a read-only viewer that follows this job.

        The viewer takes a new pane id. A pane whose program still runs
        raises `CommandError`, unless `kill` ends that program first.
        """
        await self.server.run(operations.view_job(self.id, pane.id, kill))

    async def refresh(self) -> Job:
        record = await self.server.run(operations.job(self.id))
        if record is None:
            raise LookupError("the job %d is gone" % (self.id,))
        self.record = record
        return self


class Server:
    """
    A pymux server, addressed by the path of its socket.

    Nothing here holds a connection open. Every read and every command
    opens a socket, sends one message and reads the answer, which is
    what the server expects.

    `environment` and `cwd` say who every command comes from, for a
    caller that acts for someone else; `Connection.run` says what the
    server does with them. Left out, they are this process's own.
    """

    def __init__(
        self,
        socket_path: str,
        environment: Mapping[str, str] | None = None,
        cwd: str | None = None,
    ) -> None:
        self.connection = Connection(socket_path)
        self.environment = environment
        self.cwd = cwd

    def __repr__(self) -> str:
        return "Server(%r)" % (self.socket_path,)

    @property
    def socket_path(self) -> str:
        return self.connection.socket_path

    @classmethod
    def enclosing(cls, environment: Mapping[str, str] | None = None) -> tuple[Server, Pane] | None:
        """
        The server and the pane this process runs inside, or nothing.

        pymux names both in `$PYMUX` for every program it starts in a
        pane, as `<socket path>,<pane number>`. `environment` is read
        in place of this process's own.
        """
        socket_path, _, number = (os.environ if environment is None else environment).get("PYMUX", "").rpartition(",")
        if not socket_path or not number.isdigit():
            return None
        server = cls(socket_path)
        return server, Pane(server, {"pane_id": "%" + number})

    @classmethod
    async def list(cls) -> builtins.list[Server]:
        """
        Every pymux server of this user in the default place.

        A server started with a socket path of its own is not in here.
        Name that path to reach it.
        """
        # `socket_paths` probes each socket with a blocking connect.
        return [cls(path) for path in await anyio.to_thread.run_sync(socket_paths)]

    @classmethod
    async def first(cls) -> Server:
        "The one server that is running. Raises when there is not one."
        paths = await anyio.to_thread.run_sync(socket_paths)
        if not paths:
            raise ServerNotRunning("no pymux server is running")
        if len(paths) > 1:
            raise ServerNotRunning("several pymux servers are running: %s" % (", ".join(paths),))
        return cls(paths[0])

    # -- the wire ------------------------------------------------------

    async def cmd(
        self,
        command: str | Sequence[str],
        pane_id: str | None = None,
        check: bool = True,
    ) -> CommandResult:
        """
        Run one pymux command and read what it says.

        This is the way out of the object model: anything the command
        line can do, this can do.
        """
        return await self.connection.run(
            command, pane_id=pane_id, check=check, environment=self.environment, cwd=self.cwd
        )

    async def run[T](self, operation: Operation[T]) -> T:
        "Run one operation of `operations.py`."
        return await drive(operation, self._ask)

    async def _ask(self, ask: Ask) -> CommandResult:
        return await self.cmd(ask.arguments, check=ask.check)

    def _window(self, row: Row | None) -> Window | None:
        return None if row is None else Window(self, row)

    def _panes(self, found: builtins.list[Row]) -> builtins.list[Pane]:
        return [Pane(self, row) for row in found]

    async def is_alive(self) -> bool:
        "True when a server answers on this socket."
        return await self.connection.is_alive()

    async def capture_client(self, client: str | None = None) -> str:
        """
        What a client is shown, as text: the whole of pymux on its
        terminal, a row to a line. `client` is its name as `list-clients`
        prints it; the client a person used last without it.
        """
        return await self.run(operations.capture_client(client))

    async def capture_client_png(
        self,
        client: str | None = None,
        fonts: Sequence[str] = (),
        fallbacks: Sequence[str] = (),
        cell: tuple[int, int] | None = None,
        fit: int | None = None,
    ) -> bytes:
        """
        What a client is shown, as PNG bytes: the status line, the
        borders, every pane and what is drawn over them, in the client's
        colours. The fonts, `cell` and `fit` are `Pane.capture_png`'s;
        the cell defaults to the client terminal's.
        """
        return await self.run(operations.capture_client_png(client, fonts, fallbacks, cell, fit))

    async def html_stylesheet(self, pane: Pane | None = None) -> str:
        """
        The stylesheet that `Pane.capture_html` is written against.

        A caller needs it once and the markup of a frame needs it to
        mean anything: the spans name custom properties, and a page
        that defines none of them draws nothing.

        With a pane it carries that pane's own colours as well -- the
        theme the terminal of a person is showing, and whatever the
        program in the pane set with "OSC 4". Without one it is the
        conventional palette, which is what one stylesheet for every
        pane should be. Lillecarl/pymux#452.
        """
        return await self.run(operations.html_stylesheet(None if pane is None else pane.id))

    # -- what it holds -------------------------------------------------

    async def info(self) -> Row:
        "What the server says about itself."
        return await self.run(operations.server_info())

    async def sessions(self, tags: Mapping[str, str | None] | None = None) -> builtins.list[Session]:
        "Every session of this server, oldest first, or those carrying every tag. A bare key takes any value."
        found = operations.tagged_sessions(tags) if tags else operations.sessions()
        return [Session(self, row) for row in await self.run(found)]

    async def session(self) -> Session:
        "The oldest session of this server."
        found = await self.sessions()
        if not found:
            raise LookupError("the server has no session")
        return found[0]

    async def session_named(self, name: str) -> Session | None:
        "The session with this name, or None."
        row = await self.run(operations.session_named(name))
        return None if row is None else Session(self, row)

    async def new_session(
        self, name: str, attach: bool = False, tags: Mapping[str, str | None] | None = None
    ) -> Session | None:
        "Make a session and return it. `tags` tags the session."
        row = await self.run(operations.new_session(name, attach, tags or {}))
        return None if row is None else Session(self, row)

    async def windows(self, tags: Mapping[str, str | None] | None = None) -> builtins.list[Window]:
        """
        Every window of the session a client last looked at. With `tags`,
        the windows of every session carrying every tag.
        """
        found = operations.tagged_windows(tags) if tags else operations.windows()
        return [Window(self, row) for row in await self.run(found)]

    async def panes(self, tags: Mapping[str, str | None] | None = None) -> builtins.list[Pane]:
        "Every pane of every window, or those carrying every tag."
        return self._panes(await self.run(operations.tagged_panes(tags) if tags else operations.panes()))

    async def tags(self) -> dict[str, Tags]:
        'The tags of every session, window and pane, by id: `{"%5": {"agent": "a1"}}`.'
        return await self.run(operations.all_tags())

    async def set_tags(self, target: str | int, tags: Mapping[str, str | None]) -> None:
        "Tag a session, window or pane by its id (`$0`, `@1`, `%5`), or a job by its number."
        await self.run(operations.set_tags(target, tags))

    async def unset_tags(self, target: str | int, *keys: str) -> None:
        "Take keys off what `target` names, as `set_tags` reads it."
        await self.run(operations.unset_tags(target, keys))

    async def window(self, index: int) -> Window | None:
        "The window at this index, or None."
        return self._window(await self.run(operations.window_at(index)))

    async def pane(self, pane_id: str) -> Pane | None:
        'The pane with this id ("%1001"), or None.'
        row = await self.run(operations.pane(pane_id))
        return None if row is None else Pane(self, row)

    async def has_session(self, name: str = "") -> bool:
        "True when the server answers to this session name."
        return await self.run(operations.has_session(name))

    async def sql(self, query: str, params: Mapping[str, object] | None = None) -> builtins.list[SqlRow]:
        """
        Every row of one read-only question, as dictionaries.

        The server's jobs are the tables `jobs` and `job_tags`, and its
        sessions, windows, panes and clients are under `live`:
        `live.windows`. `:name` in the query reads `params`. A question
        that writes is refused.
        """
        return await self.run(operations.sql(query, params))

    # -- jobs ----------------------------------------------------------

    async def run_job(
        self,
        command: str,
        tags: Mapping[str, str | None] | None = None,
        directory: str | None = None,
        env: Mapping[str, str] | None = None,
        pty: bool = False,
    ) -> Job:
        """
        Start `command` through the shell as a job, and answer it at once.

        It runs in `directory`, or where this server's caller stands, with
        the caller's environment and `env` over it. A tag with the value
        `None` is a bare key. The caller's agent session stamps the job
        as the tag `session`, so `jobs({"session": ...})` finds it.
        `pty` gives it a terminal instead of pipes.
        """
        record = await self.run(operations.run_job(command, tags or {}, directory, env or {}, pty))
        return Job(self, record)

    async def jobs(self, tags: Mapping[str, str | None] | None = None) -> builtins.list[Job]:
        "Every job the server remembers, oldest first, or those carrying every tag. A bare key takes any value."
        return [Job(self, record) for record in await self.run(operations.jobs(tags))]

    async def job(self, job_id: int) -> Job | None:
        "The job with this id, or None when the server forgot it."
        record = await self.run(operations.job(job_id))
        return None if record is None else Job(self, record)

    async def kill(self) -> None:
        "End the server and everything in it."
        await self.run(operations.kill_server())
