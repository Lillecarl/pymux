"""
The text a pane holds at another size, sent to a client's terminal as
kitty's text sizing, OSC 66. Lillecarl/pymux#890.

A pane's cells hold each block at scale one (`pyte.text_sizes`), so
prompt_toolkit draws that on every terminal. For a terminal that said it
draws OSC 66, each block goes over its cells after the frame.

**A block that goes away asks for a full repaint.** The terminal holds a
block as one thing, and text that lands on a lower row of it moves past
it, so a frame that writes there puts the text in the wrong place. A
full repaint erases the screen first, which erases every block, and the
frame after it sends them all again. A float drawn over a block is one
such frame: it draws past the block, and the repaint after it puts the
float back.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING

from pyte import control, escape
from pyte.sequences.csi import csi
from pyte.sgr import parameters_of

from ..layout.rects import Rect

if TYPE_CHECKING:
    from pyte.sequences.text_sizing import TextSize
    from pyte.text_sizes import SizedBlock

    from .graphics import ClientGraphics, PaneView

__all__ = ["ClientTextSizes"]


class ClientTextSizes:
    """
    The blocks one client's terminal shows, by the cell each starts on.
    """

    def __init__(self, graphics: ClientGraphics) -> None:
        self.graphics = graphics
        #: What the terminal answered the probe with: it draws text in
        #: as many cells as "w" says, and it draws "s" as well.
        self.widths = False
        self.scales = False
        self._sent: dict[tuple[int, int], str] = {}

    @property
    def speaks(self) -> bool:
        "Whether the terminal draws any part of OSC 66."
        return self.widths or self.scales

    def draws(self, size: TextSize) -> bool:
        if size.scale > 1 or size.fractional:
            return self.scales
        return self.widths

    def frame_starts(self, full: bool) -> None:
        "A full frame erases the screen first, and every block with it."
        if full:
            self._sent = {}

    def render(self, views: Iterable[PaneView]) -> None:
        if not self.speaks:
            return
        desired = self._collect(views)
        if any(desired.get(key) != sent for key, sent in self._sent.items()) and self.graphics.repaint is not None:
            self._sent = {}
            self.graphics.repaint()
            return
        new = [command for key, command in desired.items() if key not in self._sent]
        self._sent = desired
        if new:
            self.graphics.write_in_place("".join(new))

    def _collect(self, views: Iterable[PaneView]) -> dict[tuple[int, int], str]:
        desired: dict[tuple[int, int], str] = {}
        for view in views:
            if view.screen is None:
                continue
            for block in view.screen.text_sizes.blocks:
                if not self.draws(block.size):
                    continue
                column = block.x - view.horizontal_scroll
                row = block.y - view.vertical_scroll
                if column < 0 or row < 0 or column + block.columns > view.width or row + block.rows > view.height:
                    continue
                area = Rect(view.x + column, view.y + row, block.columns, block.rows)
                if view.covered and area.minus(view.covered) != [area]:
                    continue
                desired[(area.x, area.y)] = _command(block, area)
        return desired


def _command(block: SizedBlock, area: Rect) -> str:
    return (
        csi(escape.CUP, area.y + 1, area.x + 1)
        + control.CSI
        + parameters_of(block.appearance.rendition)
        + escape.SGR
        + str(block.size)
    )
