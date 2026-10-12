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
from collections.abc import Callable, Coroutine, Iterable
from typing import Any, NamedTuple

import anyio
from prompt_toolkit.output import ColorDepth
from pyte import escape
from pyte.images import PixelFormat
from pyte.sequences import Csi, csi, esc
from pyte.sixel import stored_payload
from pyte.terminfo import DeviceExtension

# Through the modules, so the kernels `pymux.kernels.install()` puts there
# are the ones that run.
from . import blocks, sixel
from .blocks import blocks_for, rows_for_cells
from .log import logger

__all__ = [
    "CELL_SIZE_QUERY",
    "QUERY_SEQUENCE",
    "VERSION_QUERY",
    "ClientGraphics",
    "OuterImageIds",
    "PaneView",
    "draws_placeholders",
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


# Reply of the cell size query: "CSI 6 ; height ; width t". The 6 is
# xterm's answer code for the 16 that was asked; it is not a
# `WindowOp`, which numbers the questions.
_CELL_SIZE_REPLY_RE = re.compile(r"^\x1b\[6;(\d+);(\d+)t$")

# Primary device attributes reply. One of the numbers it lists is the
# terminal saying it draws sixels.
_DEVICE_ATTRIBUTES_RE = re.compile(r"^\x1b\[\?([\d;]*)c$")
_SIXEL_ATTRIBUTE = str(int(DeviceExtension.SIXEL))

_VERSION_REPLY_RE = re.compile(r"^\x1bP>\|(.*)\x1b\\$", re.DOTALL)

#: The terminals that draw kitty's unicode placeholders, by what they
#: answer XTVERSION with, and the first version that does. WezTerm and
#: Konsole answer the graphics query and draw none.
#:
#: - kitty: "kitty(0.39.1)". Placeholders came in 0.28.0.
#: - Ghostty: "ghostty 1.3.1". `src/terminal/kitty/graphics_unicode.zig`.
_DRAWS_PLACEHOLDERS = (
    (re.compile(r"kitty\((\d+(?:\.\d+)*)"), (0, 28, 0)),
    (re.compile(r"ghostty (\d+(?:\.\d+)*)"), (1, 0, 0)),
)

# What a sixel payload is made of: printable ASCII, and the line breaks
# programs wrap it with.
_SIXEL_TEXT_RE = re.compile(r"[ -~\r\n]*")


def is_query_reply(data: str) -> bool:
    "True when `data` is the reply of `QUERY_SEQUENCE`."
    return bool(_QUERY_REPLY_RE.match(data))


def draws_placeholders(version: str, term: str) -> bool:
    """
    Whether the terminal that answered XTVERSION with `version` draws
    kitty's unicode placeholders. No query says so, so this goes by
    name. A terminal that did not answer is judged by `term`.
    Lillecarl/pymux#607.
    """
    if not version:
        return "kitty" in term
    for name, first in _DRAWS_PLACEHOLDERS:
        match = name.match(version)
        if match is not None:
            return tuple(int(part) for part in match.group(1).split(".")) >= first
    return False


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


class WindowView(NamedTuple):
    """
    A pane that shows a Wayland window, as one client sees it.

    `update(outer_id, since)` brings the terminal up to date: it answers
    `(serial, commands, whole)`, as `pysmithay.Compositor.kitty_update`
    does, so the pixels and the damage never pass through here.
    `shown()` says this client took the frame, which lets the window
    draw its next one.
    """

    pane_id: int
    x: int
    y: int
    width: int
    height: int
    update: Callable[[int, int | None], tuple[int, str | None, bool]]
    shown: Callable[[], None]


#: The pane image id a window pane's picture takes in `OuterImageIds`.
#: A program's own images count up from one.
WINDOW_IMAGE = -1


class _ShownWindow(NamedTuple):
    "What one client holds of a window pane: its image, frame and placement."

    outer_id: int
    serial: int
    put: str


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
    "One placement as it is emitted to the outer terminal."

    __slots__ = ("command", "image_id", "slot")

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
        self._repaint = repaint
        self._spawn = spawn
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
        self._images: dict[tuple[int, int], tuple[Any, int]] = {}
        self._transmitted_bytes = 0
        #: (pane id, pane image id) -> the pane image a worker thread
        #: prepares the transmission of.
        self._image_pending: dict[tuple[int, int], Any] = {}

        # (outer image id, slot) -> placement of the previous frame.
        self._placements: dict[tuple[int, int], _Placement] = {}

        # The state of a way of drawing whose pixels are the cells:
        # sixel, and half blocks. A client draws one way or the other,
        # never both, so the two share this. The encoded images are
        # cached, because cropping means encoding again and a scrolling
        # image would otherwise pay for the encoder on every frame.
        self._cell_placements: dict[tuple[int, int, int], str] = {}
        self._cell_cache: dict[tuple, tuple[Any, Any]] = {}
        self._cell_redraw = False
        #: Cache keys a worker thread is encoding.
        self._cell_pending: set[tuple] = set()

        #: Pane id -> what this client holds of that window pane.
        self._windows: dict[int, _ShownWindow] = {}

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

    def handle_reply(self, data: str) -> None:
        "Read what a terminal reply says about the outer terminal."
        if is_query_reply(data):
            self.kitty_supported = True
            return

        match = _VERSION_REPLY_RE.match(data)
        if match is not None:
            self.terminal_version = match.group(1)
            return

        match = _CELL_SIZE_REPLY_RE.match(data)
        if match is not None:
            height = int(match.group(1))
            width = int(match.group(2))
            if 0 < width <= MAX_CELL_SIZE and 0 < height <= MAX_CELL_SIZE:
                self.cell_width = width
                self.cell_height = height
                self.cell_size_known = True
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

    def render_windows(self, views: Iterable[WindowView]) -> None:
        """
        Bring the window panes this client can see up to date, and take
        down the ones it no longer can.

        Only a kitty terminal shows them: the edits are frames of a kitty
        image. A window that has not drawn yet sends nothing.
        """
        if not self.kitty_supported:
            return
        commands: list[str] = []
        seen: set[int] = set()
        shown: list[WindowView] = []
        for view in views:
            outer_id = self._image_ids.id_of(view.pane_id, WINDOW_IMAGE)
            known = self._windows.get(view.pane_id)
            serial, update, whole = view.update(outer_id, known.serial if known is not None else None)
            if update is None:
                continue
            seen.add(view.pane_id)
            put = _put_command(outer_id, 1, view.x, view.y, view.width, view.height, 0, None)
            if update:
                commands.append(update)
            if whole or known is None or known.put != put:
                commands.append(put)
            self._windows[view.pane_id] = _ShownWindow(outer_id, serial, put)
            if update:
                shown.append(view)
        gone = set(self._windows) - seen
        for pane_id in gone:
            self._delete_image(self._windows.pop(pane_id).outer_id)
        if commands:
            self._write_raw(SAVE_CURSOR + "".join(commands) + RESTORE_CURSOR)
        if commands or gone:
            self._flush()
        # After the flush, so a wait for the terminal to take the frame
        # counts the frame.
        for view in shown:
            view.shown()

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

    def draws_at_its_own_size(self, placement, image, source, columns: int, rows: int) -> bool:
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
        width, height = _drawn_size(image, source)
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
            placements = self._visible_placements(view) if forwards else self._pane_placements(view)
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
                    _put_command(
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

    def _pane_placements(self, view: PaneView):
        """
        Everything of one pane that the client should draw: the plain
        placements, and the images that the unicode placeholders in the
        text of the pane point at.
        """
        yield from self._visible_placements(view)
        yield from self._placeholder_placements(view)

    def _placeholder_placements(self, view: PaneView):
        """
        Yield the images that the unicode placeholders of one pane
        point at.

        A placeholder cell says which cell of which image it stands
        for. `Screen.placeholder_runs` gathers the neighbouring
        cells into rectangles; each rectangle becomes one placement
        with the matching piece of the image.
        """
        screen = view.screen
        if screen is None or not hasattr(screen, "placeholder_runs"):
            return

        first_row = view.vertical_scroll
        last_row = first_row + view.height - 1
        for run in screen.placeholder_runs(first_row, last_row):
            placement = view.graphics.virtual_placement(run.image_id, run.placement_id)
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
                self.cell_width,
                self.cell_height,
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

            piece = placement.crop
            if piece is not None or crop_left or crop_top or columns != placement.columns or rows != placement.rows:
                # Crop in pixels: the cell box of the placement maps
                # onto the pixels of the image. A piece that text cut
                # out of a sixel image maps through the whole box.
                piece_column, piece_row, box_columns, box_rows = piece or (0, 0, placement.columns, placement.rows)
                px_per_column = image.width / box_columns
                px_per_row = image.height / box_rows
                source = (
                    int((piece_column + crop_left) * px_per_column),
                    int((piece_row + crop_top) * px_per_row),
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

        if self._spawn is not None:
            self._image_pending[key] = image
            self._spawn(self._transmit_in_thread(key, image, outer_id))
            return None

        try:
            commands = _transmission(outer_id, image)
        except Exception:
            logger.exception("Transmitting an image to the client failed.")
            return None
        self._send_image(key, image, outer_id, commands)
        return outer_id

    async def _transmit_in_thread(self, key: tuple[int, int], image, outer_id: int) -> None:
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
        if self._repaint is not None:
            self._repaint()

    def _send_image(self, key: tuple[int, int], image, outer_id: int, commands: list[str]) -> None:
        for command in commands:
            self._write_raw(command)
            self._flush()
        self._transmitted_bytes += len(image.data)
        self._images[key] = (image, outer_id)

    def _forget_unused_images(self, live_keys) -> None:
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
        self._cell_pending &= live

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

        self._write_raw(SAVE_CURSOR + "".join(desired[key] for key in sorted(desired)) + RESTORE_CURSOR)
        self._flush()

    def _collect_sixel(self, views: Iterable[PaneView]) -> tuple[dict[tuple[int, int, int], str], set]:
        "The sixel commands of one frame, and the cache keys they used."
        desired: dict[tuple[int, int, int], str] = {}
        live: set[tuple] = set()

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
            ) in self._pane_placements(view):
                slot = slots.get(placement.image_id, 0) + 1
                slots[placement.image_id] = slot

                own_size = self.draws_at_its_own_size(placement, image, source, columns, rows)
                if own_size and source is None and image.format == PixelFormat.SIXEL:
                    key = (id(image), "as sent")
                    live.add(key)
                    data = self._as_sent(key, image)
                    if data is not None:
                        desired[(view.pane_id, placement.image_id, slot)] = "\x1b[%i;%iH%s" % (y + 1, x + 1, data)
                        continue

                key = (
                    id(image),
                    source,
                    columns * self.cell_width,
                    rows * self.cell_height,
                    own_size,
                )
                live.add(key)

                data = self._sixel_for(key, image, source, columns, rows, own_size)
                if data is None:
                    continue
                desired[(view.pane_id, placement.image_id, slot)] = "\x1b[%i;%iH%s" % (
                    y + 1,
                    x + 1,
                    data,
                )

        return desired, live

    def _collect_blocks(self, views: Iterable[PaneView]) -> tuple[dict[tuple[int, int, int], str], set]:
        "The half block rows of one frame, and the cache keys they used."
        desired: dict[tuple[int, int, int], str] = {}
        live = set()
        depth = self.color_depth

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
                    "\x1b[%i;%iH%s" % (y + offset + 1, x + 1, line) for offset, line in enumerate(lines) if line
                )

        return desired, live

    def _blocks_for(self, key: tuple, image, source, columns: int, rows: int, depth) -> list[str] | None:
        "The half block rows of one placement. (Cached by geometry.)"
        return self._cell_entry(key, image, _encode_blocks, image, source, columns, rows, depth)

    def _sixel_for(self, key: tuple, image, source, columns: int, rows: int, own_size: bool = False) -> str | None:
        "The sixel sequence of one placement. (Cached by geometry.)"
        target = None if own_size else (max(1, columns * self.cell_width), max(1, rows * self.cell_height))
        return self._cell_entry(key, image, _encode_sixel, image, source, target)

    def _as_sent(self, key: tuple, image) -> str | None:
        """
        The program's own sixel sequence, for an image drawn whole at its
        own size. Encoding it again changes the palette, so a gradient
        gains pixels the program never drew. Lillecarl/pymux#603.

        None when the payload holds anything but sixel characters: those
        bytes go to the client's terminal, and must not end the sequence.
        """
        known = self._cell_cache.get(key)
        if known is not None and known[0] is image:
            return known[1]
        payload = stored_payload(image.data)
        sent = "\x1bP%s\x1b\\" % payload if _SIXEL_TEXT_RE.fullmatch(payload) else None
        self._cell_cache[key] = (image, sent)
        return sent

    def _cell_entry(self, key: tuple, image, work: Callable, *args):
        """
        What `work(*args)` gives for one placement, from the cache.

        With `spawn` a miss answers None, and a worker thread does the
        work: the cells of the placement stay blank until the next
        frame after it is done. The kernels release the GIL, so the
        event loop keeps running beside it. Lillecarl/pymux#575.
        """
        known = self._cell_cache.get(key)
        if known is not None and known[0] is image:
            return known[1]

        if self._spawn is None:
            result = work(*args)
            self._cell_cache[key] = (image, result)
            return result

        if key not in self._cell_pending:
            self._cell_pending.add(key)
            self._spawn(self._work_in_thread(key, image, work, args))
        return None

    async def _work_in_thread(self, key: tuple, image, work: Callable, args: tuple) -> None:
        "Run one `_cell_entry` miss in a thread, and keep the answer."
        try:
            result = await anyio.to_thread.run_sync(work, *args)
        except Exception:
            logger.exception("Encoding a pane image failed.")
            result = None

        # The placement went away while the thread worked, or the
        # client reset: the answer is for nobody.
        if key not in self._cell_pending:
            return
        self._cell_pending.discard(key)
        self._cell_cache[key] = (image, result)
        if self._repaint is not None:
            self._repaint()

    # ------------------------------------------------------------------

    def reset(self) -> None:
        """
        Remove everything that this client put on the outer terminal.
        (When the client detaches, or when a popup covers the panes.)
        """
        self._cell_pending = set()
        self._image_pending = {}
        if self._windows:
            for window in self._windows.values():
                self._delete_image(window.outer_id)
            self._windows = {}
            self._flush()
        if self._cell_placements:
            self._cell_placements = {}
            self._cell_cache = {}
            self._cell_redraw = False
            if self._repaint is not None:
                self._repaint()

        if not self._images and not self._placements:
            return
        for _image, outer_id in self._images.values():
            self._delete_image(outer_id)
        self._images = {}
        self._placements = {}
        self._transmitted_bytes = 0
        self._flush()


def _transmission(outer_id: int, image) -> list[str]:
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


def _source_rgba(image, source) -> tuple[bytes, int, int] | None:
    "The RGBA pixels a placement shows, and their size."
    pixels = sixel.to_rgba(image.format, image.width, image.height, image.data)
    if pixels is None:
        return None
    if source is None:
        return pixels, image.width, image.height
    pixels = _crop_rgba(pixels, image.width, image.height, source)
    if pixels is None:
        return None
    return pixels, source[2], source[3]


def _encode_blocks(image, source, columns: int, rows: int, depth) -> list[str]:
    "The half block rows of one placement. It runs in a worker thread."
    found = _source_rgba(image, source)
    if found is None:
        return []
    pixels, width, height = found
    # Two pixels for every cell, one above the other.
    pixels = blocks.average_rgba(pixels, width, height, columns, rows_for_cells(rows))
    return blocks_for(pixels, columns, rows, depth)


def _encode_sixel(image, source, target: tuple[int, int] | None) -> str | None:
    """
    The sixel sequence of one placement, scaled to `target` pixels, or
    at its own size for None. It runs in a worker thread.

    `draws_at_its_own_size` says when the own size is right, and
    `_put_command` says why the cells below the image are safe then.
    """
    found = _source_rgba(image, source)
    if found is None:
        return None
    pixels, width, height = found
    if target is not None:
        pixels = sixel.scale_rgba(pixels, width, height, *target)
        width, height = target
    return sixel.encode_sixel(width, height, pixels)


def _drawn_size(image, source: tuple[int, int, int, int] | None) -> tuple[int, int]:
    "The pixels a placement shows: the image, or the crop out of it."
    if source is not None:
        return (source[2], source[3])
    return (image.width, image.height)


def _crop_rgba(pixels: bytes, width: int, height: int, source: tuple[int, int, int, int]) -> bytes | None:
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


def _placeholder_source(image, placement, image_column, image_row, columns, rows, cell_width, cell_height):
    """
    The rectangle of `image`, in pixels, that a run of placeholder
    cells shows. None when the run falls outside the image.

    kitty fits the image into the box that the placement covers,
    keeping the proportions and centring what is left over. A cell of
    the box therefore does not map onto a fixed piece of the image, and
    a cell along the border may show none of it.

    **The cell is the client's own.** The box is pixels on the screen
    of this client, so which part of the image a cell of it shows
    depends on the shape of that cell. A ten by twenty cell and a ten
    by nineteen one fit the same image differently.
    """
    box_width = placement.columns * cell_width
    box_height = placement.rows * cell_height

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

    left = (image_column * cell_width - x_offset) / scale
    top = (image_row * cell_height - y_offset) / scale
    right = left + columns * cell_width / scale
    bottom = top + rows * cell_height / scale

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
    source: tuple[int, int, int, int] | None,
    natural: bool = False,
) -> str:
    """
    The escape sequences that put one image at (`x`, `y`) on the outer
    terminal. The cursor moves there first; `C=1` keeps the image from
    moving it again.

    **`natural` leaves `c` and `r` out, and that is what stops the
    terminal resampling.** `c` and `r` are a box to fit the image into,
    so sending them always scales unless the box is the image to the
    pixel. With neither, kitty draws the image at its own size and
    works the covered cells out itself -- `ceil(width / cell)`, which
    is at most the `columns` and `rows` the pane reserved whenever the
    image fits the box. So nothing below the image is overdrawn.
    `draws_at_its_own_size` is where that is decided, and it also
    refuses a box the program asked for. Lillecarl/pymux#369.
    """
    parts = [
        "a=p",
        "i=%i" % outer_id,
        "p=%i" % slot,
    ]
    if not natural:
        parts.append("c=%i" % columns)
        parts.append("r=%i" % rows)
    parts += [
        "C=1",
        "q=2",
    ]
    if z:
        parts.append("z=%i" % z)
    if source is not None:
        parts.append("x=%i,y=%i,w=%i,h=%i" % source)

    return "\x1b[%i;%iH\x1b_G%s\x1b\\" % (y + 1, x + 1, ",".join(parts))
