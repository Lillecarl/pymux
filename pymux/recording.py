"""
Recordings made where the bytes pass: of one pane, or of one client.

`record-pane` keeps what a pane's program wrote and everything written
to it, keys and the answers pymux gave its queries alike. `record-client`
keeps what pymux drew on one client's terminal and the keys that client
sent: every pane, the borders, the status line, whatever floats. Both
write the three files `ptyhost-record` writes, so the same checks replay
them and `record-export` converts either. Lillecarl/pymux#609,
Lillecarl/pymux#612.

`ptyhost-record` runs the program on a pty of its own, with a plain
`TERM` and no `COLORTERM`. Claude Code drew no colour at all under it, so
the fault it was asked to catch never showed. Here the program keeps the
environment of its pane.
"""

from __future__ import annotations

import base64
import os
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from ptyhost.process import Process
from ptyhost.record import write_recording

__all__ = ["Recording", "recording_of_process", "recordings_directory"]


def recordings_directory() -> Path:
    "Where recordings go: beside the server log, in the user's state directory."
    state = os.environ.get("XDG_STATE_HOME") or "~/.local/state"
    return Path(os.path.expanduser(state)) / "pymux" / "recordings"


def a_name(what: str) -> str:
    "What a recording is called when nobody said: what it is of, and when."
    return "%s-%s" % (what, datetime.now().strftime("%Y%m%d-%H%M%S"))


class Recording:
    """
    One recording in progress.

    `lines` and `columns` are the size when it started, and a replay
    starts the screen at that size. `idle_time_limit` is the longest
    pause a player shows, kept for `record-export`. `stop` is what ends
    the listening, and whoever attached the recording sets it.
    """

    def __init__(self, name: str, lines: int, columns: int, term: str, idle_time_limit: float | None = None) -> None:
        self.name = name
        self.lines = lines
        self.columns = columns
        self.term = term
        self.idle_time_limit = idle_time_limit
        self.events: list = []
        self.stop: Callable[[], None] = lambda: None
        self._timestamp = int(time.time())
        self._started = time.monotonic()

    def hear(self, kind: str, text: str) -> None:
        'One event: "out" or "in" and text, or "size" and COLUMNSxLINES.'
        self.events.append(
            [
                round(time.monotonic() - self._started, 6),
                kind,
                text if kind == "size" else base64.b64encode(text.encode("utf-8", "surrogateescape")).decode("ascii"),
            ]
        )

    def finish(self, directory: Path | None = None) -> Path:
        "Stop listening, and write the files. Gives back the `.bin`."
        self.stop()
        return write_recording(
            directory or recordings_directory(),
            self.name,
            self.lines,
            self.columns,
            self.term,
            self.events,
            timestamp=self._timestamp,
            idle_time_limit=self.idle_time_limit,
        )


def recording_of_process(
    process: Process, name: str, lines: int, columns: int, term: str, idle_time_limit: float | None = None
) -> Recording:
    "A recording of what `process` writes and is sent, from now."
    recording = Recording(
        name or a_name((process.get_name() or "pane").rsplit("/", 1)[-1]), lines, columns, term, idle_time_limit
    )
    process.tap = recording.hear

    def stop() -> None:
        if process.tap == recording.hear:
            process.tap = None

    recording.stop = stop
    return recording
