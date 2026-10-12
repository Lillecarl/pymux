"""
`record-pane` records a pane where its program runs, and writes the files
`ptyhost-record` writes.
"""

from __future__ import annotations

import base64
import json

from prompt_toolkit.application.current import set_app
from session import once
from test_a_session_on_a_pyte_screen import attached, pymux, shows  # noqa: F401

#: The answer every pane gives "CSI c" starts so.
DEVICE_ATTRIBUTES_ANSWER = "\x1b[?"


async def test_a_recording_holds_both_directions_and_the_pane_it_ran_in(pymux, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    async with pymux.running(), attached(pymux) as session:
        await shows(session, "$")
        client_state = pymux.connections[-1].client_state
        (pane,) = pymux.panes_by_id.values()

        with set_app(client_state.app):
            pymux.handle_command("record-pane probe")
        assert pane.recording is not None
        await shows(session, "REC probe")
        size = (pane.screen.lines, pane.screen.columns)

        pane.process.write_input("printf '\\033[31mred\\033[m\\033[c'\r")
        await shows(session, "red")
        await once(
            lambda: any(
                event[1] == "in" and DEVICE_ATTRIBUTES_ANSWER in base64.b64decode(event[2]).decode()
                for event in pane.recording.events
            ),
            10.0,
            "the pane's answer was never recorded",
        )

        with set_app(client_state.app):
            pymux.handle_command("record-pane")
        assert pane.recording is None

    directory = tmp_path / "pymux" / "recordings"
    output = (directory / "probe.bin").read_bytes()
    assert b"\x1b[31mred" in output
    reads = json.loads((directory / "probe.reads.json").read_text())
    assert sum(reads["sizes"]) == len(output)
    recorded = json.loads((directory / "probe.session.json").read_text())
    assert (recorded["lines"], recorded["columns"]) == size
    assert recorded["term"] == pymux.default_terminal
    sent = "".join(base64.b64decode(event[2]).decode() for event in recorded["events"] if event[1] == "in")
    assert "printf" in sent and DEVICE_ATTRIBUTES_ANSWER in sent


async def test_a_pane_whose_program_ends_writes_its_recording(pymux, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    async with pymux.running(), attached(pymux) as session:
        await shows(session, "$")
        client_state = pymux.connections[-1].client_state
        with set_app(client_state.app):
            pymux.handle_command("split-window 'sleep 1000'")
            pymux.handle_command("record-pane ended")
        pane = max(pymux.panes_by_id.values(), key=lambda pane: pane.pane_id)
        await once(lambda: pane.recording is not None, 10.0, "the new pane was not recorded")

        pane.process.kill()
        await once(lambda: (tmp_path / "pymux" / "recordings" / "ended.bin").exists(), 10.0, "nothing was written")
