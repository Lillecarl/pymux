"""
The shade at the edge where the view cuts a pane.

A strip is a row that may be wider than the view, so a pane can run
off the screen, and its cells say nothing about it: a pane whose
program wrote nothing past the cut looks like a pane that ends there.
So the cells nearest the cut edge are shaded, strongest at the edge and
fading over `CELLS` inward, on whichever side is cut. Lillecarl/pymux#222,
Lillecarl/pymux#616.

Carl: "the tint i asked for was not full-screen, just the column near
the edge."

**Each cell's own background is shaded**, so a cell the program
coloured fades the same way as one it left alone. A class in the pane's
style could only reach the second kind, which tinted a shell's empty
cells and left its prompt's colours standing.

**It runs after the pane drew.** `_PaneMark` registers it as a deferred
draw above the pane's own z-index, which is how it lands on cells that
do not exist yet when `PlanContainer` asks the pane to draw. A band and
not the pane: marking every cell of a pane cost 14,620 instructions a
frame, and a band of four columns is a small part of that.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import NamedTuple

from prompt_toolkit.layout.screen import Char, Screen, WritePosition
from prompt_toolkit.output.vt100_colors import ANSI_COLORS_TO_RGB
from prompt_toolkit.styles import Attrs

from .plane import Rect
from .style import CUT_LIFT, tinted

__all__ = ["CELLS", "Hidden", "hex_of", "hidden_of", "shade"]

#: How many cells the shade fades over, from the cut edge inward.
CELLS = 4

#: The shaded spellings of this frame, on its screen: a style string and
#: a step give one answer for as long as the theme stands, and the
#: theme can change between two frames.
_SHADED = "pymux_cut_shade"


class Hidden(NamedTuple):
    "How many cells of a pane the view hides past each of its edges."

    left: int
    right: int
    top: int
    bottom: int


def hidden_of(view: Rect, rect: Rect) -> Hidden | None:
    "What the view hides of `rect`, or None when it shows all or none of it."
    if not view.overlaps(rect) or view.encloses(rect):
        return None
    return Hidden(
        left=max(0, view.x - rect.x),
        right=max(0, rect.right - view.right),
        top=max(0, view.y - rect.y),
        bottom=max(0, rect.bottom - view.bottom),
    )


def _band(low: int, high: int, cut_low: bool, cut_high: bool) -> dict[int, int]:
    "The step of each cell of `low..high` the shade reaches: `CELLS` at a cut edge, falling by one inward."
    steps: dict[int, int] = {}
    reach = min(CELLS, high - low)
    if cut_low:
        for inward in range(reach):
            steps[low + inward] = CELLS - inward
    if cut_high:
        for inward in range(reach):
            at = high - 1 - inward
            steps[at] = max(steps.get(at, 0), CELLS - inward)
    return steps


def hex_of(colour: str) -> str | None:
    "A prompt_toolkit colour as `#rrggbb`, or None for the default and anything unnamed."
    if len(colour) == 6 and all(digit in "0123456789abcdefABCDEF" for digit in colour):
        return "#" + colour.lower()
    rgb = ANSI_COLORS_TO_RGB.get(colour)
    return None if rgb is None else "#%02x%02x%02x" % rgb


def _shaded(style: str, step: int, resolve: Callable[[str], Attrs], backdrop: str | None) -> str:
    """
    `style` with its background moved `step` steps of `CELLS` towards the tint.

    In reverse video the background a person sees is the foreground,
    so that is the one moved. A cell whose background is the default
    takes `backdrop`, which is what the terminal really draws there.
    """
    attrs = resolve(style)
    if attrs.reverse:
        base = hex_of(attrs.color or "")
        side = "fg"
    else:
        base = hex_of(attrs.bgcolor or "") or backdrop
        side = "bg"
    if base is None:
        return style
    return "%s %s:%s" % (style, side, tinted(base, CUT_LIFT * step / CELLS))


def shade(
    screen: Screen,
    write_position: WritePosition,
    hidden: Hidden,
    resolve: Callable[[str], Attrs],
    backdrop: str | None,
) -> None:
    "Shade the cells of the pane at `write_position` nearest each edge the view cuts."
    left = write_position.xpos + hidden.left
    right = write_position.xpos + write_position.width - hidden.right
    top = write_position.ypos + hidden.top
    bottom = write_position.ypos + write_position.height - hidden.bottom
    if left >= right or top >= bottom:
        return

    across = _band(left, right, hidden.left > 0, hidden.right > 0)
    down = _band(top, bottom, hidden.top > 0, hidden.bottom > 0)

    shaded = getattr(screen, _SHADED, None)
    if shaded is None:
        shaded = {}
        setattr(screen, _SHADED, shaded)

    for y in range(top, bottom):
        row = screen.data_buffer[y]
        down_step = down.get(y, 0)
        for x in range(left, right) if down_step else across:
            step = max(down_step, across.get(x, 0))
            cell = row[x]
            key = (cell.style, step)
            style = shaded.get(key)
            if style is None:
                style = shaded[key] = _shaded(cell.style, step, resolve, backdrop)
            row[x] = Char(cell.char, style)
