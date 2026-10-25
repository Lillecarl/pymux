"""
A pymux server, and what it holds, as objects.

The shape follows libtmux: a server holds sessions, a session holds
windows, a window holds panes. A pymux server holds as many sessions as
a person makes (Lillecarl/pymux#323), so `Server.sessions` is the list
and `Server.session` is the first of them.

Every object reads its fields from the server through a format string,
the way libtmux reads them from `tmux -F`. An object holds what it read
and does not follow the server on its own. Call `refresh()` for the
fields again, or read the collection again for the objects.

What each object asks is in `operations.py`, and `async_objects.py`
runs the same operations awaited.
"""

from __future__ import annotations

# `Server.list` hides the builtin inside that class.
import builtins
from collections.abc import Iterator, Sequence
from typing import TYPE_CHECKING

from . import operations
from .connection import CommandResult, Connection, ServerNotRunning
from .operations import Ask, Operation, Row, drive
from .records import PaneFields, SessionFields, WindowFields
from .sockets import socket_paths

if TYPE_CHECKING:
    from .streams import PaneStream

__all__ = ["Pane", "Server", "Session", "Window"]


class Pane(PaneFields):
    "One pane, and the program that runs in it."

    def __init__(self, server: Server, values: Row) -> None:
        super().__init__(values)
        self.server = server

    def current_revision(self) -> int:
        """
        The revision this pane holds now, asked of the server.

        A method and not a property, because it costs a round trip and
        nothing about the word `revision` says so. This is what a loop
        seeds itself with: `revision` alone is the snapshot, and a caller
        that kept its `Pane` would wait on a number the server left long
        ago -- every wait then answers at once and the loop spins.
        """
        return self.server.run(operations.current_revision(self.id))

    @property
    def window(self) -> Window | None:
        "The window that holds this pane, read again from the server."
        return self.server._window(self.server.run(operations.window(self.window_id)))

    def send_keys(self, text: str, enter: bool = True, literal: bool = True) -> None:
        """
        Send text to the program in this pane.

        `literal` sends the text as it stands. Without it the server
        reads a word such as "Enter" or "C-c" as the name of a key,
        which is how you send a key that has no text.

        `enter` adds a Return after the text.
        """
        self.server.run(operations.send_keys(self.id, text, enter, literal))

    def send_key(self, name: str) -> None:
        'Send one named key, such as "C-c" or "Escape".'
        self.server.run(operations.send_key(self.id, name))

    def capture(self, start: int | None = None, end: int | None = None) -> str:
        """
        Read the content of this pane back as text.

        The line numbers are the ones tmux uses: 0 is the first visible
        line, and a negative number reaches into the history.
        """
        return self.server.run(operations.capture(self.id, start, end))

    def capture_html(self, start: int | None = None, end: int | None = None) -> str:
        """
        Read the content of this pane back as HTML.

        One `pre` element, holding the colours, the renditions and the
        hyperlinks that `capture` drops. `Server.html_stylesheet` is the
        other half of it, and a caller needs that once.

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
        return self.server.run(operations.capture(self.id, start, end, html=True))

    def wait_for_change(self, since: int | None = None, timeout: float | None = None) -> int:
        """
        Hold until this pane shows something else, and answer its
        revision.

        `since` is the revision last seen. The server answers at once
        when the pane has already left it, so a caller that draws a
        frame and then waits misses nothing in between -- which is what
        makes this safe to use in a loop:

            revision = pane.current_revision()
            while True:
                draw(pane.capture_html())
                revision = pane.wait_for_change(since=revision)

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
        return self.server.run(operations.wait_for_change(self.id, since, timeout))

    def stream(self, writable: bool = False) -> PaneStream:
        """
        A stream of the rows of this pane that change.

        For a caller that draws the pane rather than reading it once:
        `capture_html` gives a whole screen each time, and this gives each
        changed row once. It is async, and used as a context manager:

            async with pane.stream() as stream:
                async for frame in stream:
                    draw(frame)

        `writable` asks for a stream that takes input as well.
        `libpymux.streams` says what a frame holds. Lillecarl/pymux#461.
        """
        from .streams import PaneStream

        return PaneStream(self.server.socket_path, self.id, writable=writable)

    def clear_history(self) -> None:
        "Throw away the scrollback of this pane."
        self.server.run(operations.clear_history(self.id))

    def select(self) -> None:
        "Give this pane the keyboard."
        self.server.run(operations.select_pane(self.id))

    def kill(self) -> None:
        "End the program in this pane and take the pane away."
        self.server.run(operations.kill_pane(self.id))

    def rename(self, name: str) -> None:
        self.server.run(operations.rename_pane(self.id, name))

    def refresh(self) -> Pane:
        "Read the fields of this pane again."
        row = self.server.run(operations.pane(self.id))
        if row is None:
            raise LookupError("the pane %s is gone" % (self.id,))
        self._values = row
        return self


class Window(WindowFields):
    "One window, and the panes it holds."

    def __init__(self, server: Server, values: Row) -> None:
        super().__init__(values)
        self.server = server

    @property
    def panes(self) -> list[Pane]:
        "Every pane of this window."
        return self.server._panes(self.server.run(operations.window_panes(self.id)))

    @property
    def active_pane(self) -> Pane | None:
        for pane in self.panes:
            if pane.active:
                return pane
        return None

    def split(
        self,
        command: str | None = None,
        vertical: bool = True,
        start_directory: str | None = None,
        select: bool = True,
    ) -> Pane | None:
        """
        Split this window and return the pane that appears.

        `vertical` puts the new pane below the old one, which is what
        tmux calls a vertical split.
        """
        row = self.server.run(operations.split_window(self.id, command, vertical, start_directory, select))
        return None if row is None else Pane(self.server, row)

    def select(self) -> None:
        "Show this window."
        self.server.run(operations.select_window(self.index))

    def rename(self, name: str) -> None:
        self.server.run(operations.rename_window(self.index, name))

    def kill(self) -> None:
        "End every pane of this window and take the window away."
        self.server.run(operations.kill_window(self.index))

    def refresh(self) -> Window:
        row = self.server.run(operations.window(self.id))
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

    @property
    def windows(self) -> list[Window]:
        return [Window(self.server, row) for row in self.server.run(operations.session_windows(self.id))]

    @property
    def active_window(self) -> Window | None:
        for window in self.windows:
            if window.active:
                return window
        return None

    @property
    def panes(self) -> list[Pane]:
        return self.server._panes(self.server.run(operations.session_panes(self.id)))

    def new_window(
        self,
        command: str | None = None,
        name: str | None = None,
        start_directory: str | None = None,
        select: bool = True,
    ) -> Window | None:
        "Make a window in this session and return it."
        return self.server._window(
            self.server.run(operations.new_window(self.name, command, name, start_directory, select))
        )

    def rename(self, name: str) -> None:
        self.server.run(operations.rename_session(self.id, name))

    def kill(self) -> None:
        "End this session. The last one to go ends the server."
        self.server.run(operations.kill_session(self.id))

    def refresh(self) -> Session:
        row = self.server.run(operations.session(self.id))
        if row is None:
            raise LookupError("the session %s is gone" % (self.id,))
        self._values = row
        return self


class Server:
    """
    A pymux server, addressed by the path of its socket.

    Nothing here holds a connection open. Every read and every command
    opens a socket, sends one message and reads the answer, which is
    what the server expects.
    """

    def __init__(self, socket_path: str) -> None:
        self.connection = Connection(socket_path)

    def __repr__(self) -> str:
        return "Server(%r)" % (self.socket_path,)

    @property
    def socket_path(self) -> str:
        return self.connection.socket_path

    @classmethod
    def list(cls) -> list[Server]:
        """
        Every pymux server of this user in the default place.

        A server started with a socket path of its own is not in here.
        Name that path to reach it.
        """
        return [cls(path) for path in socket_paths()]

    @classmethod
    def first(cls) -> Server:
        "The one server that is running. Raises when there is not one."
        return cls(_the_one(socket_paths()))

    # -- the wire ------------------------------------------------------

    def cmd(
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
        return self.connection.run(command, pane_id=pane_id, check=check)

    def run[T](self, operation: Operation[T]) -> T:
        "Run one operation of `operations.py`."
        return drive(operation, self._ask)

    def _ask(self, ask: Ask) -> CommandResult:
        return self.cmd(ask.arguments, check=ask.check)

    def _window(self, row: Row | None) -> Window | None:
        return None if row is None else Window(self, row)

    def _panes(self, found: builtins.list[Row]) -> builtins.list[Pane]:
        return [Pane(self, row) for row in found]

    def is_alive(self) -> bool:
        "True when a server answers on this socket."
        return self.connection.is_alive()

    def html_stylesheet(self, pane: Pane | None = None) -> str:
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
        return self.run(operations.html_stylesheet(None if pane is None else pane.id))

    # -- what it holds -------------------------------------------------

    @property
    def info(self) -> Row:
        "What the server says about itself."
        return self.run(operations.server_info())

    @property
    def sessions(self) -> builtins.list[Session]:
        "Every session of this server, oldest first."
        return [Session(self, row) for row in self.run(operations.sessions())]

    @property
    def session(self) -> Session:
        "The oldest session of this server."
        found = self.sessions
        if not found:
            raise LookupError("the server has no session")
        return found[0]

    def session_named(self, name: str) -> Session | None:
        "The session with this name, or None."
        row = self.run(operations.session_named(name))
        return None if row is None else Session(self, row)

    def new_session(self, name: str, attach: bool = False) -> Session | None:
        "Make a session and return it."
        row = self.run(operations.new_session(name, attach))
        return None if row is None else Session(self, row)

    @property
    def windows(self) -> builtins.list[Window]:
        "Every window of the session a client last looked at."
        return [Window(self, row) for row in self.run(operations.windows())]

    @property
    def panes(self) -> builtins.list[Pane]:
        "Every pane of every window."
        return self._panes(self.run(operations.panes()))

    def window(self, index: int) -> Window | None:
        "The window at this index, or None."
        return self._window(self.run(operations.window_at(index)))

    def pane(self, pane_id: str) -> Pane | None:
        'The pane with this id ("%1001"), or None.'
        row = self.run(operations.pane(pane_id))
        return None if row is None else Pane(self, row)

    def has_session(self, name: str = "") -> bool:
        "True when the server answers to this session name."
        return self.run(operations.has_session(name))

    def kill(self) -> None:
        "End the server and everything in it."
        self.run(operations.kill_server())

    def __iter__(self) -> Iterator[Window]:
        return iter(self.windows)


def _the_one(paths: list[str]) -> str:
    "The one socket path in `paths`, or `ServerNotRunning` saying why not."
    if not paths:
        raise ServerNotRunning("no pymux server is running")
    if len(paths) > 1:
        raise ServerNotRunning("several pymux servers are running: %s" % (", ".join(paths),))
    return paths[0]
