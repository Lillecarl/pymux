"""
The live store says what the objects say, after every step that changed
them, and holds no screen. Step 5 of Lillecarl/pymux#399.

Each check compares the live tables with what the snapshot's writer
makes of the objects at that moment. Nothing here flushes the store by
hand: a change the store was not told of stays behind, and fails here.
"""

from __future__ import annotations

import sqlite3

import anyio
from prompt_toolkit.application.current import set_app
from test_a_session_on_a_pyte_screen import attached, pymux, said, settled, shows  # noqa: F401 -- a fixture

from pymux import live, snapshot

#: Each one changes a table the live store holds.
COMMANDS = (
    "split-window -v",
    "resize-pane -U 3",
    "rename-window built",
    "set-option status-left left",
    "set-environment -g WHERE here",
    "set-window-option -g frame-rate 10",
    "new-window",
    "set-window-option strip on",
    "split-window -h",
    "select-pane -L",
    "unlink-window",
    "new-session -d -s second",
    "set-buffer -b kept text",
    "bind-key -n F5 display-message five",
    "set-hook pane-died 'display-message died'",
    "display-message told",
    "kill-pane",
)


def _tables(db: sqlite3.Connection) -> dict[str, list[tuple]]:
    return {
        table: sorted(db.execute("SELECT * FROM %s" % table).fetchall(), key=repr) for table in snapshot.WHOLE_TABLES
    }


def _now(pymux) -> dict[str, list[tuple]]:
    "What the writer makes of the objects as they are."
    scratch = sqlite3.connect(":memory:")
    scratch.executescript(snapshot.LIVE_SCHEMA)
    snapshot.write_tables(pymux, scratch)
    return _tables(scratch)


async def _caught_up(pymux, what: str) -> None:
    "The live tables come to equal the objects, without anybody asking."
    deadline = anyio.current_time() + 10 * live.WRITE_AFTER
    while True:
        now = _now(pymux)
        behind = [table for table in snapshot.WHOLE_TABLES if _tables(pymux.live.db)[table] != now[table]]
        if not behind:
            return
        assert anyio.current_time() < deadline, "after %s the live store is behind in %s" % (what, behind)
        await anyio.sleep(0.02)


def test_the_live_store_holds_no_screen_and_no_job():
    db = sqlite3.connect(":memory:")
    db.executescript(snapshot.LIVE_SCHEMA)
    held = {name for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert held == set(snapshot.WHOLE_TABLES)
    assert not held & {"pane_programs", "screen_appearances", "screen_rows", "jobs", "job_tags", "job_output"}


async def test_the_live_store_follows_every_kind_of_change(pymux, tmp_path):
    async with pymux.running(), attached(pymux) as session:
        await shows(session, "$")
        connection = pymux.connections[-1]
        # The web viewer sends no id; a terminal client sends one in the
        # attach itself, before anything is written. This is not a path.
        connection.client_id = "here"
        pymux.live.mark()
        await _caught_up(pymux, "the attach")

        with set_app(connection.client_state.app):
            for command in COMMANDS:
                pymux.handle_command(command)
                await settled(session, 0.05)
                await _caught_up(pymux, command)

        # A pane says where it is, with no command and no key.
        shell = pymux.arrangement.get_active_pane()
        shell.process.write_input("printf '\\033]7;file://far/srv\\a'\r")
        with anyio.fail_after(10):
            while shell.current_directory != "/srv":
                await anyio.sleep(0.02)
        await _caught_up(pymux, "a pane's OSC 7")

        # A key changes the client: the prefix is held.
        said(session, type="input", keys="C-b")
        with anyio.fail_after(10):
            while not connection.client_state.has_prefix:
                await anyio.sleep(0.02)
        await _caught_up(pymux, "the prefix key")
        # And let go of: a held prefix draws its keys later, after the
        # server here has gone.
        said(session, type="input", keys="Escape")
        with anyio.fail_after(10):
            while connection.client_state.has_prefix:
                await anyio.sleep(0.02)
        await _caught_up(pymux, "letting go of the prefix")
