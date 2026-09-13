"""
Pymux string formatting.
"""

import datetime
import os
import re
import socket
from typing import TYPE_CHECKING, Callable, Dict, Optional

if TYPE_CHECKING:
    from pymux.arrangement import Pane, Window
    from pymux.main import Pymux
    from pymux.session import Session

__all__ = ["format_pymux_string"]


def format_pymux_string(
    pymux: "Pymux",
    string: str,
    window: Optional["Window"] = None,
    pane: Optional["Pane"] = None,
    session: Optional["Session"] = None,
) -> str:
    """
    Apply pymux sting formatting. (Similar to tmux.)
    E.g.  #P is replaced by the index of the active pane.

    We try to stay compatible with tmux, if possible. Both the classic
    `#S`-style symbols and the tmux `#{variable}` syntax are supported.

    One thing that we won't support (for now) is colors, because our styling
    works different. (With a Style class.) On the other hand, in the future, we
    could allow things like `#[token=Token.Title.PID]`. This gives a clean
    separation of semantics and colors, making it easy to write different color
    schemes.
    """
    # A caller that draws for one client passes that client's session.
    # Without it the answer is the session of whoever asks, which is
    # right for a command and wrong for a status line drawn for
    # somebody else. Lillecarl/pymux#323.
    if session is None:
        session = pymux.current_session

    arrangement = session.arrangement

    if window is None:
        window = arrangement.get_active_window()

    if pane is None:
        pane = window.active_pane

    def id_of_pane() -> str:
        return "%s" % (pane.pane_id,)

    def index_of_pane() -> str:
        try:
            return "%s" % (window.get_pane_index(pane),)
        except ValueError:
            return "/"

    def index_of_window() -> str:
        return "%s" % (window.index,)

    def name_of_window() -> str:
        return window.name or "(noname)"

    def window_flags() -> str:
        z = "Z" if window.zoom else ""

        if window == arrangement.get_active_window():
            return "*" + z
        elif window == arrangement.get_previous_active_window():
            return "-" + z
        else:
            return z + " "

    def name_of_session() -> str:
        return session.name

    def title_of_pane() -> str:
        return pane.screen.titles.window

    def hostname() -> str:
        return socket.gethostname()

    def literal() -> str:
        return "#"

    format_table = {
        "#D": id_of_pane,
        "#F": window_flags,
        "#I": index_of_window,
        "#P": index_of_pane,
        "#S": name_of_session,
        "#T": title_of_pane,
        "#W": name_of_window,
        "#h": hostname,
        "##": literal,
    }

    # Date/time formatting. The clock that test-mode pins runs here
    # as well, so a formatted status line holds still too.
    if "%" in string:
        try:
            string = pymux.displayed_now().strftime(string)
        except ValueError:  # strftime format ends with raw %
            string = "<ValueError>"

    # Apply '#' formatting.
    for symbol, f in format_table.items():
        if symbol in string:
            string = string.replace(symbol, f())

    # Apply `#{variable}` formatting. (tmux syntax.)
    if "#{" in string:

        def format_variable(match: "re.Match[str]") -> str:
            variable = match.group(1)
            handler = tmux_variables.get(variable)
            if handler is None:
                return ""
            try:
                return str(handler(pymux, window, pane, session))
            except Exception:
                return ""

        string = re.sub(r"#\{([a-zA-Z0-9_]+)\}", format_variable, string)

    return string


def _pane_pid(pymux, window, pane, session) -> str:
    "PID of the process running in the pane."
    # A backend that has no number to give says so with `None`: a
    # program at the other end of an ssh connection runs somewhere
    # else, and one that has not started has no id yet.
    pid = pane.process.backend.pid
    return str(pid) if pid else ""


def _pane_current_command(pymux, window, pane, session) -> str:
    "Name of the command running in the pane."
    name = pane.process.get_name()
    if name:
        return os.path.basename(name)
    return ""


def _pane_current_path(pymux, window, pane, session) -> str:
    "Working directory of the process in the pane."
    try:
        return pane.process.get_cwd()
    except Exception:
        return ""


def _history_size(pymux, window, pane, session) -> str:
    "Number of lines in the history."
    process = pane.process
    return str(min(pymux.history_limit, pane.screen.line_offset + process.sy))


def _pane_active(pymux, window, pane, session) -> str:
    return "1" if window.active_pane == pane else "0"


def _pane_index(pymux, window, pane, session) -> str:
    try:
        return str(window.get_pane_index(pane))
    except ValueError:
        return ""


def _window_active(pymux, window, pane, session) -> str:
    return "1" if window == session.arrangement.get_active_window() else "0"


def _window_flags(pymux, window, pane, session) -> str:
    z = "Z" if window.zoom else ""

    if window == session.arrangement.get_active_window():
        return "*" + z
    elif window == session.arrangement.get_previous_active_window():
        return "-" + z
    else:
        return z


def _window_panes(pymux, window, pane, session) -> str:
    return str(len(window.panes))


def _window_name(pymux, window, pane, session) -> str:
    return window.name or ""


def _window_index(pymux, window, pane, session) -> str:
    return str(window.index)


def _window_id(pymux, window, pane, session) -> str:
    return "@%s" % (window.window_id,)


def _pane_id(pymux, window, pane, session) -> str:
    return "%s%s" % (tmux_pane_id_prefix(), pane.pane_id)


def tmux_pane_id_prefix() -> str:
    "Paned IDs are formatted like tmux: `%<id>`."
    return "%"


def _session_id(pymux, window, pane, session) -> str:
    "Session ID, the way tmux spells one: `$<number>`."
    return "$%s" % (session.session_id,)


def _session_attached(pymux, window, pane, session) -> str:
    "Number of clients attached to this session."
    return str(
        len(
            [
                client
                for client in pymux._client_states.values()
                if not client.temporary and client.session is session
            ]
        )
    )


def _session_windows(pymux, window, pane, session) -> str:
    return str(len(session.arrangement.windows))


def _socket_path(pymux, window, pane, session) -> str:
    return pymux.socket_name or ""


def _pid(pymux, window, pane, session) -> str:
    return str(os.getpid())


def _version(pymux, window, pane, session) -> str:
    from pymux import __version__

    return __version__


def _created(pymux, window, pane, session) -> str:
    return str(int(pymux.created))


#: Mapping of tmux `#{variable}` names. Variables that pymux doesn't know
#: resolve to an empty string. (libtmux requires all fields of its format
#: template to be present, but it ignores the empty ones.)
tmux_variables: Dict[
    str, Callable[["Pymux", "Window", "Pane", "Session"], str]
] = {
    # Pane.
    "pane_id": _pane_id,
    "pane_index": _pane_index,
    "pane_active": _pane_active,
    "pane_width": lambda p, w, pane, s: str(pane.process.sx),
    "pane_height": lambda p, w, pane, s: str(pane.process.sy),
    "pane_title": lambda p, w, pane, s: pane.screen.titles.window,
    "pane_pid": _pane_pid,
    "pane_current_command": _pane_current_command,
    "pane_current_path": _pane_current_path,
    "pane_start_path": _pane_current_path,
    "pane_dead": lambda p, w, pane, s: "1" if pane.process.is_terminated else "0",
    "pane_in_mode": lambda p, w, pane, s: "1" if pane.is_copying else "0",
    "pane_synchronized": lambda p, w, pane, s: "1" if w.synchronize_panes else "0",
    "history_size": _history_size,
    "history_limit": lambda p, w, pane, s: str(p.history_limit),
    # Window.
    "window_id": _window_id,
    "window_index": _window_index,
    "window_name": _window_name,
    "window_active": _window_active,
    "window_flags": _window_flags,
    "window_panes": _window_panes,
    "window_width": lambda p, w, pane, s: str(pane.process.sx),
    "window_height": lambda p, w, pane, s: str(pane.process.sy),
    # Session.
    "session_id": _session_id,
    "session_name": lambda p, w, pane, s: s.name,
    "session_attached": _session_attached,
    "session_windows": _session_windows,
    "session_path": lambda p, w, pane, s: p.original_cwd,
    "session_created": lambda p, w, pane, s: str(int(s.created)),
    # Server.
    "socket_path": _socket_path,
    "pid": _pid,
    "version": _version,
    "start_time": _created,
    "host": lambda p, w, pane, s: socket.gethostname(),
    "hostname": lambda p, w, pane, s: socket.gethostname(),
    "history_bytes": lambda p, w, pane, s: "0",
}
