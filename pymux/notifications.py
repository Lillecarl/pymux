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
import re
from collections import OrderedDict
from typing import Optional, Tuple

__all__ = ["NotificationRoutes"]

#: The characters that an identifier may hold. (The same set as kitty.)
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_+.-]{1,64}$")

#: How many notifications to remember. A program that never reads its
#: answers must not grow the table without end; the oldest goes first.
MAX_ROUTES = 256


def split_payload(param: str) -> Tuple[str, str, str]:
    """
    The payload of an OSC 99 is "<metadata> ; <text>". The text may
    hold a semicolon of its own, so only the first one counts.
    """
    metadata, semicolon, text = param.partition(";")
    return metadata, semicolon, text


def read_identifier(metadata: str) -> Optional[str]:
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
        self._outgoing: "OrderedDict[Tuple[int, str], str]" = OrderedDict()
        # Our identifier -> (pane id, identifier of the program).
        self._incoming: "OrderedDict[str, Tuple[int, str]]" = OrderedDict()

    def outgoing(self, pane_id: int, param: str) -> str:
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

    def incoming(self, param: str) -> Optional[Tuple[int, str]]:
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
