"""
`record-session` records a session through a client with no terminal.

The cast is judged by asciinema's own reader, `asciinema convert -f txt`,
which plays it on its own virtual terminal and prints the screen it ends
on: a reader of ours would agree with our writer. Lillecarl/pyterm#1064.
"""

from __future__ import annotations

import json
import os
import subprocess

import pytest
from prompt_toolkit.data_structures import Size
from ptyhost.asciicast import asciicast
from session import once, over_connection
from test_record_client import replayed

from pymux.answer import Answer
from pymux.commands import call_command_handler
from pymux.commands.type_text import typed
from pymux.connection.recorder import recorder_of
from pymux.keys.typing_pace import Pace, delays

#: What a cast must never hold. A player writes the cast to the viewer's
#: terminal, which would answer a query into the viewer's shell; OSC 52
#: would set their clipboard; and the recorder's own marks are not the demo.
NEVER = {
    "a device attributes query": "\x1b[c",
    "a secondary device attributes query": "\x1b[>c",
    "a cursor position query": "\x1b[6n",
    "a foreground colour query": "\x1b]10;?",
    "a background colour query": "\x1b]11;?",
    "a kitty keyboard query": "\x1b[?u",
    "a clipboard write": "\x1b]52;",
    "the recording mark": "REC ",
    "the recording message": "Recorded",
}


async def run(pymux, *argv: str) -> list[str]:
    "Run a command the way the socket does; what it printed."
    pymux.answer = answer = Answer()
    try:
        waiting = call_command_handler(argv[0], pymux, list(argv[1:]))
        if waiting is not None:
            await waiting
    finally:
        pymux.answer = None
    assert answer.errors == []
    return answer.output


async def type_into(pymux, text: str) -> None:
    pane = pymux.arrangement.get_active_window().active_pane

    async def no_wait(seconds: float) -> None:
        pass

    await typed(pymux, pane, text, delays(text, Pace()), no_wait)


def plays_as(tmp_path, cast: str) -> str:
    "The screen asciinema shows at the end of `cast`."
    (tmp_path / "demo.cast").write_text(cast)
    subprocess.run(
        [
            os.environ["PYMUX_ASCIINEMA"],
            "convert",
            "-f",
            "txt",
            str(tmp_path / "demo.cast"),
            str(tmp_path / "demo.txt"),
        ],
        check=True,
    )
    return (tmp_path / "demo.txt").read_text()


@pytest.fixture
def state_home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    return tmp_path / "state" / "pymux" / "recordings"


async def test_a_session_nobody_watches_records_to_a_cast(tmp_path, state_home):
    async with over_connection() as session:
        pymux = session.pymux
        # What a person's first attach did long ago on a real server: the
        # first window, with the shell.
        await pymux.startup()
        assert await run(pymux, "record-session", "-x", "60", "-y", "12", "demo") == ["demo"]
        recorder = recorder_of(pymux, pymux.current_session)
        assert recorder is not None and recorder.holds_size
        assert pymux.clients == [] and pymux.latest_client() is None
        await type_into(pymux, "echo typed-by-a-person\n")
        recording = recorder.client.recording

        def shown() -> list[str]:
            return replayed({"lines": recording.lines, "columns": recording.columns, "events": recording.events})

        await once(
            lambda: any("typed-by-a-person" in row for row in shown()),
            10.0,
            lambda: "the recording never drew the echo; it shows:\n%s" % "\n".join(shown()),
        )
        (written,) = await run(pymux, "record-session")
        assert recorder_of(pymux, pymux.current_session) is None

    recording = json.loads((state_home / "demo.session.json").read_text())
    assert (recording["columns"], recording["lines"]) == (60, 12)
    cast = asciicast(recording)
    header = json.loads(cast.splitlines()[0])
    assert header["idle_time_limit"] == 1.5
    for what, sequence in NEVER.items():
        assert sequence not in cast, "the cast holds %s" % what
    assert "typed-by-a-person" in plays_as(tmp_path, cast)
    assert written.endswith("demo.bin")


def pane_size(pymux) -> tuple[int, int]:
    screen = pymux.arrangement.get_active_window().active_pane.screen
    return screen.lines, screen.columns


async def test_a_person_who_attaches_mid_recording_moves_no_pane(state_home):
    "The recorder held the size first, so a bigger and a smaller terminal both leave it."
    async with over_connection() as session:
        pymux = session.pymux
        await pymux.startup()
        await run(pymux, "record-session", "-x", "70", "-y", "20", "demo")
        await once(lambda: pane_size(pymux)[1] == 70, 10.0, lambda: "the plane is %r" % (pane_size(pymux),))
        recorded = pane_size(pymux)
        for size in (Size(rows=40, columns=120), Size(rows=10, columns=40)):
            person, _ = await session.attach("a person", size)
            await once(lambda person=person: person.app.output.get_size() == size, 10.0, "the person never sized")
            pymux.invalidate()
            await session.detach(person)
            assert pane_size(pymux) == recorded
        recorder = recorder_of(pymux, pymux.current_session)
        assert [event for event in recorder.client.recording.events if event[1] == "size"] == []
        await run(pymux, "record-session")


async def test_a_recorder_takes_the_size_of_the_person_watching(state_home):
    "And stays out of the vote, so it moves none of their panes."
    async with over_connection() as session:
        pymux = session.pymux
        person, _ = await session.attach("a person", Size(rows=30, columns=100))
        await once(lambda: pane_size(pymux)[1] == 100, 10.0, lambda: "the plane is %r" % (pane_size(pymux),))
        before = pane_size(pymux)
        await run(pymux, "record-session", "-x", "50", "-y", "10", "demo")
        recorder = recorder_of(pymux, pymux.current_session)
        assert not recorder.holds_size and recorder.ignore_size
        pymux.invalidate()
        await once(lambda: len(recorder.client.recording.events) > 1, 10.0, "the recorder drew nothing")
        assert pane_size(pymux) == before
        await run(pymux, "record-session")
        await session.detach(person)
