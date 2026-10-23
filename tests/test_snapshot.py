"""
A snapshot holds every saved field, and loads back to the same tables.

Lillecarl/pymux#399.
"""

from __future__ import annotations

import dataclasses
import importlib
import json
import os
import shutil
import sys
from typing import override

import anyio
import apsw
import pytest
from prompt_toolkit.application.current import set_app
from prompt_toolkit.clipboard import ClipboardData
from prompt_toolkit.data_structures import Size
from prompt_toolkit.selection import SelectionState, SelectionType
from ptyhost.backends.posix import PosixBackend
from pyte.keep import Keep
from pyte.page import RowMark
from session import once
from test_a_session_on_a_pyte_screen import attached, settled, shows
from test_every_attribute_has_a_fate import declared, live_objects

from pymux import snapshot, tables
from pymux.arrangement import LayoutTypes
from pymux.commands.wait_for import WaitChannel
from pymux.enums import Chooser, WindowCodec, WindowSize
from pymux.jobs import JobFeed, JobPipes
from pymux.main import Pymux
from pymux.notifications import Urgency
from pymux.query import ask


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


@pytest.mark.parametrize("name", sorted(snapshot.WRITES))
def test_a_snapshot_writes_every_saved_field(name):
    saved = {attribute for attribute, fate in declared(_class(name)).items() if fate == Keep.SAVED}
    writes = snapshot.WRITES[name]

    assert writes == saved, "saved and not written: %s; written and not saved: %s" % (
        sorted(saved - writes),
        sorted(writes - saved),
    )


@pytest.mark.parametrize("row", tables.ROWS, ids=lambda row: row.TABLE)
def test_a_row_names_the_columns_of_its_table(row):
    db = apsw.Connection(":memory:")
    db.execute(tables.SCHEMA)

    columns = [column[1] for column in db.execute("PRAGMA table_info(%s)" % (row.TABLE,))]

    assert [field.name for field in dataclasses.fields(row)] == columns


@pytest.mark.parametrize("row", tables.ROWS, ids=lambda row: row.TABLE)
def test_a_row_names_the_key_of_its_table(row):
    db = apsw.Connection(":memory:")
    db.execute(tables.SCHEMA)

    key = sorted((column[5], column[1]) for column in db.execute("PRAGMA table_info(%s)" % (row.TABLE,)) if column[5])

    assert row.KEY == tuple(name for _, name in key)


def test_every_table_has_a_row():
    assert [row.TABLE for row in tables.ROWS] == list(tables.TABLES)


async def test_every_class_with_a_saved_field_is_written(pymux):
    "A class the walk finds and nothing in `snapshot.py` names would be lost whole."
    async with pymux.running(), attached(pymux) as session:
        await shows(session, "$")
        named = snapshot.WRITES.keys()
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


def _dump(path) -> dict[str, list]:
    "Every table's definition and its rows, in the order the rows sort."
    db = apsw.Connection(str(path))
    try:
        return {
            name: [sql, sorted(db.execute("SELECT * FROM %s" % name).fetchall(), key=repr)]
            for name, sql in db.execute("SELECT name, sql FROM sqlite_master WHERE type = 'table' ORDER BY name")
        }
    finally:
        db.close()


def _rows(path, sql: str) -> list[tuple]:
    db = apsw.Connection(str(path))
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
    stacked, strip, _viewing = first.arrangement.windows
    pane = stacked.panes[0]
    pane.chosen_name = "named"
    pane.clock_mode = True
    pane.current_directory = "/srv"
    pane.current_host = "far"
    pane.command_zone = "C"
    pane.last_exit_status = 2
    pane.bell = True
    pane.screen.mark_row(RowMark.PROMPT)
    pane.user_vars = {"badge": "on", "role": "build"}
    strip.panes[0].user_vars = {"badge": "off"}
    strip.panes[0].screen.mark_row(RowMark.PROMPT | RowMark.OUTPUT)

    stacked.zoom = True
    stacked.synchronize_panes = True
    stacked.manual_size = Size(rows=30, columns=100)
    stacked.window_size = WindowSize.LARGEST
    stacked.previous_selected_layout = LayoutTypes.TILED
    stacked.frame_rate = 60
    stacked.frame_rate_set = True
    stacked.wayland_scale = "auto*1.5"
    stacked.window_codec = WindowCodec.VP9
    stacked.monitor_bell = False
    stacked.bell = True
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
    first.arrangement._window_history_for_cli[client_state.app] = [strip]
    pymux.named_buffers = {"one": "first text", "two": "second\ntext"}
    pymux.clipboard.set_data(ClipboardData("copied", SelectionType.LINES))
    pymux.prompt_history.append_string("rename-window a")
    pymux.prompt_history.append_string("rename-window b")
    pymux.message_log.extend(["said once", "said twice"])
    pymux.hooks = {"after-new-window": ["display-message new", "display-panes"], "pane-died": ["kill-pane"]}
    pymux.key_bindings_manager.add_custom_binding("F5", "display-message", ["it's F5"], table="root")
    pymux.key_bindings_manager.add_custom_binding("x", "kill-pane", [])

    # Copy mode, for this client. Not the shell, which the round trip
    # types into.
    with set_app(client_state.app):
        for place, copying in enumerate(strip.panes[:2]):
            copying.terminal.enter_copy_mode().buffer.cursor_position = place
        strip.panes[1].terminal.copy_view.buffer.selection_state = SelectionState(0, SelectionType.LINES)

    signalled, held = WaitChannel(), WaitChannel()
    signalled.woken = True
    held.locked = True
    pymux.wait_channels = {"done": signalled, "turn": held}
    pymux.startup_command = "htop"

    center = pymux.notification_center
    center.add("built", "all green", Urgency.LOW, pane.pane_id)
    center.add("deploy", "", Urgency.CRITICAL, None)
    center.add_osc99(strip.panes[0].pane_id, "i=half:d=0:p=body;still coming")
    center._records = [record._replace(at=1000.0 + n) for n, record in enumerate(center._records)]
    pymux.notifications.outgoing(pane.pane_id, "i=one;first")
    pymux.notifications.outgoing(strip.panes[0].pane_id, "i=two;second")

    client_state.name = "desk"
    client_state.full_screen = True
    client_state.theme = "grey"
    pymux.returning_clients["gone"] = {
        "session_id": second.session_id,
        "previous_session_id": None,
        "windows": {second.session_id: (None, elsewhere.window_id)},
        "connected": 1000.5,
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
        "connected": None,
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
    # This server forks every pane here; `_Copied` writes the other value.
    ("pane_programs", "parent"),
}


async def _run_jobs(pymux) -> None:
    "Jobs that differ in every column a snapshot writes, and one that runs on."
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
        job = jobs.submit(command, directory, tags, {"WHO": command}, pty)
        await jobs.supervise(job)
    jobs._forget(job)
    # These run past the snapshot: one on a terminal, one on pipes.
    # Lillecarl/pymux#556.
    for pty in (True, False):
        running = jobs.submit("echo started; exec sleep 60", None, [("long", None)], dict(os.environ), pty)
        pymux.tasks.start_soon(jobs.supervise, running)
        await once(lambda running=running: b"started" in running.kept("stdout"), 10.0, "a running job never printed")
    # A question kept, and a seed taken away, which must stay away.
    jobs.save_query("mine", "SELECT id FROM jobs WHERE pty = 1", None)
    assert jobs.forget_query("running")


async def _questions_of(pymux) -> list[tuple]:
    "What a question answers about the kept questions."
    return (await ask(pymux, "SELECT name, sql, description FROM saved_queries ORDER BY name", {}, 0, 0)).rows


def _copy_modes_of(pymux) -> list:
    "What the returning client reads the history of, as its record says."
    for connection in pymux.connections:
        if connection.client_id == "returning":
            return connection.client_state.modes()["copying"]
    return pymux.returning_clients["returning"]["modes"]["copying"]


def _bindings_of(pymux) -> dict:
    return {
        key: (binding.written, binding.command, binding.arguments)
        for key, binding in pymux.key_bindings_manager.custom_bindings.items()
    }


def _jobs_of(pymux) -> list[tuple]:
    return [
        (job.job_id, job.command, job.returncode, job.error, job.tags, job.kept("stdout"), job.kept("stderr"))
        for job in pymux.jobs.listing()
    ]


def _constant_columns(path) -> list[tuple[str, str]]:
    "Every column that holds one value across a table of several rows."
    db = apsw.Connection(str(path))
    try:
        found = []
        for table in tables.TABLES:
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


def _viewers_of(pymux) -> dict:
    "Each pane that views a job, by the job's id."
    return {pane.process.job.job_id: pane for pane in pymux.panes_by_id.values() if isinstance(pane.process, JobFeed)}


def _window_count(pymux) -> int:
    return sum(len(session.arrangement.windows) for session in pymux.sessions)


def _pane_count(pymux) -> int:
    return sum(len(window.panes) for session in pymux.sessions for window in session.arrangement.windows)


async def test_a_snapshot_loads_back_to_the_same_tables_and_programs(pymux, tmp_path):
    alternate = tmp_path / "alternate.sh"
    alternate.write_text("printf '%s\\r\\n\\033[?1049h\\033[31mALT\\033[0m\\033[3'\nsleep 60\n" % ("wrapped " * 20))
    pymux.socket_name = str(tmp_path / "server.sock")
    async with pymux.running(), attached(pymux) as session:
        await shows(session, "$")
        await _run_jobs(pymux)
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
                # A pane that views a finished job, and one that views a
                # job running on, of other sizes. Lillecarl/pymux#715.
                "new-window",
                "view-job -k 2",
                "split-window -v",
                "view-job -k 5",
                "resize-pane -U 2",
                "split-window -h",
                "display-popup -w 40 -h 10 -T pop 'sleep 60'",
                "new-session -d -s second",
            ):
                windows = _window_count(pymux)
                panes = _pane_count(pymux)
                pymux.handle_command(command)
                if command == "new-window":
                    # A window is made on a later turn, and a screen that
                    # sat still for `settled` under load did not wait for
                    # it: `unlink-window` then took the strip instead.
                    # Lillecarl/pymux#664.
                    await once(lambda: _window_count(pymux) > windows, 10.0, "the new window never came")
                elif command.startswith("split-window"):
                    # So is a pane: the strip had one column when the
                    # test set the width of its second. Lillecarl/pymux#664.
                    await once(lambda: _pane_count(pymux) > panes, 10.0, "the new pane never came")
                elif command.startswith("view-job"):
                    viewed = int(command.split()[-1])
                    await once(lambda: viewed in _viewers_of(pymux), 10.0, "the viewer never came")
                await settled(session, 0.1)
        await settled(session)
        _vary_what_no_command_reaches(pymux, connection.client_state)
        jobs = _jobs_of(pymux)
        questions = await _questions_of(pymux)

        first = tmp_path / "first.sqlite"
        snapshot.save(pymux, first)
        shell = pymux.sessions[0].arrangement.windows[0].panes[0]
        before = _text(shell)

        # What an upgrade does to the old server: its programs go on, and
        # it no longer reads them.
        for pane in snapshot._panes_of(pymux):
            if not isinstance(pane.process, JobFeed):
                pane.process.backend.pause_reading()
        pymux.jobs.pause_reading()

        # The new server takes a copy of every fd the snapshot names. The
        # old one stays each program's parent, so each one's end is seen
        # here and its code is not.
        fresh = Pymux()
        fresh.test_mode = True
        fresh.socket_name = pymux.socket_name
        second = tmp_path / "second.sqlite"
        try:
            async with fresh.running():
                snapshot.load(fresh, first, _Copied())
                snapshot.save(fresh, second)
                await snapshot.start(fresh)
                assert _jobs_of(fresh) == jobs
                # The jobs run on under the new server, which can end them.
                adopted = [job for job in fresh.jobs.listing() if not job.is_done]
                assert [job.pty for job in adopted] == [True, False]
                for job in adopted:
                    assert fresh.jobs.kill(job)
                    with anyio.fail_after(10):
                        await fresh.jobs.wait(job)
                    assert (job.returncode, job.kept("stdout")) == (None, b"started\n")
                # Each viewer replays its job, and says once how it ended.
                viewers = _viewers_of(fresh)
                for job_id, ended in ((2, "[job 2 exited 3]"), (5, "[job 5 exited None]")):
                    with anyio.fail_after(10):
                        while ended not in _text(viewers[job_id]):
                            await anyio.sleep(0.05)
                    assert _text(viewers[job_id]).count(ended) == 1
                assert "started" in _text(viewers[5])
                assert await _questions_of(fresh) == questions
                assert "running" not in dict((name, sql) for name, sql, _ in questions)
                assert fresh.named_buffers == pymux.named_buffers
                clipboard = fresh.clipboard.get_data()
                assert (clipboard.text, clipboard.type) == ("copied", SelectionType.LINES)
                assert fresh.prompt_history.get_strings()[-2:] == ["rename-window a", "rename-window b"]
                assert list(fresh.message_log)[-2:] == ["said once", "said twice"]
                assert fresh.hooks == pymux.hooks
                assert fresh.notification_center.notifications() == pymux.notification_center.notifications()
                assert fresh.notification_center._pending == pymux.notification_center._pending
                assert fresh.notification_center._next == pymux.notification_center._next
                assert fresh.notifications._incoming == pymux.notifications._incoming
                assert fresh.notifications._outgoing == pymux.notifications._outgoing
                assert {name: (c.woken, c.locked) for name, c in fresh.wait_channels.items()} == {
                    "done": (True, False),
                    "turn": (False, True),
                }
                assert fresh.startup_command == "htop"
                assert _copy_modes_of(fresh) == _copy_modes_of(pymux)
                assert len(_copy_modes_of(fresh)) == 2
                assert _bindings_of(fresh) == _bindings_of(pymux)
                assert (fresh.jobs.submit("true")).job_id == pymux.jobs.last_id + 1
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
                    assert back.session.arrangement.previous_window_for(back.app) is elsewhere
                    assert (back.name, back.read_only, back.message) == ("phone", True, "job 2 exited 4")
                    assert "gone" not in fresh.returning_clients
                    assert back.connection.created == 1000.5
                    # What it was in the middle of, and the prompt holds the keyboard.
                    assert (back.has_prefix, back.key_tables, back.confirm_text) == (True, ["resize"], "Kill the pane?")
                    assert back.chooser is Chooser.WINDOW and back.choose_window_filter.text == "sl"
                    assert back.chooser_return_to == (back.session, elsewhere)
                    assert (back.prompt_command, back.prompt_buffer.text) == ("rename-window '%%'", "half typ")
                    assert back.app.layout.has_focus(back.prompt_buffer)
                async with attached(fresh):
                    back = fresh.connections[-1].client_state
                    fresh.welcome_back(back, "away")
                    assert (back.menu_title, back.menu_entries) == ("Do", [("a", "again", "display-message again")])
                    assert back.command_buffer.text == "kill-ses"
                    assert back.app.layout.has_focus(back.command_buffer)
                async with attached(fresh):
                    back = fresh.connections[-1].client_state
                    fresh.welcome_back(back, "returning")
                    copying = [pane for pane in fresh.panes_by_id.values() if pane.is_copying_for(back.app)]
                    assert len(copying) == 2
                    assert not any(pane.process.suspended for pane in fresh.panes_by_id.values())
                fresh.stop()
        finally:
            for pane in list(fresh.panes_by_id.values()):
                pane.process.kill()

    assert _rows(first, "SELECT count(*) FROM panes") == [(12,)]
    assert _rows(first, "SELECT job_id FROM pane_viewers ORDER BY 1") == [(2,), (5,)]
    # One under each root: the stack split beside, and the strip's
    # first column, which turning the strip on wraps the old root in.
    # And the viewers' window: split down, then beside.
    assert _rows(first, "SELECT count(*) FROM splits WHERE parent_split_id IS NOT NULL") == [(3,)]
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
    assert _dump(_as_the_takeover_leaves_it(second, tmp_path / "second-taken.sqlite")) == _dump(
        _as_the_takeover_leaves_it(first, tmp_path / "first-taken.sqlite")
    )


class _Copied(snapshot.Takeover):
    """
    A copy of each fd the snapshot names, for a second server in this
    process. The first server stays each program's parent and reaps it,
    so the second waits for it as a stranger.
    """

    @override
    def pane(self, program: snapshot.Program) -> PosixBackend:
        row = program.row
        assert row.master_fd is not None
        return PosixBackend.adopt(os.dup(row.master_fd), row.pane_pid, parent=False)

    @override
    def job(self, row: tables.JobRow) -> PosixBackend | JobPipes | None:
        assert row.fds is not None and row.pid is not None
        fds = [os.dup(fd) for fd in json.loads(row.fds)]
        if row.pty:
            return PosixBackend.adopt(fds[0], row.pid, parent=False)
        stdout, stderr = fds
        return JobPipes(row.pid, stdout, stderr, parent=False)


def _as_the_takeover_leaves_it(path, copy):
    "A copy of a snapshot with what `_Copied` changes: other fd numbers, and no program the server's child."
    shutil.copy(path, copy)
    db = apsw.Connection(str(copy))
    try:
        db.execute("UPDATE pane_programs SET master_fd = NULL, parent = NULL")
        db.execute("UPDATE jobs SET fds = NULL, parent = NULL")
    finally:
        db.close()
    return copy


@pytest.mark.parametrize("off_by", [1, -2], ids=["newer", "older with no step"])
def test_a_snapshot_of_another_version_is_refused(pymux, tmp_path, off_by):
    path = tmp_path / "old.sqlite"
    snapshot.save(pymux, path)
    db = apsw.Connection(str(path))
    db.execute("PRAGMA user_version = %d" % (snapshot.SNAPSHOT_VERSION + off_by))
    db.close()

    with pytest.raises(snapshot.SnapshotError, match="version"):
        snapshot.load(Pymux(), path)


def _shape(db: apsw.Connection) -> dict[str, list[tuple]]:
    "Each table's columns, as sqlite reads them: name, type, not null, default, key."
    names = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")]
    return {name: [tuple(column)[1:] for column in db.execute("PRAGMA table_info(%s)" % name)] for name in names}


def _shape_of_a_fresh_snapshot() -> dict[str, list[tuple]]:
    db = apsw.Connection(":memory:")
    db.execute(tables.SCHEMA)
    return _shape(db)


def test_the_table_list_is_the_schema_in_order():
    "`LIVE_TABLES` is cut from it by name. Lillecarl/pymux#555."
    db = apsw.Connection(":memory:")
    db.execute(tables.SCHEMA)
    made = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY rowid")]
    assert list(tables.TABLES) == made


def test_a_version_30_snapshot_held_status_fps(pymux, tmp_path):
    "The server before it went had a status-fps option, saved on."
    path = tmp_path / "v30.sqlite"
    snapshot.save(pymux, path)
    db = apsw.Connection(str(path))
    db.execute("INSERT OR REPLACE INTO options (name, value) VALUES ('status-fps', 1)")
    db.execute("PRAGMA user_version = 30")
    db.close()

    loaded = Pymux()
    snapshot.load(loaded, path)
    assert not hasattr(loaded, "status_fps")


@pytest.mark.parametrize("pty", [True, False], ids=["on a terminal", "on pipes"])
async def test_a_running_job_this_server_forked_is_carried_by_its_fds(pymux, tmp_path, pty):
    "An exec keeps it a child of the server, on the fds the snapshot names. Lillecarl/pymux#736."
    async with pymux.running():
        job = pymux.jobs.submit("exec sleep 60", pty=pty)
        pymux.tasks.start_soon(pymux.jobs.supervise, job)
        await once(lambda: job.pid is not None, 10.0, "the job never started")
        try:
            path = tmp_path / "carried.sqlite"
            snapshot.save(pymux, path)
            assert _rows(path, "SELECT pid, fds, parent FROM jobs") == [(job.pid, json.dumps(snapshot._fds_of(job)), 1)]
            assert len(snapshot._fds_of(job)) == (1 if pty else 2)
        finally:
            pymux.jobs.kill(job)


async def test_a_job_that_ends_between_two_writes_is_written_ended(pymux, tmp_path):
    """
    An upgrade writes once, checks the new build, and writes again in
    the pause. A job that ended in between still read as running, and
    the new image adopted fds the exec no longer carried. Lillecarl/pymux#736.
    """
    async with pymux.running():
        go = tmp_path / "go"
        job = pymux.jobs.submit("while [ ! -e %s ]; do sleep 0.02; done; exit 3" % go)
        pymux.tasks.start_soon(pymux.jobs.supervise, job)
        await once(lambda: job.pid is not None, 10.0, "the job never started")
        written = snapshot.Snapshot(tmp_path / "twice.sqlite")
        try:
            written.write(pymux)
            go.touch()
            with anyio.fail_after(10):
                await pymux.jobs.wait(job)
            written.write(pymux)
            written.finish()
        except BaseException:
            written.abandon()
            raise
    assert _rows(tmp_path / "twice.sqlite", "SELECT status, returncode, fds FROM jobs") == [("done", 3, None)]


async def test_a_snapshot_is_refused_while_a_command_waits(pymux, tmp_path):
    "The exec closes the waiting connection, and the script behind it would fail."
    channel = WaitChannel()
    channel.waiters.append(anyio.Event())
    pymux.wait_channels["done"] = channel
    with pytest.raises(snapshot.SnapshotError, match="channel done"):
        snapshot.save(pymux, tmp_path / "waiting.sqlite")
