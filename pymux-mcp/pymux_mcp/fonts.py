"""
The font this package carries for a PNG of a pane.

pymux draws with the system's monospace font when fontconfig names one.
This one is for a server that finds none: the Nix build writes the
paths of a Nerd Font's four faces into `fonts.json` in the installed
package. A checkout run from source has no such file, and no bundled
font.
"""

from __future__ import annotations

import json
from functools import cache
from importlib.resources import files

__all__ = ["bundled"]


@cache
def bundled() -> tuple[str, ...]:
    "The bundled font's files, regular first, or nothing."
    found = files(__package__).joinpath("fonts.json")
    if not found.is_file():
        return ()
    return tuple(json.loads(found.read_text()))
