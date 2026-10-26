"""
The rows of the notification hub: a heading for each window that sent
something, newest window first, and its messages under it.

A list of records in time order said what arrived and not where to go:
the same window came back on every other row, and no row named the
window or the program. Lillecarl/pymux#768.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.arrangement import Pane, Window
    from pymux.main import Pymux
    from pymux.notifications import Notification
    from pymux.session import Session

__all__ = ["Heading", "Message", "age", "heading_text", "rows_of"]


@dataclass(frozen=True, slots=True)
class Heading:
    "One window's line. `window` is None for records whose pane is gone."

    session: Session | None
    window: Window | None
    #: The pane of the newest record, which Enter goes to.
    pane: Pane | None
    newest: Notification
    unread: bool


@dataclass(frozen=True, slots=True)
class Message:
    "One message, or a run of the same one, newest kept."

    record: Notification
    count: int
    unread: bool
    heading: Heading


def age(seconds: float) -> str:
    "How long ago, in the largest unit that fits: 40s, 3m, 2h, 5d."
    seconds = max(0, int(seconds))
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return "%d%s" % (seconds // size, unit)
    return "%ds" % seconds


def _unread(window: Window | None, record: Notification) -> bool:
    "Unread while the window's mark is up and nobody looked since it came."
    return window is not None and window.notified and record.at > window.seen_at


def heading_text(heading: Heading) -> str:
    "`session:index name`, and the program when the name is something else."
    if heading.window is None or heading.session is None:
        return "(pane gone)"
    text = "%s:%s %s" % (heading.session.name, heading.window.index, heading.window.name)
    if heading.pane is not None:
        # The backend names the program by its path: a store path is
        # most of the row.
        program = os.path.basename(heading.pane.process.get_name() or "")
        if program and program != heading.window.name:
            text += " " + program
    return text


def rows_of(pymux: Pymux, records: list[Notification], text: str = "") -> list[Heading | Message]:
    """
    The hub's rows for `records`, newest first.

    A search keeps a whole window when its heading matches, and
    otherwise the messages that do; a window with none left goes.
    """
    groups: dict[int | None, list[Notification]] = {}
    places: dict[int | None, tuple] = {}
    for record in reversed(records):
        found = pymux.window_of_pane(record.pane_id)
        key = None if found is None else id(found[1])
        if key not in groups:
            groups[key] = []
            places[key] = found if found is not None else (None, None, None)
        groups[key].append(record)

    rows: list[Heading | Message] = []
    for key, group in groups.items():
        session, window, pane = places[key]
        heading = Heading(
            session=session,
            window=window,
            pane=pane,
            newest=group[0],
            unread=any(_unread(window, record) for record in group),
        )
        messages: list[Message] = []
        for record in group:
            if messages and (messages[-1].record.title, messages[-1].record.body) == (record.title, record.body):
                last = messages[-1]
                messages[-1] = Message(last.record, last.count + 1, last.unread, heading)
            else:
                messages.append(Message(record, 1, _unread(window, record), heading))

        if text and text not in heading_text(heading).lower():
            messages = [
                message
                for message in messages
                if text in message.record.title.lower() or text in message.record.body.lower()
            ]
            if not messages:
                continue
        rows.append(heading)
        rows.extend(messages)
    return rows
