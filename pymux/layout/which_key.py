"""
The keys the prefix leads to, in a box, while `which-key` is on and the
prefix waits for its second key. Lillecarl/pymux#29.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from prompt_toolkit.formatted_text import StyleAndTextTuples
from prompt_toolkit.layout.containers import (
    Container,
    HSplit,
    Window,
    WindowAlign,
)
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension as D

if TYPE_CHECKING:
    from .layout import LayoutManager

__all__ = ["WhichKey"]


class WhichKey:
    """
    The which-key box of one client's layout.
    """

    def __init__(self, manager: LayoutManager) -> None:
        self.manager = manager

        # Built once, like the command line: a window built fresh each
        # frame is one the layout never focused. Nothing here takes
        # focus, but the same rule keeps the box one object.
        self._box: Container | None = None

    def box(self) -> Container:
        """
        The keys the prefix leads to, in a box.

        Built once, for the reason `CommandLine.window` gives.
        Lillecarl/pymux#29.
        """
        if self._box is not None:
            return self._box

        self._box = HSplit(
            [
                Window(
                    height=1,
                    align=WindowAlign.CENTER,
                    content=FormattedTextControl([("class:commandpalette.title", " Prefix ")]),
                    style="class:commandpalette.titlebar",
                ),
                Window(
                    content=FormattedTextControl(self.tokens),
                    height=lambda: D(min=1, max=self.manager.palette_rows() - 1),
                    style="class:commandpalette",
                ),
            ],
        )
        return self._box

    def tokens(self) -> StyleAndTextTuples:
        """
        The keys the prefix leads to, one row each.

        Two columns, the key and the command it runs, the key column
        one width for the whole list so the meanings line up under
        each other. A prefix that leads nowhere says so rather than
        drawing an empty box. Lillecarl/pymux#29.
        """
        rows = self.manager.pymux.key_bindings_manager.prefix_keys()
        if not rows:
            return [("class:commandpalette", " No keys follow the prefix. ")]

        width = max(len(key) for key, _meaning in rows) + 2
        tokens: StyleAndTextTuples = []
        for key, meaning in rows:
            tokens.append(("class:which-key.key", key.ljust(width)))
            tokens.append(("class:commandpalette", meaning + "\n"))
        return tokens
