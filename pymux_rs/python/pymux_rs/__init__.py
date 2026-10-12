"""
Rust kernels for pymux's image loops.

Each is the Python function of the same name in `pymux/sixel.py` or
`pymux/blocks.py`, byte for byte, and releases the GIL while it works. This package imports
nothing of pymux, which depends on it: `pymux.kernels.install()` puts
the kernels in place. Lillecarl/pymux#574.
"""

from __future__ import annotations

from . import _native

__all__ = ["Vp9Decoder", "average_rgba", "encode_sixel", "rgb_to_rgba", "scale_rgba"]

#: `pymux.sixel.encode_sixel`.
encode_sixel = _native.encode_sixel

#: `pymux.sixel.scale_rgba`, for a size that changes and pixels that hold
#: the whole image. Anything else is the Python's to answer.
scale_rgba = _native.scale_rgba

#: The RGB branch of `pymux.sixel.to_rgba`, for data that holds every
#: pixel: `rgb_to_rgba(data, pixels)`.
rgb_to_rgba = _native.rgb_to_rgba

#: `pymux.blocks.average_rgba`, for a size that changes and pixels that
#: hold the whole image.
average_rgba = _native.average_rgba

#: A window pane's VP9 stream, decoded on the client:
#: `Vp9Decoder().decode(data, rects=None)` answers `(width, height,
#: [(x, y, w, h, rgb)])`. Lillecarl/pymux#639.
Vp9Decoder = _native.Vp9Decoder
