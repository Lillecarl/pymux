"""
A recording of one pane, made where its program runs.

`record-pane` starts one, and the same command stops it. It keeps what
the program wrote and everything written to it, keys and the answers
pymux gave its queries alike, and writes the three files `ptyhost-record`
writes, so the same checks replay it.

`ptyhost-record` runs the program on a pty of its own, with a plain
`TERM` and no `COLORTERM`. Claude Code drew no colour at all under it, so
the fault it was asked to catch never showed. Here the program keeps the
environment of its pane.
"""

from __future__ import annotations

import base64
import os
import time
from datetime import datetime
from pathlib import Path

from ptyhost.process import Process
from ptyhost.record import write_recording

__all__ = ["Recording", "recordings_directory"]


def recordings_directory() -> Path:
    "Where recordings go: beside the server log, in the user's state directory."
    state = os.environ.get("XDG_STATE_HOME") or "~/.local/state"
    return Path(os.path.expanduser(state)) / "pymux" / "recordings"


class Recording:
    """
    One recording in progress, of the program `process` runs.

    `lines` and `columns` are the size of the pane when it started. A
    replay starts the screen at that size.
    """

    def __init__(self, process: Process, name: str, lines: int, columns: int, term: str) -> None:
        self.process = process
        self.name = name or "%s-%s" % (
            (process.get_name() or "pane").rsplit("/", 1)[-1],
            datetime.now().strftime("%Y%m%d-%H%M%S"),
        )
        self.lines = lines
        self.columns = columns
        self.term = term
        self.events: list = []
        self._started = time.monotonic()
        process.tap = self._tap

    def _tap(self, direction: str, text: str) -> None:
        self.events.append(
            [
                round(time.monotonic() - self._started, 6),
                direction,
                base64.b64encode(text.encode("utf-8", "surrogateescape")).decode("ascii"),
            ]
        )

    def finish(self, directory: Path | None = None) -> Path:
        "Stop listening, and write the files. Gives back the `.bin`."
        if self.process.tap == self._tap:
            self.process.tap = None
        return write_recording(
            directory or recordings_directory(), self.name, self.lines, self.columns, self.term, self.events
        )
