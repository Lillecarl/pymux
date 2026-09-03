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
3. Neither: nothing is drawn. The cells under an image stay blank.

The outer terminal is asked which of these it speaks at attach time.
The placements of the previous frame are remembered, so an unchanged
screen emits nothing.
"""
import base64
import random
import re
import zlib
from typing import Callable, Dict, Iterable, List, NamedTuple, Optional, Tuple

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
        repaint: Optional[Callable[[], None]] = None,
    ) -> None:
        self._write_raw = write_raw
        self._flush = flush
        self._repaint = repaint

        #: True once the outer terminal answered the graphics query.
        self.kitty_supported = False

        #: True when the device attributes name sixel.
        self.sixel_supported = False

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

        # Sixel state. The encoded images are cached: cropping means
        # re-encoding, so a scrolling image would otherwise pay for the
        # whole encoder on every frame.
        self._sixel_placements: Dict[Tuple[int, int, int], str] = {}
        self._sixel_cache: Dict[tuple, Tuple[object, str]] = {}
        self._sixel_redraw = False

    # ------------------------------------------------------------------
    # Detection.

    @property
    def supported(self) -> bool:
        "True when the outer terminal can draw images at all."
        return self.kitty_supported or self.sixel_supported

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

    # ------------------------------------------------------------------
    # Frames.

    def render(self, views: Iterable[PaneView]) -> None:
        """
        Draw the images of one frame. `views` holds one `PaneView` per
        pane that the client can see.

        The kitty graphics protocol comes first: it draws above the
        text, so it never fights with the renderer. Sixel is the
        fallback.
        """
        if self.kitty_supported:
            self._render_kitty(views)
        elif self.sixel_supported:
            self._render_sixel(views)

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

            for placement in self._visible_placements(view):
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

    def _outer_image_id(self, pane_id, placement, image) -> Optional[int]:
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
        try:
            desired, live = self._collect_sixel(views)
        except Exception:
            logger.exception("Encoding the pane images failed.")
            return

        for key in list(self._sixel_cache):
            if key not in live:
                del self._sixel_cache[key]

        changed = desired != self._sixel_placements
        if not changed and not self._sixel_redraw:
            return

        self._sixel_placements = desired
        if changed and self._repaint is not None:
            # The next frame paints the whole screen, which wipes the
            # pixels. Draw again once it did.
            self._repaint()
            self._sixel_redraw = True
        else:
            self._sixel_redraw = False

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
            ) in self._visible_placements(view):
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

    def _sixel_for(
        self, key: tuple, image, source, columns: int, rows: int
    ) -> Optional[str]:
        "The sixel sequence of one placement. (Cached by geometry.)"
        known = self._sixel_cache.get(key)
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

        self._sixel_cache[key] = (image, encoded)
        return encoded

    # ------------------------------------------------------------------

    def reset(self) -> None:
        """
        Remove everything that this client put on the outer terminal.
        (When the client detaches, or when a popup covers the panes.)
        """
        if self._sixel_placements:
            self._sixel_placements = {}
            self._sixel_cache = {}
            self._sixel_redraw = False
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
) -> Optional[bytes]:
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


def _put_command(
    outer_id: int,
    slot: int,
    x: int,
    y: int,
    columns: int,
    rows: int,
    z: int,
    source: Optional[Tuple[int, int, int, int]],
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
