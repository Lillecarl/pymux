"""
`action-log`: what a person does goes to `actions.db`, and nothing a
pane shows or a person types into one.

The rules this judges: nothing is written while the option is off; a
command is written with what started it and the client, session,
window and pane it happened at; send-keys text and a buffer's value
are their length; a command that fails says why; the outermost origin
wins; the upkeep keeps the newest rows; a file of another schema is
moved aside.
"""

from __future__ import annotations

import json

import apsw
import pytest
from prompt_toolkit.application.current import set_app
from session import create_session

from pymux.actions import SCHEMA_VERSION, Kind, Origin, _upkeep, note, started_by


def _rows(path) -> list[dict]:
    db = apsw.Connection(str(path))
    try:
        cursor = db.execute("SELECT * FROM actions ORDER BY id")
        names = [column[0] for column in cursor.description]
        return [dict(zip(names, row)) for row in cursor]
    finally:
        db.close()


async def _logged(pymux, state, path, *commands, origin=None) -> list[dict]:
    "Turn the log on at `path`, run `commands` as `state`'s client, and read what it wrote."
    pymux.actions.path = path
    pymux.action_log = True
    with set_app(state.app):
        for command in commands:
            if origin is None:
                pymux.handle_command(command)
            else:
                with started_by(*origin):
                    pymux.handle_command(command)
    await pymux.actions.settled()
    return _rows(path)


async def test_nothing_is_written_while_it_is_off(tmp_path):
    async with create_session() as (pymux, state):
        pymux.actions.path = tmp_path / "actions.db"
        with set_app(state.app):
            pymux.handle_command("rename-window off")
        await pymux.actions.settled()
        assert pymux.actions.recorded == 0
        assert not pymux.actions.path.exists()


async def test_a_command_is_written_with_where_and_how(tmp_path):
    async with create_session() as (pymux, state):
        rows = await _logged(
            pymux, state, tmp_path / "actions.db", "rename-window built", origin=(Origin.KEY, "prefix", ",")
        )
        window = state.session.arrangement.get_active_window()
        assert len(rows) == 1
        row = rows[0]
        assert row["kind"] == Kind.COMMAND
        assert row["name"] == "rename-window"
        assert json.loads(row["arguments"])["name"] == "built"
        assert (row["origin"], row["key_table"], row["key"]) == ("key", "prefix", ",")
        assert row["session"] == state.session.name
        assert row["window_id"] == window.window_id
        assert row["pane_id"] == window.active_pane.pane_id
        assert row["error"] is None


async def test_typed_text_and_buffers_are_their_length(tmp_path):
    async with create_session() as (pymux, state):
        rows = await _logged(
            pymux,
            state,
            tmp_path / "actions.db",
            "send-keys -l 'secret text'",
            "set-buffer -b kept 'a secret'",
            "send-keys -X cancel",
        )
        by_name = [(row["name"], json.loads(row["arguments"])) for row in rows]
        assert by_name[0] == ("send-keys", {**by_name[0][1], "keys": {"length": len("secret text")}})
        assert by_name[1] == ("set-buffer", {"buffer_name": "kept", "value": {"length": len("a secret")}})
        assert by_name[2][1]["keys"] == ["cancel"]
        assert "secret" not in json.dumps(by_name)


async def test_a_failed_command_says_why(tmp_path):
    async with create_session() as (pymux, state):
        rows = await _logged(
            pymux,
            state,
            tmp_path / "actions.db",
            "no-such-command a b",
            "set-buffer -Z 'a secret'",
            "kill-pane -t %999",
        )
        assert [(row["name"], bool(row["error"])) for row in rows] == [
            ("no-such-command", True),
            ("set-buffer", True),
            ("kill-pane", True),
        ]
        assert rows[0]["arguments"] == '["a", "b"]'
        # It did not parse, so nothing says which word was the value.
        assert rows[1]["arguments"] is None
        assert json.loads(rows[2]["arguments"])["target_pane"] == "%999"


async def test_the_outermost_origin_wins(tmp_path):
    async with create_session() as (pymux, state):
        pymux.actions.path = tmp_path / "actions.db"
        pymux.action_log = True
        with set_app(state.app), started_by(Origin.MOUSE), started_by(Origin.MENU):
            note(pymux, Kind.CLICK, "select-pane")
        await pymux.actions.settled()
        assert [row["origin"] for row in _rows(pymux.actions.path)] == ["mouse"]


async def test_a_command_over_the_socket_has_no_client(tmp_path):
    async with create_session() as (pymux, state):
        pymux.actions.path = tmp_path / "actions.db"
        pymux.action_log = True
        with set_app(state.app), started_by(Origin.SOCKET):
            state.temporary = True
            try:
                pymux.handle_command("rename-window over")
            finally:
                state.temporary = False
        await pymux.actions.settled()
        row = _rows(pymux.actions.path)[0]
        assert (row["origin"], row["client"], row["session"]) == ("socket", None, None)


async def test_the_upkeep_keeps_the_newest(tmp_path):
    async with create_session() as (pymux, state):
        path = tmp_path / "actions.db"
        await _logged(pymux, state, path, *("rename-window w%d" % n for n in range(5)))
    db = apsw.Connection(str(path))
    try:
        _upkeep(db, 2)
        names = [json.loads(arguments)["name"] for (arguments,) in db.execute("SELECT arguments FROM actions")]
    finally:
        db.close()
    assert names == ["w3", "w4"]


async def test_a_file_of_another_schema_is_moved_aside(tmp_path):
    path = tmp_path / "actions.db"
    old = apsw.Connection(str(path))
    old.execute("CREATE TABLE actions(x); PRAGMA user_version = %d" % (SCHEMA_VERSION + 1))
    old.close()
    async with create_session() as (pymux, state):
        rows = await _logged(pymux, state, path, "rename-window new")
    assert [row["name"] for row in rows] == ["rename-window"]
    assert (tmp_path / ("actions.v%d.db" % (SCHEMA_VERSION + 1))).exists()


@pytest.mark.parametrize("enabled", [True, False])
async def test_the_option_turns_it_on_and_off(tmp_path, enabled):
    async with create_session() as (pymux, state):
        pymux.actions.path = tmp_path / "actions.db"
        with set_app(state.app):
            pymux.handle_command("set-option action-log %s" % ("on" if enabled else "off"))
            pymux.handle_command("rename-window toggled")
        await pymux.actions.settled()
        names = [row["name"] for row in _rows(pymux.actions.path)] if enabled else []
        # The command that turns it on is the first it writes.
        assert names == (["set-option", "rename-window"] if enabled else [])
        assert pymux.actions.path.exists() is enabled
