"""
A snapshot holds every saved field, and loads back to the same tables.

Lillecarl/pymux#399.
"""

from __future__ import annotations

import importlib
import sqlite3

import pytest
from prompt_toolkit.application.current import set_app
from prompt_toolkit.data_structures import Size
from pyte.keep import Keep
from test_a_session_on_a_pyte_screen import attached, settled, shows
from test_every_attribute_has_a_fate import declared

from pymux import snapshot
from pymux.arrangement import LayoutTypes
from pymux.enums import WindowSize
from pymux.main import Pymux


@pytest.fixture
def pymux():
    mux = Pymux()
    mux.test_mode = True
    try:
        yield mux
    finally:
        for pane in list(mux.panes_by_id.values()):
            if not pane.process.is_terminated:
                pane.process.kill()


def _class(name: str) -> type:
    module, _, qualname = name.rpartition(".")
    return getattr(importlib.import_module(module), qualname)


@pytest.mark.parametrize("name", sorted(snapshot.WRITES.keys() | snapshot.LATER.keys()))
def test_a_snapshot_writes_every_saved_field_or_says_it_does_not_yet(name):
    saved = {attribute for attribute, fate in declared(_class(name)).items() if fate == Keep.SAVED}
    writes = snapshot.WRITES.get(name, frozenset())
    later = snapshot.LATER.get(name, frozenset())

    assert not writes & later, "both written and left for later: %s" % sorted(writes & later)
    assert writes | later == saved, "saved and not written: %s; written and not saved: %s" % (
        sorted(saved - writes - later),
        sorted((writes | later) - saved),
    )


def _dump(path) -> list[str]:
    db = sqlite3.connect(path)
    try:
        return list(db.iterdump())
    finally:
        db.close()


def _rows(path, sql: str) -> list[tuple]:
    db = sqlite3.connect(path)
    try:
        return db.execute(sql).fetchall()
    finally:
        db.close()


def _vary_what_no_command_reaches(pymux, client_state) -> None:
    """
    Give every field a value a fresh object does not have, so a load
    that forgot one cannot pass by writing the default back.
    """
    first, second = pymux.sessions
    stacked, strip = first.arrangement.windows
    pane = stacked.panes[0]
    pane.chosen_name = "named"
    pane.clock_mode = True
    pane.current_directory = "/srv"
    pane.current_host = "far"
    pane.command_zone = "C"
    pane.last_exit_status = 2
    pane.marks = [3, 7]
    pane.user_vars = {"badge": "on", "role": "build"}
    strip.panes[0].user_vars = {"badge": "off"}
    strip.panes[0].marks = [1]

    stacked.zoom = True
    stacked.synchronize_panes = True
    stacked.manual_size = Size(rows=30, columns=100)
    stacked.window_size = WindowSize.LARGEST
    stacked.previous_selected_layout = LayoutTypes.TILED
    stacked.frame_rate = 60
    strip.set_column_width(strip.root[0], 0.75)
    strip.set_column_width(strip.root[1], 0.5)
    strip.root.set_weight(strip.root[1], 2)
    stacked.set_column_width(stacked.root, 0.4)

    first.default_size = Size(rows=40, columns=120)
    first.environment["ALSO"] = "this"
    second.environment["ONLY"] = "there"
    second.arrangement.base_index = 0
    second.arrangement.renumber_windows = True
    second.arrangement.window_defaults["synchronize_panes"] = True
    pymux.global_environment["UNSET"] = None

    client_state.previous_session = second
    (elsewhere,) = second.arrangement.windows
    second.arrangement._active_window_for_cli[client_state.app] = elsewhere
    first.arrangement._prev_active_window_for_cli[client_state.app] = strip
    pymux.returning_clients["gone"] = {
        "session_id": second.session_id,
        "previous_session_id": None,
        "windows": {second.session_id: (None, elsewhere.window_id)},
    }


#: Columns that hold one value in every row however the server is used.
CONSTANT = {
    ("counters", "value"),  # five counters, and two of them may agree
}


def _constant_columns(path) -> list[tuple[str, str]]:
    "Every column that holds one value across a table of several rows."
    db = sqlite3.connect(path)
    try:
        found = []
        for table in snapshot.TABLES:
            if db.execute("SELECT count(*) FROM %s" % table).fetchone()[0] < 2:
                found.append((table, "(fewer than two rows)"))
                continue
            for column in [row[1] for row in db.execute("PRAGMA table_info(%s)" % table)]:
                distinct = db.execute(
                    "SELECT count(DISTINCT coalesce(%s, 'NULL')) FROM %s" % (column, table)
                ).fetchone()[0]
                if distinct < 2 and (table, column) not in CONSTANT:
                    found.append((table, column))
        return found
    finally:
        db.close()


async def test_a_snapshot_loads_back_to_the_same_tables(pymux, tmp_path):
    async with pymux.running(), attached(pymux) as session:
        await shows(session, "$")
        connection = pymux.connections[-1]
        # The web viewer here sends no id; a terminal client does.
        connection.client_id = "returning"
        with set_app(connection.client_state.app):
            for command in (
                "split-window -v",
                "split-window -v",
                "resize-pane -U 3",
                # The resized pane leaves its weight behind: an entry
                # outlives the child it was written for.
                "kill-pane",
                "split-window -h",
                "rename-window built",
                "set-option status-left left",
                "set-environment -g WHERE here",
                "set-environment ONLY session",
                "set-window-option -g frame-rate 10",
                "new-window",
                "set-window-option strip on",
                "split-window -h",
                "select-pane -L",
                "new-window",
                "unlink-window",
                "display-popup -w 40 -h 10 -T pop 'sleep 60'",
                "new-session -d -s second",
            ):
                pymux.handle_command(command)
                await settled(session, 0.1)
        await settled(session)
        _vary_what_no_command_reaches(pymux, connection.client_state)

        first = tmp_path / "first.sqlite"
        snapshot.save(pymux, first)

    assert _rows(first, "SELECT count(*) FROM panes") == [(8,)]
    # One under each root: the stack split beside, and the strip's
    # first column, which turning the strip on wraps the old root in.
    assert _rows(first, "SELECT count(*) FROM splits WHERE parent_split_id IS NOT NULL") == [(2,)]
    assert _rows(first, "SELECT window_name FROM windows WHERE window_name IS NOT NULL") == [("built",)]
    assert _rows(first, "SELECT value FROM options WHERE name = 'status-left'") == [("left",)]
    assert _rows(first, "SELECT value FROM window_defaults WHERE name = 'frame-rate'") == [(10,)]
    assert _rows(
        first, "SELECT count(*) FROM split_weights WHERE kind = 'pane' AND id NOT IN (SELECT pane_id FROM panes)"
    ) == [(1,)]
    assert _rows(first, "SELECT client_id FROM clients ORDER BY 1") == [("gone",), ("returning",)]
    assert _rows(first, "SELECT overlay_title FROM sessions WHERE overlay_pane_id IS NOT NULL") == [("pop",)]
    assert _constant_columns(first) == []

    fresh = Pymux()
    snapshot.load(fresh, first, make_terminal=lambda pane_id: object())
    second = tmp_path / "second.sqlite"
    snapshot.save(fresh, second)

    assert _dump(second) == _dump(first)


def test_a_snapshot_of_another_version_is_refused(pymux, tmp_path):
    path = tmp_path / "old.sqlite"
    snapshot.save(pymux, path)
    db = sqlite3.connect(path)
    db.execute("PRAGMA user_version = %d" % (snapshot.SNAPSHOT_VERSION + 1))
    db.commit()
    db.close()

    with pytest.raises(snapshot.SnapshotError):
        snapshot.load(Pymux(), path, make_terminal=lambda pane_id: object())
