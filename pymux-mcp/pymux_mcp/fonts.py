"""
The fonts this package carries for a PNG of a pane.

pymux draws with the system's monospace font when fontconfig names one;
`Fonts.faces` is for a server that finds none, a Nerd Font's four
faces. `Fonts.fallbacks` go with every picture, for each glyph the font
lacks: icon fonts, emoji, and GNU Unifont last
(`pymux/nix/fallback-fonts.nix`). The Nix build writes the paths into `fonts.json`
in the installed package. A checkout run from source has no such file,
and no bundled font.
"""

from __future__ import annotations

import json
from functools import cache
from importlib.resources import files
from typing import NamedTuple

__all__ = ["Fonts", "bundled"]


class Fonts(NamedTuple):
    faces: tuple[str, ...]
    fallbacks: tuple[str, ...]


@cache
def bundled() -> Fonts:
    "The bundled fonts' files, or none."
    found = files(__package__).joinpath("fonts.json")
    if not found.is_file():
        return Fonts((), ())
    held = json.loads(found.read_text())
    return Fonts(tuple(held["faces"]), tuple(held["fallbacks"]))
