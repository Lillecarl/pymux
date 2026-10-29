"""
A grapheme cluster as the outer terminal draws it. Lillecarl/pymux#798.

A pane keeps a family emoji, a flag or a skin tone in one cell two
columns wide, as Unicode says. Terminals disagree: WezTerm draws a flag
one column wide, and xterm draws a family as its three faces, six
columns. The renderer counts two columns either way, so on such a
terminal everything after the cluster stands in the wrong place.

wcwidth carries what ucs-detect measured on each terminal, by name.
XTVERSION gives the name: TERM cannot, as iTerm2 and WezTerm both send
xterm-256color. Better still, the terminal is measured the way
ucs-detect measures, by writing one cluster of each kind and reading
the cursor back, and its answers outrank its table. Lillecarl/pymux#801.

A cluster the terminal draws narrower is padded with blanks, and one it
draws wider goes out as its first character.
"""

from __future__ import annotations

import re
from enum import StrEnum
from functools import lru_cache
from typing import Final, NamedTuple

from pyte.cells import character_width
from wcwidth import iter_graphemes, list_term_programs
from wcwidth import width as terminal_width

#: Every name and alias wcwidth has measurements for, lowercase.
_KNOWN: Final = frozenset(list_term_programs())

#: The characters that make a cluster narrower than its pieces: a zero
#: width joiner, a regional indicator, a skin tone. A frame without one
#: holds no cluster to fit.
_JOINS: Final = re.compile("[\u200d\U0001f1e6-\U0001f1ff\U0001f3fb-\U0001f3ff]")
_ZWJ: Final = "\u200d"
_REGIONAL: Final = re.compile("[\U0001f1e6-\U0001f1ff]")

#: Text outside the ASCII range, or an OSC string, which is a title or
#: a link and no cells: it goes out as it is.
_TEXT: Final = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?|[^\x00-\x7f]+")


class Kind(StrEnum):
    "The three ways a cluster joins, which terminals each draw their own way."

    FAMILY = "family"
    FLAG = "flag"
    TONE = "tone"


#: One cluster of each kind, and the row its probe writes it on. Row 1
#: is ctrl+F3's ("CSI 1;5R") and row 2 the text emoji probe's.
PROBES: Final = {
    3: (Kind.FAMILY, "\U0001f468\u200d\U0001f469\u200d\U0001f467"),
    4: (Kind.FLAG, "\U0001f1f8\U0001f1ea"),
    5: (Kind.TONE, "\U0001f44d\U0001f3fd"),
}

#: The rows a terminal needs for the probe: a shorter one moves the
#: cursor to its last row, and its answers name the wrong probe.
PROBE_ROWS: Final = max(PROBES)

#: Writes each probe's cluster at the start of its row, asks where the
#: cursor went, and erases the row again: xterm draws the family six
#: columns wide. The answers repaint.
CLUSTER_WIDTH_PROBE: Final = (
    "\x1b7"
    + "".join("\x1b[%d;1H%s\x1b[6n\x1b[%d;1H\x1b[8X" % (row, cluster, row) for row, (_, cluster) in PROBES.items())
    + "\x1b8"
)


def kind_of(cluster: str) -> Kind:
    "How `cluster` joins. A toned family counts as a family."
    if _ZWJ in cluster:
        return Kind.FAMILY
    if _REGIONAL.search(cluster):
        return Kind.FLAG
    return Kind.TONE


def _parts(cluster: str) -> int:
    return sum(map(character_width, cluster))


class Drawn(NamedTuple):
    "The columns a terminal took for each probe's cluster, as far as it answered."

    family: int | None = None
    flag: int | None = None
    tone: int | None = None

    def answered(self, row: int, column: int) -> Drawn:
        "With the cursor report of `row` read: the cursor stands after the cluster."
        kind, _ = PROBES[row]
        return self._replace(**{kind: column - 1})

    def heard(self) -> bool:
        "Whether the terminal answered for any kind."
        return any(drawn is not None for drawn in self)

    def differs(self) -> bool:
        "Whether any cluster this terminal answered for is not two columns."
        return any(getattr(self, kind) not in (None, character_width(cluster)) for kind, cluster in PROBES.values())

    def width(self, cluster: str) -> int:
        """
        The columns this terminal takes for `cluster`. One that drew the
        probe as its pieces draws every cluster of the kind so; one that
        did not answer is taken at Unicode's word.
        """
        kind = kind_of(cluster)
        drawn = getattr(self, kind)
        if drawn is None:
            return character_width(cluster)
        probe = next(probe for probe_kind, probe in PROBES.values() if probe_kind == kind)
        if drawn == _parts(probe) != character_width(probe):
            return _parts(cluster)
        return drawn


@lru_cache(maxsize=64)
def term_program_of(version: str, term: str) -> str | None:
    """
    The name wcwidth knows the terminal by, or None.

    `version` is the XTVERSION answer: "WezTerm 20240203-110809-5046fc22",
    "iTerm2 3.7.6", "ghostty 1.4.0", "kitty(0.39.1)". wcwidth reads only
    the bare name, so the first word goes, cut at its parenthesis. A
    terminal that did not answer is judged by `term`.
    """
    if version.strip():
        name = version.split(maxsplit=1)[0].split("(", 1)[0].lower()
        if name in _KNOWN:
            return name
    term = term.lower()
    return term if term in _KNOWN else None


def table_width(cluster: str, term_program: str) -> int:
    "The columns wcwidth's table for `term_program` gives `cluster`."
    return terminal_width(cluster, term_program=term_program)


@lru_cache(maxsize=1024)
def _fitted(cluster: str, fit: str | Drawn) -> str:
    def width(text: str) -> int:
        return table_width(text, fit) if isinstance(fit, str) else fit.width(text)

    cells = character_width(cluster)
    drawn = width(cluster)
    if drawn == cells:
        return cluster
    if drawn < cells:
        return cluster + " " * (cells - drawn)
    first = cluster[0]
    return first + " " * max(0, cells - character_width(first))


def _joined(cluster: str) -> bool:
    "Whether a pane holds `cluster` in one cell narrower than its pieces."
    return len(cluster) > 1 and character_width(cluster) < _parts(cluster)


def _fit_text(match: re.Match[str], fit: str | Drawn) -> str:
    text = match.group()
    if text.startswith("\x1b") or not _JOINS.search(text):
        return text
    return "".join(_fitted(cluster, fit) if _joined(cluster) else cluster for cluster in iter_graphemes(text))


def fitted(frame: str, fit: str | Drawn) -> str:
    """
    `frame` with every cluster the way the terminal takes two columns
    for it: `fit` is the name of its table in wcwidth, or what it
    answered the probe.
    """
    if not _JOINS.search(frame):
        return frame
    return _TEXT.sub(lambda match: _fit_text(match, fit), frame)
