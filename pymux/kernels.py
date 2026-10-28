"""
The Rust kernels, in place of the Python loops they replace.

`pymux.main` calls `install()` when it is imported, which is before
anything draws and after the toolkit is loaded anyway. Not in the
package's `__init__`: `prompt_toolkit_rs` imports prompt_toolkit, and a
detached command such as `pymux ls` must not pay for the toolkit.
`PYTERM_PURE=1` keeps the Python, and so does a build without the
kernels: the answers are the same either way. Lillecarl/pymux#566.

pymux's own image kernels are put in place here and not by their
package, because pymux depends on `pymux_rs` and so it cannot import
pymux. Lillecarl/pymux#574.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from pyte.images import PixelFormat

if TYPE_CHECKING:
    from types import ModuleType

__all__ = ["install"]

#: Set to anything but "" or "0", and `install()` installs nothing.
PURE = "PYTERM_PURE"

#: pymux's own functions that `install()` replaced, by where they live.
_replaced: dict[str, object] = {}


def install() -> None:
    try:
        import pyte_rs
    except ImportError:
        pass
    else:
        pyte_rs.install()

    try:
        import prompt_toolkit_rs
    except ImportError:
        pass
    else:
        prompt_toolkit_rs.install()

    try:
        import pymux_rs
    except ImportError:
        pass
    else:
        if os.environ.get(PURE, "") in ("", "0"):
            _install_images(pymux_rs)


def _install_images(native: ModuleType) -> None:
    """
    `pymux.sixel`'s loops in Rust. The kernels answer the cases a frame
    meets, and every edge -- a size out of bounds, data cut short, a size
    that does not change -- goes to the Python, so nothing answers
    differently.
    """
    from . import blocks, sixel

    if _replaced:
        return
    pure_average = _replaced["pymux.blocks.average_rgba"] = blocks.average_rgba
    pure_encode = _replaced["pymux.sixel.encode_sixel"] = sixel.encode_sixel
    pure_scale = _replaced["pymux.sixel.scale_rgba"] = sixel.scale_rgba
    pure_to_rgba = _replaced["pymux.sixel.to_rgba"] = sixel.to_rgba

    def encode_sixel(width: int, height: int, rgba: bytes, max_colors: int = sixel.MAX_COLORS) -> str | None:
        if not (0 < width and 0 < height and width * height <= sixel.MAX_PIXELS):
            return pure_encode(width, height, rgba, max_colors)
        return native.encode_sixel(width, height, rgba, max(1, min(max_colors, sixel.MAX_COLORS)))

    def scale_rgba(rgba: bytes, width: int, height: int, new_width: int, new_height: int) -> bytes:
        if (
            (new_width, new_height) == (width, height)
            or min(width, height, new_width, new_height) <= 0
            or len(rgba) < width * height * 4
            or new_width * new_height > sixel.MAX_PIXELS
        ):
            return pure_scale(rgba, width, height, new_width, new_height)
        return native.scale_rgba(rgba, width, height, new_width, new_height)

    def to_rgba(image_format: int, width: int, height: int, data: bytes) -> bytes | None:
        pixels = width * height
        if image_format == PixelFormat.RGB and 0 < pixels <= sixel.MAX_PIXELS and len(data) >= pixels * 3:
            return native.rgb_to_rgba(data, pixels)
        return pure_to_rgba(image_format, width, height, data)

    def average_rgba(rgba: bytes, width: int, height: int, new_width: int, new_height: int) -> bytes:
        if (
            (new_width, new_height) == (width, height)
            or min(width, height, new_width, new_height) <= 0
            or len(rgba) < width * height * 4
            or new_width * new_height > sixel.MAX_PIXELS
        ):
            return pure_average(rgba, width, height, new_width, new_height)
        return native.average_rgba(rgba, width, height, new_width, new_height)

    blocks.average_rgba = average_rgba
    sixel.encode_sixel = encode_sixel
    sixel.scale_rgba = scale_rgba
    sixel.to_rgba = to_rgba
