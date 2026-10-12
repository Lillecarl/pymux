"""
`record-client` records what pymux drew on one client's terminal.
Lillecarl/pymux#612.
"""

from __future__ import annotations

import base64
import json

from prompt_toolkit.application.current import set_app
from pyte.screen import Screen
from pyte.streams import Stream
from session import once
from test_a_session_on_a_pyte_screen import attached, pymux, rows_of, shows  # noqa: F401


def replayed(recording: dict) -> list[str]:
    "The rows a terminal shows after the recorded bytes, at the recorded sizes."
    screen = Screen(recording["lines"], recording["columns"], write_process_input=lambda answer: None)
    stream = Stream(screen)
    for _seconds, kind, data in recording["events"]:
        if kind == "out":
            stream.feed(base64.b64decode(data).decode())
        elif kind == "size":
            columns, lines = map(int, data.split("x"))
            screen.resize(lines, columns)
    top = screen.line_offset
    return [line.text for line in screen.page.text_lines(top, top + screen.lines - 1)]


async def recorded(pymux, tmp_path, monkeypatch, then=(), resize=False) -> tuple[dict, list[str]]:
    """
    Record a client through some typing, and a resize if asked; the
    recording, and what the client showed. `then` are commands to run
    after, each with the file it writes.

    A resize draws every cell again, so a recording that needed the
    whole screen at its start and lacked it passes with one.
    """
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    async with pymux.running(), attached(pymux) as session:
        await shows(session, "$")
        connection = pymux.connections[-1]
        with set_app(connection.client_state.app):
            pymux.handle_command("record-client -i 0.5 probe")
        assert connection.recording is not None
        await shows(session, "REC probe")

        if resize:
            session.resize(16, 70)
            await once(lambda: connection.size.columns == 70, 10.0, "the server never took the new size")
        said_keys = "echo recorded-$((6*7))\r"
        session.take(json.dumps({"type": "text", "text": said_keys}))
        await shows(session, "recorded-42")
        # The status line changes when the recording ends.
        shown = rows_of(session)[:-1]

        with set_app(connection.client_state.app):
            pymux.handle_command("record-client")
        assert connection.recording is None
        await shows(session, "Recorded")

        directory = tmp_path / "pymux" / "recordings"
        for command, writes in then:
            with set_app(connection.client_state.app):
                pymux.handle_command(command)
            await once(lambda writes=writes: (directory / writes).exists(), 10.0, "%s wrote nothing" % command)
    return json.loads((directory / "probe.session.json").read_text()), shown


async def test_a_replay_of_the_recording_shows_what_the_client_showed(pymux, tmp_path, monkeypatch):
    """
    The recording starts with a whole frame, so a terminal that saw only
    the recording ends up showing what the client did.
    """
    recording, shown = await recorded(pymux, tmp_path, monkeypatch)
    assert (recording["lines"], recording["columns"]) == (12, 60)
    assert recording["idle_time_limit"] == 0.5
    keys = "".join(base64.b64decode(event[2]).decode() for event in recording["events"] if event[1] == "in")
    assert "recorded-$((6*7))" in keys
    assert replayed(recording)[:-1] == shown


async def test_a_resize_is_recorded_and_replayed(pymux, tmp_path, monkeypatch):
    recording, shown = await recorded(pymux, tmp_path, monkeypatch, resize=True)
    assert [event[2] for event in recording["events"] if event[1] == "size"] == ["70x16"]
    assert replayed(recording)[:-1] == shown


async def test_a_client_that_detaches_writes_its_recording(pymux, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    async with pymux.running(), attached(pymux) as session:
        await shows(session, "$")
        connection = pymux.connections[-1]
        with set_app(connection.client_state.app):
            pymux.handle_command("record-client gone")
        await shows(session, "REC gone")
        connection.detach_and_close()
        await once(lambda: (tmp_path / "pymux" / "recordings" / "gone.bin").exists(), 10.0, "nothing was written")
