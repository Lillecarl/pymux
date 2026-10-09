"""
A server's window management, in an sqlite file a new build can load.

A hot upgrade writes this, and the new build reads it back
(Lillecarl/pymux#399). The tables are meant to become the live store
later, so they are shaped for a person or an agent to query: one row
per session, window, split and pane, and a column named after the
`#{format}` variable where one exists.

**It runs synchronously, on purpose.** The pause of an upgrade is the
event loop standing still. A writer that awaited anything would let a
pane read between two tables, and the file would describe two moments.

What each class writes is declared in `WRITES`, and what it does not
write yet in `LATER`. `tests/test_snapshot.py` holds the two against
every `Keep.SAVED` in the classes' `KEEP`, so a saved field that no
table holds fails a test instead of vanishing in an upgrade.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Callable
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any
from weakref import ref

from prompt_toolkit.data_structures import Size

from .arrangement import HSplit, LayoutTypes, Pane, VSplit, Window, _Split, node_key
from .ids import PaneId, SessionId, WindowId, WindowIndex
from .options import ALL_OPTIONS
from .session import Session

if TYPE_CHECKING:
    from ptterm import Terminal

    from .main import ClientState, Pymux

__all__ = ["SNAPSHOT_VERSION", "load", "save"]

#: The shape this build writes, in `PRAGMA user_version`. A load
#: refuses any other. The first change of shape adds the one step up
#: from the version before, and only that step.
SNAPSHOT_VERSION = 1

SCHEMA = """
CREATE TABLE counters(
  name TEXT PRIMARY KEY,
  value INTEGER NOT NULL
);
CREATE TABLE options(
  name TEXT PRIMARY KEY,
  value
);
CREATE TABLE global_environment(
  name TEXT PRIMARY KEY,
  value TEXT
);
CREATE TABLE sessions(
  session_id INTEGER PRIMARY KEY,
  session_name TEXT NOT NULL,
  position INTEGER NOT NULL,
  session_created REAL NOT NULL,
  last_used INTEGER NOT NULL,
  default_height INTEGER NOT NULL,
  default_width INTEGER NOT NULL,
  base_index INTEGER NOT NULL,
  renumber_windows INTEGER NOT NULL,
  last_active_window_id INTEGER,
  overlay_pane_id INTEGER,
  overlay_title TEXT NOT NULL,
  overlay_width TEXT,
  overlay_height TEXT
);
CREATE TABLE session_environment(
  session_id INTEGER NOT NULL REFERENCES sessions(session_id),
  name TEXT NOT NULL,
  value TEXT,
  PRIMARY KEY (session_id, name)
);
CREATE TABLE window_defaults(
  session_id INTEGER NOT NULL REFERENCES sessions(session_id),
  name TEXT NOT NULL,
  value,
  PRIMARY KEY (session_id, name)
);
CREATE TABLE windows(
  window_id INTEGER PRIMARY KEY,
  session_id INTEGER NOT NULL REFERENCES sessions(session_id),
  linked INTEGER NOT NULL,
  position INTEGER NOT NULL,
  window_index INTEGER NOT NULL,
  window_name TEXT,
  root_split_id INTEGER NOT NULL,
  active_pane_id INTEGER,
  previous_active_pane_id INTEGER,
  previous_layout TEXT,
  strip INTEGER NOT NULL,
  window_size TEXT NOT NULL,
  manual_height INTEGER,
  manual_width INTEGER,
  zoom INTEGER NOT NULL,
  synchronize_panes INTEGER NOT NULL,
  frame_rate INTEGER NOT NULL
);
CREATE TABLE splits(
  split_id INTEGER PRIMARY KEY,
  window_id INTEGER NOT NULL REFERENCES windows(window_id),
  kind TEXT NOT NULL CHECK (kind IN ('hsplit', 'vsplit')),
  parent_split_id INTEGER REFERENCES splits(split_id),
  position INTEGER
);
CREATE TABLE panes(
  pane_id INTEGER PRIMARY KEY,
  window_id INTEGER REFERENCES windows(window_id),
  parent_split_id INTEGER REFERENCES splits(split_id),
  position INTEGER,
  pane_name TEXT,
  clock_mode INTEGER NOT NULL,
  pane_current_path TEXT,
  current_host TEXT,
  command_zone TEXT,
  last_exit_status INTEGER,
  pane_revision INTEGER NOT NULL
);
CREATE TABLE pane_marks(
  pane_id INTEGER NOT NULL REFERENCES panes(pane_id),
  position INTEGER NOT NULL,
  row INTEGER NOT NULL,
  PRIMARY KEY (pane_id, position)
);
CREATE TABLE pane_user_vars(
  pane_id INTEGER NOT NULL REFERENCES panes(pane_id),
  name TEXT NOT NULL,
  value TEXT NOT NULL,
  PRIMARY KEY (pane_id, name)
);
CREATE TABLE split_weights(
  split_id INTEGER NOT NULL REFERENCES splits(split_id),
  kind TEXT NOT NULL CHECK (kind IN ('pane', 'split')),
  id INTEGER NOT NULL,
  weight INTEGER NOT NULL,
  PRIMARY KEY (split_id, kind, id)
);
CREATE TABLE column_widths(
  window_id INTEGER NOT NULL REFERENCES windows(window_id),
  kind TEXT NOT NULL CHECK (kind IN ('pane', 'split')),
  id INTEGER NOT NULL,
  width REAL NOT NULL,
  PRIMARY KEY (window_id, kind, id)
);
CREATE TABLE clients(
  client_id TEXT PRIMARY KEY,
  session_id INTEGER REFERENCES sessions(session_id),
  previous_session_id INTEGER REFERENCES sessions(session_id)
);
CREATE TABLE client_windows(
  client_id TEXT NOT NULL REFERENCES clients(client_id),
  session_id INTEGER NOT NULL REFERENCES sessions(session_id),
  active_window_id INTEGER REFERENCES windows(window_id),
  previous_window_id INTEGER REFERENCES windows(window_id),
  PRIMARY KEY (client_id, session_id)
);
"""

#: The tables in the order a dump compares them.
TABLES = (
    "counters",
    "options",
    "global_environment",
    "sessions",
    "session_environment",
    "window_defaults",
    "windows",
    "splits",
    "panes",
    "pane_marks",
    "pane_user_vars",
    "split_weights",
    "column_widths",
    "clients",
    "client_windows",
)

_SPLIT_KINDS: dict[type[_Split], str] = {HSplit: "hsplit", VSplit: "vsplit"}


def _server_options() -> dict[str, str]:
    "Option name to `Pymux` attribute, for every option the server holds."
    from .main import Pymux

    return {
        name: option.attribute_name
        for name, option in ALL_OPTIONS.items()
        if option.attribute_name is not None and Pymux.KEEP.get(option.attribute_name) is not None
    }


def _window_option_names() -> dict[str, str]:
    "Window attribute to option name, for `window_defaults`."
    from .options import ALL_WINDOW_OPTIONS

    return {option.attribute_name: name for name, option in ALL_WINDOW_OPTIONS.items() if option.attribute_name}


#: What each class writes into a table. `tests/test_snapshot.py` says
#: this equals the class's `Keep.SAVED` fields, less `LATER`.
WRITES: dict[str, frozenset[str]] = {
    "pymux.session.Session": frozenset(Session.KEEP),
    "pymux.arrangement.Arrangement": frozenset(
        {
            "windows",
            "_unlinked_windows",
            "base_index",
            "renumber_windows",
            "window_defaults",
            "_last_active_window",
            "_active_window_for_cli",
            "_prev_active_window_for_cli",
        }
    ),
    "pymux.arrangement.Window": frozenset(Window.KEEP),
    "pymux.arrangement._Split": frozenset(_Split.KEEP),
    "pymux.arrangement.Pane": frozenset(Pane.KEEP) - {"terminal"},
    "pymux.main.ClientState": frozenset({"session", "previous_session"}),
    "pymux.main.Pymux": frozenset(
        {"sessions", "_session_counter", "_uses", "global_environment", "returning_clients"}
        | set(_server_options().values())
    ),
}

#: Saved fields no table holds yet. Each is one step of
#: Lillecarl/pymux#399 still to come.
LATER: dict[str, frozenset[str]] = {
    # The screen and the process: the next step.
    "pymux.arrangement.Pane": frozenset({"terminal"}),
    # What a person was in the middle of, and how the client is set.
    "pymux.main.ClientState": frozenset(
        {
            "name",
            "last_used",
            "read_only",
            "ignore_size",
            "full_screen",
            "theme",
            "swap_dark_and_light",
            "has_prefix",
            "key_tables",
            "message",
            "confirmations",
            "prompt_text",
            "prompt_command",
            "prompt_buffer",
            "command_buffer",
            "display_popup",
            "menu_entries",
            "menu_title",
            "chooser_return_to",
            "choose_window",
            "choose_window_index",
            "choose_window_filter",
            "choose_window_command",
            "choose_buffer",
            "choose_job",
            "choose_notifications",
            "choose_options",
        }
    ),
    # The server's own records, and what the next process is started with.
    "pymux.main.Pymux": frozenset(
        {
            "created",
            "socket_name",
            "listener",
            "original_cwd",
            "source_file",
            "startup_command",
            "_startup_done",
            "startup_errors",
            "_runs_standalone",
            "_serves_one_terminal",
            "clipboard",
            "named_buffers",
            "prompt_history",
            "hooks",
            "wait_channels",
            "message_log",
            "notifications",
            "notification_center",
            "jobs",
            "key_bindings_manager",
        }
    ),
}


def _value(value: Any) -> Any:
    "A value as sqlite holds it. An enum is its value; anything odd raises."
    if isinstance(value, Enum):
        return value.value
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    raise TypeError("a snapshot cannot hold %r" % (value,))


def _like(default: Any, value: Any) -> Any:
    "A value read back, in the type of the fresh object's own default."
    if value is None:
        return None
    if isinstance(default, bool):
        return bool(value)
    if isinstance(default, Enum):
        return type(default)(value)
    return value


def _optional_id(item) -> int | None:
    return None if item is None else int(node_key(item)[1])


def save(pymux: Pymux, path: str | os.PathLike[str], pending: dict[str, dict] | None = None) -> None:
    """
    Write the window management of `pymux` to `path`.

    The file appears whole or not at all: it is written beside `path`,
    synced, and renamed over it.
    """
    path = Path(path)
    partial = path.with_name(path.name + ".partial")
    partial.unlink(missing_ok=True)
    db = sqlite3.connect(partial)
    try:
        with db:
            db.executescript(SCHEMA)
            _write(pymux, db)
            db.execute("PRAGMA user_version = %d" % SNAPSHOT_VERSION)
    finally:
        db.close()
    with open(partial, "rb") as written:
        os.fsync(written.fileno())
    os.replace(partial, path)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _write(pymux: Pymux, db: sqlite3.Connection) -> None:
    db.executemany(
        "INSERT INTO counters VALUES (?, ?)",
        [
            ("pane", Pane._pane_counter),
            ("window", Window._window_counter),
            ("split", _Split._split_counter),
            ("session", pymux._session_counter),
            ("uses", pymux._uses),
        ],
    )
    db.executemany(
        "INSERT INTO options VALUES (?, ?)",
        [(name, _value(getattr(pymux, attribute))) for name, attribute in sorted(_server_options().items())],
    )
    db.executemany("INSERT INTO global_environment VALUES (?, ?)", sorted(pymux.global_environment.items()))

    window_option_names = _window_option_names()
    for position, session in enumerate(pymux.sessions):
        arrangement = session.arrangement
        db.execute(
            "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                session.session_id,
                session.name,
                position,
                session.created,
                session.last_used,
                session.default_size.rows,
                session.default_size.columns,
                arrangement.base_index,
                arrangement.renumber_windows,
                last.window_id if (last := arrangement._last_active_window) is not None else None,
                _optional_id(session.overlay_pane),
                session.overlay_title,
                session.overlay_width,
                session.overlay_height,
            ),
        )
        db.executemany(
            "INSERT INTO session_environment VALUES (?, ?, ?)",
            [(session.session_id, name, value) for name, value in sorted(session.environment.items())],
        )
        db.executemany(
            "INSERT INTO window_defaults VALUES (?, ?, ?)",
            [
                (session.session_id, window_option_names.get(attribute, attribute), _value(value))
                for attribute, value in sorted(arrangement.window_defaults.items())
            ],
        )
        for linked, windows in ((True, arrangement.windows), (False, arrangement._unlinked_windows)):
            for place, window in enumerate(windows):
                _write_window(db, session, window, linked, place)
        if session.overlay_pane is not None:
            _write_pane(db, session.overlay_pane, None, None, None)

    _write_clients(pymux, db)


def _write_window(db: sqlite3.Connection, session: Session, window: Window, linked: bool, place: int) -> None:
    previous = window._prev_active_pane() if window._prev_active_pane is not None else None
    manual = window.manual_size
    db.execute(
        "INSERT INTO windows VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            window.window_id,
            session.session_id,
            linked,
            place,
            int(window.index),
            window.chosen_name,
            window.root.split_id,
            _optional_id(window._active_pane),
            _optional_id(previous),
            _value(window.previous_selected_layout),
            window._strip,
            _value(window.window_size),
            manual.rows if manual is not None else None,
            manual.columns if manual is not None else None,
            window.zoom,
            window.synchronize_panes,
            window.frame_rate,
        ),
    )
    db.executemany(
        "INSERT INTO column_widths VALUES (?, ?, ?, ?)",
        [(window.window_id, kind, id, width) for (kind, id), width in sorted(window.column_widths.items())],
    )
    _write_split(db, window, window.root, None, None)


def _write_split(db: sqlite3.Connection, window: Window, split: _Split, parent: _Split | None, place: int | None):
    db.execute(
        "INSERT INTO splits VALUES (?, ?, ?, ?, ?)",
        (
            split.split_id,
            window.window_id,
            _SPLIT_KINDS[type(split)],
            parent.split_id if parent is not None else None,
            place,
        ),
    )
    db.executemany(
        "INSERT INTO split_weights VALUES (?, ?, ?, ?)",
        [(split.split_id, kind, id, weight) for (kind, id), weight in sorted(split.weights.items())],
    )
    for position, child in enumerate(split):
        if isinstance(child, Pane):
            _write_pane(db, child, window, split, position)
        else:
            _write_split(db, window, child, split, position)


def _write_pane(
    db: sqlite3.Connection, pane: Pane, window: Window | None, parent: _Split | None, place: int | None
) -> None:
    db.execute(
        "INSERT INTO panes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            pane.pane_id,
            window.window_id if window is not None else None,
            parent.split_id if parent is not None else None,
            place,
            pane.chosen_name,
            pane.clock_mode,
            pane.current_directory,
            pane.current_host,
            pane.command_zone,
            pane.last_exit_status,
            pane.revision,
        ),
    )
    db.executemany(
        "INSERT INTO pane_marks VALUES (?, ?, ?)",
        [(pane.pane_id, position, row) for position, row in enumerate(pane.marks)],
    )
    db.executemany(
        "INSERT INTO pane_user_vars VALUES (?, ?, ?)",
        [(pane.pane_id, name, value) for name, value in sorted(pane.user_vars.items())],
    )


def _write_clients(pymux: Pymux, db: sqlite3.Connection) -> None:
    """
    Each client by the id it keeps across reattaches, and the windows it
    was on in each session. A client the server loaded and that has not
    come back yet is written again as it was read.
    """
    clients: dict[str, dict] = {key: dict(value) for key, value in pymux.returning_clients.items()}
    for connection in pymux.connections:
        state: ClientState | None = connection.client_state
        if state is None or not connection.client_id:
            continue
        windows = {}
        for session in pymux.sessions:
            arrangement = session.arrangement
            active = arrangement._active_window_for_cli.get(state.app)
            previous = arrangement._prev_active_window_for_cli.get(state.app)
            if active is not None or previous is not None:
                windows[session.session_id] = (
                    active.window_id if active is not None else None,
                    previous.window_id if previous is not None else None,
                )
        clients[connection.client_id] = {
            "session_id": state.session.session_id if state.session is not None else None,
            "previous_session_id": state.previous_session.session_id if state.previous_session is not None else None,
            "windows": windows,
        }
    for client_id, client in sorted(clients.items()):
        db.execute(
            "INSERT INTO clients VALUES (?, ?, ?)",
            (client_id, client["session_id"], client["previous_session_id"]),
        )
        db.executemany(
            "INSERT INTO client_windows VALUES (?, ?, ?, ?)",
            [(client_id, session_id, *pair) for session_id, pair in sorted(client["windows"].items())],
        )


class SnapshotError(Exception):
    "A snapshot this build cannot load."


def load(pymux: Pymux, path: str | os.PathLike[str], make_terminal: Callable[[PaneId], Terminal]) -> None:
    """
    Replace the window management of a fresh `pymux` with a snapshot's.

    `make_terminal` gives each pane its terminal: the screen and the
    process are not in these tables.
    """
    db = sqlite3.connect("file:%s?mode=ro" % Path(path), uri=True)
    try:
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version != SNAPSHOT_VERSION:
            raise SnapshotError("snapshot version %d, and this build reads %d" % (version, SNAPSHOT_VERSION))
        _read(pymux, db, make_terminal)
    finally:
        db.close()


def _rows(db: sqlite3.Connection, sql: str, *params) -> list[sqlite3.Row]:
    db.row_factory = sqlite3.Row
    return db.execute(sql, params).fetchall()


def _read(pymux: Pymux, db: sqlite3.Connection, make_terminal: Callable[[PaneId], Terminal]) -> None:
    counters = {row["name"]: row["value"] for row in _rows(db, "SELECT * FROM counters")}

    server_options = _server_options()
    for row in _rows(db, "SELECT * FROM options"):
        attribute = server_options.get(row["name"])
        # An option this build dropped is not an error: it has nothing to hold it.
        if attribute is not None:
            setattr(pymux, attribute, _like(getattr(pymux, attribute), row["value"]))
    pymux.global_environment = {row["name"]: row["value"] for row in _rows(db, "SELECT * FROM global_environment")}

    panes: dict[int, Pane] = {}

    def pane_of(row: sqlite3.Row) -> Pane:
        pane = Pane(make_terminal(PaneId(row["pane_id"])))
        pane.pane_id = PaneId(row["pane_id"])
        pane.chosen_name = row["pane_name"]
        pane.clock_mode = bool(row["clock_mode"])
        pane.current_directory = row["pane_current_path"]
        pane.current_host = row["current_host"]
        pane.command_zone = row["command_zone"]
        pane.last_exit_status = row["last_exit_status"]
        pane.revision = row["pane_revision"]
        pane.marks = [
            mark["row"]
            for mark in _rows(db, "SELECT row FROM pane_marks WHERE pane_id = ? ORDER BY position", pane.pane_id)
        ]
        pane.user_vars = {
            var["name"]: var["value"]
            for var in _rows(db, "SELECT * FROM pane_user_vars WHERE pane_id = ?", pane.pane_id)
        }
        panes[pane.pane_id] = pane
        pymux.panes_by_id[pane.pane_id] = pane
        return pane

    def split_of(row: sqlite3.Row) -> _Split:
        split = (HSplit if row["kind"] == "hsplit" else VSplit)()
        split.split_id = row["split_id"]
        split.weights = {
            (weight["kind"], weight["id"]): weight["weight"]
            for weight in _rows(db, "SELECT * FROM split_weights WHERE split_id = ?", split.split_id)
        }
        children = [
            *[
                (r["position"], split_of(r))
                for r in _rows(db, "SELECT * FROM splits WHERE parent_split_id = ?", split.split_id)
            ],
            *[
                (r["position"], pane_of(r))
                for r in _rows(db, "SELECT * FROM panes WHERE parent_split_id = ?", split.split_id)
            ],
        ]
        split.extend(child for _, child in sorted(children, key=lambda pair: pair[0]))
        return split

    windows: dict[int, Window] = {}
    window_attributes = {name: attribute for attribute, name in _window_option_names().items()}
    sessions = []
    for row in _rows(db, "SELECT * FROM sessions ORDER BY position"):
        session = Session(SessionId(row["session_id"]), row["session_name"])
        session.created = row["session_created"]
        session.last_used = row["last_used"]
        session.default_size = Size(rows=row["default_height"], columns=row["default_width"])
        session.overlay_title = row["overlay_title"]
        session.overlay_width = row["overlay_width"]
        session.overlay_height = row["overlay_height"]
        session.environment = {
            env["name"]: env["value"]
            for env in _rows(db, "SELECT * FROM session_environment WHERE session_id = ?", session.session_id)
        }
        arrangement = session.arrangement
        arrangement.base_index = row["base_index"]
        arrangement.renumber_windows = bool(row["renumber_windows"])
        for default in _rows(db, "SELECT * FROM window_defaults WHERE session_id = ?", session.session_id):
            attribute = window_attributes.get(default["name"], default["name"])
            fresh = Window()
            if hasattr(fresh, attribute):
                arrangement.window_defaults[attribute] = _like(getattr(fresh, attribute), default["value"])

        for window_row in _rows(
            db, "SELECT * FROM windows WHERE session_id = ? ORDER BY linked DESC, position", session.session_id
        ):
            window = Window(WindowIndex(window_row["window_index"]))
            window.window_id = WindowId(window_row["window_id"])
            window.chosen_name = window_row["window_name"]
            (root_row,) = _rows(db, "SELECT * FROM splits WHERE split_id = ?", window_row["root_split_id"])
            window.root = split_of(root_row)  # type: ignore[assignment]
            window._active_pane = panes.get(window_row["active_pane_id"])
            previous = panes.get(window_row["previous_active_pane_id"])
            window._prev_active_pane = ref(previous) if previous is not None else None
            layout = window_row["previous_layout"]
            window.previous_selected_layout = LayoutTypes(layout) if layout is not None else None
            window._strip = bool(window_row["strip"])
            window.window_size = type(window.window_size)(window_row["window_size"])
            if window_row["manual_height"] is not None:
                window.manual_size = Size(rows=window_row["manual_height"], columns=window_row["manual_width"])
            window.zoom = bool(window_row["zoom"])
            window.synchronize_panes = bool(window_row["synchronize_panes"])
            window.frame_rate = window_row["frame_rate"]
            window.column_widths = {
                (width["kind"], width["id"]): width["width"]
                for width in _rows(db, "SELECT * FROM column_widths WHERE window_id = ?", window.window_id)
            }
            (arrangement.windows if window_row["linked"] else arrangement._unlinked_windows).append(window)
            windows[window.window_id] = window

        arrangement._last_active_window = windows.get(row["last_active_window_id"])
        if row["overlay_pane_id"] is not None:
            (overlay_row,) = _rows(db, "SELECT * FROM panes WHERE pane_id = ?", row["overlay_pane_id"])
            session.overlay_pane = pane_of(overlay_row)
        sessions.append(session)

    pymux.sessions = sessions

    pymux.returning_clients = {}
    for row in _rows(db, "SELECT * FROM clients"):
        pymux.returning_clients[row["client_id"]] = {
            "session_id": row["session_id"],
            "previous_session_id": row["previous_session_id"],
            "windows": {
                window["session_id"]: (window["active_window_id"], window["previous_window_id"])
                for window in _rows(db, "SELECT * FROM client_windows WHERE client_id = ?", row["client_id"])
            },
        }

    Pane._pane_counter = counters["pane"]
    Window._window_counter = counters["window"]
    _Split._split_counter = counters["split"]
    pymux._session_counter = counters["session"]
    pymux._uses = counters["uses"]
