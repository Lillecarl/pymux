"""
A grapheme cluster as the outer terminal draws it. Lillecarl/pymux#798.

A pane keeps a family emoji, a flag or a skin tone in one cell two
columns wide, as Unicode says. Terminals disagree: WezTerm draws a flag
one column wide, and xterm draws a family as its three faces, six
columns. The renderer counts two columns either way, so on such a
terminal everything after the cluster stands in the wrong place.

wcwidth carries what ucs-detect measured on each terminal, by name.
XTVERSION gives the name: TERM cannot, as iTerm2 and WezTerm both send
xterm-256color. A cluster the terminal draws narrower is padded with
blanks, and one it draws wider goes out as its first character.
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Final

from pyte.cells import character_width
from wcwidth import iter_graphemes, list_term_programs
from wcwidth import width as terminal_width

#: Every name and alias wcwidth has measurements for, lowercase.
_KNOWN: Final = frozenset(list_term_programs())

#: The characters that make a cluster narrower than its pieces: a zero
#: width joiner, a regional indicator, a skin tone. A frame without one
#: holds no cluster to fit.
_JOINS: Final = re.compile("[\u200d\U0001f1e6-\U0001f1ff\U0001f3fb-\U0001f3ff]")

#: Text outside the ASCII range, or an OSC string, which is a title or
#: a link and no cells: it goes out as it is.
_TEXT: Final = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?|[^\x00-\x7f]+")


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


@lru_cache(maxsize=1024)
def _fitted(cluster: str, term_program: str) -> str:
    cells = character_width(cluster)
    drawn = terminal_width(cluster, term_program=term_program)
    if drawn == cells:
        return cluster
    if drawn < cells:
        return cluster + " " * (cells - drawn)
    first = cluster[0]
    return first + " " * max(0, cells - terminal_width(first, term_program=term_program))


def _joined(cluster: str) -> bool:
    "Whether a pane holds `cluster` in one cell narrower than its pieces."
    return len(cluster) > 1 and character_width(cluster) < sum(map(character_width, cluster))


def _fit_text(match: re.Match[str], term_program: str) -> str:
    text = match.group()
    if text.startswith("\x1b") or not _JOINS.search(text):
        return text
    return "".join(_fitted(cluster, term_program) if _joined(cluster) else cluster for cluster in iter_graphemes(text))


def fitted(frame: str, term_program: str) -> str:
    "`frame` with every cluster the way `term_program` takes two columns for it."
    if not _JOINS.search(frame):
        return frame
    return _TEXT.sub(lambda match: _fit_text(match, term_program), frame)
