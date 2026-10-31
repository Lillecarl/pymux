"""
What a snapshot holds: its version and the steps up to it, the fields
each class writes, and the panes and terminals it carries.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ptterm import Terminal
from ptyhost.backends.posix import PosixBackend
from ptyhost.backends.posix_utils import PtyReader
from ptyhost.process import Process
from pyte.freeze import saved_fields
from pyte.images import GraphicsImage, GraphicsPlacement, GraphicsState
from pyte.keep import Keep
from pyte.osc import ColorOverrides, PointerShapes
from pyte.page import CursorPosition, Page
from pyte.screen import Screen
from pyte.streams import Stream
from pyte.titles import Titles

from ...jobs import Job
from ...options.client_options import ClientOptions
from ...options.option_fields import Declared, Options
from ...options.server_options import ServerOptions
from ...options.session_options import SessionOptions
from ...options.tables import (
    ClientOptionRow,
    create_table,
    literal,
)
from ...options.window_options import WindowOptions
from ..session import Session
from ..window_tree import Pane, Window, _Split

if TYPE_CHECKING:
    from ..main import Pymux


__all__ = [
    "CLIENT_MODES",
    "CLIENT_SETTINGS",
    "CLIENT_SETTING_TYPES",
    "OLD_CLIENT_COLUMNS",
    "SAVED",
    "SNAPSHOT_VERSION",
    "STEPS",
    "WRITES",
    "SnapshotError",
    "panes_of",
    "terminal_of",
]


#: The options of each scope a snapshot holds.
SAVED: dict[type[Options], tuple[Declared, ...]] = {
    scope: tuple(each for each in scope.DECLARED.values() if each.keep is Keep.SAVED)
    for scope in (ServerOptions, SessionOptions, WindowOptions, ClientOptions)
}


#: The shape this build writes, in `PRAGMA user_version`. A load
#: takes an older one up through `STEPS`, and refuses any other.
SNAPSHOT_VERSION = 40


#: What a client holds besides its options, each a column of `clients`
#: and an attribute of `ClientState`, with the type sqlite gives back as.
CLIENT_SETTING_TYPES: dict[str, type] = {
    "last_used": int,
    "read_only": bool,
    "ignore_size": bool,
    "message": str,
}


CLIENT_SETTINGS = tuple(CLIENT_SETTING_TYPES)


#: The columns of `clients` that 39 kept a client's options in, by the
#: option's attribute where the name differs.
OLD_CLIENT_COLUMNS = {"swap_light_and_dark_colors": "swap_dark_and_light"}


def _client_options_move_out() -> str:
    """
    39 kept a client's options as columns of `clients`, which a NULL
    left unset. 40 gives them a table of their own, and
    `swap-light-and-dark-colors` its attribute's name. Lillecarl/pymux#769.
    """
    declared = SAVED[ClientOptions]
    old = {each.attribute: OLD_CLIENT_COLUMNS.get(each.attribute, each.attribute) for each in declared}
    values = ",\n  ".join(
        "coalesce(%s, %s)" % (old[each.attribute], literal(each.stored(each.default))) for each in declared
    )
    return (
        create_table(ClientOptionRow)
        + "INSERT INTO client_options(client_id, %s) SELECT client_id,\n  %s\nFROM clients;\n"
        % (", ".join(each.attribute for each in declared), values)
        + "".join("ALTER TABLE clients DROP COLUMN %s;\n" % column for column in old.values())
    )


#: What takes a snapshot of the version before this one up to it. A
#: snapshot only ever goes from a running build to the next one, so a
#: change of shape adds the one step from the version before it and
#: deletes any older step. An added table or column needs none:
#: `missing_from` adds it.
STEPS: dict[int, str] = {39: _client_options_move_out()}


#: The fields `ClientState.modes` holds, one JSON value in `client_modes`.
CLIENT_MODES = frozenset(
    {
        "has_prefix",
        "key_tables",
        "confirmations",
        "prompt_text",
        "prompt_command",
        "prompt_buffer",
        "command_buffer",
        "display_popup",
        "menu_entries",
        "menu_title",
        "chooser_return_to",
        "chooser",
        "choose_window_index",
        "choose_window_filter",
        "choose_window_command",
    }
)


#: What each class writes into a table. `tests/test_snapshot.py` says
#: this equals the class's `Keep.SAVED` fields.
WRITES: dict[str, frozenset[str]] = {
    "pymux.core.session.Session": frozenset(Session.KEEP),
    "pymux.core.arrangement.Arrangement": frozenset(
        {
            "windows",
            "_unlinked_windows",
            "window_defaults",
            "_last_active_window",
            "_active_window_for_cli",
            "_window_history_for_cli",
        }
    ),
    "pymux.core.window_tree.Window": frozenset(Window.KEEP)
    - {"_revisions_seen", "_quiet_since", "_silence_armed", "seen_at"},
    **{
        "%s.%s" % (scope.__module__, scope.__qualname__): frozenset(each.attribute for each in saved)
        for scope, saved in SAVED.items()
    },
    "pymux.core.window_tree._Split": frozenset(_Split.KEEP),
    "pymux.core.window_tree.Pane": frozenset(name for name, fate in Pane.KEEP.items() if fate == Keep.SAVED),
    "pymux.connection.client_state.ClientState": frozenset(
        {"session", "previous_session", "options", *CLIENT_SETTINGS, *CLIENT_MODES}
    ),
    # The record goes by the client's id, in `clients`.
    "pymux.connection.server.ServerConnection": frozenset({"client_state", "created"}),
    # In `hooks`, one row per command.
    "pymux.core.hooks.Hooks": frozenset({"commands"}),
    "pymux.jobs.JobTable": frozenset({"_jobs", "last_id", "saved_queries", "forgotten_seeds"}),
    # The kept counts are the lengths of the chunks `job_output` holds.
    "pymux.jobs.Job": frozenset(name for name, fate in Job.KEEP.items() if fate == Keep.SAVED),
    # A viewer's row, in `pane_viewers`.
    "pymux.jobs.JobFeed": frozenset({"job", "sx", "sy"}),
    "ptterm.terminal.Terminal": frozenset({"terminal_control"}),
    "ptterm.terminal._TerminalControl": frozenset({"screen", "stream", "process"}),
    # What `pyte.freeze` walks writes every saved field, by construction.
    **{
        "%s.%s" % (cls.__module__, cls.__qualname__): frozenset(saved_fields(cls))
        for cls in (
            Screen,
            Page,
            CursorPosition,
            Titles,
            ColorOverrides,
            PointerShapes,
            GraphicsState,
            GraphicsImage,
            GraphicsPlacement,
            Stream,
            Process,
            PosixBackend,
            PtyReader,
        )
    },
    "pymux.core.main.Pymux": frozenset(
        {"sessions", "_session_counter", "_uses", "global_environment", "returning_clients", "jobs"}
        | {"created", "socket_name", "listener", "original_cwd", "source_file", "_startup_done"}
        | {"clipboard", "named_buffers", "prompt_history", "message_log", "hooks", "key_bindings_manager"}
        | {"notifications", "notification_center", "wait_channels"}
        | {"startup_command", "options", "session_defaults"}
    ),
}


def panes_of(pymux: Pymux) -> list[Pane]:
    "Every pane a snapshot holds, in the order the tables write them."
    panes = []
    for session in pymux.sessions:
        arrangement = session.arrangement
        for window in [*arrangement.windows, *arrangement._unlinked_windows]:
            panes.extend(window.panes)
        if session.overlay_pane is not None:
            panes.append(session.overlay_pane)
    return panes


def terminal_of(pane: Pane) -> Terminal:
    """
    The terminal of a pane the snapshot holds. A Wayland window lives on
    the server's compositor, which no handover carries, and the upgrade
    closes it before it writes. Lillecarl/pymux#620.
    """
    terminal = pane.terminal
    if not isinstance(terminal, Terminal):
        raise SnapshotError("pane %%%d shows a Wayland window, which no handover carries" % pane.pane_id)
    return terminal


class SnapshotError(Exception):
    "A snapshot this build cannot load."
