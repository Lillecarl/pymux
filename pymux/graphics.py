"""
Image output for the outer terminal of a client.

The panes store the images that their programs transmit (see
`pyte.images` and `pyte.sixel`), but a pane cannot draw pixels
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

from __future__ import annotations

import base64
import random
import re
import zlib
from collections.abc import Awaitable, Callable, Coroutine, Iterable, Iterator, Sequence
from typing import Any, NamedTuple

import anyio
from libpymux.protocol import Codec, Field, Message, Packet
from prompt_toolkit.output import ColorDepth
from pyte import escape
from pyte.images import GraphicsImage, GraphicsPlacement, PixelFormat
from pyte.sequences import Csi, csi, esc
from pyte.terminfo import DeviceExtension

# Through the modules, so the kernels `pymux.kernels.install()` puts there
# are the ones that run.
from . import sixel
from .cell_images import CellImages
from .kitty_frames import SHM_PROBE_ID
from .log import logger
from .placements import (
    Placed,
    Source,
    drawn_size,
    pane_placements,
    put_command,
    visible_placements,
)
from .plane import Rect

__all__ = [
    "CELL_SIZE_QUERY",
    "QUERY_SEQUENCE",
    "VERSION_QUERY",
    "ClientGraphics",
    "OuterImageIds",
    "PaneView",
    "is_query_reply",
]

# Image id of the support query. The reply repeats it, which is how the
# reply is recognised.
QUERY_IMAGE_ID = 31

# Support query: transmit a one pixel RGB image and ask about it. A
# terminal that speaks the protocol answers "ESC _ G i=31;OK ESC \".
# One that does not answers nothing. (The sequence kitty documents.)
# Three bytes of payload, because the format is three bytes a pixel.
QUERY_SEQUENCE = "\x1b_Gi=%i,s=1,v=1,a=q,t=d,f=%i;AAAA\x1b\\" % (
    QUERY_IMAGE_ID,
    PixelFormat.RGB,
)

# Ask the outer terminal for the size of one cell in pixels. "CSI 16 t"
# is xterm's window operation for it, which `pyte.parameters.WindowOp`
# calls REPORT_CELL_SIZE_PIXELS. Sixel needs it: the pixels of a sixel
# image are the cells, so the image has to match the cell size.
CELL_SIZE_QUERY = csi(Csi.XTWINOPS, 16)

# Ask the outer terminal its name and version (XTVERSION). It answers
# "DCS > | text ST" whatever TERM says, so ssh and a TERM a person set
# do not hide it.
VERSION_QUERY = csi(Csi.XTVERSION, 0, private=">")

# The cell size to assume when the terminal does not report one. It
# matches the size that `pyte.images` assumes, so an image then
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
SAVE_CURSOR = esc(escape.DECSC)
RESTORE_CURSOR = esc(escape.DECRC)

# The reply of the query: "ESC _ G i=31;OK ESC \". (Terminals add other
# keys before the semicolon, so the image id is matched on its own.)
_QUERY_REPLY_RE = re.compile(r"^\x1b_G(?:[^;]*,)?i=%i(?:,[^;]*)?;OK" % QUERY_IMAGE_ID)

# The reply of the shared memory probe, when kitty read the object.
# Lillecarl/pymux#634.
_SHM_REPLY_RE = re.compile(r"^\x1b_G(?:[^;]*,)?i=%i(?:,[^;]*)?;OK" % SHM_PROBE_ID)


# Reply of the cell size query: "CSI 6 ; height ; width t". The 6 is
# xterm's answer code for the 16 that was asked; it is not a
# `WindowOp`, which numbers the questions.
_CELL_SIZE_REPLY_RE = re.compile(r"^\x1b\[6;(\d+);(\d+)t$")

# Primary device attributes reply. One of the numbers it lists is the
# terminal saying it draws sixels.
_DEVICE_ATTRIBUTES_RE = re.compile(r"^\x1b\[\?([\d;]*)c$")
_SIXEL_ATTRIBUTE = str(int(DeviceExtension.SIXEL))

_VERSION_REPLY_RE = re.compile(r"^\x1bP>\|(.*)\x1b\\$", re.DOTALL)

#: A kitty graphics reply naming an image: OK or an error, both say the
#: terminal processed the command.
_IMAGE_REPLY_RE = re.compile(r"^\x1b_G(?:[^;]*,)?i=(\d+)(?:,[^;]*)?;")


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
    graphics: Any
    #: The screen of the pane. The unicode placeholders live in its
    #: cells, so drawing them needs the text and not only the images.
    screen: Any = None
    #: What the client drew over the pane, in cells of its screen: a
    #: popup, a menu, the which-key box. An image is cut around them.
    #: `layout.pane_covers`. Lillecarl/pymux#816.
    covered: tuple[Rect, ...] = ()


class WindowView(NamedTuple):
    """
    A pane that shows a Wayland window, as one client sees it.

    `await update(outer_id, since)` brings the terminal up to date: it
    answers `(serial, commands, whole)`, as `pysmithay.Compositor.kitty_update`
    does, so the pixels and the damage never pass through here.
    `await rects(since)` answers the same as raw pixels, as
    `pysmithay.Compositor.frame_rects` does, for a client that encodes
    them itself. `shown(sent)` says this client is up to date, which lets
    the window draw its next frame; `sent` says pixels went out to get it
    there.

    `codec` is the window's `window-codec`. `await video(encoder)` answers
    the next frame in `encoder`'s VP9 stream, as
    `pysmithay.Compositor.vp9_frame` does, and `encoder()` makes one: a
    client holds one stream per window pane. Lillecarl/pymux#639.
    """

    pane_id: int
    x: int
    y: int
    width: int
    height: int
    update: Callable[[int, int | None], Awaitable[tuple[int, str | None, bool]]]
    shown: Callable[[bool], None]
    rects: Callable[[int | None], Awaitable[FrameRects | None]]
    codec: str = ""
    video: Callable[[Any], Awaitable[VideoFrame | None]] | None = None
    encoder: Callable[[], Any] | None = None


#: What `WindowView.rects` answers: serial, whole, width, height, and
#: the rectangles as `(x, y, w, h, rgb)`.
FrameRects = tuple[int, bool, int, int, list[tuple[int, int, int, int, bytes]]]

#: What `WindowView.video` answers: serial, keyframe, width, height, the
#: encoded frame, and the rectangles that changed as `(x, y, w, h)`.
VideoFrame = tuple[int, bool, int, int, bytes, list[tuple[int, int, int, int]]]

#: The most pixel bytes one `window-frame` packet carries. A key queued
#: behind a part waits for at most this much on the link.
FRAME_PART_BYTES = 64 * 1024


def frame_parts(image_id: int, frame: FrameRects, after: str) -> list[Message]:
    """
    A frame as `window-frame` packets of at most `FRAME_PART_BYTES` of
    pixels each: every rectangle cut into bands of whole rows, which the
    client puts back together. Lillecarl/pymux#634.
    """
    _serial, whole, width, height, rects = frame
    head: Message = {
        Field.CMD: Packet.WINDOW_FRAME,
        Field.IMAGE: image_id,
        Field.WHOLE: whole,
        Field.WIDTH: width,
        Field.HEIGHT: height,
    }
    parts: list[Message] = []
    bands: list[list[int | bytes]] = []
    size = 0
    for x, y, w, h, pixels in rects:
        row = w * 3
        rows = max(1, FRAME_PART_BYTES // max(1, row))
        for top in range(0, h, rows):
            count = min(rows, h - top)
            if bands and size + count * row > FRAME_PART_BYTES:
                parts.append({**head, Field.RECTS: bands})
                bands = []
                size = 0
            bands.append([x, y + top, w, count, pixels[top * row : (top + count) * row]])
            size += count * row
    parts.append({**head, Field.RECTS: bands, Field.LAST: True, Field.AFTER: after})
    return parts


def video_parts(image_id: int, codec: Codec, frame: VideoFrame, after: str) -> list[Message]:
    """
    An encoded frame as `window-frame` packets of at most
    `FRAME_PART_BYTES` each. The last part says what changed, which the
    client takes from the frame it decodes. Lillecarl/pymux#639.
    """
    _serial, keyframe, width, height, data, rects = frame
    head: Message = {
        Field.CMD: Packet.WINDOW_FRAME,
        Field.IMAGE: image_id,
        Field.CODEC: codec,
        Field.WHOLE: keyframe,
        Field.WIDTH: width,
        Field.HEIGHT: height,
    }
    pieces = [data[at : at + FRAME_PART_BYTES] for at in range(0, len(data), FRAME_PART_BYTES)] or [b""]
    parts = [{**head, Field.VIDEO: piece} for piece in pieces]
    parts[-1].update({Field.RECTS: [list(rect) for rect in rects], Field.LAST: True, Field.AFTER: after})
    return parts


#: The pane image id a window pane's picture takes in `OuterImageIds`.
#: A program's own images count up from one.
WINDOW_IMAGE = -1

#: kitty draws a placement whose z-index is under this one under every
#: cell with a background colour of its own, and not only under the text.
UNDER_BACKGROUNDS = -(1 << 30)

#: The z-index a window pane's picture is placed at: under every cell
#: with a background colour of its own. The pane's cells take the default
#: background, so the picture shows there, and a prompt, a menu or a
#: message drawn over the pane covers it. Zero or above would draw it
#: over them all. Lillecarl/pymux#647.
WINDOW_Z = UNDER_BACKGROUNDS - 1


class _ShownWindow(NamedTuple):
    "What one client holds of a window pane: its image, frame and placement."

    outer_id: int
    serial: int
    put: str
    #: The codec of the frames sent, or "" for raw pixels.
    codec: str = ""


#: The placement id of a virtual placement whose program gave none. A
#: placeholder with no underline colour may show any virtual placement
#: of its image, and the slots pymux numbers itself count up from one.
VIRTUAL_SLOT = 0xFFFFFF

#: The first id past what the 24 bits of a colour carry.
_COLOUR_IDS = 1 << 24


class OuterImageIds:
    """
    The id each pane image has on the terminals of the clients.

    **Every client takes the same id.** A pane passes its image cells to
    a kitty client as text with the id in the colour, and a pane builds
    that text once for every client. So the id cannot be a client's own.

    An image that the pane replaces keeps its id, and so does one that
    the pane drops: a row built with the id can stand on the screen long
    after. The ids start at a random offset, so that pymux does not
    overwrite the images of another program that shares the terminal,
    and stay under 24 bits, so no placeholder needs the third mark.
    Lillecarl/pymux#600.
    """

    def __init__(self) -> None:
        self._ids: dict[tuple[int, int], int] = {}
        self._next = random.randrange(1 << 20, 1 << 23)

    def id_of(self, pane_id: int, image_id: int) -> int:
        key = (pane_id, image_id)
        found = self._ids.get(key)
        if found is None:
            found = self._ids[key] = self._next
            self._next = self._next + 1 if self._next + 1 < _COLOUR_IDS else 1
        return found


class _Placement:
    """
    One placement as it is emitted to the outer terminal, and the rows of
    the screen it covers. A virtual placement covers none: kitty draws it
    through the placeholder cells, which a scroll moves with the text.
    """

    __slots__ = ("command", "image_id", "rows", "slot")

    def __init__(self, image_id: int, slot: int, command: str, rows: range = range(0)) -> None:
        self.image_id = image_id
        self.slot = slot
        self.command = command
        self.rows = rows


#: A scroll the renderer wrote: first row, last row, distance.
Scroll = tuple[int, int, int]


def moved_by(rows: range, scrolls: Sequence[Scroll]) -> bool:
    """
    Whether a scroll moved what lies on these rows: kitty moves an image
    that lies wholly inside a scrolled region, and only such an image
    (`scroll_filter_margins_func`, `kitty/graphics.c`). Lillecarl/pymux#821.
    """
    return bool(rows) and any(top <= rows[0] and rows[-1] <= bottom for top, bottom, _distance in scrolls)


class ClientGraphics:
    """
    The graphics state of the outer terminal of one client.

    :param write_raw: Write a string to the outer terminal, unescaped.
    :param flush: Send what `write_raw` collected.
    :param repaint: Ask the renderer to paint the whole screen again.
        The sixel output needs it: those pixels are the cells, so the
        text under a changed image has to be written once more.
    :param spawn: Run a coroutine in the background of this client.
        With it, a kitty transmission and the sixel and half block
        encoding run in a worker thread, and the frame goes on without
        the image. Without it, the frame does the work.
    :param image_ids: The ids of the pane images on the outer terminal,
        shared by every client of the server.
    :param reads_placeholders: Whether the outer terminal draws kitty's
        unicode placeholders. See `forwards_placeholders`.
    """

    def __init__(
        self,
        write_raw: Callable[[str], None],
        flush: Callable[[], None],
        repaint: Callable[[], None] | None = None,
        color_depth: Callable[[], ColorDepth] | None = None,
        spawn: Callable[[Coroutine[Any, Any, None]], None] | None = None,
        image_ids: OuterImageIds | None = None,
        reads_placeholders: Callable[[], bool] | None = None,
    ) -> None:
        self._write_raw = write_raw
        self._flush = flush
        self.repaint = repaint
        self.spawn = spawn
        #: The colour depth of the outer terminal, read when it is
        #: needed. Only the half blocks ask: they are cells, so they
        #: take the colours that the terminal has.
        self._color_depth = color_depth

        #: True once the outer terminal answered the graphics query.
        self.kitty_supported = False

        #: Writes a window pane's frame as `window-frame` packets, for a
        #: client that encodes the pixels for its terminal itself; None
        #: when the frame goes as kitty commands. Lillecarl/pymux#634.
        self.send_frame: Callable[[list[Message]], Awaitable[None]] | None = None

        #: Waits until the client answered the window frames it was
        #: sent, for a client that answers; a window pane's next frame
        #: is read only after it. Lillecarl/pymux#641.
        self.frame_room: Callable[[], Awaitable[None]] | None = None

        #: The codecs this client decodes, as its `start-gui` says.
        #: Lillecarl/pymux#639.
        self.codecs: frozenset[str] = frozenset()

        #: Called when the terminal answered the shared memory probe
        #: (`kitty_frames.shm_probe`) with OK.
        self.on_shm: Callable[[], None] | None = None

        #: True when the device attributes name sixel.
        self.sixel_supported = False

        #: True once the device attributes closed the detection. Half
        #: blocks wait for it: drawing them before the answer would
        #: put text where an image is about to go.
        self.detected = False

        #: Size of one cell of the outer terminal, in pixels.
        self.cell_width = DEFAULT_CELL_WIDTH
        self.cell_height = DEFAULT_CELL_HEIGHT

        #: True once the terminal answered "CSI 16 t". The pane takes
        #: its cell size from a client, and a default is not an answer:
        #: a terminal that never replied must not hold the pane at ten
        #: by twenty while a client that did reply waits behind it.
        #: Lillecarl/pymux#369.
        self.cell_size_known = False

        #: What the terminal answered XTVERSION with, or "".
        self.terminal_version = ""

        self._image_ids = image_ids if image_ids is not None else OuterImageIds()
        self._reads_placeholders = reads_placeholders

        # (pane id, pane image id) -> (pane image, outer image id)
        self._images: dict[tuple[int, int], tuple[GraphicsImage, int]] = {}
        self._transmitted_bytes = 0
        #: (pane id, pane image id) -> the pane image a worker thread
        #: prepares the transmission of.
        self._image_pending: dict[tuple[int, int], Any] = {}

        # (outer image id, slot) -> placement of the previous frame.
        self._placements: dict[tuple[int, int], _Placement] = {}

        self.cells = CellImages(self)

        #: Pane id -> what this client holds of that window pane.
        self._windows: dict[int, _ShownWindow] = {}
        #: Pane id -> where each window pane shows now: its put command.
        self._window_places: dict[int, str] = {}
        #: The scrolls the renderer wrote since the placements, and since
        #: the window panes, were last put: each may have moved one.
        self._scrolled_placements: list[Scroll] = []
        self._scrolled_windows: list[Scroll] = []
        #: The window panes whose update is being encoded, and how often
        #: each had left view when it started.
        self._window_pending: dict[int, int] = {}
        #: Pane id -> how often it left view. Leaving deletes this
        #: client's image, so an update encoded before is for an image
        #: that is gone. Lillecarl/pymux#753.
        self._window_departures: dict[int, int] = {}
        #: The ones that drew again meanwhile, and the view to send it by.
        self._window_again: dict[int, WindowView] = {}
        #: Moves on `reset`, so an update encoded before it is dropped.
        self._window_generation = 0
        #: Pane id -> this client's VP9 stream of that window pane.
        self._encoders: dict[int, Any] = {}

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
    def forwards_placeholders(self) -> bool:
        """
        True when the image cells of a pane reach this terminal as kitty's
        unicode placeholders, and the terminal fits each image itself.

        The fit is the terminal's own, so the picture is the one it draws
        without pymux. A placement that pymux fitted instead was off by
        the rounding of each run. Lillecarl/pymux#600.

        Answering the graphics query is not enough: WezTerm does and
        draws no placeholder. The id travels in the foreground colour,
        so a terminal that takes fewer than 24 bits would get the wrong
        image.
        """
        return (
            self.kitty_supported
            and self._reads_placeholders is not None
            and self._reads_placeholders()
            and self.color_depth == ColorDepth.DEPTH_24_BIT
        )

    @property
    def color_depth(self) -> ColorDepth:
        "What the outer terminal can colour a cell with."
        if self._color_depth is None:
            return ColorDepth.DEPTH_24_BIT
        try:
            return self._color_depth()
        except Exception:
            return ColorDepth.DEPTH_24_BIT

    def write_in_place(self, commands: str) -> None:
        "Write `commands` with the cursor saved around them, and send them."
        self._write_raw(SAVE_CURSOR + commands + RESTORE_CURSOR)
        self._flush()

    def is_frame_answer(self, data: str) -> bool:
        "Whether `data` is the terminal's answer to a window pane's frame. Lillecarl/pymux#643."
        match = _IMAGE_REPLY_RE.match(data)
        if match is None:
            return False
        outer_id = int(match.group(1))
        return any(window.outer_id == outer_id for window in self._windows.values())

    def take_cell_size(self, data: str) -> bool | None:
        """
        Read a cell size report: None when `data` is none, else whether
        the cell is new.

        **It is read whenever it comes, not in the detection alone.** A
        terminal moved to a display of another density keeps its columns
        and changes its cell, so the server asks again at each resize.
        kitty on a Mac answered 19x35 at attach, 10x18 later with the same
        342 columns, and reported the mouse in the second all along: each
        click landed at half its column. Lillecarl/pymux#774.
        """
        match = _CELL_SIZE_REPLY_RE.match(data)
        if match is None:
            return None
        return self.set_cell_size(width=int(match.group(2)), height=int(match.group(1)))

    def set_cell_size(self, width: int, height: int) -> bool:
        "Take a cell size in pixels, and say whether it is new. A size out of range changes nothing."
        if not (0 < width <= MAX_CELL_SIZE and 0 < height <= MAX_CELL_SIZE):
            return False
        new = not self.cell_size_known or (width, height) != (self.cell_width, self.cell_height)
        self.cell_width = width
        self.cell_height = height
        self.cell_size_known = True
        return new

    def handle_reply(self, data: str) -> None:
        "Read what a terminal reply says about the outer terminal."
        if is_query_reply(data):
            self.kitty_supported = True
            return

        if _SHM_REPLY_RE.match(data):
            if self.on_shm is not None:
                self.on_shm()
            return

        match = _VERSION_REPLY_RE.match(data)
        if match is not None:
            self.terminal_version = match.group(1)
            return

        if self.take_cell_size(data) is not None:
            return

        match = _DEVICE_ATTRIBUTES_RE.match(data)
        if match is not None:
            attributes = [p for p in match.group(1).split(";") if p]
            self.sixel_supported = _SIXEL_ATTRIBUTE in attributes
            # The last reply of the detection. What did not answer by
            # now is not there, so the half blocks know where they
            # stand.
            was_detected = self.detected
            self.detected = True
            if not was_detected:
                # What the window pane features rest on, per terminal.
                # Lillecarl/pymux#651.
                logger.info(
                    "The terminal %s: kitty graphics %s, sixel %s.",
                    self.terminal_version or "with no version",
                    "yes" if self.kitty_supported else "no",
                    "yes" if self.sixel_supported else "no",
                )
            if not was_detected and self.repaint is not None:
                # The frames drawn so far carried no image, because
                # nothing knew yet whether this terminal takes one. Ask
                # for another, or an image on a quiet pane waits for
                # the next thing that happens to the pane.
                self.repaint()

    # ------------------------------------------------------------------
    # Frames.

    def scrolled(self, scrolls: Sequence[Scroll]) -> None:
        """
        The renderer wrote these scrolls to this client's terminal. A
        kitty terminal moved each image wholly inside one, so the next
        frame puts it back where pymux has it. Lillecarl/pymux#821.
        """
        if scrolls and self.kitty_supported:
            self._scrolled_placements.extend(scrolls)
            self._scrolled_windows.extend(scrolls)

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
            self.cells.render_sixel(views)
        elif self.detected:
            self.cells.render_blocks(views)

    def render_windows(self, views: Iterable[WindowView]) -> None:
        """
        Bring the window panes this client can see up to date, and take
        down the ones it no longer can.

        Only a kitty terminal shows them: the edits are frames of a kitty
        image. A window that has not drawn yet sends nothing.

        **The frame is encoded off this thread**, and written when it is
        ready, by `_update_window`. One update per window is out at a
        time; a frame drawn meanwhile goes with the next one, as damage
        the two share. A window that moved is put again at once, without
        waiting for its pixels. Lillecarl/pymux#631.
        """
        if not self.kitty_supported or self.spawn is None:
            return
        commands: list[str] = []
        places: dict[int, str] = {}
        scrolls = self._scrolled_windows
        if scrolls:
            self._scrolled_windows = []
        for view in views:
            outer_id = self._image_ids.id_of(view.pane_id, WINDOW_IMAGE)
            put = put_command(outer_id, 1, view.x, view.y, view.width, view.height, WINDOW_Z, None)
            places[view.pane_id] = put
            known = self._windows.get(view.pane_id)
            moved = bool(scrolls) and moved_by(range(view.y, view.y + view.height), scrolls)
            if known is not None and (known.put != put or moved):
                commands.append(put)
                self._windows[view.pane_id] = known._replace(put=put)
            self._start_window_update(view)
        self._window_places = places
        gone = set(self._windows) - set(places)
        for pane_id in gone:
            self._delete_image(self._windows.pop(pane_id).outer_id)
            self._encoders.pop(pane_id, None)
            self._window_departures[pane_id] = self._window_departures.get(pane_id, 0) + 1
        if commands:
            self.write_in_place("".join(commands))
        elif gone:
            self._flush()

    def refresh_window(self, view: WindowView) -> None:
        """
        Send one window pane's new frame, with no frame of the cells
        around it: the window drew, and nothing else moved.

        Only for a window the last frame placed. A frame drawn while
        an update is out goes with the next one, which starts as soon
        as that one is written. Lillecarl/pymux#636.
        """
        if not self.kitty_supported or self.spawn is None or view.pane_id not in self._window_places:
            return
        if view.pane_id in self._window_pending:
            self._window_again[view.pane_id] = view
            return
        self._start_window_update(view)

    def _start_window_update(self, view: WindowView) -> None:
        if self.spawn is None:
            return
        departures = self._window_departures.get(view.pane_id, 0)
        started = self._window_pending.get(view.pane_id)
        if started is not None:
            # Back in view while an update for the image it left is out:
            # that one is dropped, and a whole frame follows it.
            if started != departures:
                self._window_again[view.pane_id] = view
            return
        self._window_pending[view.pane_id] = departures
        outer_id = self._image_ids.id_of(view.pane_id, WINDOW_IMAGE)
        known = self._windows.get(view.pane_id)
        # Raw edits go on top of raw frames only: what a decoder made is
        # near the pixels, not them.
        since = known.serial if known is not None and not known.codec else None
        self.spawn(self._update_window(view, outer_id, since, self._window_generation))

    def _still_current(self, pane_id: int, generation: int) -> bool:
        "Is an update started at `generation` still for the image this client holds?"
        return generation == self._window_generation and self._window_pending.get(
            pane_id
        ) == self._window_departures.get(pane_id, 0)

    async def _update_window(self, view: WindowView, outer_id: int, since: int | None, generation: int) -> None:
        "Await one window's update, and write it if the window still shows here."
        try:
            await self._write_window_update(view, outer_id, since, generation)
        finally:
            self._window_pending.pop(view.pane_id, None)
            again = self._window_again.pop(view.pane_id, None)
            if again is not None and generation == self._window_generation:
                self.refresh_window(again)

    async def _write_window_update(self, view: WindowView, outer_id: int, since: int | None, generation: int) -> None:
        if self.send_frame is not None:
            if self.frame_room is not None:
                await self.frame_room()
            if self._codec_of(view) is Codec.VP9:
                await self._send_video_frame(self.send_frame, view, outer_id, generation)
            else:
                self._encoders.pop(view.pane_id, None)
                await self._send_window_frame(self.send_frame, view, outer_id, since, generation)
            return
        try:
            serial, update, whole = await view.update(outer_id, since)
        except Exception:
            logger.exception("Encoding a window pane's frame failed.")
            return
        put = self._window_places.get(view.pane_id)
        if update is None or put is None or not self._still_current(view.pane_id, generation):
            return
        known = self._windows.get(view.pane_id)
        commands = [update] if update else []
        if whole or known is None or known.put != put:
            commands.append(put)
        self._windows[view.pane_id] = _ShownWindow(outer_id, serial, put)
        if commands:
            self.write_in_place("".join(commands))
        # After the flush, so a wait for the terminal to take the frame
        # counts the frame. A client already up to date shows the frame
        # too: a commit that drew nothing waits on its callback as well,
        # and Firefox paints nothing until it is answered.
        # Lillecarl/pymux#638.
        view.shown(bool(update))

    async def _send_window_frame(
        self,
        send_frame: Callable[[list[Message]], Awaitable[None]],
        view: WindowView,
        outer_id: int,
        since: int | None,
        generation: int,
    ) -> None:
        "The same as raw pixels, for a client that encodes them for its terminal. Lillecarl/pymux#634."
        try:
            frame = await view.rects(since)
        except Exception:
            logger.exception("Reading a window pane's frame failed.")
            return
        put = self._window_places.get(view.pane_id)
        if frame is None or put is None or not self._still_current(view.pane_id, generation):
            return
        serial, whole, _width, _height, rects = frame
        known = self._windows.get(view.pane_id)
        after = put if whole or known is None or known.put != put else ""
        self._windows[view.pane_id] = _ShownWindow(outer_id, serial, put)
        if rects or after:
            await send_frame(self._answered(frame_parts(outer_id, frame, after)))
        view.shown(bool(rects))

    def _codec_of(self, view: WindowView) -> Codec | None:
        "The codec this client gets the window pane in, or None for raw pixels."
        if view.video is None or view.encoder is None or view.codec not in self.codecs:
            return None
        return Codec(view.codec) if view.codec in Codec else None

    async def _send_video_frame(
        self,
        send_frame: Callable[[list[Message]], Awaitable[None]],
        view: WindowView,
        outer_id: int,
        generation: int,
    ) -> None:
        "The window pane's next frame in this client's VP9 stream. Lillecarl/pymux#639."
        assert view.video is not None and view.encoder is not None
        encoder = self._encoders.get(view.pane_id)
        if encoder is None:
            encoder = self._encoders[view.pane_id] = view.encoder()
        try:
            frame = await view.video(encoder)
        except Exception:
            logger.exception("Encoding a window pane's frame failed.")
            return
        put = self._window_places.get(view.pane_id)
        if frame is None or put is None or not self._still_current(view.pane_id, generation):
            return
        serial, keyframe, _width, _height, data, _rects = frame
        known = self._windows.get(view.pane_id)
        after = put if keyframe or known is None or known.put != put else ""
        self._windows[view.pane_id] = _ShownWindow(outer_id, serial, put, Codec.VP9)
        if data or after:
            await send_frame(self._answered(video_parts(outer_id, Codec.VP9, frame, after)))
        view.shown(bool(data))

    def _answered(self, parts: list[Message]) -> list[Message]:
        "Ask a client that answers frames to let its terminal answer this one. Lillecarl/pymux#643."
        if self.frame_room is not None:
            parts[-1][Field.ANSWER] = True
        return parts

    def _render_kitty(self, views: Iterable[PaneView]) -> None:
        try:
            desired = self._collect(views)
        except Exception:
            # A frame is not worth killing the client for.
            logger.exception("Collecting graphics placements failed.")
            return

        commands: list[str] = []

        # Placements that are gone. (A placement that only moved is
        # replaced in place by the put command below, so it is not
        # deleted first: that would make the image flicker.)
        for key, placement in self._placements.items():
            if key not in desired:
                commands.append("\x1b_Ga=d,d=i,i=%i,p=%i,q=2\x1b\\" % (placement.image_id, placement.slot))

        # New and changed placements, and the ones a scroll moved.
        scrolls = self._scrolled_placements
        if scrolls:
            self._scrolled_placements = []
        for key, placement in desired.items():
            previous = self._placements.get(key)
            if (
                previous is None
                or previous.command != placement.command
                or (scrolls and moved_by(placement.rows, scrolls))
            ):
                commands.append(placement.command)

        self._placements = desired

        if not commands:
            return

        self.write_in_place("".join(commands))

    def draws_at_its_own_size(
        self, placement: GraphicsPlacement, image: GraphicsImage, source: Source | None, columns: int, rows: int
    ) -> bool:
        """
        Whether this placement should be drawn at the size of its own
        pixels, rather than fitted to the cells it covers.

        Three things have to hold.

        **The placement must not be a virtual one.** A virtual
        placement is drawn through the unicode placeholders in the text
        of the pane, and there the run of placeholder cells *is* the
        box: the program put those cells on the screen to be filled,
        and `_placeholder_source` cuts the piece of the image that each
        one shows. Drawing that piece at its own size would leave the
        cells around it empty.

        **The program must not have asked for the box.** "c=3,r=2" with
        a two pixel image means "draw it over three cells by two", and
        fitting it there is the answer. Only a box the terminal worked
        out with `ceil(pixels / cell)` is an artefact.

        **The pixels must fit the cells**, measured in the cell this
        client's terminal really has. A pane reserves cells against the
        cell it was told, and the client draws them in its own. When
        the two are the same cell the box is the image rounded up, so
        the image fits with less than one cell of slack and both paths
        draw it untouched -- which is the whole of "never resample".
        When the cells differ the box can be smaller, and then there is
        nothing to do but fit the image to it. A client whose cell is
        larger fits as well and leaves more slack; drawing in the slack
        would stretch the image to a size nobody asked for.
        Lillecarl/pymux#369.
        """
        if placement.virtual or getattr(placement, "asked_for_the_box", True):
            return False
        width, height = drawn_size(image, source)
        return columns * self.cell_width >= width and rows * self.cell_height >= height

    def _collect(self, views: Iterable[PaneView]) -> dict[tuple[int, int], _Placement]:
        "The placements that this frame should show."
        desired: dict[tuple[int, int], _Placement] = {}
        live_keys = set()
        forwards = self.forwards_placeholders

        for view in views:
            # An image stays on the outer terminal as long as the pane
            # holds it, also while no placement shows it. Scrolling an
            # image in and out must not re-transmit the pixels.
            for image_id in view.graphics.images_by_id:
                live_keys.add((view.pane_id, image_id))

            slots: dict[int, int] = {}

            if forwards:
                self._collect_virtual(view, desired)
            placements = visible_placements(view) if forwards else self.placements_of(view)
            for placement in placements:
                (
                    pane_placement,
                    image,
                    columns,
                    rows,
                    x,
                    y,
                    source,
                ) = placement

                outer_id = self._outer_image_id(view.pane_id, pane_placement, image)
                if outer_id is None:
                    continue

                slot = slots.get(outer_id, 0) + 1
                slots[outer_id] = slot

                desired[(outer_id, slot)] = _Placement(
                    outer_id,
                    slot,
                    put_command(
                        outer_id,
                        slot,
                        x,
                        y,
                        columns,
                        rows,
                        pane_placement.z,
                        source,
                        natural=self.draws_at_its_own_size(pane_placement, image, source, columns, rows),
                    ),
                    range(y, y + rows),
                )

        self._forget_unused_images(live_keys)
        return desired

    def _collect_virtual(self, view: PaneView, desired: dict[tuple[int, int], _Placement]) -> None:
        """
        The virtual placements behind the placeholders that `view` shows,
        for a terminal that reads the placeholders itself.

        Each one carries the box the program asked for, so the terminal
        fits the image to that box the way it does without pymux. It
        has no position: the cells of the pane are where it draws.
        """
        screen = view.screen
        if screen is None or not hasattr(screen, "placeholder_runs"):
            return
        first_row = view.vertical_scroll
        shown = {run.image_id for run in screen.placeholder_runs(first_row, first_row + view.height - 1)}
        for image_id in shown:
            placement = view.graphics.virtual_placement(image_id)
            image = view.graphics.images_by_id.get(image_id)
            if placement is None or image is None or not placement.columns or not placement.rows:
                continue
            outer_id = self._outer_image_id(view.pane_id, placement, image)
            if outer_id is None:
                continue
            # The program's own id, which an underlined placeholder names
            # in its underline colour. A key below zero stays clear of the
            # slots of the placements that pymux puts itself.
            slot = placement.placement_id or VIRTUAL_SLOT
            desired[(outer_id, -slot)] = _Placement(
                outer_id,
                slot,
                "\x1b_Ga=p,U=1,i=%i,p=%i,c=%i,r=%i,q=2\x1b\\" % (outer_id, slot, placement.columns, placement.rows),
            )

    def placements_of(self, view: PaneView) -> Iterator[Placed]:
        return pane_placements(view, self.cell_width, self.cell_height)

    # ------------------------------------------------------------------
    # Image transmission.

    def _outer_image_id(self, pane_id: int, placement: GraphicsPlacement, image: GraphicsImage) -> int | None:
        """
        The id of `image` on the outer terminal. Transmits the image the
        first time it is needed. Returns None when it cannot be sent,
        and while a worker thread prepares it.

        **A placement waits for its image.** A put command that reaches
        the terminal before the transmission draws nothing, so the
        placement stays out of the frame until the image is sent.
        """
        key = (pane_id, placement.image_id)
        known = self._images.get(key)
        if known is not None and known[0] is image:
            return known[1]
        if self._image_pending.get(key) is image:
            return None

        if len(image.data) > MAX_IMAGE_BYTES:
            return None
        if self._transmitted_bytes + len(image.data) > MAX_TOTAL_BYTES:
            return None

        if known is not None:
            # The pane replaced the image under the same id. Drop the
            # old one from the outer terminal.
            self._delete_image(known[1])
            self._transmitted_bytes -= len(known[0].data)
            del self._images[key]

        outer_id = self._image_ids.id_of(pane_id, placement.image_id)
        if known is not None:
            # The replaced image went with its placements, under the id
            # the new one takes, so each has to be put again.
            self._placements = {key: put for key, put in self._placements.items() if put.image_id != outer_id}

        if self.spawn is not None:
            self._image_pending[key] = image
            self.spawn(self._transmit_in_thread(key, image, outer_id))
            return None

        try:
            commands = _transmission(outer_id, image)
        except Exception:
            logger.exception("Transmitting an image to the client failed.")
            return None
        self._send_image(key, image, outer_id, commands)
        return outer_id

    async def _transmit_in_thread(self, key: tuple[int, int], image: GraphicsImage, outer_id: int) -> None:
        """
        Prepare one transmission in a worker thread, and send it.
        Lillecarl/pymux#576.

        A transmission that fails keeps its pending entry, so the frames
        after it do not try the same image again.
        """
        try:
            commands = await anyio.to_thread.run_sync(_transmission, outer_id, image)
        except Exception:
            logger.exception("Transmitting an image to the client failed.")
            return

        # The pane dropped or replaced the image while the thread
        # worked, or the client reset.
        if self._image_pending.get(key) is not image:
            return
        del self._image_pending[key]
        self._send_image(key, image, outer_id, commands)
        if self.repaint is not None:
            self.repaint()

    def _send_image(self, key: tuple[int, int], image: GraphicsImage, outer_id: int, commands: list[str]) -> None:
        for command in commands:
            self._write_raw(command)
            self._flush()
        self._transmitted_bytes += len(image.data)
        self._images[key] = (image, outer_id)

    def _forget_unused_images(self, live_keys: set[tuple[int, int]]) -> None:
        "Free the images that no pane holds anymore."
        for key, (image, outer_id) in list(self._images.items()):
            if key not in live_keys:
                self._delete_image(outer_id)
                self._transmitted_bytes -= len(image.data)
                del self._images[key]
        for key in list(self._image_pending):
            if key not in live_keys:
                del self._image_pending[key]

    def _delete_image(self, outer_id: int) -> None:
        self._write_raw("\x1b_Ga=d,d=I,i=%i,q=2\x1b\\" % outer_id)

    # ------------------------------------------------------------------
    # Sixel.

    # ------------------------------------------------------------------

    def reset(self) -> None:
        """
        Remove everything that this client put on the outer terminal.
        (When the client detaches, or when a popup covers the panes.)
        """
        self._image_pending = {}
        self._window_places = {}
        self._window_again = {}
        self._window_generation += 1
        self._encoders = {}
        if self._windows:
            for window in self._windows.values():
                self._delete_image(window.outer_id)
            self._windows = {}
            self._flush()
        self.cells.reset()

        if not self._images and not self._placements:
            return
        for _image, outer_id in self._images.values():
            self._delete_image(outer_id)
        self._images = {}
        self._placements = {}
        self._transmitted_bytes = 0
        self._flush()


def _transmission(outer_id: int, image: GraphicsImage) -> list[str]:
    "The commands that send one image to the outer terminal. It runs in a worker thread."
    if image.format == PixelFormat.PNG:
        keys = "f=%i" % PixelFormat.PNG
        payload = base64.b64encode(image.data).decode("ascii")
    elif image.format == PixelFormat.SIXEL:
        # Kitty takes no sixel: it gets the pixels.
        pixels = sixel.to_rgba(image.format, image.width, image.height, image.data)
        if pixels is None:
            raise ValueError("the sixel image does not decode")
        keys = "f=%i,s=%i,v=%i,o=z" % (PixelFormat.RGBA, image.width, image.height)
        payload = base64.b64encode(zlib.compress(pixels, 1)).decode("ascii")
    else:
        # Compress: the data travels through the client socket.
        keys = "f=%i,s=%i,v=%i,o=z" % (image.format, image.width, image.height)
        payload = base64.b64encode(zlib.compress(image.data, 1)).decode("ascii")

    chunks = [payload[i : i + CHUNK_SIZE] for i in range(0, len(payload), CHUNK_SIZE)] or [""]
    commands = []
    for index, chunk in enumerate(chunks):
        more = 1 if index < len(chunks) - 1 else 0
        # The keys of the first chunk govern the transmission.
        control = "a=t,i=%i,t=d,q=2,%s,m=%i" % (outer_id, keys, more) if index == 0 else "m=%i" % more
        commands.append("\x1b_G%s;%s\x1b\\" % (control, chunk))
    return commands
