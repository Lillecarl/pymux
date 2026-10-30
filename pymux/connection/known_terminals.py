"""
What pymux knows about a terminal by its name. Lillecarl/pymux#827.

No query answers these questions, so pymux asks the terminal for its
name and version (XTVERSION) and looks the answer up in `TERMINALS`. A
terminal that does not answer is looked up by its TERM, and then its
version is unknown: an ability that starts at a version does not count.

To teach pymux about a terminal, add a `KnownTerminal` to `TERMINALS`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Final, NamedTuple

from pyte.osc import Osc
from wcwidth import list_term_programs

__all__ = ["TERMINALS", "Identity", "KnownTerminal", "identify", "name_of", "wcwidth_name"]

#: A version as numbers: "0.39.1" is (0, 39, 1).
type Version = tuple[int, ...]


@dataclass(frozen=True)
class KnownTerminal:
    #: The first word of its XTVERSION answer, in lower case and cut at
    #: a parenthesis: "kitty(0.39.1)" is "kitty", "iTerm2 3.6.4" is "iterm2".
    name: str
    #: The TERM values that name this terminal and no other.
    #: "xterm-256color" names none: half the terminals set it.
    terms: frozenset[str] = frozenset()
    #: The first version that draws kitty's Unicode placeholders.
    #: WezTerm and Konsole answer the graphics query and draw none.
    #: Lillecarl/pymux#607.
    placeholders: Version | None = None
    #: The first version that draws ConEmu's progress, "OSC 9 ; 4". Any
    #: other terminal may show an OSC 9 as a notification.
    #: Lillecarl/pymux#788.
    progress: Version | None = None
    #: The notification OSC it shows, for a terminal that does not
    #: answer the OSC 99 query. Lillecarl/pymux#826.
    notification: Osc | None = None
    #: Whether it saves an iTerm2 download. Ghostty parses one and drops
    #: it (`src/terminal/osc/parsers/iterm2.zig`). Lillecarl/pymux#746.
    saves_downloads: bool = False
    #: The number of the first pixel in a mouse report in pixels.
    #: Lillecarl/pymux#648.
    first_pixel: int = 1
    #: Its entry in pyte's `KEY_PROFILES`: what it writes for a key with
    #: no kitty keyboard protocol. Lillecarl/pymux#706.
    key_profile: str | None = None


TERMINALS: Final = (
    # Placeholders came in 0.28.0. The progress went to the macOS dock
    # in 0.44.0, and to a bar on the window in 0.47.0.
    KnownTerminal(
        "kitty",
        terms=frozenset({"xterm-kitty"}),
        placeholders=(0, 28, 0),
        progress=(0, 44, 0),
        first_pixel=0,
        key_profile="kitty",
    ),
    # `src/terminal/kitty/graphics_unicode.zig`.
    KnownTerminal(
        "ghostty",
        terms=frozenset({"xterm-ghostty"}),
        placeholders=(1, 0, 0),
        progress=(1, 2, 0),
        notification=Osc.SIMPLE_NOTIFICATION,
        key_profile="ghostty",
    ),
    KnownTerminal("iterm2", progress=(3, 6, 0), notification=Osc.MESSAGE, saves_downloads=True),
    KnownTerminal(
        "wezterm",
        terms=frozenset({"wezterm"}),
        notification=Osc.SIMPLE_NOTIFICATION,
        key_profile="wezterm",
    ),
    KnownTerminal("foot", notification=Osc.SIMPLE_NOTIFICATION),
    KnownTerminal("xterm", key_profile="xterm"),
)

_BY_NAME: Final = {known.name: known for known in TERMINALS}
_BY_TERM: Final = {term: known for known in TERMINALS for term in known.terms}

#: Every name and alias wcwidth has measurements for, lowercase.
_WCWIDTH_NAMES: Final = frozenset(list_term_programs())

_VERSION_RE: Final = re.compile(r"\d+(?:\.\d+)*")


def name_of(version: str) -> str:
    """
    The name in an XTVERSION answer, or "". The answers begin with it:
    "kitty(0.49.2)", "ghostty 1.2.0", "WezTerm 20240203-110809-5046fc22",
    "XTerm(390)".
    """
    words = version.split(maxsplit=1)
    return words[0].split("(", 1)[0].lower() if words else ""


class Identity(NamedTuple):
    "A terminal, as its answers name it."

    known: KnownTerminal | None
    version: Version | None

    def _since(self, first: Version | None) -> bool:
        return first is not None and self.version is not None and self.version >= first

    def draws_placeholders(self) -> bool:
        return self.known is not None and self._since(self.known.placeholders)

    def draws_progress(self) -> bool:
        return self.known is not None and self._since(self.known.progress)

    @property
    def notification_osc(self) -> Osc | None:
        return self.known.notification if self.known is not None else None

    @property
    def saves_downloads(self) -> bool:
        return self.known is not None and self.known.saves_downloads

    @property
    def first_pixel(self) -> int:
        return self.known.first_pixel if self.known is not None else 1

    @property
    def key_profile(self) -> str | None:
        return self.known.key_profile if self.known is not None else None


@lru_cache(maxsize=64)
def identify(version: str, term: str) -> Identity:
    "The terminal that answered XTVERSION with `version`, or else the one `term` names."
    name = name_of(version)
    if name:
        numbers = _VERSION_RE.search(version, len(name))
        return Identity(
            _BY_NAME.get(name),
            tuple(int(part) for part in numbers.group().split(".")) if numbers is not None else None,
        )
    return Identity(_BY_TERM.get(term), None)


@lru_cache(maxsize=64)
def wcwidth_name(version: str, term: str) -> str | None:
    """
    The name wcwidth measured the terminal under, or None. wcwidth knows
    more terminals than `TERMINALS`, and some by their TERM.
    """
    name = name_of(version)
    if name in _WCWIDTH_NAMES:
        return name
    term = term.lower()
    return term if term in _WCWIDTH_NAMES else None
