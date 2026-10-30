"""
URL mode: a label on every link a pane shows, and the label's letters
open the link or copy it.

It is foot's (`show-urls-launch`, `url-mode.c`): the same letters, the
same labels, the same order. A label is one letter while one letter is
enough and two after that, no label is the start of another, and the
link nearest the bottom gets the first one, because that is where a
person who just ran something is looking. One URL shown twice gets one
label.

The last letter in lower case opens the link on the client's machine,
and in upper case copies it. Escape leaves. The pane goes on drawing
while the labels are up, and a label moves with its link: it is a place
on the page, not on the screen. Lillecarl/pymux#608.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple

from ptterm import Terminal
from pyte.links import Link, LinkRule, links_in

if TYPE_CHECKING:
    from collections.abc import Sequence

    from .window_tree import Pane

__all__ = ["LABEL_LETTERS", "Target", "UrlMode", "labels"]

#: foot's `[url].label-letters`: the home row first, then the keys
#: around it.
LABEL_LETTERS = "sadfjklewcmpgh"


def labels(count: int, letters: str = LABEL_LETTERS) -> list[str]:
    """
    `count` labels, none the start of another, the shortest first.

    foot's `generate_key_combos`: a label taken as a prefix makes one
    longer label for each letter, and stops being a label itself.
    """
    if count == 0:
        return []
    hints = [""]
    taken = 0
    # At least once, as foot's do-while: the empty hint is never a label.
    while True:
        prefix = hints[taken]
        taken += 1
        hints.extend(letter + prefix for letter in letters)
        if len(hints) - taken >= count:
            return [hint[::-1] for hint in hints[taken : taken + count]]


class Target(NamedTuple):
    label: str
    link: Link


class UrlMode:
    "The labels one client shows over one pane, and what has been typed."

    def __init__(self, pane: Pane, targets: list[Target]) -> None:
        self.pane = pane
        self.targets = targets
        self.typed = ""

    @classmethod
    def over(cls, pane: Pane, rules: Sequence[LinkRule] = ()) -> UrlMode:
        """
        The links `pane` shows now, labelled, and the text `rules` makes
        links of. A window pane shows none that pymux can read.
        """
        terminal = pane.terminal
        links: list[Link] = []
        if isinstance(terminal, Terminal):
            shown = terminal.shown_rows()
            page = terminal.screen.page
            links = links_in(page, *shown, rules) if shown is not None and page is not None else []
        given: dict[str, str] = {}
        names = iter(labels(len({link.url for link in links})))
        targets = []
        for link in reversed(links):
            if link.url not in given:
                given[link.url] = next(names)
            targets.append(Target(given[link.url], link))
        return cls(pane, targets[::-1])

    def waiting(self) -> list[Target]:
        "The targets whose label still starts with what was typed."
        return [target for target in self.targets if target.label.startswith(self.typed)]

    def press(self, letter: str) -> Target | None:
        """
        Take one letter. Gives back the target it completed, or None.
        A letter no label goes on with is dropped.
        """
        typed = self.typed + letter.lower()
        for target in self.targets:
            if target.label == typed:
                return target
        if any(target.label.startswith(typed) for target in self.targets):
            self.typed = typed
        return None

    def back(self) -> None:
        self.typed = self.typed[:-1]
