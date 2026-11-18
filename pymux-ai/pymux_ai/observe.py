"""
One look at a pane, for a model: its text, a picture, and the jobs that
still run where it works.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

from libpymux import Pane
from pymux_mcp.api import capture

if TYPE_CHECKING:
    from libpymux import Server
    from libpymux.operations import JobRecord
    from pyte.program_status import Report

    from pymux.ids import PaneId
    from pymux.plugin_api import Host

__all__ = ["HISTORY_LINES", "Observation", "observe"]

#: How far back the text reaches: the screen and some of what scrolled
#: off it, so the agent's last message is whole although the screen
#: shows only its end.
HISTORY_LINES = 200


@dataclass(frozen=True, slots=True)
class Observation:
    "One look at a pane, after a program in it reported `report`."

    pane: str
    report: Report
    directory: str | None
    text: str
    png: bytes
    #: Jobs that run under `directory`: work the agent may wait on.
    jobs: tuple[JobRecord, ...]


async def observe(host: Host, pane_id: PaneId, report: Report) -> Observation:
    server = host.server
    target = "%%%d" % pane_id
    pane = host.pane(pane_id)
    directory = pane.current_path() if pane is not None else None
    text = await Pane(server, {"pane_id": target}).capture(-HISTORY_LINES, None)
    picture = await capture(server, target, format="png")
    assert not isinstance(picture, str)
    return Observation(target, report, directory, text, picture.data, await _jobs_under(server, directory))


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
