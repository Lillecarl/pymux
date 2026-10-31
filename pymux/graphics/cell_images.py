"""
Images drawn in the cells themselves, for a terminal with no kitty
graphics: sixel, whose pixels are the cells, and half blocks, which
need nothing of a terminal but colour (`blocks.py`).

Both share one rule: the images go out after the text of the frame, and
a change asks the renderer for a full repaint, which puts the text back
under an image that moved away. The encoded images are cached, because
cropping means encoding again and a scrolling image would otherwise pay
for the encoder on every frame.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING, Any

import anyio
from prompt_toolkit.output import ColorDepth
from pyte import escape
from pyte.images import GraphicsImage, PixelFormat
from pyte.sequences.csi import csi
from pyte.sequences.strings import dcs
from pyte.sixel import stored_payload

from ..log import logger

# Through the modules, so the kernels `pymux.graphics.kernels.install()` puts there
# are the ones that run.
from . import blocks, sixel
from .blocks import blocks_for, rows_for_cells
from .placements import Source, source_rgba

if TYPE_CHECKING:
    from .graphics import ClientGraphics, PaneView

__all__ = ["CellImages"]

# What a sixel payload is made of: printable ASCII, and the line breaks
# programs wrap it with.
_SIXEL_TEXT_RE = re.compile(r"[ -~\r\n]*")

#: What the cell cache keys an encoded placement by: the image and
#: whatever about the placement changes the encoding.
type CellKey = tuple[object, ...]

#: One frame of a way of drawing whose pixels are the cells: the
#: commands by pane, image and slot, and the cache keys they used.
type Collected = tuple[dict[tuple[int, int, int], str], set[CellKey]]


def _encode_blocks(
    image: GraphicsImage, source: Source | None, columns: int, rows: int, depth: ColorDepth
) -> list[str]:
    "The half block rows of one placement. It runs in a worker thread."
    found = source_rgba(image, source)
    if found is None:
        return []
    pixels, width, height = found
    # Two pixels for every cell, one above the other.
    pixels = blocks.average_rgba(pixels, width, height, columns, rows_for_cells(rows))
    return blocks_for(pixels, columns, rows, depth)


def _encode_sixel(image: GraphicsImage, source: Source | None, target: tuple[int, int] | None) -> str | None:
    """
    The sixel sequence of one placement, scaled to `target` pixels, or
    at its own size for None. It runs in a worker thread.

    `draws_at_its_own_size` says when the own size is right, and
    `put_command` says why the cells below the image are safe then.
    """
    found = source_rgba(image, source)
    if found is None:
        return None
    pixels, width, height = found
    if target is not None:
        pixels = sixel.scale_rgba(pixels, width, height, *target)
        width, height = target
    return sixel.encode_sixel(width, height, pixels)


class CellImages:
    """
    One client's images as cells: what the last frame drew, and the
    encodings it keeps. A client draws one way or the other, never
    both, so sixel and half blocks share this.
    """

    def __init__(self, graphics: ClientGraphics) -> None:
        self.graphics = graphics

        self._placements: dict[tuple[int, int, int], str] = {}
        self._cache: dict[CellKey, tuple[GraphicsImage, Any]] = {}
        self._redraw = False
        #: Cache keys a worker thread is encoding.
        self._pending: set[CellKey] = set()

    def render_sixel(self, views: Iterable[PaneView]) -> None:
        self._render(self._collect_sixel, views)

    def render_blocks(self, views: Iterable[PaneView]) -> None:
        self._render(self._collect_blocks, views)

    def reset(self) -> None:
        "Forget what was drawn and what is being encoded; cells that showed an image are painted again."
        self._pending = set()
        if not self._placements:
            return
        self._placements = {}
        self._cache = {}
        self._redraw = False
        if self.graphics.repaint is not None:
            self.graphics.repaint()

    def _render(self, collect: Callable[[Iterable[PaneView]], Collected], views: Iterable[PaneView]) -> None:
        try:
            desired, live = collect(views)
        except Exception:
            logger.exception("Encoding the pane images failed.")
            return

        for key in list(self._cache):
            if key not in live:
                del self._cache[key]
        self._pending &= live

        changed = desired != self._placements
        if not changed and not self._redraw:
            return

        self._placements = desired
        if changed and self.graphics.repaint is not None:
            # The next frame paints the whole screen, which wipes the
            # pixels. Draw again once it did.
            self.graphics.repaint()
            self._redraw = True
        else:
            self._redraw = False

        if not desired:
            return

        self.graphics.write_in_place("".join(desired[key] for key in sorted(desired)))

    def _collect_sixel(self, views: Iterable[PaneView]) -> Collected:
        "The sixel commands of one frame, and the cache keys they used."
        desired: dict[tuple[int, int, int], str] = {}
        live: set[CellKey] = set()

        for view in views:
            slots: dict[int, int] = {}
            for (
                placement,
                image,
                columns,
                rows,
                x,
                y,
                source,
            ) in self.graphics.placements_of(view):
                slot = slots.get(placement.image_id, 0) + 1
                slots[placement.image_id] = slot

                own_size = self.graphics.draws_at_its_own_size(placement, image, source, columns, rows)
                if own_size and source is None and image.format == PixelFormat.SIXEL:
                    key = (id(image), "as sent")
                    live.add(key)
                    data = self._as_sent(key, image)
                    if data is not None:
                        desired[(view.pane_id, placement.image_id, slot)] = csi(escape.CUP, y + 1, x + 1) + data
                        continue

                key = (
                    id(image),
                    source,
                    columns * self.graphics.cell_width,
                    rows * self.graphics.cell_height,
                    own_size,
                )
                live.add(key)

                data = self._sixel_for(key, image, source, columns, rows, own_size)
                if data is None:
                    continue
                desired[(view.pane_id, placement.image_id, slot)] = csi(escape.CUP, y + 1, x + 1) + data

        return desired, live

    def _collect_blocks(self, views: Iterable[PaneView]) -> Collected:
        "The half block rows of one frame, and the cache keys they used."
        desired: dict[tuple[int, int, int], str] = {}
        live: set[CellKey] = set()
        depth = self.graphics.color_depth

        for view in views:
            slots: dict[int, int] = {}
            for (
                placement,
                image,
                columns,
                rows,
                x,
                y,
                source,
            ) in self.graphics.placements_of(view):
                slot = slots.get(placement.image_id, 0) + 1
                slots[placement.image_id] = slot

                key = (id(image), source, columns, rows, depth)
                live.add(key)

                lines = self._blocks_for(key, image, source, columns, rows, depth)
                if not lines:
                    continue

                # One cursor move for each row: the rows of an image are
                # not the rows of the screen once a pane is not at the
                # left edge.
                desired[(view.pane_id, placement.image_id, slot)] = "".join(
                    csi(escape.CUP, y + offset + 1, x + 1) + line for offset, line in enumerate(lines) if line
                )

        return desired, live

    def _blocks_for(
        self, key: CellKey, image: GraphicsImage, source: Source | None, columns: int, rows: int, depth: ColorDepth
    ) -> list[str] | None:
        "The half block rows of one placement. (Cached by geometry.)"
        return self._entry(key, image, _encode_blocks, image, source, columns, rows, depth)

    def _sixel_for(
        self,
        key: CellKey,
        image: GraphicsImage,
        source: Source | None,
        columns: int,
        rows: int,
        own_size: bool = False,
    ) -> str | None:
        "The sixel sequence of one placement. (Cached by geometry.)"
        target = (
            None if own_size else (max(1, columns * self.graphics.cell_width), max(1, rows * self.graphics.cell_height))
        )
        return self._entry(key, image, _encode_sixel, image, source, target)

    def _as_sent(self, key: CellKey, image: GraphicsImage) -> str | None:
        """
        The program's own sixel sequence, for an image drawn whole at its
        own size. Encoding it again changes the palette, so a gradient
        gains pixels the program never drew. Lillecarl/pymux#603.

        None when the payload holds anything but sixel characters: those
        bytes go to the client's terminal, and must not end the sequence.
        """
        known = self._cache.get(key)
        if known is not None and known[0] is image:
            return known[1]
        payload = stored_payload(image.data)
        sent = dcs(payload) if _SIXEL_TEXT_RE.fullmatch(payload) else None
        self._cache[key] = (image, sent)
        return sent

    def _entry(self, key: CellKey, image: GraphicsImage, work: Callable[..., Any], *args: object) -> Any:
        """
        What `work(*args)` gives for one placement, from the cache.

        With `spawn` a miss answers None, and a worker thread does the
        work: the cells of the placement stay blank until the next
        frame after it is done. The kernels release the GIL, so the
        event loop keeps running beside it. Lillecarl/pymux#575.
        """
        known = self._cache.get(key)
        if known is not None and known[0] is image:
            return known[1]

        if self.graphics.spawn is None:
            result = work(*args)
            self._cache[key] = (image, result)
            return result

        if key not in self._pending:
            self._pending.add(key)
            self.graphics.spawn(self._work_in_thread(key, image, work, args))
        return None

    async def _work_in_thread(
        self, key: CellKey, image: GraphicsImage, work: Callable[..., Any], args: tuple[object, ...]
    ) -> None:
        "Run one `_entry` miss in a thread, and keep the answer."
        try:
            result = await anyio.to_thread.run_sync(work, *args)
        except Exception:
            logger.exception("Encoding a pane image failed.")
            result = None

        # The placement went away while the thread worked, or the
        # client reset: the answer is for nobody.
        if key not in self._pending:
            return
        self._pending.discard(key)
        self._cache[key] = (image, result)
        if self.graphics.repaint is not None:
            self.graphics.repaint()
