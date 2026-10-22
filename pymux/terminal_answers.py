"""
Everything a client's terminal said when it was asked, kept.

pymux asks an attaching terminal a dozen questions and acts on a few of
the answers. The rest used to go: a terminal nobody documents, such as
a phone's, could only be guessed at. Now every reply is kept per
client, and `show-terminal` reads them back. Lillecarl/pymux#725.

**Keyed by what was said.** The same reply again adds to a count
rather than a row, so the status answer behind every frame
(Lillecarl/pymux#259) is one row however long the client stays, and the
whole store is capped besides.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

__all__ = ["SURVEY", "Answer", "TerminalAnswers", "readable"]

#: Asked once, when a client's terminal has been detected, for nothing
#: but to know the answer. Secondary device attributes, and DECRQM for
#: the mouse modes, focus events, alternate scroll, bracketed paste,
#: grapheme clustering, theme reports and in-band resize. pymux asks
#: the version, the colours, the keyboard and the graphics already.
SURVEY = "\x1b[>c" + "".join(
    "\x1b[?%d$p" % mode for mode in (1000, 1002, 1003, 1004, 1006, 1007, 2004, 2027, 2031, 2048)
)

#: More different replies than this from one terminal is a terminal
#: talking nonsense; the oldest go.
MOST_KEPT = 256


@dataclass
class Answer:
    "One reply, how often it came, and when first and last."

    text: str
    count: int
    first: float
    last: float


class TerminalAnswers:
    "What one client's terminal replied, in the order it first said it."

    def __init__(self) -> None:
        self._answers: dict[str, Answer] = {}

    def heard(self, text: str) -> None:
        now = time.time()
        answer = self._answers.get(text)
        if answer is not None:
            answer.count += 1
            answer.last = now
            return
        self._answers[text] = Answer(text, 1, now, now)
        while len(self._answers) > MOST_KEPT:
            del self._answers[next(iter(self._answers))]

    def __iter__(self):
        return iter(self._answers.values())

    def __len__(self) -> int:
        return len(self._answers)


def readable(text: str) -> str:
    "A reply with its control characters written out: ESC as \\e, the rest as \\xNN."
    out = []
    for char in text:
        if char == "\x1b":
            out.append("\\e")
        elif char == "\x07":
            out.append("\\a")
        elif ord(char) < 0x20 or ord(char) == 0x7F or 0x80 <= ord(char) < 0xA0:
            out.append("\\x%02x" % ord(char))
        else:
            out.append(char)
    return "".join(out)
