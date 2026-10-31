"""
The shade at the edge where the view cuts a pane.

A strip is a row that may be wider than the view, so a pane can run
off the screen, and its cells say nothing about it: a pane whose
program wrote nothing past the cut looks like a pane that ends there.
So the cells nearest the cut edge are shaded, strongest at the edge and
fading inward, on whichever side is cut. `cut-shade-width` and
`cut-shade-strength` say how far and how much. Lillecarl/pymux#222,
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
frame, and a band of a few columns is a small part of that.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import NamedTuple

from prompt_toolkit.layout.screen import Char, Screen, WritePosition
from prompt_toolkit.output.vt100_colors import ANSI_COLORS_TO_RGB
from prompt_toolkit.styles import Attrs

from ..style.style import tinted
from .rects import Rect

__all__ = ["Hidden", "hex_of", "hidden_of", "lifts", "shade"]

#: The shaded spellings of this frame, on its screen: a style string and
#: a lift give one answer for as long as the theme stands, and the theme
#: can change between two frames.
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


def lifts(width: int, strength: float) -> list[float]:
    "How far each cell of the band moves, from the cut edge inward: `strength` at the edge, falling evenly."
    return [strength * (width - inward) / width for inward in range(width)]


def _band(low: int, high: int, cut_low: bool, cut_high: bool, fade: list[float]) -> dict[int, float]:
    "The lift of each cell of `low..high` the shade reaches."
    band: dict[int, float] = {}
    reach = min(len(fade), high - low)
    if cut_low:
        for inward in range(reach):
            band[low + inward] = fade[inward]
    if cut_high:
        for inward in range(reach):
            at = high - 1 - inward
            band[at] = max(band.get(at, 0.0), fade[inward])
    return band


def hex_of(colour: str) -> str | None:
    "A prompt_toolkit colour as `#rrggbb`, or None for the default and anything unnamed."
    if len(colour) == 6 and all(digit in "0123456789abcdefABCDEF" for digit in colour):
        return "#" + colour.lower()
    rgb = ANSI_COLORS_TO_RGB.get(colour)
    return None if rgb is None else "#%02x%02x%02x" % rgb


def _shaded(style: str, lift: float, resolve: Callable[[str], Attrs], backdrop: str | None) -> str:
    """
    `style` with its background moved `lift` of the way to black or white.

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
    return "%s %s:%s" % (style, side, tinted(base, lift))


def shade(
    screen: Screen,
    write_position: WritePosition,
    hidden: Hidden,
    fade: list[float],
    resolve: Callable[[str], Attrs],
    backdrop: str | None,
) -> None:
    "Shade the cells of the pane at `write_position` nearest each edge the view cuts, by `fade`."
    left = write_position.xpos + hidden.left
    right = write_position.xpos + write_position.width - hidden.right
    top = write_position.ypos + hidden.top
    bottom = write_position.ypos + write_position.height - hidden.bottom
    if not fade or left >= right or top >= bottom:
        return

    across = _band(left, right, hidden.left > 0, hidden.right > 0, fade)
    down = _band(top, bottom, hidden.top > 0, hidden.bottom > 0, fade)

    shaded: dict[tuple[str, float], str] | None = getattr(screen, _SHADED, None)
    if shaded is None:
        shaded = {}
        setattr(screen, _SHADED, shaded)

    for y in range(top, bottom):
        row = screen.data_buffer[y]
        down_lift = down.get(y, 0.0)
        for x in range(left, right) if down_lift else across:
            lift = max(down_lift, across.get(x, 0.0))
            cell = row[x]
            key = (cell.style, lift)
            style = shaded.get(key)
            if style is None:
                style = shaded[key] = _shaded(cell.style, lift, resolve, backdrop)
            row[x] = Char(cell.char, style)
