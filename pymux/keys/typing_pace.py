"""
When each key of a text goes out, so a recording looks typed by a person.

A person types in bursts: fast inside a word, slower at a space, slower
again after a full stop, and stops to think before Enter. An even beat
looks like a machine in a replay. Nothing here sleeps: `delays` is a
pure function of the text and the pace, and the caller waits.
Lillecarl/pyterm#1064.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Final

__all__ = ["WORDS_PER_MINUTE", "Pace", "delays"]

#: A practised typist, not a fast one: a demo is read as it plays.
WORDS_PER_MINUTE: Final = 70.0
#: A word, for words per minute, is five characters: the typing-test rule.
WORD: Final = 5
#: How far one key's wait strays from the beat: the sigma of a lognormal.
SPREAD: Final = 0.35
#: No key comes sooner than this share of the beat, or later than the
#: other: a lognormal has a long tail, and a five-second key reads as a hang.
FASTEST: Final = 0.35
SLOWEST: Final = 3.0
#: Extra beats before the first key of a word, and after these.
WORD_GAP: Final = (0.5, 1.5)
SENTENCE_END: Final = ".!?;:,"
SENTENCE_GAP: Final = (1.5, 3.0)
#: Beats a person spends reading the line before pressing Enter.
ENTER_GAP: Final = (4.0, 8.0)
#: A key pressed again with the same finger.
REPEAT: Final = 0.7


@dataclass(frozen=True, slots=True)
class Pace:
    words_per_minute: float = WORDS_PER_MINUTE
    #: The same seed gives the same delays, so a demo records the same twice.
    seed: int = 0

    @property
    def beat(self) -> float:
        return 60.0 / (self.words_per_minute * WORD)


def delays(text: str, pace: Pace) -> list[float]:
    "The seconds to wait before each character of `text`; the first goes at once."
    rng = random.Random(pace.seed)
    beat = pace.beat
    waits: list[float] = []
    previous = ""
    for index, char in enumerate(text):
        if index == 0:
            waits.append(0.0)
            previous = char
            continue
        wait = beat * min(max(rng.lognormvariate(0.0, SPREAD), FASTEST), SLOWEST)
        if char == previous and char.isalnum():
            wait *= REPEAT
        if char == "\n":
            wait += beat * rng.uniform(*ENTER_GAP)
        elif previous in SENTENCE_END:
            wait += beat * rng.uniform(*SENTENCE_GAP)
        elif previous.isspace() and not char.isspace():
            wait += beat * rng.uniform(*WORD_GAP)
        waits.append(round(wait, 4))
        previous = char
    return waits
