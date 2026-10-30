"""
Box drawing and block elements, U+2500 to U+259F, drawn as foot draws
them: from the cell's own geometry, not from a font. A font's glyph
need not reach the cell's edge, so a border or block art drawn from one
breaks between cells. Lillecarl/pymux#777, Lillecarl/pymux#778.

`mask` answers one cell as an alpha mask, for `screenshot.draw` to fill
with the foreground. The shapes and the rounding follow foot's
`box-drawing.c`. The lines are read from each character's Unicode name,
so "UP LIGHT AND RIGHT DOWN HEAVY" is one light arm and two heavy ones.
"""

from __future__ import annotations

import math
import unicodedata
from functools import cache
from typing import Final, Literal

from PIL import Image, ImageDraw

type Weight = Literal["light", "heavy", "double"]
type Box = tuple[int, int, int, int]
type Side = Literal["left", "right", "up", "down"]

INK: Final = 255
# foot's box-drawing-base-thickness, in points per point of cell diagonal.
BASE_THICKNESS: Final = 0.04
# foot draws an arc or a diagonal at four times the size, and averages down.
SUPERSAMPLE: Final = 4

_WEIGHTS: Final[dict[str, Weight]] = {"LIGHT": "light", "SINGLE": "light", "HEAVY": "heavy", "DOUBLE": "double"}
_SIDES: Final[dict[str, tuple[Side, ...]]] = {
    "LEFT": ("left",),
    "RIGHT": ("right",),
    "UP": ("up",),
    "DOWN": ("down",),
    "HORIZONTAL": ("left", "right"),
    "VERTICAL": ("up", "down"),
}
_OPPOSITE: Final[dict[Side, Side]] = {"left": "right", "right": "left", "up": "down", "down": "up"}
_ACROSS: Final[dict[Side, tuple[Side, Side]]] = {
    "left": ("up", "down"),
    "right": ("up", "down"),
    "up": ("left", "right"),
    "down": ("left", "right"),
}
# U+2596 to U+259F in order, as the quarters each fills: 1 upper left,
# 2 upper right, 3 lower left, 4 lower right.
_QUADRANTS: Final = ("3", "4", "1", "134", "14", "123", "124", "2", "23", "234")
# Pillow's angles for each arc's quarter of its circle, clockwise from three o'clock.
_ARC_ANGLES: Final = {0x256D: (180, 270), 0x256E: (270, 360), 0x256F: (0, 90), 0x2570: (90, 180)}


def _name(code: int) -> list[str]:
    return unicodedata.name(chr(code), "").removeprefix("BOX DRAWINGS ").split()


@cache
def arms(code: int) -> dict[Side, Weight] | None:
    """
    The arms of a box-drawing line, from its Unicode name. A phrase
    with no weight takes the one before it: "LIGHT DOWN AND RIGHT". None
    for a dash, an arc, a diagonal, or anything outside U+2500 to U+257F.
    """
    words = _name(code)
    if not 0x2500 <= code <= 0x257F or {"DASH", "ARC", "DIAGONAL"} & set(words):
        return None
    found: dict[Side, Weight] = {}
    weight: Weight = "light"
    for phrase in " ".join(words).split(" AND "):
        weight = next((_WEIGHTS[word] for word in phrase.split() if word in _WEIGHTS), weight)
        for word in phrase.split():
            for side in _SIDES.get(word, ()):
                found[side] = weight
    return found


def light_thickness(width: int, height: int) -> int:
    "foot's at 96 dpi, which is what a font sized in pixels comes to. A heavy line is three."
    return max(1, int(BASE_THICKNESS * math.hypot(width, height) * 96 / 72))


def _band(length: int, thick: int) -> tuple[int, int]:
    "Where a line `thick` wide lies across the middle of `length`, as foot centres it."
    return (length - thick) // 2, (length + thick) // 2


def _arm(side: Side, start: int, end: int, band: tuple[int, int]) -> Box:
    "The box of one arm: along from `start` to `end`, across over `band`."
    if side in ("left", "right"):
        return start, band[0], end, band[1]
    return band[0], start, band[1], end


def _lines(found: dict[Side, Weight], width: int, height: int, light: int) -> tuple[list[Box], list[Box], list[Box]]:
    """
    For a character's arms, in the order they are drawn: the double
    arms' boxes, the middles cleared out of them, and the other arms.

    An arm runs from the cell's edge to the far side of the line across
    it, or past the middle when there is none. A double arm is one band
    three lights wide with its middle light cleared, the way foot's two
    lines and their corners come out. A single arm crosses that middle
    when it goes on through, as in ╪. One that meets a double passing
    straight through stops at its near line, as in ╟; one that meets a
    double's end closes it, as in ╒.
    """
    doubles: list[Box] = []
    clear: list[Box] = []
    others: list[Box] = []

    def thickness(weight: Weight) -> int:
        return light if weight == "light" else 3 * light

    for side, weight in found.items():
        horizontal = side in ("left", "right")
        length, breadth = (width, height) if horizontal else (height, width)
        across = [found[other] for other in _ACROSS[side] if other in found]
        junction = max((thickness(other) for other in across), default=thickness(weight))
        near, far = _band(length, junction)
        if weight != "double" and len(across) == 2 and set(across) == {"double"} and _OPPOSITE[side] not in found:
            near, far = near + 2 * light, far - 2 * light
        start, end = (0, far) if side in ("left", "up") else (near, length)
        band = _band(breadth, thickness(weight))
        if weight != "double":
            others.append(_arm(side, start, end, band))
            continue
        doubles.append(_arm(side, start, end, band))
        middle = (band[0] + light, band[1] - light)
        # A double that goes straight on keeps its middle open across the junction.
        closed = 0 if found.get(_OPPOSITE[side]) == "double" else light
        inner = (0, end - closed) if side in ("left", "up") else (start + closed, length)
        clear.append(_arm(side, *inner, middle))
    return doubles, clear, others


def _dashes(length: int, count: int, gap: int) -> list[tuple[int, int]]:
    "foot's dashes along a line: equal ones, with what is left over given to the middle."
    dash = (length - (count - 1) * gap) // count
    while dash <= 0 and gap > 1:
        gap -= 1
        dash = (length - (count - 1) * gap) // count
    if dash <= 0:
        return [(0, length)]
    remaining = length - count * dash - (count - 1) * gap
    widths = [dash] * count
    middle = list(range(1, count - 1)) or [count - 1]
    for place, index in enumerate(middle):
        widths[index] += remaining // len(middle) + (remaining % len(middle) if place == len(middle) - 1 else 0)
    spans: list[tuple[int, int]] = []
    start = 0
    for each in widths:
        spans.append((start, start + each))
        start += each + gap
    return spans


def _dashed(words: list[str], width: int, height: int, light: int) -> list[Box]:
    count = {"DOUBLE": 2, "TRIPLE": 3, "QUADRUPLE": 4}[words[1]]
    thick = light if words[0] == "LIGHT" else 3 * light
    if words[-1] == "HORIZONTAL":
        band = _band(height, thick)
        return [(start, band[0], end, band[1]) for start, end in _dashes(width, count, light)]
    gap = light if count == 4 else 3 * light
    band = _band(width, thick)
    return [(band[0], start, band[1], end) for start, end in _dashes(height, count, gap)]


def _blocks(code: int, width: int, height: int) -> tuple[list[Box], int]:
    "A block element's boxes, and how much ink each takes: a shade takes a quarter, a half or three."
    w, h = width, height

    def half_up(value: float) -> int:
        "C's round, as foot rounds a block's edge. Python's round goes to even."
        return int(value + 0.5)

    if code == 0x2580:
        return [(0, 0, w, half_up(h / 2))], INK
    if 0x2581 <= code <= 0x2588:
        return [(0, h - half_up((code - 0x2580) * h / 8), w, h)], INK
    if 0x2589 <= code <= 0x258F:
        return [(0, 0, half_up((0x2590 - code) * w / 8), h)], INK
    if code == 0x2590:
        return [(half_up(w / 2), 0, w, h)], INK
    if 0x2591 <= code <= 0x2593:
        return [(0, 0, w, h)], (code - 0x2590) * 0x40
    if code == 0x2594:
        return [(0, 0, w, half_up(h / 8))], INK
    if code == 0x2595:
        return [(w - half_up(w / 8), 0, w, h)], INK
    # foot rounds a quarter out, so two quarters overlap and never part.
    left, right, upper, lower = (0, (w + 1) // 2), (w // 2, w), (0, (h + 1) // 2), (h // 2, h)
    quarters = {"1": (left, upper), "2": (right, upper), "3": (left, lower), "4": (right, lower)}
    return [(x1, y1, x2, y2) for (x1, x2), (y1, y2) in (quarters[q] for q in _QUADRANTS[code - 0x2596])], INK


def _arc(code: int, width: int, height: int, light: int) -> tuple[Image.Image, list[Box]]:
    """
    A rounded corner: a quarter circle as wide as a light line, and the
    straight lines from its ends to the cell's edges. The radius is the
    same for all four corners, as foot keeps it.
    """
    inner = (min(width, height) - light) // 2
    radius = inner + light / 2
    across, along = _band(width, light), _band(height, light)
    centre_x = across[1] + inner if code in (0x256D, 0x2570) else across[0] - inner
    centre_y = along[1] + inner if code in (0x256D, 0x256E) else along[0] - inner
    big = Image.new("L", (width * SUPERSAMPLE, height * SUPERSAMPLE), 0)
    outer = (radius + light / 2) * SUPERSAMPLE
    x, y = centre_x * SUPERSAMPLE, centre_y * SUPERSAMPLE
    ImageDraw.Draw(big).arc(
        (x - outer, y - outer, x + outer, y + outer), *_ARC_ANGLES[code], fill=INK, width=light * SUPERSAMPLE
    )
    vertical_to = height if code in (0x256D, 0x256E) else 0
    horizontal_to = width if code in (0x256D, 0x2570) else 0
    straight = [
        (across[0], min(centre_y, vertical_to), across[1], max(centre_y, vertical_to)),
        (min(centre_x, horizontal_to), along[0], max(centre_x, horizontal_to), along[1]),
    ]
    return big.reduce(SUPERSAMPLE), straight


def _diagonal(code: int, width: int, height: int, light: int) -> Image.Image:
    "One or both corner-to-corner lines, a light line wide across."
    big = Image.new("L", (width * SUPERSAMPLE, height * SUPERSAMPLE), 0)
    pen = ImageDraw.Draw(big)
    w, h, half = width * SUPERSAMPLE, height * SUPERSAMPLE, light * SUPERSAMPLE / 2
    if code in (0x2571, 0x2573):
        pen.polygon([(w - half, 0), (w + half, 0), (half, h), (-half, h)], fill=INK)
    if code in (0x2572, 0x2573):
        pen.polygon([(-half, 0), (half, 0), (w + half, h), (w - half, h)], fill=INK)
    return big.reduce(SUPERSAMPLE)


def _filled(picture: Image.Image, boxes: list[Box], value: int) -> None:
    pen = ImageDraw.Draw(picture)
    for x1, y1, x2, y2 in boxes:
        if x2 > x1 and y2 > y1:
            pen.rectangle((x1, y1, x2 - 1, y2 - 1), fill=value)


@cache
def mask(code: int, width: int, height: int) -> Image.Image | None:
    """
    How much of the foreground each pixel of a `width` by `height` cell
    takes, for a character in U+2500 to U+259F. None for any other, which
    the font draws. A mask is shared between calls: never change one.
    """
    if not 0x2500 <= code <= 0x259F:
        return None
    light = light_thickness(width, height)
    picture = Image.new("L", (width, height), 0)
    if code >= 0x2580:
        boxes, value = _blocks(code, width, height)
        _filled(picture, boxes, value)
    elif code in _ARC_ANGLES:
        picture, straight = _arc(code, width, height, light)
        _filled(picture, straight, INK)
    elif 0x2571 <= code <= 0x2573:
        picture = _diagonal(code, width, height, light)
    elif (found := arms(code)) is not None:
        doubles, clear, others = _lines(found, width, height, light)
        _filled(picture, doubles, INK)
        _filled(picture, clear, 0)
        _filled(picture, others, INK)
    else:
        _filled(picture, _dashed(_name(code), width, height, light), INK)
    return picture
