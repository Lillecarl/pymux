"""
kitty's extra cursors on a terminal that draws them itself.

A pane's program sets extra cursors with kitty's multiple cursors
protocol, and ptterm draws them as styled cells. A client whose
terminal answered "CSI > SP q" gets the focused pane's cursors as its
own instead, moved to where the pane is drawn: kitty draws them with
the blink and the colours of its cursor. kitty holds one colour pair
for every extra cursor, so only one pane can have its colours, and that
is the pane the person works in; the others keep the drawn cells.
Lillecarl/pyterm#893.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pyte.extra_cursors import ExtraCommand, ExtraShape, Where
from pyte.sequences.csi import Csi, Marker, Parameter, csi

if TYPE_CHECKING:
    from pyte.extra_cursors import ExtraCursors
    from pyte.screen import Screen

    from ..layout.rects import Rect

__all__ = ["ExtraCursorsSent", "Placed", "cursors_on_the_client"]

#: How many points one sequence carries, so that no sequence is long
#: enough for a terminal to drop it.
_POINTS_PER_SEQUENCE = 64

#: A cursor on the client's screen: its row, its column and its shape.
Placed = tuple[int, int, ExtraShape]


def cursors_on_the_client(
    screen: Screen,
    x: int,
    y: int,
    width: int,
    height: int,
    vertical_scroll: int,
    horizontal_scroll: int,
    covered: tuple[Rect, ...],
) -> frozenset[Placed]:
    """
    The extra cursors of `screen`, in cells of the client's screen,
    for a pane drawn at `x`, `y`. A cursor outside the pane, or under
    something this client drew over it, is left out.
    """
    placed = set()
    line_offset = screen.line_offset
    for (row, column), shape in screen.extra_cursors.cells.items():
        down = line_offset + row - vertical_scroll
        across = column - horizontal_scroll
        if not (0 <= down < height and 0 <= across < width):
            continue
        cell_y, cell_x = y + down, x + across
        if any(rect.x <= cell_x < rect.right and rect.y <= cell_y < rect.bottom for rect in covered):
            continue
        placed.add((cell_y, cell_x, shape))
    return frozenset(placed)


def _sequence(*params: Parameter) -> str:
    return csi(Csi.DECSCUSR, *params, private=Marker.GREATER)


class ExtraCursorsSent:
    "What one client's terminal was last told, so a frame sends only a change."

    def __init__(self) -> None:
        self.cursors: frozenset[Placed] = frozenset()
        self.colors: tuple[tuple[int, ...], tuple[int, ...]] = ((0,), (0,))

    def update(self, cursors: frozenset[Placed], extra: ExtraCursors | None) -> str:
        "The bytes that take the terminal from what it was told to `cursors`."
        out = []
        if cursors != self.cursors:
            out.append(_sequence(ExtraShape.NONE, Where.RECTANGLES))
            by_shape: dict[ExtraShape, list[tuple[int, int]]] = {}
            for row, column, shape in sorted(cursors):
                by_shape.setdefault(shape, []).append((row, column))
            for shape, points in by_shape.items():
                for start in range(0, len(points), _POINTS_PER_SEQUENCE):
                    chunk = points[start : start + _POINTS_PER_SEQUENCE]
                    out.append(_sequence(shape, *((Where.POINTS, row + 1, column + 1) for row, column in chunk)))
            self.cursors = cursors
        # The colours stay with the pane that set them until another has
        # cursors to show.
        if extra is not None and cursors:
            colors = (extra.text_color.parameters(), extra.cursor_color.parameters())
            if colors != self.colors:
                out.append(_sequence(ExtraCommand.TEXT_COLOR, colors[0]))
                out.append(_sequence(ExtraCommand.CURSOR_COLOR, colors[1]))
                self.colors = colors
        return "".join(out)
