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

A notification with no identifier gets one too, and every notification
asks the terminal to report a click (`a=report`): the click shows the
pane it came from, on the client whose terminal was clicked. The
program is told of the click only when it asked itself. Lillecarl/pymux#779.
"""

from __future__ import annotations

import base64
import re
import time
from collections import OrderedDict
from collections.abc import Callable, Container
from enum import StrEnum
from typing import Final, NamedTuple

from pymux.ids import PaneId

__all__ = ["QUERY", "QUERY_IDENTIFIER", "Notification", "NotificationCenter", "NotificationRoutes", "Route"]

#: The characters that an identifier may hold. (The same set as kitty.)
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_+.-]{1,64}$")

#: How many notifications to remember. A program that never reads its
#: answers must not grow the table without end; the oldest goes first.
MAX_ROUTES = 256

#: How many notifications the hub keeps. Same rule as the routes: a
#: noisy pane must not grow the list without end.
MAX_NOTIFICATIONS = 256

#: The identifier of pymux's own question, what the terminal supports
#: (`p=?`). The routes mint decimal identifiers, so no answer to a
#: notification can be taken for the answer to this.
QUERY_IDENTIFIER: Final = "pymux"
QUERY: Final = "i=%s:p=?;" % QUERY_IDENTIFIER

#: The payload types that ask the terminal something rather than show
#: a notification: no click to report.
_QUESTIONS: Final = frozenset({"?", "alive", "close"})


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


class NotificationSource(StrEnum):
    """
    What a notification came through, so the hub can say it: which
    programs use which is still being learned. Lillecarl/pymux#768.
    """

    #: A record from before the hub kept this.
    UNKNOWN = ""
    OSC_99 = "osc 99"
    OSC_777 = "osc 777"
    OSC_9 = "osc 9"
    ATTENTION = "osc 1337"
    COMMAND = "pymux notify"


class Notification(NamedTuple):
    """One notification the hub shows, oldest stored first."""

    id: int
    title: str
    body: str
    urgency: int
    pane_id: PaneId | None
    at: float
    source: NotificationSource = NotificationSource.UNKNOWN
    #: When a client first looked at the window it came from; None while unseen.
    seen_at: float | None = None


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
        #: Called with each finished record, which is what marks the
        #: window it came from. Lillecarl/pymux#767.
        self.on_added: Callable[[Notification], None] | None = None

    def add(
        self,
        title: str,
        body: str = "",
        urgency: int = Urgency.NORMAL,
        pane_id: PaneId | None = None,
        source: NotificationSource = NotificationSource.UNKNOWN,
    ) -> Notification:
        "Record a finished notification."
        record = Notification(
            id=self._next,
            title=title,
            body=body,
            urgency=urgency,
            pane_id=pane_id,
            at=time.time(),
            source=source,
        )
        self._next += 1
        return self._store(record)

    def _store(self, record: Notification) -> Notification:
        self._records.append(record)
        while len(self._records) > self.limit:
            del self._records[0]
        if self.on_added is not None:
            self.on_added(record)
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
                source=NotificationSource.OSC_99,
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
                source=NotificationSource.OSC_99,
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
        return self._store(pending)

    def notifications(self) -> list[Notification]:
        "Every finished notification, oldest first."
        return list(self._records)

    def sweep(self, seen_of: Callable[[Notification], float | None], keep: float, now: float) -> None:
        """
        Mark what `seen_of` says was seen, and forget what was seen more
        than `keep` seconds ago. An unseen one stays, until the cap. Zero
        keeps every one. Lillecarl/pymux#780.
        """
        records = []
        for record in self._records:
            if record.seen_at is None:
                record = record._replace(seen_at=seen_of(record))
            if not keep or record.seen_at is None or now - record.seen_at < keep:
                records.append(record)
        self._records = records


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
    "The metadata with this value for its 'i' key, which it gains if it had none."
    fields = [field for field in metadata.split(":") if field]
    for index, field in enumerate(fields):
        key, sign, _value = field.partition("=")
        if key == "i" and sign:
            fields[index] = "i=" + identifier
            return ":".join(fields)
    return ":".join(["i=" + identifier, *fields])


def osc99_of(identifier: str, title: str, body: str, urgency: int) -> list[str]:
    """
    The OSC 99 payloads of a notification pymux sends itself: a title
    chunk, and a body chunk when there is a body. Base64 (`e=1`), so
    no text a program wrote can end the sequence. kitty shows nothing
    without a title, so a body alone stands in for one.
    """
    if not title:
        title, body = body, ""
    head = "i=%s:a=report:u=%i:e=1" % (identifier, urgency)
    encoded = base64.b64encode(title.encode()).decode("ascii")
    if not body:
        return ["%s:p=title;%s" % (head, encoded)]
    return [
        "%s:d=0:p=title;%s" % (head, encoded),
        "%s:p=body;%s" % (head, base64.b64encode(body.encode()).decode("ascii")),
    ]


def read_actions(metadata: str) -> list[str]:
    "The values of the 'a' key: `focus`, `report`, or either with a minus to turn it off."
    for field in metadata.split(":"):
        key, sign, value = field.partition("=")
        if key == "a" and sign:
            return [action for action in value.split(",") if action]
    return []


def is_question(param: str) -> bool:
    "Whether an OSC 99 payload asks the terminal something rather than shows a notification."
    metadata, _semicolon, _text = split_payload(param)
    return read_payload_type(metadata) in _QUESTIONS


def can_close(answer: str) -> bool:
    """
    Whether a `p=?` answer says the terminal can close a notification:
    `close` among the payload types of its `p` key. Not `c=1`, which
    says it reports a close a person made. kitty 0.49.2 answers
    `p=title,body,?,close,icon,alive,buttons` and no `c`.
    """
    for part in answer.split(":"):
        key, _equals, values = part.partition("=")
        if key == "p":
            return "close" in values.split(",")
    return False


def close_of(identifier: str) -> str:
    "The payload that takes a notification off the terminal's screen."
    return "i=%s:p=close;" % identifier


def with_report(metadata: str) -> str:
    "The metadata asking the terminal to report a click, beside the actions it named."
    if "report" in read_actions(metadata):
        return metadata
    fields = [field for field in metadata.split(":") if field]
    for index, field in enumerate(fields):
        key, sign, value = field.partition("=")
        if key == "a" and sign:
            fields[index] = "a=" + ",".join([*read_actions(metadata), "report"])
            return ":".join(fields)
    return ":".join([*fields, "a=report"])


def is_done(metadata: str) -> bool:
    "Whether this chunk ends its notification: `d` missing or not zero."
    for field in metadata.split(":"):
        key, sign, value = field.partition("=")
        if key == "d" and sign:
            return value != "0"
    return True


def is_click(param: str) -> bool:
    "Whether a terminal's answer reports a click: it names no payload type."
    metadata, _semicolon, _text = split_payload(param)
    return all(field.partition("=")[0] != "p" for field in metadata.split(":"))


class Route(NamedTuple):
    "Where the answer to one notification goes."

    pane_id: PaneId
    #: The program's own identifier; "" when it named none, which the
    #: program is answered as "i=0".
    identifier: str
    #: The program asked for the click itself, so it is told of it.
    reports: bool = False
    #: A click shows the pane, unless the program said `a=-focus`.
    focuses: bool = True


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
        # (pane id, identifier of the program) -> our identifier. An
        # identifier of "" is the notification without one being sent.
        self._outgoing: OrderedDict[tuple[PaneId, str], str] = OrderedDict()
        # Our identifier -> where its answer goes.
        self._incoming: OrderedDict[str, Route] = OrderedDict()
        # Ours, while more chunks of the notification follow.
        self._open: set[str] = set()
        # Ours, shown on the terminals and not yet seen in pymux, and
        # their panes. Lillecarl/pymux#799.
        self._shown: dict[str, PaneId] = {}

    def outgoing(self, pane_id: PaneId, param: str) -> str:
        """
        The payload to send to the terminal of the user, with an
        identifier that names this pane, and asking for a click.

        kitty joins chunks with no identifier while `d=0` says more
        follow, so those keep one identifier until the last.
        """
        metadata, semicolon, text = split_payload(param)
        identifier = read_identifier(metadata)
        question = read_payload_type(metadata) in _QUESTIONS
        if identifier is None:
            if question:
                return param  # Its answer says "i=0", which names nothing.
            identifier = ""

        key = (pane_id, identifier)
        ours = self._outgoing.get(key)
        if ours is None:
            ours = self._mint()
            self._outgoing[key] = ours
            route = Route(pane_id, identifier)
        else:
            self._outgoing.move_to_end(key)
            self._incoming.move_to_end(ours)
            route = self._incoming[ours]

        if not question:
            actions = read_actions(metadata)
            # A notification after one that ended starts afresh, as kitty's does.
            base = route if ours in self._open else Route(pane_id, identifier)
            route = base._replace(
                reports=base.reports or "report" in actions,
                focuses=base.focuses and "-focus" not in actions,
            )
            metadata = with_report(metadata)
            if is_done(metadata):
                self._open.discard(ours)
                self._shown[ours] = pane_id
                if not identifier:
                    del self._outgoing[key]
            else:
                self._open.add(ours)

        self._incoming[ours] = route
        self._forget_old()
        return replace_identifier(metadata, ours) + semicolon + text

    def own(self, pane_id: PaneId | None) -> str:
        "An identifier for a notification pymux sends itself, about this pane; one about no pane routes nowhere."
        ours = self._mint()
        if pane_id is None:
            return ours
        self._incoming[ours] = Route(pane_id, "")
        self._shown[ours] = pane_id
        self._forget_old()
        return ours

    @property
    def any_shown(self) -> bool:
        return bool(self._shown)

    def seen(self, pane_ids: Container[PaneId]) -> list[str]:
        "Ours that a person has now seen the pane of, for the terminals to close. Each is given once."
        closed = [ours for ours, pane_id in self._shown.items() if pane_id in pane_ids]
        for ours in closed:
            del self._shown[ours]
        return closed

    def incoming(self, param: str) -> tuple[Route, str] | None:
        """
        Where an answer goes, and the payload to give the program, with
        the identifier it chose. None when the answer names no
        notification of ours.
        """
        metadata, semicolon, text = split_payload(param)
        identifier = read_identifier(metadata)
        if identifier is None:
            return None

        route = self._incoming.get(identifier)
        if route is None:
            return None

        return (route, replace_identifier(metadata, route.identifier or "0") + semicolon + text)

    def _mint(self) -> str:
        ours = "%i" % self._next
        self._next += 1
        return ours

    def _forget_old(self) -> None:
        "Drop the oldest notifications once the table is full."
        while len(self._incoming) > self.limit:
            ours, route = self._incoming.popitem(last=False)
            self._open.discard(ours)
            self._shown.pop(ours, None)
            if self._outgoing.get((route.pane_id, route.identifier)) == ours:
                del self._outgoing[route.pane_id, route.identifier]
