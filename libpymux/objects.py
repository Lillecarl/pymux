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
"""

from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple, Union

from .connection import CommandResult, Connection, ServerNotRunning, socket_paths

__all__ = ["Server", "Session", "Window", "Pane"]

#: What separates two fields of one row. A format string carries it
#: between the variables, and nothing a terminal writes holds it.
_SEPARATOR = "\x1f"

_SERVER_FIELDS = ("socket_path", "pid", "version", "host", "start_time")

_SESSION_FIELDS = (
    "session_id",
    "session_name",
    "session_attached",
    "session_windows",
    "session_path",
    "session_created",
)

_WINDOW_FIELDS = (
    "window_id",
    "window_index",
    "window_name",
    "window_active",
    "window_flags",
    "window_panes",
    "window_width",
    "window_height",
)

_PANE_FIELDS = (
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
    "history_size",
    "history_limit",
)


def _format_string(fields: Sequence[str]) -> str:
    "The format string that asks for these fields, in this order."
    return _SEPARATOR.join("#{%s}" % name for name in fields)


def _rows(result: CommandResult, fields: Sequence[str]) -> List[Dict[str, str]]:
    "One dictionary for each line that the server printed."
    rows = []
    for line in result.stdout.splitlines():
        if not line:
            continue
        values = line.split(_SEPARATOR)
        # A field that the server does not know comes back empty, and a
        # short row means the server is older than this library.
        values += [""] * (len(fields) - len(values))
        rows.append(dict(zip(fields, values)))
    return rows


def _as_int(value: str, fallback: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _as_bool(value: str) -> bool:
    return value == "1"


class _Object:
    "What every object here shares: a server, and the fields it read."

    _fields: Tuple[str, ...] = ()

    def __init__(self, server: "Server", values: Dict[str, str]) -> None:
        self.server = server
        self._values = dict(values)

    def __getitem__(self, name: str) -> str:
        "One field, as the server wrote it."
        return self._values[name]

    def get(self, name: str, default: str = "") -> str:
        "One field, or `default` when the server does not know it."
        return self._values.get(name, default)

    @property
    def fields(self) -> Dict[str, str]:
        "Every field that this object read, as text."
        return dict(self._values)


class Pane(_Object):
    "One pane, and the program that runs in it."

    _fields = _PANE_FIELDS

    def __repr__(self) -> str:
        return "Pane(%r, command=%r)" % (self.id, self.current_command)

    # -- what it is ----------------------------------------------------

    @property
    def id(self) -> str:
        'The pane id, as a target: "%1001".'
        return self._values["pane_id"]

    @property
    def index(self) -> int:
        "The place of this pane in its window."
        return _as_int(self._values.get("pane_index", ""), -1)

    @property
    def window_id(self) -> str:
        'The id of the window that holds this pane: "@3".'
        return self._values.get("window_id", "")

    @property
    def active(self) -> bool:
        "True when this is the pane of its window that takes the keyboard."
        return _as_bool(self._values.get("pane_active", ""))

    @property
    def width(self) -> int:
        return _as_int(self._values.get("pane_width", ""))

    @property
    def height(self) -> int:
        return _as_int(self._values.get("pane_height", ""))

    @property
    def title(self) -> str:
        "The title that the program in the pane set."
        return self._values.get("pane_title", "")

    @property
    def pid(self) -> int:
        "The process id of the program in the pane."
        return _as_int(self._values.get("pane_pid", ""), -1)

    @property
    def current_command(self) -> str:
        return self._values.get("pane_current_command", "")

    @property
    def current_path(self) -> str:
        return self._values.get("pane_current_path", "")

    @property
    def dead(self) -> bool:
        "True when the program of the pane has ended."
        return _as_bool(self._values.get("pane_dead", ""))

    @property
    def in_mode(self) -> bool:
        """
        True when something is drawn over the pane's program.

        **Not copy mode alone.** `#{pane_in_mode}` counts every mode
        the server draws, the clock among them, so a name saying copy
        mode said the wrong thing for one of the two.
        `mode` is which one. Lillecarl/pymux#363.
        """
        return _as_bool(self._values.get("pane_in_mode", ""))

    @property
    def mode(self) -> str:
        "What is drawn over the program: `copy-mode`, `clock-mode`, or ''."
        return self._values.get("pane_mode", "")

    @property
    def window(self) -> Optional["Window"]:
        "The window that holds this pane, read again from the server."
        for window in self.server.windows:
            if window.id == self.window_id:
                return window
        return None

    # -- what it does --------------------------------------------------

    def send_keys(self, text: str, enter: bool = True, literal: bool = True) -> None:
        """
        Send text to the program in this pane.

        `literal` sends the text as it stands. Without it the server
        reads a word such as "Enter" or "C-c" as the name of a key,
        which is how you send a key that has no text.

        `enter` adds a Return after the text.
        """
        if text:
            arguments = ["send-keys", "-t", self.id]
            if literal:
                arguments.append("-l")
            arguments.append(text)
            self.server.cmd(arguments)
        if enter:
            self.server.cmd(["send-keys", "-t", self.id, "Enter"])

    def send_key(self, name: str) -> None:
        'Send one named key, such as "C-c" or "Escape".'
        self.server.cmd(["send-keys", "-t", self.id, name])

    def capture(self, start: Optional[int] = None, end: Optional[int] = None) -> str:
        """
        Read the content of this pane back as text.

        The line numbers are the ones tmux uses: 0 is the first visible
        line, and a negative number reaches into the history.
        """
        arguments = ["capture-pane", "-p", "-t", self.id]
        if start is not None:
            arguments += ["-S", str(start)]
        if end is not None:
            arguments += ["-E", str(end)]
        return self.server.cmd(arguments).stdout

    def clear_history(self) -> None:
        "Throw away the scrollback of this pane."
        self.server.cmd(["clear-history", "-t", self.id])

    def select(self) -> None:
        "Give this pane the keyboard."
        self.server.cmd(["select-pane", "-t", self.id])

    def kill(self) -> None:
        "End the program in this pane and take the pane away."
        self.server.cmd(["kill-pane", "-t", self.id])

    def rename(self, name: str) -> None:
        self.server.cmd(["rename-pane", "-t", self.id, name])

    def refresh(self) -> "Pane":
        "Read the fields of this pane again."
        for pane in self.server.panes:
            if pane.id == self.id:
                self._values = pane._values
                return self
        raise LookupError("the pane %s is gone" % (self.id,))


class Window(_Object):
    "One window, and the panes it holds."

    _fields = _WINDOW_FIELDS

    def __repr__(self) -> str:
        return "Window(%r, name=%r)" % (self.id, self.name)

    @property
    def id(self) -> str:
        'The window id, as a target: "@3".'
        return self._values["window_id"]

    @property
    def index(self) -> int:
        return _as_int(self._values.get("window_index", ""), -1)

    @property
    def name(self) -> str:
        return self._values.get("window_name", "")

    @property
    def active(self) -> bool:
        "True when this is the window that the session shows."
        return _as_bool(self._values.get("window_active", ""))

    @property
    def flags(self) -> str:
        return self._values.get("window_flags", "")

    @property
    def width(self) -> int:
        return _as_int(self._values.get("window_width", ""))

    @property
    def height(self) -> int:
        return _as_int(self._values.get("window_height", ""))

    @property
    def panes(self) -> List[Pane]:
        "Every pane of this window."
        return [pane for pane in self.server.panes if pane.window_id == self.id]

    @property
    def active_pane(self) -> Optional[Pane]:
        for pane in self.panes:
            if pane.active:
                return pane
        return None

    def split(
        self,
        command: Optional[str] = None,
        vertical: bool = True,
        start_directory: Optional[str] = None,
        select: bool = True,
    ) -> Optional[Pane]:
        """
        Split this window and return the pane that appears.

        `vertical` puts the new pane below the old one, which is what
        tmux calls a vertical split.
        """
        arguments = ["split-window", "-t", self.id]
        arguments.append("-v" if vertical else "-h")
        if start_directory is not None:
            arguments += ["-c", start_directory]
        if not select:
            arguments.append("-d")
        arguments += ["-P", "-F", _format_string(_PANE_FIELDS)]
        if command is not None:
            arguments.append(command)
        rows = _rows(self.server.cmd(arguments), _PANE_FIELDS)
        return Pane(self.server, rows[0]) if rows else None

    def select(self) -> None:
        "Show this window."
        self.server.cmd(["select-window", "-t", str(self.index)])

    def rename(self, name: str) -> None:
        self.server.cmd(["rename-window", "-t", str(self.index), name])

    def kill(self) -> None:
        "End every pane of this window and take the window away."
        self.server.cmd(["kill-window", "-t", str(self.index)])

    def refresh(self) -> "Window":
        for window in self.server.windows:
            if window.id == self.id:
                self._values = window._values
                return self
        raise LookupError("the window %s is gone" % (self.id,))


class Session(_Object):
    """
    One session of a server.

    Everything it reads names itself with `-t`, so two sessions of one
    server answer for themselves and not for whichever one a client is
    on.
    """

    _fields = _SESSION_FIELDS

    def __repr__(self) -> str:
        return "Session(%r)" % (self.name,)

    @property
    def id(self) -> str:
        'The session id, as tmux spells one: "$0".'
        return self._values.get("session_id", "$0")

    @property
    def name(self) -> str:
        return self._values.get("session_name", "")

    @property
    def attached(self) -> int:
        "How many clients are looking at this session."
        return _as_int(self._values.get("session_attached", ""))

    @property
    def path(self) -> str:
        "The directory that the server started in."
        return self._values.get("session_path", "")

    @property
    def windows(self) -> List[Window]:
        return [
            Window(self.server, values)
            for values in self.server._query(
                ["list-windows", "-t", self.id], _WINDOW_FIELDS
            )
        ]

    @property
    def active_window(self) -> Optional[Window]:
        for window in self.windows:
            if window.active:
                return window
        return None

    @property
    def panes(self) -> List[Pane]:
        return [pane for window in self.windows for pane in window.panes]

    def new_window(
        self,
        command: Optional[str] = None,
        name: Optional[str] = None,
        start_directory: Optional[str] = None,
        select: bool = True,
    ) -> Optional[Window]:
        "Make a window in this session and return it."
        arguments = ["new-window", "-t", "%s:" % (self.name,)]
        if name is not None:
            arguments += ["-n", name]
        if start_directory is not None:
            arguments += ["-c", start_directory]
        if not select:
            arguments.append("-d")
        arguments += ["-P", "-F", _format_string(_WINDOW_FIELDS)]
        if command is not None:
            arguments.append(command)
        rows = _rows(self.server.cmd(arguments), _WINDOW_FIELDS)
        return Window(self.server, rows[0]) if rows else None

    def rename(self, name: str) -> None:
        self.server.cmd(["rename-session", "-t", self.id, name])

    def kill(self) -> None:
        "End this session. The last one to go ends the server."
        self.server.cmd(["kill-session", "-t", self.id])

    def refresh(self) -> "Session":
        for session in self.server.sessions:
            if session.id == self.id:
                self._values = session._values
                return self
        raise LookupError("the session %s is gone" % (self.id,))


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
    def list(cls) -> List["Server"]:
        """
        Every pymux server of this user in the default place.

        A server started with a socket path of its own is not in here.
        Name that path to reach it.
        """
        return [cls(path) for path in socket_paths()]

    @classmethod
    def first(cls) -> "Server":
        "The one server that is running. Raises when there is not one."
        found = cls.list()
        if not found:
            raise ServerNotRunning("no pymux server is running")
        if len(found) > 1:
            raise ServerNotRunning(
                "several pymux servers are running: %s"
                % (", ".join(server.socket_path for server in found),)
            )
        return found[0]

    # -- the wire ------------------------------------------------------

    def cmd(
        self,
        command: Union[str, Sequence[str]],
        pane_id: Optional[str] = None,
        check: bool = True,
    ) -> CommandResult:
        """
        Run one pymux command and read what it says.

        This is the way out of the object model: anything the command
        line can do, this can do.
        """
        return self.connection.run(command, pane_id=pane_id, check=check)

    def is_alive(self) -> bool:
        "True when a server answers on this socket."
        return self.connection.is_alive()

    def _query(
        self, command: Sequence[str], fields: Sequence[str]
    ) -> List[Dict[str, str]]:
        arguments = list(command) + ["-F", _format_string(fields)]
        return _rows(self.cmd(arguments), fields)

    # -- what it holds -------------------------------------------------

    @property
    def info(self) -> Dict[str, str]:
        "What the server says about itself."
        rows = self._query(["list-sessions"], _SERVER_FIELDS)
        return rows[0] if rows else {}

    @property
    def sessions(self) -> List[Session]:
        "Every session of this server, oldest first."
        return [
            Session(self, values)
            for values in self._query(["list-sessions"], _SESSION_FIELDS)
        ]

    @property
    def session(self) -> Session:
        "The oldest session of this server."
        found = self.sessions
        if not found:
            raise LookupError("the server has no session")
        return found[0]

    def session_named(self, name: str) -> Optional[Session]:
        "The session with this name, or None."
        for session in self.sessions:
            if session.name == name:
                return session
        return None

    def new_session(self, name: str, attach: bool = False) -> Optional[Session]:
        "Make a session and return it."
        arguments = ["new-session", "-s", name]
        if not attach:
            arguments.append("-d")
        arguments += ["-P", "-F", _format_string(_SESSION_FIELDS)]
        rows = _rows(self.cmd(arguments), _SESSION_FIELDS)
        return Session(self, rows[0]) if rows else None

    @property
    def windows(self) -> List[Window]:
        "Every window of the session a client last looked at."
        return [
            Window(self, values)
            for values in self._query(["list-windows"], _WINDOW_FIELDS)
        ]

    @property
    def panes(self) -> List[Pane]:
        "Every pane of every window."
        return [
            Pane(self, values)
            for values in self._query(["list-panes", "-a"], _PANE_FIELDS)
        ]

    def window(self, index: int) -> Optional[Window]:
        "The window at this index, or None."
        for window in self.windows:
            if window.index == index:
                return window
        return None

    def pane(self, pane_id: str) -> Optional[Pane]:
        'The pane with this id ("%1001"), or None.'
        for pane in self.panes:
            if pane.id == pane_id:
                return pane
        return None

    def has_session(self, name: str = "") -> bool:
        "True when the server answers to this session name."
        arguments = ["has-session"]
        if name:
            arguments += ["-t", name]
        return self.cmd(arguments, check=False).ok

    def kill(self) -> None:
        "End the server and everything in it."
        self.cmd("kill-server", check=False)

    def __iter__(self) -> Iterator[Window]:
        return iter(self.windows)
