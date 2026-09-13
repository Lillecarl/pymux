"""
Pymux string formatting.

A format string names facts: about the server, and about what the
caller is asking about. `FormatContext` is that question -- the server,
the session, the window, the pane, and the client this is drawn for --
and every variable reads one.

**One object, not four arguments.** A variable that reads a context can
read a fact nobody thought of when it was written: the client arrived
that way, and `#{client_hostname}` is the first variable that needed
something no session, window or pane knows. A renderer can take a
context too, which is what Lillecarl/pymux#333 is for.
Lillecarl/pymux#330.
"""

import os
import re
import socket
from typing import TYPE_CHECKING, Callable, Dict, NamedTuple, Optional

if TYPE_CHECKING:
    from pymux.arrangement import Pane, Window
    from pymux.main import ClientState, Pymux
    from pymux.session import Session

__all__ = ["FormatContext", "format_pymux_string"]


class FormatContext(NamedTuple):
    """
    What a format variable may read.

    `client` is the only one that can be missing. A command formats
    without one -- `display-message` typed in a pane has no terminal of
    its own -- and tmux answers a `client_` variable with nothing in
    the same case (`ft->c == NULL` in its `format.c`).
    """

    pymux: "Pymux"
    session: "Session"
    window: "Window"
    pane: "Pane"
    client: Optional["ClientState"] = None


def _hostname() -> str:
    "The machine this server runs on. (`#H`, `#{host}`.)"
    return socket.gethostname()


def _hostname_short() -> str:
    """
    The same name without its domain. (`#h`, `#{host_short}`.)

    tmux cuts at the first dot, and spells it this way round: `#H` is
    the whole name and `#h` is the short one. `format_cb_host_short` in
    tmux's `format.c`. Lillecarl/pymux#331.
    """
    return _hostname().split(".")[0]


def format_pymux_string(
    pymux: "Pymux",
    string: str,
    window: Optional["Window"] = None,
    pane: Optional["Pane"] = None,
    session: Optional["Session"] = None,
    client: Optional["ClientState"] = None,
) -> str:
    """
    Apply pymux string formatting. (Similar to tmux.)
    E.g.  #P is replaced by the index of the active pane.

    We try to stay compatible with tmux, if possible. Both the classic
    `#S`-style symbols and the tmux `#{variable}` syntax are supported.

    What is left out of the caller is read from the server: the session
    of whoever asks, its active window, that window's active pane.

    **A caller that draws for one client passes that client.** Its
    session is what the frame shows, which is right for a status line
    and wrong for a command; and a `client_` variable has nothing else
    to read. Lillecarl/pymux#323, Lillecarl/pymux#330.
    """
    if session is None:
        session = client.session if client is not None else pymux.current_session

    arrangement = session.arrangement

    if window is None:
        window = arrangement.get_active_window()

    if pane is None:
        pane = window.active_pane

    return format_in_context(
        FormatContext(pymux, session, window, pane, client), string
    )


def format_in_context(context: FormatContext, string: str) -> str:
    "Apply the formatting to a question that is already complete."
    # Date/time formatting. The clock that test-mode pins runs here
    # as well, so a formatted status line holds still too.
    if "%" in string:
        try:
            string = context.pymux.displayed_now().strftime(string)
        except ValueError:  # strftime format ends with raw %
            string = "<ValueError>"

    # Apply '#' formatting.
    for symbol, handler in symbol_variables.items():
        if symbol in string:
            string = string.replace(symbol, handler(context))

    # Apply `#{variable}` formatting. (tmux syntax.)
    if "#{" in string:

        def format_variable(match: "re.Match[str]") -> str:
            variable = match.group(1)
            handler = tmux_variables.get(variable)
            if handler is None:
                return ""
            try:
                return str(handler(context))
            except Exception:
                return ""

        string = re.sub(r"#\{([a-zA-Z0-9_]+)\}", format_variable, string)

    return string


# ---------------------------------------------------------------------
# The `#X` symbols.
#
# Three of them do not answer what the `#{name}` of the same fact
# answers, and the difference is on purpose. Each one is a status line
# that must not jump or go blank, and each is noted where it is.
# ---------------------------------------------------------------------


def _symbol_pane_index(context: FormatContext) -> str:
    try:
        return "%s" % (context.window.get_pane_index(context.pane),)
    except ValueError:
        # A pane that is not in this window. "/" holds the column that
        # a number held; `#{pane_index}` answers nothing instead.
        return "/"


def _symbol_window_name(context: FormatContext) -> str:
    # A window with no name keeps its place in the status line.
    return context.window.name or "(noname)"


def _symbol_window_flags(context: FormatContext) -> str:
    arrangement = context.session.arrangement
    window = context.window
    z = "Z" if window.zoom else ""

    if window == arrangement.get_active_window():
        return "*" + z
    elif window == arrangement.get_previous_active_window():
        return "-" + z
    else:
        # One column wide either way, so the windows beside this one do
        # not move when it becomes the current one. `#{window_flags}`
        # answers the flags alone.
        return z + " "


def _symbol_pane_id(context: FormatContext) -> str:
    return "%s" % (context.pane.pane_id,)


def _symbol_window_index(context: FormatContext) -> str:
    return "%s" % (context.window.index,)


def _symbol_session_name(context: FormatContext) -> str:
    return context.session.name


def _symbol_pane_title(context: FormatContext) -> str:
    return context.pane.screen.titles.window


#: The `#X` symbols, in the order they are applied. `##` is last, so
#: that the symbols above it are read first.
symbol_variables: Dict[str, Callable[[FormatContext], str]] = {
    "#D": _symbol_pane_id,
    "#F": _symbol_window_flags,
    "#H": lambda context: _hostname(),
    "#I": _symbol_window_index,
    "#P": _symbol_pane_index,
    "#S": _symbol_session_name,
    "#T": _symbol_pane_title,
    "#W": _symbol_window_name,
    "#h": lambda context: _hostname_short(),
    "##": lambda context: "#",
}


# ---------------------------------------------------------------------
# The `#{name}` variables.
# ---------------------------------------------------------------------


def _pane_pid(context: FormatContext) -> str:
    "PID of the process running in the pane."
    # A backend that has no number to give says so with `None`: a
    # program at the other end of an ssh connection runs somewhere
    # else, and one that has not started has no id yet.
    pid = context.pane.process.backend.pid
    return str(pid) if pid else ""


def _pane_current_command(context: FormatContext) -> str:
    "Name of the command running in the pane."
    name = context.pane.process.get_name()
    if name:
        return os.path.basename(name)
    return ""


def _pane_current_path(context: FormatContext) -> str:
    "Working directory of the process in the pane."
    try:
        return context.pane.process.get_cwd()
    except Exception:
        return ""


def _history_size(context: FormatContext) -> str:
    "Number of lines in the history."
    pane = context.pane
    return str(
        min(context.pymux.history_limit, pane.screen.line_offset + pane.process.sy)
    )


def _pane_active(context: FormatContext) -> str:
    return "1" if context.window.active_pane == context.pane else "0"


def _pane_index(context: FormatContext) -> str:
    try:
        return str(context.window.get_pane_index(context.pane))
    except ValueError:
        return ""


def _window_active(context: FormatContext) -> str:
    return (
        "1"
        if context.window == context.session.arrangement.get_active_window()
        else "0"
    )


def _window_flags(context: FormatContext) -> str:
    arrangement = context.session.arrangement
    window = context.window
    z = "Z" if window.zoom else ""

    if window == arrangement.get_active_window():
        return "*" + z
    elif window == arrangement.get_previous_active_window():
        return "-" + z
    else:
        return z


def _window_panes(context: FormatContext) -> str:
    return str(len(context.window.panes))


def _window_name(context: FormatContext) -> str:
    return context.window.name or ""


def _window_index(context: FormatContext) -> str:
    return str(context.window.index)


def _window_id(context: FormatContext) -> str:
    return "@%s" % (context.window.window_id,)


def _pane_id(context: FormatContext) -> str:
    return "%s%s" % (tmux_pane_id_prefix(), context.pane.pane_id)


def tmux_pane_id_prefix() -> str:
    "Paned IDs are formatted like tmux: `%<id>`."
    return "%"


def _session_id(context: FormatContext) -> str:
    "Session ID, the way tmux spells one: `$<number>`."
    return "$%s" % (context.session.session_id,)


def _session_attached(context: FormatContext) -> str:
    "Number of clients attached to this session."
    return str(
        len(
            [
                client
                for client in context.pymux._client_states.values()
                if not client.temporary and client.session is context.session
            ]
        )
    )


def _session_windows(context: FormatContext) -> str:
    return str(len(context.session.arrangement.windows))


def _socket_path(context: FormatContext) -> str:
    return context.pymux.socket_name or ""


def _pid(context: FormatContext) -> str:
    return str(os.getpid())


def _version(context: FormatContext) -> str:
    from pymux import __version__

    return __version__


def _created(context: FormatContext) -> str:
    return str(int(context.pymux.created))


def _client_hostname(context: FormatContext) -> str:
    """
    The machine the client this is drawn for runs on.

    **tmux has no such variable**, because every tmux client is on the
    server's machine. pymux reaches a server over ssh, so `#{host}` is
    the server's answer and this is the other one. The client reports
    it when it attaches. Lillecarl/pymux#287, Lillecarl/pymux#330.
    """
    client = context.client
    if client is None:
        return ""
    return getattr(client.connection, "hostname", "") or ""


#: Mapping of tmux `#{variable}` names. Variables that pymux doesn't know
#: resolve to an empty string. (libtmux requires all fields of its format
#: template to be present, but it ignores the empty ones.)
tmux_variables: Dict[str, Callable[[FormatContext], str]] = {
    # Pane.
    "pane_id": _pane_id,
    "pane_index": _pane_index,
    "pane_active": _pane_active,
    "pane_width": lambda c: str(c.pane.process.sx),
    "pane_height": lambda c: str(c.pane.process.sy),
    "pane_title": lambda c: c.pane.screen.titles.window,
    "pane_pid": _pane_pid,
    "pane_current_command": _pane_current_command,
    "pane_current_path": _pane_current_path,
    "pane_start_path": _pane_current_path,
    "pane_dead": lambda c: "1" if c.pane.process.is_terminated else "0",
    "pane_in_mode": lambda c: "1" if c.pane.is_copying else "0",
    "pane_synchronized": lambda c: "1" if c.window.synchronize_panes else "0",
    "history_size": _history_size,
    "history_limit": lambda c: str(c.pymux.history_limit),
    # Window.
    "window_id": _window_id,
    "window_index": _window_index,
    "window_name": _window_name,
    "window_active": _window_active,
    "window_flags": _window_flags,
    "window_panes": _window_panes,
    "window_width": lambda c: str(c.pane.process.sx),
    "window_height": lambda c: str(c.pane.process.sy),
    # Session.
    "session_id": _session_id,
    "session_name": lambda c: c.session.name,
    "session_attached": _session_attached,
    "session_windows": _session_windows,
    "session_path": lambda c: c.pymux.original_cwd,
    "session_created": lambda c: str(int(c.session.created)),
    # Client.
    "client_hostname": _client_hostname,
    # Server.
    "socket_path": _socket_path,
    "pid": _pid,
    "version": _version,
    "start_time": _created,
    "host": lambda c: _hostname(),
    "hostname": lambda c: _hostname(),
    "host_short": lambda c: _hostname_short(),
    "history_bytes": lambda c: "0",
}
