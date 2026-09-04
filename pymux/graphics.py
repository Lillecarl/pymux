"""
Image output for the outer terminal of a client.

The panes store the images that their programs transmit (see
`ptterm.graphics` and `ptterm.sixel`), but a pane cannot draw pixels
itself. This module draws them: after every render it re-emits the
images of the visible panes to the outer terminal of one client.

Terminals differ in what they can draw, so the client picks the best
way that its terminal offers:

1. The kitty graphics protocol. The pixel data goes over once per
   client, under an id that this module assigns, and every frame puts
   each image at its cell. Images live above the text, so a placement
   only has to be sent again when it changes.
2. Sixel. The pixels are the cells, so the image is re-encoded for the
   cell size of the terminal, and a changed image asks the renderer for
   a full repaint before it is drawn again.
3. Half blocks. A terminal that draws no pixels still draws coloured
   text, and one cell of text carries two pixels (see `blocks.py`). It
   is a poor picture and it is a picture.

The outer terminal is asked which of these it speaks at attach time.
Half blocks need no answer, so every client draws something, and a
pane never has to be told that images are not available. That is the
rule: translate a capability down, do not take it away.

The placements of the previous frame are remembered, so an unchanged
screen emits nothing.
"""
import base64
import random
import re
import zlib
from typing import Callable, Dict, Iterable, List, NamedTuple, Tuple

from prompt_toolkit.output import ColorDepth
from ptterm.graphics import ASSUMED_CELL_HEIGHT, ASSUMED_CELL_WIDTH

from .blocks import average_rgba, blocks_for, rows_for_cells
from .log import logger
from .sixel import encode_sixel, scale_rgba, to_rgba

__all__ = [
    "CELL_SIZE_QUERY",
    "QUERY_SEQUENCE",
    "ClientGraphics",
    "PaneView",
    "is_query_reply",
]

# Image id of the support query. The reply repeats it, which is how the
# reply is recognised.
QUERY_IMAGE_ID = 31

# Support query: transmit a one pixel RGB image and ask about it. A
# terminal that speaks the protocol answers "ESC _ G i=31;OK ESC \".
# One that does not answers nothing. (The sequence kitty documents.)
QUERY_SEQUENCE = "\x1b_Gi=%i,s=1,v=1,a=q,t=d,f=24;AAAA\x1b\\" % QUERY_IMAGE_ID

# Ask the outer terminal for the size of one cell in pixels. The reply
# is "CSI 6 ; height ; width t". Sixel needs it: the pixels of a sixel
# image are the cells, so the image has to match the cell size.
CELL_SIZE_QUERY = "\x1b[16t"

# The cell size to assume when the terminal does not report one. It
# matches the size that `ptterm.graphics` assumes, so an image then
# keeps the pixel size that the pane gave it.
DEFAULT_CELL_WIDTH = 10
DEFAULT_CELL_HEIGHT = 20

# The largest cell that a report is believed to name.
MAX_CELL_SIZE = 256

# Base64 characters per transmission chunk. The protocol allows at most
# 4096.
CHUNK_SIZE = 4096

# Per client transmission limits. The pane state has its own, much
# larger limits; these bound what one client pushes over its socket.
MAX_IMAGE_BYTES = 64 * 1024 * 1024
MAX_TOTAL_BYTES = 256 * 1024 * 1024

# Save and restore the cursor around a batch. prompt_toolkit tracks the
# cursor of the outer terminal itself, so it has to find it where it
# left it.
SAVE_CURSOR = "\x1b7"
RESTORE_CURSOR = "\x1b8"

# The reply of the query: "ESC _ G i=31;OK ESC \". (Terminals add other
# keys before the semicolon, so the image id is matched on its own.)
_QUERY_REPLY_RE = re.compile(
    r"^\x1b_G(?:[^;]*,)?i=%i(?:,[^;]*)?;OK" % QUERY_IMAGE_ID
)


# Reply of the cell size query.
_CELL_SIZE_REPLY_RE = re.compile(r"^\x1b\[6;(\d+);(\d+)t$")

# Primary device attributes reply. Attribute 4 means sixel.
_DEVICE_ATTRIBUTES_RE = re.compile(r"^\x1b\[\?([\d;]*)c$")


def is_query_reply(data: str) -> bool:
    "True when `data` is the reply of `QUERY_SEQUENCE`."
    return bool(_QUERY_REPLY_RE.match(data))


class PaneView(NamedTuple):
    """
    Where one pane sits on the screen of the client, and what it holds.

    `x` and `y` are the top left cell of the pane on the screen. The
    scroll values turn a row of the scroll buffer of the pane into a row
    of the screen.
    """

    pane_id: int
    x: int
    y: int
    width: int
    height: int
    vertical_scroll: int
    horizontal_scroll: int
    graphics: object
    #: The screen of the pane. The unicode placeholders live in its
    #: cells, so drawing them needs the text and not only the images.
    screen: object = None


class _Placement:
    "One placement as it is emitted to the outer terminal."

    __slots__ = ("image_id", "slot", "command")

    def __init__(self, image_id: int, slot: int, command: str) -> None:
        self.image_id = image_id
        self.slot = slot
        self.command = command


class ClientGraphics:
    """
    The graphics state of the outer terminal of one client.

    :param write_raw: Write a string to the outer terminal, unescaped.
    :param flush: Send what `write_raw` collected.
    :param repaint: Ask the renderer to paint the whole screen again.
        The sixel output needs it: those pixels are the cells, so the
        text under a changed image has to be written once more.
    """

    def __init__(
        self,
        write_raw: Callable[[str], None],
        flush: Callable[[], None],
        repaint: Callable[[], None] | None = None,
        color_depth: Callable[[], ColorDepth] | None = None,
    ) -> None:
        self._write_raw = write_raw
        self._flush = flush
        self._repaint = repaint
        #: The colour depth of the outer terminal, read when it is
        #: needed. Only the half blocks ask: they are cells, so they
        #: take the colours that the terminal has.
        self._color_depth = color_depth

        #: True once the outer terminal answered the graphics query.
        self.kitty_supported = False

        #: True when the device attributes name sixel.
        self.sixel_supported = False

        #: True once the device attributes closed the detection. Half
        #: blocks wait for it: drawing them before the answer would
        #: put text where an image is about to go.
        self.detected = False

        #: Size of one cell of the outer terminal, in pixels.
        self.cell_width = DEFAULT_CELL_WIDTH
        self.cell_height = DEFAULT_CELL_HEIGHT

        # Image ids of the outer terminal start at a random offset, so
        # that pymux does not overwrite the images of another program
        # that shares the terminal.
        self._next_image_id = random.randrange(1 << 20, 1 << 28)

        # (pane id, pane image id) -> (pane image, outer image id)
        self._images: Dict[Tuple[int, int], Tuple[object, int]] = {}
        self._transmitted_bytes = 0

        # (outer image id, slot) -> placement of the previous frame.
        self._placements: Dict[Tuple[int, int], _Placement] = {}

        # The state of a way of drawing whose pixels are the cells:
        # sixel, and half blocks. A client draws one way or the other,
        # never both, so the two share this. The encoded images are
        # cached, because cropping means encoding again and a scrolling
        # image would otherwise pay for the encoder on every frame.
        self._cell_placements: Dict[Tuple[int, int, int], str] = {}
        self._cell_cache: Dict[tuple, Tuple[object, object]] = {}
        self._cell_redraw = False

    # ------------------------------------------------------------------
    # Detection.

    @property
    def supported(self) -> bool:
        """
        True when this client draws images.

        Every terminal that answered the detection does, because half
        blocks need nothing of the terminal but colour. Before the
        answer, only a terminal that already said yes draws.
        """
        return self.kitty_supported or self.sixel_supported or self.detected

    @property
    def blocks_wanted(self) -> bool:
        "True when this client falls back to half blocks."
        return self.detected and not self.kitty_supported and not self.sixel_supported

    @property
    def color_depth(self) -> ColorDepth:
        "What the outer terminal can colour a cell with."
        if self._color_depth is None:
            return ColorDepth.DEPTH_24_BIT
        try:
            return self._color_depth()
        except Exception:
            return ColorDepth.DEPTH_24_BIT

    def handle_reply(self, data: str) -> None:
        "Read what a terminal reply says about the outer terminal."
        if is_query_reply(data):
            self.kitty_supported = True
            return

        match = _CELL_SIZE_REPLY_RE.match(data)
        if match is not None:
            height = int(match.group(1))
            width = int(match.group(2))
            if 0 < width <= MAX_CELL_SIZE and 0 < height <= MAX_CELL_SIZE:
                self.cell_width = width
                self.cell_height = height
            return

        match = _DEVICE_ATTRIBUTES_RE.match(data)
        if match is not None:
            attributes = [p for p in match.group(1).split(";") if p]
            self.sixel_supported = "4" in attributes
            # The last reply of the detection. What did not answer by
            # now is not there, so the half blocks know where they
            # stand.
            was_detected = self.detected
            self.detected = True
            if not was_detected and self._repaint is not None:
                # The frames drawn so far carried no image, because
                # nothing knew yet whether this terminal takes one. Ask
                # for another, or an image on a quiet pane waits for
                # the next thing that happens to the pane.
                self._repaint()

    # ------------------------------------------------------------------
    # Frames.

    def render(self, views: Iterable[PaneView]) -> None:
        """
        Draw the images of one frame. `views` holds one `PaneView` per
        pane that the client can see.

        The kitty graphics protocol comes first: it draws above the
        text, so it never fights with the renderer. Sixel comes next.
        Half blocks are what is left, and they need nothing of the
        terminal, so every client draws something.
        """
        if self.kitty_supported:
            self._render_kitty(views)
        elif self.sixel_supported:
            self._render_sixel(views)
        elif self.detected:
            self._render_blocks(views)

    def _render_kitty(self, views: Iterable[PaneView]) -> None:
        try:
            desired = self._collect(views)
        except Exception:
            # A frame is not worth killing the client for.
            logger.exception("Collecting graphics placements failed.")
            return

        commands: List[str] = []

        # Placements that are gone. (A placement that only moved is
        # replaced in place by the put command below, so it is not
        # deleted first: that would make the image flicker.)
        for key, placement in self._placements.items():
            if key not in desired:
                commands.append(
                    "\x1b_Ga=d,d=i,i=%i,p=%i,q=2\x1b\\"
                    % (placement.image_id, placement.slot)
                )

        # New and changed placements.
        for key, placement in desired.items():
            previous = self._placements.get(key)
            if previous is None or previous.command != placement.command:
                commands.append(placement.command)

        self._placements = desired

        if not commands:
            return

        self._write_raw(SAVE_CURSOR + "".join(commands) + RESTORE_CURSOR)
        self._flush()

    def _collect(
        self, views: Iterable[PaneView]
    ) -> Dict[Tuple[int, int], _Placement]:
        "The placements that this frame should show."
        desired: Dict[Tuple[int, int], _Placement] = {}
        live_keys = set()

        for view in views:
            # An image stays on the outer terminal as long as the pane
            # holds it, also while no placement shows it. Scrolling an
            # image in and out must not re-transmit the pixels.
            for image_id in view.graphics.images_by_id:
                live_keys.add((view.pane_id, image_id))

            slots: Dict[int, int] = {}

            for placement in self._pane_placements(view):
                (
                    pane_placement,
                    image,
                    columns,
                    rows,
                    x,
                    y,
                    source,
                ) = placement

                outer_id = self._outer_image_id(
                    view.pane_id, pane_placement, image
                )
                if outer_id is None:
                    continue

                slot = slots.get(outer_id, 0) + 1
                slots[outer_id] = slot

                desired[(outer_id, slot)] = _Placement(
                    outer_id,
                    slot,
                    _put_command(
                        outer_id,
                        slot,
                        x,
                        y,
                        columns,
                        rows,
                        pane_placement.z,
                        source,
                    ),
                )

        self._forget_unused_images(live_keys)
        return desired

    @classmethod
    def _pane_placements(cls, view: PaneView):
        """
        Everything of one pane that the client should draw: the plain
        placements, and the images that the unicode placeholders in the
        text of the pane point at.
        """
        yield from cls._visible_placements(view)
        yield from cls._placeholder_placements(view)

    @staticmethod
    def _placeholder_placements(view: PaneView):
        """
        Yield the images that the unicode placeholders of one pane
        point at.

        A placeholder cell says which cell of which image it stands
        for. `BetterScreen.placeholder_runs` gathers the neighbouring
        cells into rectangles; each rectangle becomes one placement
        with the matching piece of the image.
        """
        screen = view.screen
        if screen is None or not hasattr(screen, "placeholder_runs"):
            return

        first_row = view.vertical_scroll
        last_row = first_row + view.height - 1
        for run in screen.placeholder_runs(first_row, last_row):
            placement = view.graphics.virtual_placement(
                run.image_id, run.placement_id
            )
            if placement is None or not placement.columns or not placement.rows:
                continue
            image = view.graphics.images_by_id.get(run.image_id)
            if image is None or not image.width or not image.height:
                continue

            x = run.column - view.horizontal_scroll
            y = run.row - first_row

            crop_left = max(0, -x)
            columns = min(run.columns - crop_left, view.width - x - crop_left)
            rows = min(run.rows, view.height - y)
            if columns <= 0 or rows <= 0:
                continue  # Outside the pane.

            source = _placeholder_source(
                image,
                placement,
                run.image_column + crop_left,
                run.image_row,
                columns,
                rows,
            )
            if source is None:
                continue  # The empty border around a fitted image.

            yield (
                placement,
                image,
                columns,
                rows,
                view.x + x + crop_left,
                view.y + y,
                source,
            )

    @staticmethod
    def _visible_placements(view: PaneView):
        """
        Yield the placements of one pane that the client can see, with
        their position on the screen and their cropping.

        The pane stores rows of its scroll buffer, so the scroll of the
        pane turns them into screen rows. A placement that hangs over
        the edge of the pane is cropped instead of dropped: kitty takes
        a source rectangle in pixels on the put command.
        """
        width = view.width
        height = view.height

        for placement in view.graphics.placements:
            if placement.virtual:
                continue  # Unicode placeholders: the text carries them.
            image = view.graphics.images_by_id.get(placement.image_id)
            if image is None or not placement.columns or not placement.rows:
                continue

            # Position in the window, before cropping.
            x = placement.x - view.horizontal_scroll
            y = placement.y - view.vertical_scroll

            crop_left = max(0, -x)
            crop_top = max(0, -y)
            columns = min(placement.columns - crop_left, width - x - crop_left)
            rows = min(placement.rows - crop_top, height - y - crop_top)
            if columns <= 0 or rows <= 0:
                continue  # Fully outside the pane.

            if (
                crop_left
                or crop_top
                or columns != placement.columns
                or rows != placement.rows
            ):
                # Crop in pixels: the cell box of the placement maps
                # onto the pixels of the image.
                px_per_column = image.width / placement.columns
                px_per_row = image.height / placement.rows
                source = (
                    int(crop_left * px_per_column),
                    int(crop_top * px_per_row),
                    max(1, int(columns * px_per_column)),
                    max(1, int(rows * px_per_row)),
                )
            else:
                source = None

            yield (
                placement,
                image,
                columns,
                rows,
                view.x + x + crop_left,
                view.y + y + crop_top,
                source,
            )

    # ------------------------------------------------------------------
    # Image transmission.

    def _outer_image_id(self, pane_id, placement, image) -> int | None:
        """
        The id of `image` on the outer terminal. Transmits the image the
        first time it is needed. Returns None when it cannot be sent.
        """
        key = (pane_id, placement.image_id)
        known = self._images.get(key)
        if known is not None and known[0] is image:
            return known[1]

        if len(image.data) > MAX_IMAGE_BYTES:
            return None
        if self._transmitted_bytes + len(image.data) > MAX_TOTAL_BYTES:
            return None

        if known is not None:
            # The pane replaced the image under the same id. Drop the
            # old one from the outer terminal.
            self._delete_image(known[1])

        outer_id = self._next_image_id
        self._next_image_id += 1

        try:
            self._transmit(outer_id, image)
        except Exception:
            logger.exception("Transmitting an image to the client failed.")
            return None

        self._transmitted_bytes += len(image.data)
        self._images[key] = (image, outer_id)
        return outer_id

    def _transmit(self, outer_id: int, image) -> None:
        "Send the pixel data of one image to the outer terminal."
        if image.format == 100:
            keys = "f=100"
            payload = base64.b64encode(image.data).decode("ascii")
        else:
            # Compress: the data travels through the client socket.
            keys = "f=%i,s=%i,v=%i,o=z" % (
                image.format,
                image.width,
                image.height,
            )
            payload = base64.b64encode(zlib.compress(image.data, 1)).decode(
                "ascii"
            )

        chunks = [
            payload[i : i + CHUNK_SIZE]
            for i in range(0, len(payload), CHUNK_SIZE)
        ] or [""]

        for index, chunk in enumerate(chunks):
            more = 1 if index < len(chunks) - 1 else 0
            if index == 0:
                # The keys of the first chunk govern the transmission.
                control = "a=t,i=%i,t=d,q=2,%s,m=%i" % (outer_id, keys, more)
            else:
                control = "m=%i" % more
            self._write_raw("\x1b_G%s;%s\x1b\\" % (control, chunk))
            self._flush()

    def _forget_unused_images(self, live_keys) -> None:
        "Free the images that no pane holds anymore."
        for key, (image, outer_id) in list(self._images.items()):
            if key not in live_keys:
                self._delete_image(outer_id)
                self._transmitted_bytes -= len(image.data)
                del self._images[key]

    def _delete_image(self, outer_id: int) -> None:
        self._write_raw("\x1b_Ga=d,d=I,i=%i,q=2\x1b\\" % outer_id)

    # ------------------------------------------------------------------
    # Sixel.

    def _render_sixel(self, views: Iterable[PaneView]) -> None:
        """
        Draw the images as sixel.

        Sixel pixels are the cells, so the renderer and the images
        write to the same place. Two rules keep them apart: the images
        go out after the text of the frame, and a change asks for a
        full repaint, which puts the text back under the image that
        moved away.
        """
        self._render_cells(self._collect_sixel, views)

    def _render_blocks(self, views: Iterable[PaneView]) -> None:
        """
        Draw the images as half blocks.

        These are cells, like sixel pixels are, so they follow the same
        two rules: they go out after the text of the frame, and a
        change asks for a full repaint, which puts the text back where
        an image moved away from.
        """
        self._render_cells(self._collect_blocks, views)

    def _render_cells(self, collect, views: Iterable[PaneView]) -> None:
        """
        Draw one frame of a way whose pixels are the cells.

        The images go out after the text of the frame, and a change
        asks the renderer for a full repaint, so the text comes back
        under an image that moved away.
        """
        try:
            desired, live = collect(views)
        except Exception:
            logger.exception("Encoding the pane images failed.")
            return

        for key in list(self._cell_cache):
            if key not in live:
                del self._cell_cache[key]

        changed = desired != self._cell_placements
        if not changed and not self._cell_redraw:
            return

        self._cell_placements = desired
        if changed and self._repaint is not None:
            # The next frame paints the whole screen, which wipes the
            # pixels. Draw again once it did.
            self._repaint()
            self._cell_redraw = True
        else:
            self._cell_redraw = False

        if not desired:
            return

        self._write_raw(
            SAVE_CURSOR
            + "".join(desired[key] for key in sorted(desired))
            + RESTORE_CURSOR
        )
        self._flush()

    def _collect_sixel(
        self, views: Iterable[PaneView]
    ) -> Tuple[Dict[Tuple[int, int, int], str], set]:
        "The sixel commands of one frame, and the cache keys they used."
        desired: Dict[Tuple[int, int, int], str] = {}
        live = set()

        for view in views:
            slots: Dict[int, int] = {}
            for (
                placement,
                image,
                columns,
                rows,
                x,
                y,
                source,
            ) in self._pane_placements(view):
                slot = slots.get(placement.image_id, 0) + 1
                slots[placement.image_id] = slot

                key = (
                    id(image),
                    source,
                    columns * self.cell_width,
                    rows * self.cell_height,
                )
                live.add(key)

                data = self._sixel_for(key, image, source, columns, rows)
                if data is None:
                    continue
                desired[(view.pane_id, placement.image_id, slot)] = (
                    "\x1b[%i;%iH%s" % (y + 1, x + 1, data)
                )

        return desired, live

    def _collect_blocks(
        self, views: Iterable[PaneView]
    ) -> Tuple[Dict[Tuple[int, int, int], str], set]:
        "The half block rows of one frame, and the cache keys they used."
        desired: Dict[Tuple[int, int, int], str] = {}
        live = set()
        depth = self.color_depth

        for view in views:
            slots: Dict[int, int] = {}
            for (
                placement,
                image,
                columns,
                rows,
                x,
                y,
                source,
            ) in self._pane_placements(view):
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
                    "\x1b[%i;%iH%s" % (y + offset + 1, x + 1, line)
                    for offset, line in enumerate(lines)
                    if line
                )

        return desired, live

    def _blocks_for(
        self, key: tuple, image, source, columns: int, rows: int, depth
    ) -> List[str]:
        "The half block rows of one placement. (Cached by geometry.)"
        known = self._cell_cache.get(key)
        if known is not None and known[0] is image:
            return known[1]

        pixels = to_rgba(image.format, image.width, image.height, image.data)
        if pixels is None:
            return []

        width, height = image.width, image.height
        if source is not None:
            pixels = _crop_rgba(pixels, width, height, source)
            if pixels is None:
                return []
            width, height = source[2], source[3]

        # Two pixels for every cell, one above the other.
        pixels = average_rgba(pixels, width, height, columns, rows_for_cells(rows))
        lines = blocks_for(pixels, columns, rows, depth)

        self._cell_cache[key] = (image, lines)
        return lines

    def _sixel_for(
        self, key: tuple, image, source, columns: int, rows: int
    ) -> str | None:
        "The sixel sequence of one placement. (Cached by geometry.)"
        known = self._cell_cache.get(key)
        if known is not None and known[0] is image:
            return known[1]

        pixels = to_rgba(image.format, image.width, image.height, image.data)
        if pixels is None:
            return None

        width, height = image.width, image.height
        if source is not None:
            pixels = _crop_rgba(pixels, width, height, source)
            if pixels is None:
                return None
            width, height = source[2], source[3]

        target_width = max(1, columns * self.cell_width)
        target_height = max(1, rows * self.cell_height)
        pixels = scale_rgba(pixels, width, height, target_width, target_height)

        encoded = encode_sixel(target_width, target_height, pixels)
        if encoded is None:
            return None

        self._cell_cache[key] = (image, encoded)
        return encoded

    # ------------------------------------------------------------------

    def reset(self) -> None:
        """
        Remove everything that this client put on the outer terminal.
        (When the client detaches, or when a popup covers the panes.)
        """
        if self._cell_placements:
            self._cell_placements = {}
            self._cell_cache = {}
            self._cell_redraw = False
            if self._repaint is not None:
                self._repaint()

        if not self._images and not self._placements:
            return
        for image, outer_id in self._images.values():
            self._delete_image(outer_id)
        self._images = {}
        self._placements = {}
        self._transmitted_bytes = 0
        self._flush()


def _crop_rgba(
    pixels: bytes, width: int, height: int, source: Tuple[int, int, int, int]
) -> bytes | None:
    "Cut the source rectangle out of RGBA pixels."
    left, top, crop_width, crop_height = source
    if left < 0 or top < 0 or crop_width <= 0 or crop_height <= 0:
        return None
    if left + crop_width > width or top + crop_height > height:
        return None

    out = bytearray()
    for row in range(top, top + crop_height):
        start = (row * width + left) * 4
        out += pixels[start : start + crop_width * 4]
    return bytes(out)


def _placeholder_source(image, placement, image_column, image_row, columns, rows):
    """
    The rectangle of `image`, in pixels, that a run of placeholder
    cells shows. None when the run falls outside the image.

    kitty fits the image into the box that the placement covers,
    keeping the proportions and centring what is left over. A cell of
    the box therefore does not map onto a fixed piece of the image, and
    a cell along the border may show none of it.
    """
    box_width = placement.columns * ASSUMED_CELL_WIDTH
    box_height = placement.rows * ASSUMED_CELL_HEIGHT

    if image.width * box_height > image.height * box_width:
        # The image fills the box sideways. What is left over is a
        # border above and below it.
        scale = box_width / image.width
        x_offset = 0.0
        y_offset = (box_height - image.height * scale) / 2
    else:
        scale = box_height / image.height
        y_offset = 0.0
        x_offset = (box_width - image.width * scale) / 2

    left = (image_column * ASSUMED_CELL_WIDTH - x_offset) / scale
    top = (image_row * ASSUMED_CELL_HEIGHT - y_offset) / scale
    right = left + columns * ASSUMED_CELL_WIDTH / scale
    bottom = top + rows * ASSUMED_CELL_HEIGHT / scale

    # Cut away what falls outside the image. A run along the border
    # keeps all of its cells, so the piece that is left stretches over
    # them. That is at most one cell of error, on the first row and the
    # last one.
    left, top = max(0.0, left), max(0.0, top)
    right = min(float(image.width), right)
    bottom = min(float(image.height), bottom)
    if right - left < 1 or bottom - top < 1:
        return None

    return (
        int(left),
        int(top),
        max(1, int(right - left)),
        max(1, int(bottom - top)),
    )


def _put_command(
    outer_id: int,
    slot: int,
    x: int,
    y: int,
    columns: int,
    rows: int,
    z: int,
    source: Tuple[int, int, int, int] | None,
) -> str:
    """
    The escape sequences that put one image at (`x`, `y`) on the outer
    terminal. The cursor moves there first; `C=1` keeps the image from
    moving it again.
    """
    parts = [
        "a=p",
        "i=%i" % outer_id,
        "p=%i" % slot,
        "c=%i" % columns,
        "r=%i" % rows,
        "C=1",
        "q=2",
    ]
    if z:
        parts.append("z=%i" % z)
    if source is not None:
        parts.append("x=%i,y=%i,w=%i,h=%i" % source)

    return "\x1b[%i;%iH\x1b_G%s\x1b\\" % (y + 1, x + 1, ",".join(parts))
