"""
Routing the answers to desktop notifications back to a pane.

A program in a pane asks for a notification with "OSC 99". It can ask
to be told when the user clicks the notification, when it closes, or
which of its notifications are still alive. The terminal of the user
answers with another OSC 99, and the answer names the notification by
the identifier that the program chose.

That answer arrives at the client, which serves every pane. Two panes
pick their identifiers without knowing about each other, so the same
one may well mean two different notifications. pymux therefore gives
every notification an identifier of its own on the way out, and puts
the identifier of the program back on the way in. kitty leaves room
for this on purpose: every answer carries the identifier.

An OSC 99 without an identifier is passed on untouched. The answer to
one carries "i=0", which names nothing, so there is nothing to route.
"""

from __future__ import annotations

import re
import time
from collections import OrderedDict
from typing import NamedTuple

from pymux.ids import PaneId

__all__ = ["Notification", "NotificationCenter", "NotificationRoutes"]

#: The characters that an identifier may hold. (The same set as kitty.)
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_+.-]{1,64}$")

#: How many notifications to remember. A program that never reads its
#: answers must not grow the table without end; the oldest goes first.
MAX_ROUTES = 256

#: How many notifications the hub keeps. Same rule as the routes: a
#: noisy pane must not grow the list without end.
MAX_NOTIFICATIONS = 256


class Urgency:
    """How insistently a notification asks, 0 low through 2 critical."""

    LOW = 0
    NORMAL = 1
    CRITICAL = 2


def read_urgency(metadata: str) -> int:
    "The value of the 'u' key of the metadata, or normal."
    for field in metadata.split(":"):
        key, sign, value = field.partition("=")
        if key == "u" and sign and value in ("0", "1", "2"):
            return int(value)
    return Urgency.NORMAL


def read_payload_type(metadata: str) -> str:
    "The value of the 'p' key of the metadata, or 'title'."
    for field in metadata.split(":"):
        key, sign, value = field.partition("=")
        if key == "p" and sign:
            return value
    return "title"


class Notification(NamedTuple):
    """One notification the hub shows, oldest stored first."""

    id: int
    title: str
    body: str
    urgency: int
    pane_id: PaneId | None
    at: float


class NotificationCenter:
    """
    The notifications the hub shows, across every source.

    A record is one finished notification: OSC 99 arrives in pieces --
    a title chunk, a body chunk, the same identifier -- so chunks with
    an identifier assemble here and a chunk without one stands alone.
    A finished record never changes, which is what makes the hub a
    list and not a stream.
    """

    def __init__(self, limit: int = MAX_NOTIFICATIONS) -> None:
        self.limit = limit
        self._next = 1
        self._records: list[Notification] = []
        self._pending: dict[tuple[PaneId | None, str], Notification] = {}

    def add(
        self,
        title: str,
        body: str = "",
        urgency: int = Urgency.NORMAL,
        pane_id: PaneId | None = None,
    ) -> Notification:
        "Record a finished notification."
        record = Notification(
            id=self._next,
            title=title,
            body=body,
            urgency=urgency,
            pane_id=pane_id,
            at=time.time(),
        )
        self._next += 1
        self._records.append(record)
        while len(self._records) > self.limit:
            del self._records[0]
        return record

    def add_osc99(self, pane_id: PaneId | None, param: str) -> Notification | None:
        """
        Record one OSC 99 chunk, assembling chunks with an identifier.

        A chunk whose metadata says it is done -- `d` missing or
        non-zero -- finishes its notification and returns it. A chunk
        that says more follows is held, and a chunk with no identifier
        to assemble by stands alone. Nothing is returned for a held
        chunk.
        """
        metadata, _semicolon, text = split_payload(param)
        identifier = read_identifier(metadata)
        payload_type = read_payload_type(metadata)
        urgency = read_urgency(metadata)
        done = True
        for field in metadata.split(":"):
            key, sign, value = field.partition("=")
            if key == "d" and sign:
                done = value != "0"

        if identifier is None:
            return self.add(
                title=text if payload_type == "title" else "",
                body=text if payload_type == "body" else "",
                urgency=urgency,
                pane_id=pane_id,
            )

        key = (pane_id, identifier)
        pending = self._pending.get(key)
        if pending is None:
            pending = Notification(
                id=self._next,
                title="",
                body="",
                urgency=urgency,
                pane_id=pane_id,
                at=time.time(),
            )
            self._next += 1
        title = pending.title
        body = pending.body
        if payload_type == "title":
            title = title + text if title else text
        elif payload_type == "body":
            body = body + text if body else text
        pending = pending._replace(title=title, body=body, urgency=urgency)
        if not done:
            self._pending[key] = pending
            # A chunk train that never finalizes must not hold its
            # seat for ever; the oldest waiting goes first.
            while len(self._pending) > self.limit:
                self._pending.pop(next(iter(self._pending)))
            return None
        self._pending.pop(key, None)
        self._records.append(pending)
        while len(self._records) > self.limit:
            del self._records[0]
        return pending

    def notifications(self) -> list[Notification]:
        "Every finished notification, oldest first."
        return list(self._records)


def split_payload(param: str) -> tuple[str, str, str]:
    """
    The payload of an OSC 99 is "<metadata> ; <text>". The text may
    hold a semicolon of its own, so only the first one counts.
    """
    metadata, semicolon, text = param.partition(";")
    return metadata, semicolon, text


def read_identifier(metadata: str) -> str | None:
    "The value of the 'i' key of the metadata, or None."
    for field in metadata.split(":"):
        key, sign, value = field.partition("=")
        if key == "i" and sign:
            return value if _IDENTIFIER_RE.match(value) else None
    return None


def replace_identifier(metadata: str, identifier: str) -> str:
    "The metadata with another value for its 'i' key."
    fields = metadata.split(":")
    for index, field in enumerate(fields):
        key, sign, _value = field.partition("=")
        if key == "i" and sign:
            fields[index] = "i=" + identifier
    return ":".join(fields)


class NotificationRoutes:
    """
    Which pane a notification belongs to.

    A notification keeps one identifier for its whole life, because a
    program sends it in pieces and updates it later. Asking twice for
    the same one therefore gives the same answer back.
    """

    def __init__(self, limit: int = MAX_ROUTES) -> None:
        self.limit = limit
        self._next = 1
        # (pane id, identifier of the program) -> our identifier.
        self._outgoing: OrderedDict[tuple[PaneId, str], str] = OrderedDict()
        # Our identifier -> (pane id, identifier of the program).
        self._incoming: OrderedDict[str, tuple[PaneId, str]] = OrderedDict()

    def outgoing(self, pane_id: PaneId, param: str) -> str:
        """
        The payload to send to the terminal of the user, with an
        identifier that names this pane.
        """
        metadata, semicolon, text = split_payload(param)
        identifier = read_identifier(metadata)
        if identifier is None:
            return param  # Nothing to route an answer by.

        key = (pane_id, identifier)
        ours = self._outgoing.get(key)
        if ours is None:
            ours = "%i" % self._next
            self._next += 1
            self._outgoing[key] = ours
            self._incoming[ours] = key
            self._forget_old()
        else:
            self._outgoing.move_to_end(key)
            self._incoming.move_to_end(ours)

        return replace_identifier(metadata, ours) + semicolon + text

    def incoming(self, param: str) -> tuple[PaneId, str] | None:
        """
        The pane that an answer belongs to, and the payload to give it,
        with the identifier that the program chose. None when the
        answer names no notification of ours.
        """
        metadata, semicolon, text = split_payload(param)
        identifier = read_identifier(metadata)
        if identifier is None:
            return None

        known = self._incoming.get(identifier)
        if known is None:
            return None

        pane_id, original = known
        return (pane_id, replace_identifier(metadata, original) + semicolon + text)

    def _forget_old(self) -> None:
        "Drop the oldest notifications once the table is full."
        while len(self._incoming) > self.limit:
            ours, key = self._incoming.popitem(last=False)
            self._outgoing.pop(key, None)
