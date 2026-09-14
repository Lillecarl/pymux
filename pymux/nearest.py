"""
Which known theme is this terminal closest to.

A client asks its terminal what colours it really draws with, and the
answers arrive as OSC replies: the default background, the default
foreground and the first sixteen palette entries
(`COLOR_QUERIES` in `colors.py`). pymux holds hundreds of themes that
name the same colours. So the theme of a terminal is not something to
invent -- it is a search. Lillecarl/pymux#346.

**The distance is OKLab, not RGB.** Two colours that are the same
distance apart in RGB can be obviously different and barely different
to the eye, and a search that picks by RGB distance picks a scheme
that looks wrong while scoring well. OKLab is a space built so that
the straight-line distance between two colours is close to how
different they look. The constants below are Bjorn Ottosson's, from
the reference implementation of `oklab`.

**The background weighs most, one colour against one colour.** A
person sees the screen behind everything and one accent hardly at
all. The sixteen still carry most of a full answer between them --
sixteen ones against a three and a two -- and that is on purpose: a
palette that matches all through is what says which scheme this is,
and most terminals answer the two defaults and nothing else anyway.
The weights are a judgement, not a measurement, and they are named
here so that a picture that looks wrong has somewhere to be answered.
"""

from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from pyte.colors import Color, parse_color

__all__ = [
    "NEAREST",
    "nearest_theme",
    "oklab",
    "wanted_from",
]

#: The theme name that means "whichever known theme this terminal is
#: closest to". It is a name like any other: a person writes it, and
#: `show-client-options` says both it and what it matched.
NEAREST = "nearest"

#: How much each answer carries. The screen behind everything, then
#: the text on it, then the sixteen a program draws with.
WEIGHT_BACKGROUND = 3.0
WEIGHT_FOREGROUND = 2.0
WEIGHT_PALETTE = 1.0

#: The order this module keeps an answer in: the sixteen, then the
#: background, then the foreground. `wanted_from` and the candidates
#: both use it, so one index means one colour in both.
_WEIGHTS = [WEIGHT_PALETTE] * 16 + [WEIGHT_BACKGROUND, WEIGHT_FOREGROUND]

#: The roles a theme names for the two defaults. `pane` is the screen
#: the chrome draws on and `text` is what is written on it, which is
#: what a terminal reports as its background and its foreground.
_BACKGROUND_ROLE = "pane"
_FOREGROUND_ROLE = "text"

Lab = Tuple[float, float, float]


def _from_srgb(channel: int) -> float:
    "One 0-255 channel as the light it stands for."
    value = channel / 255.0
    if value <= 0.04045:
        return value / 12.92
    return ((value + 0.055) / 1.055) ** 2.4


def oklab(colour: "Color | str") -> Lab:
    """
    One colour in OKLab: lightness, and two axes of hue.

    Takes a `Color` or a "#rrggbb" string, which is how a terminal's
    answer and a theme's role arrive.
    """
    if isinstance(colour, str):
        parsed = parse_color(colour)
        if parsed is None:
            raise ValueError("not a colour: %r" % (colour,))
        colour = parsed

    red = _from_srgb(colour.red)
    green = _from_srgb(colour.green)
    blue = _from_srgb(colour.blue)

    long_ = 0.4122214708 * red + 0.5363325363 * green + 0.0514459929 * blue
    medium = 0.2119034982 * red + 0.6806995451 * green + 0.1073969566 * blue
    short = 0.0883024619 * red + 0.2817188376 * green + 0.6299787005 * blue

    long_ = long_ ** (1 / 3)
    medium = medium ** (1 / 3)
    short = short ** (1 / 3)

    return (
        0.2104542553 * long_ + 0.7936177850 * medium - 0.0040720468 * short,
        1.9779984951 * long_ - 2.4285922050 * medium + 0.4505937099 * short,
        0.0259040371 * long_ + 0.7827717662 * medium - 0.8086757660 * short,
    )


def _apart(one: Lab, other: Lab) -> float:
    "How far apart two colours look, squared."
    return sum((a - b) ** 2 for a, b in zip(one, other))


def wanted_from(colors) -> List[Optional[Lab]]:
    """
    What one terminal said about itself, in this module's order.

    `colors` is a `DefaultColors`: an entry it never learned is `None`
    here, and the search then scores the candidates on the rest.
    """
    answers: List[Color | None] = list(colors.ansi) + [
        colors.background,
        colors.foreground,
    ]
    return [None if one is None else oklab(one) for one in answers]


def _theme_names() -> List[str]:
    """
    Every theme `set-client-option theme` takes.

    `nearest` is in that list and is not a theme, so it is dropped
    here rather than left for `_candidate` to fail on: a name that
    cannot be built is a name nobody can be matched to, and this one
    is the search itself.
    """
    from pymux.options import ALL_CLIENT_OPTIONS

    offered = ALL_CLIENT_OPTIONS["theme"].get_all_values(None)
    return [name for name in offered if name != NEAREST]


def _candidate(name: str) -> Optional[List[Lab]]:
    "One theme's eighteen colours, or None when it cannot be read."
    from pymux.style import roles_of

    try:
        roles = roles_of(name)
        written = [roles["color-%i" % index] for index in range(16)] + [
            roles[_BACKGROUND_ROLE],
            roles[_FOREGROUND_ROLE],
        ]
        return [oklab(one) for one in written]
    except Exception:
        # A source that cannot build this theme is a theme nobody can
        # be matched to. `set-client-option theme` says so for the
        # person who names it; a search says nothing and moves on.
        return None


#: Every candidate, built once. Reading all of them costs about fifty
#: milliseconds -- 337 base16 schemes, 53 pygments styles and the hand
#: themes -- and a client learns its colours one reply at a time, so a
#: search that rebuilt them would pay that for each of eighteen
#: replies.
_CANDIDATES: Dict[str, List[Lab]] = {}

#: The names the table above was built from. **Not a plain "built
#: yet" flag**: the collection of schemes is read from a path that the
#: environment names, so a process where that path changes has a table
#: of themes that are no longer offered. Reading the names again is
#: cheap; reading every scheme is not.
_BUILT_FROM: Tuple[str, ...] = ()


def candidates() -> Dict[str, List[Lab]]:
    "Every theme that can be matched, by name, built once."
    global _BUILT_FROM

    names = tuple(_theme_names())
    if names != _BUILT_FROM:
        _CANDIDATES.clear()
        for name in names:
            one = _candidate(name)
            if one is not None:
                _CANDIDATES[name] = one
        _BUILT_FROM = names
    return _CANDIDATES


def _score(wanted: Sequence[Optional[Lab]], candidate: Sequence[Lab]) -> float:
    "How far one theme is from what a terminal said."
    return sum(
        weight * _apart(one, candidate[index])
        for index, (one, weight) in enumerate(zip(wanted, _WEIGHTS))
        if one is not None
    )


def nearest_theme(wanted: Iterable[Optional[Lab]]) -> Optional[str]:
    """
    The theme closest to what a terminal said, or `None` when it said
    nothing.

    Ties go to the first name in order, so one terminal always matches
    one theme: two schemes that are the same colours are the same
    answer, and a picture of the match is a picture that can be
    recorded.
    """
    wanted = list(wanted)
    if all(one is None for one in wanted):
        return None

    best = None
    closest = 0.0
    for name in sorted(candidates()):
        distance = _score(wanted, candidates()[name])
        if best is None or distance < closest:
            best, closest = name, distance
    return best
