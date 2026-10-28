"""
A recording as timed frames, the way `<pymux-pane>` draws a live pane.

The element draws rows of styled runs and holds no terminal of its own,
so a recording's bytes go through a pyte screen here, and `PaneView`
says after each piece what changed -- the same welcome and frames a
live viewer gets, each with the time it came at. Any player that can
hand the element a message plays it. Lillecarl/pymux#610.

**The idle limit is applied here, and the header says which one.** An
asciicast keeps the true times and leaves the limit to the player, so
the limit can change later. A replay is already drawn, so changing the
limit means exporting again, and every player then plays it the same.
"""

from __future__ import annotations

import base64
import codecs
from typing import Any

from pyte.screen import Screen
from pyte.streams import Stream

from pymux.web.protocol import PaneView

__all__ = ["FORMAT", "limit_idle_time", "replay_of"]

#: What the file says it is, so a player can refuse anything else.
FORMAT = "pymux-replay"


def limit_idle_time(times: list[float], limit: float | None) -> list[float]:
    """
    `times` with every pause longer than `limit` shortened to it.

    asciinema's `limit_idle_time`: the time cut from one pause is cut
    from every event after it too.
    """
    if limit is None:
        return list(times)
    cut = 0.0
    previous = 0.0
    answer = []
    for time in times:
        cut += max(0.0, time - previous - limit)
        previous = time
        answer.append(time - cut)
    return answer


def replay_of(recording: dict[str, Any], idle_time_limit: float | None = None) -> dict[str, Any]:
    """
    The replay of `recording`, which is what a `.session.json` holds.

    `idle_time_limit` replaces the one the recording carries, if any.
    Keys are not in it: a replay shows what was drawn.
    """
    if idle_time_limit is None:
        idle_time_limit = recording.get("idle_time_limit")
    lines, columns = recording["lines"], recording["columns"]
    # Nobody hears the answers: the recording holds the ones given.
    screen = Screen(lines, columns, write_process_input=lambda answer: None)
    stream = Stream(screen)
    view = PaneView()
    # A read ends where it ends, often inside a character.
    decoder = codecs.getincrementaldecoder("utf-8")("replace")

    times = [0.0]
    messages = [view.welcome(screen, 0, writable=False)]
    for revision, (seconds, kind, data) in enumerate(recording["events"], start=1):
        if kind == "out":
            stream.feed(decoder.decode(base64.b64decode(data)))
        elif kind == "size":
            columns, lines = (int(number) for number in data.split("x"))
            screen.resize(lines, columns)
        else:
            continue
        frame = view.frame(screen, revision)
        if frame is not None:
            times.append(seconds)
            messages.append(frame)

    answer: dict[str, Any] = {
        "format": FORMAT,
        "version": 1,
        "lines": recording["lines"],
        "columns": recording["columns"],
    }
    for key in ("term", "timestamp"):
        if recording.get(key) is not None:
            answer[key] = recording[key]
    if idle_time_limit is not None:
        answer["idle_time_limit"] = idle_time_limit
    answer["events"] = [
        [round(time, 6), message] for time, message in zip(limit_idle_time(times, idle_time_limit), messages)
    ]
    return answer
