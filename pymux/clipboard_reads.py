"""
Reading the clipboard of a client's terminal with OSC 52: the query,
and the answer cut out of the keys before the key parser sees it.
Lillecarl/pymux#383.
"""

from __future__ import annotations

import base64
from contextlib import suppress
from typing import TYPE_CHECKING, ClassVar

import anyio
from pyte.keep import Keep

from .enums import Woke

if TYPE_CHECKING:
    from .server import ServerConnection

__all__ = ["ClipboardReader"]

#: The start of a terminal's answer to a clipboard query.
_CLIPBOARD_REPLY = "\x1b]52;"

#: How long a terminal has to answer a clipboard query, in seconds. A
#: paste waits this long once; a terminal that lets it pass is not asked
#: again on this connection. Most refuse reads by default, silently.
CLIPBOARD_READ_SECONDS = 1.0

#: The longest clipboard answer taken, in characters of base64. Past it
#: the answer is dropped, and nothing of it reaches a pane as keys.
CLIPBOARD_REPLY_LIMIT = 24 << 20


class ClipboardReader:
    """
    One connection's clipboard queries, and the answers on their way.
    """

    KEEP: ClassVar[dict[str, Keep]] = {
        "connection": Keep.REBUILT,
        # Asked again of the terminal the client reattaches from.
        "answers": Keep.REBUILT,
        "_waiters": Keep.REBUILT,
        "_asked": Keep.REBUILT,
        "_partial": Keep.REBUILT,
    }

    def __init__(self, connection: ServerConnection) -> None:
        self.connection = connection

        #: Whether this terminal answers a clipboard query: None until
        #: one is asked, False after one went unanswered, True once an
        #: answer came, even a late one. Lillecarl/pymux#383.
        self.answers: bool | None = None
        #: The pastes waiting for an answer, oldest first: each one's
        #: event, and the list its text goes into.
        self._waiters: list[tuple[anyio.Event, list[str]]] = []
        #: Set once a query went out, so a late answer is still caught.
        self._asked = False
        #: The start of an answer whose end has not arrived yet.
        self._partial = ""

    async def read(self, selection: str = "c") -> str | None:
        """
        Ask this client's terminal for its clipboard, or None.

        `selection` is OSC 52's letter: "c" for the clipboard, "p" for
        the primary selection. The query goes behind the next frame, like
        a copy, so it is answered after any copy queued before it.

        None when the terminal does not answer in time, has been seen not
        to, or answered with nothing. Lillecarl/pymux#383.
        """
        if self.answers is False or self.connection.client_state is None or self.connection.client_state.temporary:
            return None
        done = anyio.Event()
        answer: list[str] = []
        self._waiters.append((done, answer))
        self._asked = True
        self.connection.forward_osc("\x1b]52;%s;?\x1b\\" % (selection,))
        self.connection.pymux.invalidate(Woke.CLIPBOARD_ASKED)
        with anyio.move_on_after(CLIPBOARD_READ_SECONDS):
            await done.wait()
        if not done.is_set():
            with suppress(ValueError):
                self._waiters.remove((done, answer))
            if self.answers is None:
                self.answers = False
            return None
        return answer[0] or None

    def take_answer(self, data: str) -> str:
        """
        Cut a clipboard answer out of the terminal's input, and return
        the rest for the key parser.

        **Not the key parser's job.** It caps a string reply at
        `MAX_STRING_LENGTH` and reads the whole prefix again on every
        character, so a clipboard of a few kilobytes would reach the pane
        as keys, after quadratic work. This sees the bytes first, and
        only once a query went out: nothing typed begins "ESC ] 52 ;".
        """
        if not self._asked and not self._partial:
            return data
        data = self._partial + data
        self._partial = ""
        start = data.find(_CLIPBOARD_REPLY)
        if start < 0:
            return data
        body = start + len(_CLIPBOARD_REPLY)
        ends = [(at, len(end)) for end in ("\x07", "\x1b\\") if (at := data.find(end, body)) >= 0]
        if not ends:
            if len(data) - start <= CLIPBOARD_REPLY_LIMIT:
                self._partial = data[start:]
            return data[:start]
        end, length = min(ends)
        self._answered(data[body:end])
        return data[:start] + self.take_answer(data[end + length :])

    def _answered(self, payload: str) -> None:
        "One answer: 'c;<base64>'. It goes to the oldest waiting paste."
        self.answers = True
        _selection, _semicolon, encoded = payload.partition(";")
        try:
            text = base64.b64decode(encoded, validate=True).decode("utf-8", "replace")
        except ValueError:
            text = ""
        if self._waiters:
            done, answer = self._waiters.pop(0)
            answer.append(text)
            done.set()
