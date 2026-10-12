"""
How dense a Wayland window in a pane draws: `wayland-scale`.

No terminal reports its DPI, so `auto` reads it off the cell. A cell
`NOMINAL_CELL_HEIGHT` pixels high is scale 1; a Retina kitty's cell is
about twice that, and so is its scale. A window's UI therefore grows
with the terminal's font. Lillecarl/pymux#637.

The value is `auto`, `auto+N`, `auto-N`, `auto*N`, or a plain number.
"""

from __future__ import annotations

import re

__all__ = ["DEFAULT_WAYLAND_SCALE", "NOMINAL_CELL_HEIGHT", "parse_scale", "resolve_scale"]

DEFAULT_WAYLAND_SCALE = "auto"

#: The cell height, in pixels, that `auto` takes as scale 1.
NOMINAL_CELL_HEIGHT = 16

#: wp_fractional_scale_v1 counts in 120ths.
_STEPS = 120

#: Below this a window lays out wider than any screen and draws nothing
#: readable.
_SMALLEST = 0.25

_FORM = re.compile(r"auto(?:([-+*])(\d+(?:\.\d+)?|\.\d+))?|(\d+(?:\.\d+)?|\.\d+)")


def parse_scale(value: str) -> str:
    "`value` as the option holds it, or ValueError when it is not one."
    spelled = value.strip().lower().replace(" ", "")
    match = _FORM.fullmatch(spelled)
    if match is None:
        raise ValueError("a scale is auto, auto+N, auto-N, auto*N or a number, not %r" % (value,))
    number = match.group(3)
    if number is not None and float(number) <= 0:
        raise ValueError("a scale is more than zero, not %r" % (value,))
    return spelled


def resolve_scale(value: str, cell_height: int) -> float:
    "The scale `value` names for a client whose cell is `cell_height` pixels high."
    match = _FORM.fullmatch(parse_scale(value))
    assert match is not None
    operator, operand, number = match.groups()
    if number is not None:
        scale = float(number)
    else:
        # A small font on a plain screen still draws at 1: auto never
        # shrinks a window below the size it would pick for itself.
        scale = max(1.0, cell_height / NOMINAL_CELL_HEIGHT)
        if operator == "+":
            scale += float(operand)
        elif operator == "-":
            scale -= float(operand)
        elif operator == "*":
            scale *= float(operand)
    return max(_SMALLEST, round(scale * _STEPS) / _STEPS)
