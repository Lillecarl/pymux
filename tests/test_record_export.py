"""
`record-export` turns a recording into a web replay or an asciicast.
Lillecarl/pymux#610.
"""

from __future__ import annotations

import base64
import json

from test_a_session_on_a_pyte_screen import pymux  # noqa: F401
from test_record_client import recorded

from pymux.web.replay import replay_of


async def test_the_web_replay_ends_on_what_the_client_showed(pymux, tmp_path, monkeypatch):
    recording, shown = await recorded(pymux, tmp_path, monkeypatch)
    replay = replay_of(recording)
    assert replay["format"] == "pymux-replay"
    welcome, *frames = (message for _time, message in replay["events"])
    assert welcome["type"] == "welcome"
    # The rows the frames leave, as the element would hold them.
    rows: dict[int, str] = {}
    for frame in frames:
        if frame.get("whole"):
            rows = {}
        for number, runs in frame["rows"].items():
            rows[int(number)] = "".join(text for _style, text in runs)
    drawn = [rows.get(number, "").rstrip() for number in range(len(shown))]
    assert drawn == [row.rstrip() for row in shown]


async def test_record_export_writes_both_formats(pymux, tmp_path, monkeypatch):
    exports = [
        ("record-export probe", "probe.web.json"),
        ("record-export -F asciicast --cast-version 2 -i 2 probe", "probe.cast"),
    ]
    await recorded(pymux, tmp_path, monkeypatch, then=exports)
    directory = tmp_path / "pymux" / "recordings"
    replay = json.loads((directory / "probe.web.json").read_text())
    assert replay["idle_time_limit"] == 0.5
    header = json.loads((directory / "probe.cast").read_text().splitlines()[0])
    assert (header["version"], header["width"], header["idle_time_limit"]) == (2, 60, 2.0)


def test_a_replay_cuts_every_pause_to_the_limit():
    "asciinema's own numbers, from `limit_idle_time` in its asciicast.rs."
    out = base64.b64encode(b"x").decode()
    recording = {"lines": 1, "columns": 10, "events": [[t, "out", out] for t in (0.0, 1.0, 3.5, 4.0, 7.5)]}
    times = [time for time, _message in replay_of(recording, idle_time_limit=2.0)["events"][1:]]
    assert times == [0.0, 1.0, 3.0, 3.5, 5.5]
    untouched = [time for time, _message in replay_of(recording)["events"][1:]]
    assert untouched == [0.0, 1.0, 3.5, 4.0, 7.5]
