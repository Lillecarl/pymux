"""
iTerm2's inline images: `OSC 1337 ; File=inline=1;...:b64`, and the
multipart form. `downloads.DownloadReader` joins the file, this turns it
into a PNG and a box of cells, and pyte places it at the cursor
(`GraphicsState.add_png`). From there it is any other image of the pane,
drawn for each client as that client can: kitty graphics, sixel or half
blocks. Lillecarl/pymux#789.

The size comes from `width` and `height`, each a number of cells, a
number of pixels (`Npx`), a share of the pane (`N%`) or `auto`, and
`preserveAspectRatio`, on by default, which fits the image inside the box
the two give. As iTerm2 does, an image wider than the pane is shrunk to
it.
"""

from __future__ import annotations

import io
import math
from typing import IO, Final

from PIL import Image, UnidentifiedImageError

__all__ = ["as_png", "cells_of"]

#: The modes Pillow writes as PNG; a CMYK JPEG, say, is converted first.
_PNG_MODES: Final = frozenset({"1", "L", "LA", "P", "RGB", "RGBA", "I", "I;16"})

#: The PNG signature: a PNG is kept as it came.
_PNG: Final = b"\x89PNG\r\n\x1a\n"


def as_png(source: bytes | IO[bytes]) -> tuple[bytes, tuple[int, int]] | None:
    "An image file as PNG, the first frame of an animation, and its size in pixels; None for no image."
    try:
        with Image.open(io.BytesIO(source) if isinstance(source, bytes) else source) as image:
            image.load()
            size = image.size
            if isinstance(source, bytes) and source.startswith(_PNG):
                return source, size
            encoded = io.BytesIO()
            (image if image.mode in _PNG_MODES else image.convert("RGBA")).save(encoded, "PNG")
    except OSError, UnidentifiedImageError, Image.DecompressionBombError, ValueError:
        return None
    return encoded.getvalue(), size


def _pixels(spec: str, cells: int, cell: int) -> float | None:
    "One of `width` and `height` in pixels: `N` cells, `Npx`, `N%` of the pane's `cells`, or None for auto."
    try:
        if spec.endswith("px"):
            return float(spec[:-2])
        if spec.endswith("%"):
            return float(spec[:-1]) / 100 * cells * cell
        if spec and spec != "auto":
            return float(spec) * cell
    except ValueError:
        return None
    return None


def cells_of(
    arguments: dict[str, str], pixels: tuple[int, int], cell: tuple[int, int], pane: tuple[int, int]
) -> tuple[int, int]:
    "The columns and rows an image of `pixels` covers, by its `arguments`, at `cell`, in a pane of `pane` cells."
    image_width, image_height = pixels
    cell_width, cell_height = cell
    columns, lines = pane
    asked_width = _pixels(arguments.get("width", ""), columns, cell_width)
    asked_height = _pixels(arguments.get("height", ""), lines, cell_height)
    keep_shape = arguments.get("preserveAspectRatio", "1") != "0"

    width, height = float(image_width), float(image_height)
    if asked_width is not None and asked_height is not None:
        scale = min(asked_width / image_width, asked_height / image_height)
        width, height = (image_width * scale, image_height * scale) if keep_shape else (asked_width, asked_height)
    elif asked_height is not None:
        width, height = (image_width * asked_height / image_height if keep_shape else width), asked_height
    elif asked_width is not None:
        width, height = asked_width, (image_height * asked_width / image_width if keep_shape else height)

    room = columns * cell_width
    if width > room:
        height = height * room / width if keep_shape else height
        width = room
    return max(1, math.ceil(width / cell_width)), max(1, math.ceil(height / cell_height))
