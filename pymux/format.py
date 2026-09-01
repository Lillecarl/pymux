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

__all__ = ["format_pymux_string"]


def format_pymux_string(
    pymux: "Pymux",
    string: str,
    window: Optional["Window"] = None,
    pane: Optional["Pane"] = None,
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
    arrangement = pymux.arrangement

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
        return pymux.session_name

    def title_of_pane() -> str:
        return pane.process.screen.title

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

    # Date/time formatting.
    if "%" in string:
        try:
            string = datetime.datetime.now().strftime(string)
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
                return str(handler(pymux, window, pane))
            except Exception:
                return ""

        string = re.sub(r"#\{([a-zA-Z0-9_]+)\}", format_variable, string)

    return string


def _pane_pid(pymux, window, pane) -> str:
    "PID of the process running in the pane."
    backend = getattr(pane.process, "backend", None)
    pid = getattr(backend, "pid", None)
    return str(pid) if pid else ""


def _pane_current_command(pymux, window, pane) -> str:
    "Name of the command running in the pane."
    name = pane.process.get_name()
    if name:
        return os.path.basename(name)
    return ""


def _pane_current_path(pymux, window, pane) -> str:
    "Working directory of the process in the pane."
    try:
        return pane.process.get_cwd()
    except Exception:
        return ""


def _history_size(pymux, window, pane) -> str:
    "Number of lines in the history."
    process = pane.process
    return str(
        min(pymux.history_limit, process.screen.line_offset + process.sy)
    )


def _pane_active(pymux, window, pane) -> str:
    return "1" if window.active_pane == pane else "0"


def _pane_index(pymux, window, pane) -> str:
    try:
        return str(window.get_pane_index(pane))
    except ValueError:
        return ""


def _window_active(pymux, window, pane) -> str:
    return "1" if window == pymux.arrangement.get_active_window() else "0"


def _window_flags(pymux, window, pane) -> str:
    z = "Z" if window.zoom else ""

    if window == pymux.arrangement.get_active_window():
        return "*" + z
    elif window == pymux.arrangement.get_previous_active_window():
        return "-" + z
    else:
        return z


def _window_panes(pymux, window, pane) -> str:
    return str(len(window.panes))


def _window_name(pymux, window, pane) -> str:
    return window.name or ""


def _window_index(pymux, window, pane) -> str:
    return str(window.index)


def _window_id(pymux, window, pane) -> str:
    return "@%s" % (window.window_id,)


def _pane_id(pymux, window, pane) -> str:
    return "%s%s" % (tmux_pane_id_prefix(), pane.pane_id)


def tmux_pane_id_prefix() -> str:
    "Paned IDs are formatted like tmux: `%<id>`."
    return "%"


def _session_id(pymux, window, pane) -> str:
    "Session ID. (One session per server: always `$0`.)"
    return "$0"


def _session_attached(pymux, window, pane) -> str:
    "Number of clients attached to this session."
    return str(len(pymux._client_states))


def _session_windows(pymux, window, pane) -> str:
    return str(len(pymux.arrangement.windows))


def _socket_path(pymux, window, pane) -> str:
    return pymux.socket_name or ""


def _pid(pymux, window, pane) -> str:
    return str(os.getpid())


def _version(pymux, window, pane) -> str:
    from pymux import __version__

    return __version__


def _created(pymux, window, pane) -> str:
    return str(int(pymux.created))


#: Mapping of tmux `#{variable}` names. Variables that pymux doesn't know
#: resolve to an empty string. (libtmux requires all fields of its format
#: template to be present, but it ignores the empty ones.)
tmux_variables: Dict[str, Callable[["Pymux", "Window", "Pane"], str]] = {
    # Pane.
    "pane_id": _pane_id,
    "pane_index": _pane_index,
    "pane_active": _pane_active,
    "pane_width": lambda p, w, pane: str(pane.process.sx),
    "pane_height": lambda p, w, pane: str(pane.process.sy),
    "pane_title": lambda p, w, pane: pane.process.screen.title,
    "pane_pid": _pane_pid,
    "pane_current_command": _pane_current_command,
    "pane_current_path": _pane_current_path,
    "pane_start_path": _pane_current_path,
    "pane_dead": lambda p, w, pane: "1" if pane.process.is_terminated else "0",
    "pane_in_mode": lambda p, w, pane: "1" if pane.display_scroll_buffer else "0",
    "pane_synchronized": lambda p, w: "1" if w.synchronize_panes else "0",
    "history_size": _history_size,
    "history_limit": lambda p, w, pane: str(p.history_limit),
    # Window.
    "window_id": _window_id,
    "window_index": _window_index,
    "window_name": _window_name,
    "window_active": _window_active,
    "window_flags": _window_flags,
    "window_panes": _window_panes,
    "window_width": lambda p, w, pane: str(pane.process.sx),
    "window_height": lambda p, w, pane: str(pane.process.sy),
    # Session.
    "session_id": _session_id,
    "session_name": lambda p, w, pane: p.session_name,
    "session_attached": _session_attached,
    "session_windows": _session_windows,
    "session_path": lambda p, w, pane: p.original_cwd,
    "session_created": _created,
    # Server.
    "socket_path": _socket_path,
    "pid": _pid,
    "version": _version,
    "start_time": _created,
    "host": lambda p, w, pane: socket.gethostname(),
    "hostname": lambda p, w, pane: socket.gethostname(),
    "history_bytes": lambda p, w, pane: "0",
}
