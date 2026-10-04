import argparse
import json
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.arrangement import Pane, Window
    from pymux.main import Pymux
    from pymux.session import Session


from pymux.commands import CommandParser, add_command
from pymux.commands.common import answer


def _pane_tree(pane: "Pane", index: int):
    """
    Everything an agent needs to aim at a pane, in JSON types.

    The directory is what the shell reported, not what the process
    table says; the command is the reverse, the basename of what
    runs. Sizes are cells, the way `list-windows` prints them.
    """
    try:
        command = pane.process.get_name()
        command = os.path.basename(command) if command else ""
    except Exception:
        command = ""
    try:
        width = pane.process.sx
        height = pane.process.sy
    except Exception:
        width = 0
        height = 0
    return {
        "pane_id": pane.pane_id,
        "index": index,
        "name": pane.name,
        "command": command or "",
        "width": width,
        "height": height,
        "current_directory": pane.current_directory,
        "current_host": pane.current_host,
        "user_vars": dict(pane.user_vars),
        "last_exit_status": pane.last_exit_status,
    }


def _window_tree(pymux: "Pymux", session: "Session", window: "Window"):
    """A window with its panes, marking the one in focus."""
    active = window.active_pane
    return {
        "window_id": window.window_id,
        "index": window.index,
        "name": window.name,
        "active": window == session.arrangement.get_active_window(),
        "panes": [
            _pane_tree(pane, index)
            | {"active": pane is active}
            for index, pane in enumerate(window.panes)
        ],
    }


def _caller_tree(pymux: "Pymux"):
    """
    The window and pane the command arrived from, or None.

    A command typed in a pane names it in the run-command packet;
    anything else -- a real client, a harness outside any pane, a
    pane that died since -- has no caller to name.
    """
    try:
        asking = pymux.get_client_state()
    except ValueError:
        return None
    if asking is None or not asking.temporary or asking.caller_pane_id is None:
        return None
    found = pymux.window_of_pane(asking.caller_pane_id)
    if found is None:
        return None
    session, window, pane = found
    return {
        "session_id": session.session_id,
        "session_name": session.name,
        "window_id": window.window_id,
        "window_index": window.index,
        "pane_id": pane.pane_id,
        "pane_index": window.get_pane_index(pane),
    }


def _clients_tree(pymux: "Pymux"):
    """
    What every attached client looks at.

    `pymux.clients` leaves the temporary clients out: nobody sits at
    one of those, so there is no focus to report.
    """
    clients = []
    for state in pymux.clients:
        pane = pymux.focused_pane_of(state)
        found = (
            pymux.window_of_pane(pane.pane_id) if pane is not None else None
        )
        clients.append(
            {
                "session_id": state.session.session_id,
                "session_name": state.session.name,
                "window_id": found[1].window_id if found is not None else None,
                "pane_id": pane.pane_id if pane is not None else None,
            }
        )
    return clients


def info(pymux: "Pymux", args: argparse.Namespace) -> None:
    """
    The whole server as JSON, for an agent to orient itself.

    `caller` is the window and pane the command arrived from;
    `clients` is what every attached person looks at; `sessions`
    holds every window and pane, with the directory each reported,
    the variables each published and the command each runs. An agent
    that wants to act aims `-t` at the ids it reads here.
    """
    tree = {
        "caller": _caller_tree(pymux),
        "clients": _clients_tree(pymux),
        "sessions": [
            {
                "session_id": session.session_id,
                "session_name": session.name,
                "windows": [
                    _window_tree(pymux, session, window)
                    for window in session.arrangement.windows
                ],
            }
            for session in pymux.sessions
        ],
    }
    answer(pymux, json.dumps(tree))


def register(subparsers: "argparse._SubParsersAction[CommandParser]"):
    add_command(subparsers, info, read_only=True)
