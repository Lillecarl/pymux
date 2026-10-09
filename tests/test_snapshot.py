"""
A snapshot holds every saved field, and loads back to the same tables.

Lillecarl/pymux#399.
"""

from __future__ import annotations

import importlib
import os
import sqlite3
import sys

import anyio
import pytest
from prompt_toolkit.application.current import set_app
from prompt_toolkit.data_structures import Size
from pyte.keep import Keep
from test_a_session_on_a_pyte_screen import attached, settled, shows
from test_every_attribute_has_a_fate import declared, live_objects

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


async def test_every_class_with_a_saved_field_is_written_or_left_for_later(pymux):
    "A class the walk finds and nothing in `snapshot.py` names would be lost whole."
    async with pymux.running(), attached(pymux) as session:
        await shows(session, "$")
        named = snapshot.WRITES.keys() | snapshot.LATER.keys() | snapshot.LATER_CLASSES
        missing = set()
        for one in live_objects(pymux):
            cls = type(one)
            names = {"%s.%s" % (base.__module__, base.__qualname__) for base in cls.__mro__}
            if Keep.SAVED in declared(cls).values() and not names & named:
                missing.add("%s.%s" % (cls.__module__, cls.__qualname__))
        assert not missing, sorted(missing)


def test_a_path_that_needs_quoting_saves_and_loads(pymux, tmp_path):
    path = tmp_path / "a b?c#d%e.sqlite"
    snapshot.save(pymux, path)
    snapshot.load(Pymux(), path)


def test_a_crashed_saves_journal_does_not_reach_the_next_save(pymux, tmp_path):
    path = tmp_path / "snapshot.sqlite"
    stale = tmp_path / "snapshot.sqlite.partial-journal"
    stale.write_bytes(b"not a journal")
    snapshot.save(pymux, path)
    assert not stale.exists()
    assert _rows(path, "SELECT count(*) FROM sessions") == [(1,)]


def test_a_snapshot_is_private_from_its_first_byte(pymux, tmp_path):
    written = snapshot.Snapshot(tmp_path / "snapshot.sqlite")
    try:
        assert written.partial.stat().st_mode & 0o777 == 0o600
        written.write(pymux)
        written.finish()
    except BaseException:
        written.abandon()
        raise
    assert (tmp_path / "snapshot.sqlite").stat().st_mode & 0o777 == 0o600


def test_a_loaded_server_adopts_the_listener_it_names(pymux, tmp_path):
    pymux.listen_on_socket(str(tmp_path / "server.sock"))
    path = tmp_path / "snapshot.sqlite"
    snapshot.save(pymux, path)

    fresh = Pymux()
    fd = snapshot.load(fresh, path)
    assert fd == pymux.listener.socket.fileno()
    assert fresh.socket_name == pymux.socket_name
    fresh.adopt_listener(fd)
    # One fd, and the old server's object is the one that closes it.
    fresh.listener.socket.detach()

    other = Pymux()
    other.listen_on_socket(str(tmp_path / "other.sock"))
    read, write = os.pipe()
    try:
        for wrong in (other.listener.socket.fileno(), read):
            with pytest.raises(OSError):
                fresh.adopt_listener(wrong)
            os.fstat(wrong)  # a refusal leaves the fd to its owner
    finally:
        other.listener.close()
        os.close(read)
        os.close(write)
    pymux.listener.close()


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
    client_state.name = "desk"
    client_state.full_screen = True
    client_state.theme = "grey"
    pymux.returning_clients["gone"] = {
        "session_id": second.session_id,
        "previous_session_id": None,
        "windows": {second.session_id: (None, elsewhere.window_id)},
        "settings": {
            "name": "phone",
            "last_used": client_state.last_used + 1,
            "read_only": True,
            "ignore_size": True,
            "full_screen": False,
            "theme": "nearest",
            "swap_dark_and_light": True,
            "message": "job 2 exited 4",
        },
        "modes": {
            "has_prefix": True,
            "key_tables": ["resize"],
            "confirmations": [["Kill the pane?", "kill-pane"]],
            "prompt": ["(rename)", "rename-window '%%'", "half typ"],
            "command": None,
            "popup": None,
            "menu": None,
            "chooser": "choose_window",
            "choose_window_index": 1,
            "choose_window_command": "select-window -t '%%'",
            "choose_window_filter": "sl",
            "chooser_return_to": [second.session_id, elsewhere.window_id],
        },
    }
    # Opening a chooser closes a menu, so the menu is another client's.
    pymux.returning_clients["away"] = {
        "session_id": first.session_id,
        "previous_session_id": second.session_id,
        "windows": {},
        "settings": {},
        "modes": {
            "has_prefix": False,
            "key_tables": [],
            "confirmations": [],
            "prompt": None,
            "command": "kill-ses",
            "popup": None,
            "menu": ["Do", [["a", "again", "display-message again"]]],
            "chooser": None,
            "choose_window_index": 0,
            "choose_window_command": "",
            "choose_window_filter": "",
            "chooser_return_to": None,
        },
    }


#: Columns that hold one value in every row however the server is used.
CONSTANT = {
    ("counters", "value"),  # five counters, and two of them may agree
    ("jobs", "status"),  # a running job refuses a snapshot
}


async def _run_jobs(pymux) -> None:
    "Finished jobs that differ in every column a snapshot writes."
    jobs = pymux.jobs
    for command, directory, tags, pty in (
        ("printf out; printf err >&2", None, [("build", None), ("session", "agent")], False),
        # The environment below is the whole of it, with no PATH.
        (
            '%s -c \'import sys; print("x" * 10**6); print("y" * 10**6, file=sys.stderr); sys.exit(3)\''
            % sys.executable,
            "/",
            [("big", "yes")],
            False,
        ),
        ("true", "/no/such/directory", [], True),
        # Forgotten below: the next id is past it all the same.
        ("true", None, [], False),
    ):
        job = await jobs.submit(command, directory, tags, {"WHO": command}, pty)
        await jobs.supervise(job)
    await jobs._forget(job)


def _jobs_of(pymux) -> list[tuple]:
    return [
        (job.job_id, job.command, job.returncode, job.error, job.tags, job.kept("stdout"), job.kept("stderr"))
        for job in pymux.jobs.listing()
    ]


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


def _text(pane) -> str:
    screen = pane.terminal.terminal_control.screen
    return "\n".join(line.text for line in screen.page.text_lines(0, screen.max_y))


async def test_a_snapshot_loads_back_to_the_same_tables_and_programs(pymux, tmp_path):
    alternate = tmp_path / "alternate.sh"
    alternate.write_text("printf '%s\\r\\n\\033[?1049h\\033[31mALT\\033[0m\\033[3'\nsleep 60\n" % ("wrapped " * 20))
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
                # A program that wraps a row on the first page, then
                # stops on the alternate one, in colour and mid-sequence.
                "split-window -h 'sh %s'" % alternate,
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
        await _run_jobs(pymux)
        jobs = _jobs_of(pymux)

        first = tmp_path / "first.sqlite"
        snapshot.save(pymux, first)
        shell = pymux.sessions[0].arrangement.windows[0].panes[0]
        before = _text(shell)

        # What `execve` does to the old server: its programs go on, and
        # it no longer serves them.
        for pane in snapshot._panes_of(pymux):
            pane.process.backend.release()

        fresh = Pymux()
        fresh.test_mode = True
        snapshot.load(fresh, first)
        second = tmp_path / "second.sqlite"
        snapshot.save(fresh, second)
        try:
            async with fresh.running():
                await snapshot.start(fresh)
                assert _jobs_of(fresh) == jobs
                assert (await fresh.jobs.submit("true")).job_id == pymux.jobs.last_id + 1
                reloaded = fresh.panes_by_id[shell.pane_id]
                assert _text(reloaded) == before
                reloaded.process.write_input("echo mar''ker\r")
                with anyio.fail_after(10):
                    while "marker" not in _text(reloaded):
                        await anyio.sleep(0.05)
                async with attached(fresh):
                    back = fresh.connections[-1].client_state
                    fresh.welcome_back(back, "gone")
                    (elsewhere,) = back.session.arrangement.windows
                    assert back.session.name == "second"
                    assert back.session.arrangement._prev_active_window_for_cli[back.app] is elsewhere
                    assert (back.name, back.read_only, back.message) == ("phone", True, "job 2 exited 4")
                    assert "gone" not in fresh.returning_clients
                    # What it was in the middle of, and the prompt holds the keyboard.
                    assert (back.has_prefix, back.key_tables, back.confirm_text) == (True, ["resize"], "Kill the pane?")
                    assert back.choose_window and back.choose_window_filter.text == "sl"
                    assert back.chooser_return_to == (back.session, elsewhere)
                    assert (back.prompt_command, back.prompt_buffer.text) == ("rename-window '%%'", "half typ")
                    assert back.app.layout.has_focus(back.prompt_buffer)
                async with attached(fresh):
                    back = fresh.connections[-1].client_state
                    fresh.welcome_back(back, "away")
                    assert (back.menu_title, back.menu_entries) == ("Do", [("a", "again", "display-message again")])
                    assert back.command_buffer.text == "kill-ses"
                    assert back.app.layout.has_focus(back.command_buffer)
                fresh.stop()
        finally:
            for pane in list(fresh.panes_by_id.values()):
                pane.process.kill()

    assert _rows(first, "SELECT count(*) FROM panes") == [(9,)]
    # One under each root: the stack split beside, and the strip's
    # first column, which turning the strip on wraps the old root in.
    assert _rows(first, "SELECT count(*) FROM splits WHERE parent_split_id IS NOT NULL") == [(2,)]
    assert _rows(first, "SELECT window_name FROM windows WHERE window_name IS NOT NULL") == [("built",)]
    assert _rows(first, "SELECT value FROM options WHERE name = 'status-left'") == [("left",)]
    assert _rows(first, "SELECT value FROM window_defaults WHERE name = 'frame-rate'") == [(10,)]
    assert _rows(
        first, "SELECT count(*) FROM split_weights WHERE kind = 'pane' AND id NOT IN (SELECT pane_id FROM panes)"
    ) == [(1,)]
    assert _rows(first, "SELECT client_id FROM clients ORDER BY 1") == [("away",), ("gone",), ("returning",)]
    assert _rows(first, "SELECT overlay_title FROM sessions WHERE overlay_pane_id IS NOT NULL") == [("pop",)]
    assert _rows(first, "SELECT count(*) FROM screen_rows WHERE text LIKE '%ALT'") == [(1,)]
    assert _rows(first, "SELECT returncode, stdout_dropped > 0, stderr_dropped > 0 FROM jobs WHERE job_id = 2") == [
        (3, 1, 1)
    ]
    assert _constant_columns(first) == []
    assert _dump(second) == _dump(first)


def test_a_snapshot_of_another_version_is_refused(pymux, tmp_path):
    path = tmp_path / "old.sqlite"
    snapshot.save(pymux, path)
    db = sqlite3.connect(path)
    db.execute("PRAGMA user_version = %d" % (snapshot.SNAPSHOT_VERSION + 1))
    db.commit()
    db.close()

    with pytest.raises(snapshot.SnapshotError):
        snapshot.load(Pymux(), path)


def test_a_snapshot_of_the_version_before_steps_up(pymux, tmp_path):
    "What the server being upgraded wrote, which is the older shape."
    path = tmp_path / "old.sqlite"
    pymux.returning_clients["gone"] = {
        "session_id": None,
        "previous_session_id": None,
        "windows": {},
        "settings": {"message": "lost"},
    }
    snapshot.save(pymux, path)
    db = sqlite3.connect(path)
    for name in (*snapshot.CLIENT_SETTINGS, "modes"):
        db.execute("ALTER TABLE clients DROP COLUMN %s" % name)
    db.execute("PRAGMA user_version = 1")
    db.commit()
    db.close()

    fresh = Pymux()
    snapshot.load(fresh, path)
    assert fresh.returning_clients["gone"]["settings"] == {}
    assert fresh.returning_clients["gone"]["modes"] is None
    assert _rows(path, "PRAGMA user_version") == [(1,)]
