"""
A session view's notifications: the page shows them, and a click on one
shows its pane. Lillecarl/pyterm#1073.

**The session screen answers as kitty does**, and pymux needs nothing
new. A client whose terminal answers OSC 99's `p=?` gets each
notification as OSC 99 under an identifier that names the pane, a close
once the pane is seen, and shows the pane when the terminal reports a
click on that identifier (Lillecarl/pyterm#781). This turns the OSC 99
the screen hears into messages for the page, and the page's click into
the report.
"""

from __future__ import annotations

import base64
import binascii
from typing import Any, NamedTuple

from pyte.osc import Osc
from pyte.sequences.strings import osc

from pymux.notify.notifications import is_done, read_identifier, read_payload_type, read_urgency, split_payload

__all__ = [
    "NOTIFICATION",
    "NOTIFICATION_CLICK",
    "NOTIFICATION_CLOSE",
    "Notices",
    "Taken",
    "click_report",
]

#: To the page: show one, under `id`, with `title`, `body` and `urgency`.
NOTIFICATION = "notification"
#: To the page: take the one under `id` down, its pane was seen.
NOTIFICATION_CLOSE = "notification-close"
#: From the page: a person clicked the one under `id`.
NOTIFICATION_CLICK = "notification-click"

#: What the screen says it shows, as kitty 0.49 answers `p=?`: a click
#: is reported, and a notification can be closed (`can_close`).
WHAT_IT_SHOWS = "a=focus,report:o=always:p=title,body,?,close:u=0,1,2"

#: The most notifications held half sent: a program that never finishes
#: one must not grow the table.
MOST_PENDING = 64


class Taken(NamedTuple):
    "What one OSC 99 the server sent asks for: an answer to write back, and a message for the page."

    answer: str | None = None
    message: dict[str, Any] | None = None


class Notices:
    "The notifications the server sends one session screen, put together chunk by chunk."

    def __init__(self) -> None:
        #: Title and body so far, by identifier.
        self._pending: dict[str | None, tuple[str, str]] = {}

    def take(self, param: str) -> Taken:
        "One OSC 99 payload, as it came after `99;`."
        metadata, _semicolon, text = split_payload(param)
        identifier = read_identifier(metadata)
        kind = read_payload_type(metadata)
        if kind == "?":
            return Taken(answer=osc(Osc.NOTIFICATION, "i=%s:p=?;%s" % (identifier or "0", WHAT_IT_SHOWS)))
        if kind == "close":
            return Taken(message={"type": NOTIFICATION_CLOSE, "id": identifier} if identifier else None)
        if kind not in ("title", "body"):
            # An icon, buttons or `alive`: nothing the page shows.
            return Taken()
        if "e=1" in metadata.split(":"):
            try:
                text = base64.b64decode(text, validate=True).decode("utf-8", errors="replace")
            except binascii.Error:
                return Taken()
        title, body = self._pending.pop(identifier, ("", ""))
        if kind == "title":
            title += text
        else:
            body += text
        if not is_done(metadata):
            self._pending[identifier] = (title, body)
            while len(self._pending) > MOST_PENDING:
                self._pending.pop(next(iter(self._pending)))
            return Taken()
        return Taken(
            message={
                "type": NOTIFICATION,
                "id": identifier or "",
                "title": title,
                "body": body,
                "urgency": read_urgency(metadata),
            }
        )


def click_report(identifier: object) -> str | None:
    "What a terminal says when a person clicks the notification `identifier`, or None for no identifier."
    if not isinstance(identifier, str) or read_identifier("i=" + identifier) != identifier:
        return None
    return osc(Osc.NOTIFICATION, "i=%s;" % identifier)
