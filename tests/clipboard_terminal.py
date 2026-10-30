"""
A client's terminal that keeps a clipboard and answers OSC 52, for the
suites that read one through `ClipboardReader.read`.
"""

from __future__ import annotations

import base64
import re

#: A query or a write of OSC 52, as the server writes it.
_OSC52 = re.compile(r"\x1b\]52;([cp]);([^\x07\x1b]*)(?:\x07|\x1b\\)")


class ClipboardTerminal:
    """
    `read_packet` for `over_connection`: it reads every frame, keeps
    what an OSC 52 write sets, and answers a query by typing the reply.

    `answers` False is a terminal that refuses reads, which is most of
    them by default: it says nothing at all. `chunk` splits each answer
    into packets of that many characters, as a slow link would.
    """

    def __init__(self, answers: bool = True, chunk: int = 0) -> None:
        self.answers = answers
        self.chunk = chunk
        self.selections: dict[str, str] = {}
        self.queries = 0
        self.typed = None
        self.state = None

    def __call__(self, packet) -> None:
        message = packet
        if message.get("cmd") != "out":
            return
        for letter, payload in _OSC52.findall(message.get("data", "")):
            if payload == "?":
                self.queries += 1
                if self.answers:
                    self._reply(letter)
            else:
                self.selections[letter] = base64.b64decode(payload).decode()

    def _reply(self, letter: str) -> None:
        encoded = base64.b64encode(self.selections.get(letter, "").encode()).decode()
        reply = "\x1b]52;%s;%s\x07" % (letter, encoded)
        size = self.chunk or len(reply)
        for start in range(0, len(reply), size):
            self.typed(self.state, reply[start : start + size])
