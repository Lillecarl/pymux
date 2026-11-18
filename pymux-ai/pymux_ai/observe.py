"""
One look at a pane, for a model: its text, a picture, and the jobs that
still run where it works.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from libpymux import CommandError, Pane

if TYPE_CHECKING:
    from libpymux import Server
    from libpymux.operations import JobRecord
    from pyte.program_status import Report

    from pymux.ids import PaneId
    from pymux.plugin_api import Host

__all__ = ["CELL", "FIT", "HISTORY_LINES", "Observation", "observe"]

#: How far back the text reaches: the screen and some of what scrolled
#: off it, so the agent's last message is whole although the screen
#: shows only its end.
HISTORY_LINES: Final = 200

#: Every model reads plain text exactly at this cell, and a 100x30 pane
#: is about 400 image tokens. pymux-mcp's README has the measurement.
CELL: Final = (7, 14)

#: The longest edge a model reads without scaling it down.
FIT: Final = 1568

#: What pymux answers when no font is there to draw with.
NO_FONT: Final = "no font to draw with"


@dataclass(frozen=True, slots=True)
class Observation:
    "One look at a pane, after a program in it reported `report`."

    pane: str
    report: Report
    directory: str | None
    text: str
    #: None where the server finds no font: the text says the words.
    png: bytes | None
    #: Jobs that run under `directory`: work the agent may wait on.
    jobs: tuple[JobRecord, ...]


async def observe(host: Host, pane_id: PaneId, report: Report) -> Observation:
    server = host.server
    target = Pane(server, {"pane_id": "%%%d" % pane_id})
    pane = host.pane(pane_id)
    directory = pane.current_path() if pane is not None else None
    text = await target.capture(-HISTORY_LINES, None)
    return Observation(target.id, report, directory, text, await _picture(target), await _jobs_under(server, directory))


async def _picture(pane: Pane) -> bytes | None:
    """
    The pane drawn in the server's own font. Not pymux-mcp's bundled
    faces: a plugin that took pymux-mcp would put its command in every
    pymux it is installed into. A glyph the font lacks draws as a box,
    and the text beside the picture still says it.
    """
    try:
        return await pane.capture_png(cell=CELL, fit=FIT)
    except CommandError as error:
        if NO_FONT not in str(error):
            raise
        return None


async def _jobs_under(server: Server, directory: str | None) -> tuple[JobRecord, ...]:
    if directory is None:
        return ()
    return tuple(
        job.record
        for job in await server.jobs()
        if job.record.running and job.record.directory is not None and _under(job.record.directory, directory)
    )


def _under(path: str, directory: str) -> bool:
    return os.path.commonpath([path, directory]) == directory
