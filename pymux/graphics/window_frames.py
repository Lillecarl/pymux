"""
A Wayland window in a pane, as one client sees it: a kitty image whose
frames are edits of the last one, placed under every cell with a
background of its own. The frame is encoded off the event loop and
written when it is ready, one update per window at a time; a client
that encodes pixels itself gets them raw or as VP9 instead.
Lillecarl/pymux#631, Lillecarl/pymux#634, Lillecarl/pymux#639.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Sequence
from typing import TYPE_CHECKING, Any, NamedTuple

from libpymux.protocol import Codec, Field, Message, Packet
from pyte.sequences.kitty_graphics import GraphicsReply

# Through the modules, so the kernels `pymux.graphics.kernels.install()` puts there
# are the ones that run.
from ..log import logger
from .placements import Scroll, moved_by, put_command

if TYPE_CHECKING:
    from .graphics import ClientGraphics

__all__ = ["WindowFrames"]


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


class WindowFrames:
    """
    One client's window panes: what it holds of each, and the updates on their way.
    """

    def __init__(self, graphics: ClientGraphics) -> None:
        self.graphics = graphics

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
        #: Pane id -> what this client holds of that window pane.
        self._shown: dict[int, _ShownWindow] = {}
        #: Pane id -> where each window pane shows now: its put command.
        self._places: dict[int, str] = {}
        #: The scrolls the renderer wrote since the window panes were
        #: last put: each may have moved one.
        self._scrolled: list[Scroll] = []
        #: The window panes whose update is being encoded, and how often
        #: each had left view when it started.
        self._pending: dict[int, int] = {}
        #: Pane id -> how often it left view. Leaving deletes this
        #: client's image, so an update encoded before is for an image
        #: that is gone. Lillecarl/pymux#753.
        self._departures: dict[int, int] = {}
        #: The ones that drew again meanwhile, and the view to send it by.
        self._again: dict[int, WindowView] = {}
        #: Moves on `reset`, so an update encoded before it is dropped.
        self._generation = 0
        #: Pane id -> this client's VP9 stream of that window pane.
        self._encoders: dict[int, Any] = {}

    def scrolled(self, scrolls: Sequence[Scroll]) -> None:
        self._scrolled.extend(scrolls)

    def reset(self) -> None:
        "Take every window pane off the terminal, and drop the updates on their way."
        self._places = {}
        self._again = {}
        self._generation += 1
        self._encoders = {}
        if self._shown:
            for window in self._shown.values():
                self.graphics.delete_image(window.outer_id)
            self._shown = {}
            self.graphics.flush()

    def is_frame_answer(self, reply: GraphicsReply) -> bool:
        "Whether `reply` is the terminal's answer to a window pane's frame. Lillecarl/pymux#643."
        return reply.image_id is not None and any(window.outer_id == reply.image_id for window in self._shown.values())

    def render(self, views: Iterable[WindowView]) -> None:
        """
        Bring the window panes this client can see up to date, and take
        down the ones it no longer can.

        Only a kitty terminal shows them: the edits are frames of a kitty
        image. A window that has not drawn yet sends nothing.

        **The frame is encoded off this thread**, and written when it is
        ready, by `_update`. One update per window is out at a
        time; a frame drawn meanwhile goes with the next one, as damage
        the two share. A window that moved is put again at once, without
        waiting for its pixels. Lillecarl/pymux#631.
        """
        if not self.graphics.kitty_supported or self.graphics.spawn is None:
            return
        commands: list[str] = []
        places: dict[int, str] = {}
        scrolls = self._scrolled
        if scrolls:
            self._scrolled = []
        for view in views:
            outer_id = self.graphics.image_ids.id_of(view.pane_id, WINDOW_IMAGE)
            put = put_command(outer_id, 1, view.x, view.y, view.width, view.height, WINDOW_Z, None)
            places[view.pane_id] = put
            known = self._shown.get(view.pane_id)
            moved = bool(scrolls) and moved_by(range(view.y, view.y + view.height), scrolls)
            if known is not None and (known.put != put or moved):
                commands.append(put)
                self._shown[view.pane_id] = known._replace(put=put)
            self._start_update(view)
        self._places = places
        gone = set(self._shown) - set(places)
        for pane_id in gone:
            self.graphics.delete_image(self._shown.pop(pane_id).outer_id)
            self._encoders.pop(pane_id, None)
            self._departures[pane_id] = self._departures.get(pane_id, 0) + 1
        if commands:
            self.graphics.write_in_place("".join(commands))
        elif gone:
            self.graphics.flush()

    def refresh(self, view: WindowView) -> None:
        """
        Send one window pane's new frame, with no frame of the cells
        around it: the window drew, and nothing else moved.

        Only for a window the last frame placed. A frame drawn while
        an update is out goes with the next one, which starts as soon
        as that one is written. Lillecarl/pymux#636.
        """
        if not self.graphics.kitty_supported or self.graphics.spawn is None or view.pane_id not in self._places:
            return
        if view.pane_id in self._pending:
            self._again[view.pane_id] = view
            return
        self._start_update(view)

    def _start_update(self, view: WindowView) -> None:
        if self.graphics.spawn is None:
            return
        departures = self._departures.get(view.pane_id, 0)
        started = self._pending.get(view.pane_id)
        if started is not None:
            # Back in view while an update for the image it left is out:
            # that one is dropped, and a whole frame follows it.
            if started != departures:
                self._again[view.pane_id] = view
            return
        self._pending[view.pane_id] = departures
        outer_id = self.graphics.image_ids.id_of(view.pane_id, WINDOW_IMAGE)
        known = self._shown.get(view.pane_id)
        # Raw edits go on top of raw frames only: what a decoder made is
        # near the pixels, not them.
        since = known.serial if known is not None and not known.codec else None
        self.graphics.spawn(self._update(view, outer_id, since, self._generation))

    def _still_current(self, pane_id: int, generation: int) -> bool:
        "Is an update started at `generation` still for the image this client holds?"
        return generation == self._generation and self._pending.get(pane_id) == self._departures.get(pane_id, 0)

    async def _update(self, view: WindowView, outer_id: int, since: int | None, generation: int) -> None:
        "Await one window's update, and write it if the window still shows here."
        try:
            await self._write_update(view, outer_id, since, generation)
        finally:
            self._pending.pop(view.pane_id, None)
            again = self._again.pop(view.pane_id, None)
            if again is not None and generation == self._generation:
                self.refresh(again)

    async def _write_update(self, view: WindowView, outer_id: int, since: int | None, generation: int) -> None:
        if self.send_frame is not None:
            if self.frame_room is not None:
                await self.frame_room()
            if self._codec_of(view) is Codec.VP9:
                await self._send_video_frame(self.send_frame, view, outer_id, generation)
            else:
                self._encoders.pop(view.pane_id, None)
                await self._send_frame(self.send_frame, view, outer_id, since, generation)
            return
        try:
            serial, update, whole = await view.update(outer_id, since)
        except Exception:
            logger.exception("Encoding a window pane's frame failed.")
            return
        put = self._places.get(view.pane_id)
        if update is None or put is None or not self._still_current(view.pane_id, generation):
            return
        known = self._shown.get(view.pane_id)
        commands = [update] if update else []
        if whole or known is None or known.put != put:
            commands.append(put)
        self._shown[view.pane_id] = _ShownWindow(outer_id, serial, put)
        if commands:
            self.graphics.write_in_place("".join(commands))
        # After the flush, so a wait for the terminal to take the frame
        # counts the frame. A client already up to date shows the frame
        # too: a commit that drew nothing waits on its callback as well,
        # and Firefox paints nothing until it is answered.
        # Lillecarl/pymux#638.
        view.shown(bool(update))

    async def _send_frame(
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
        put = self._places.get(view.pane_id)
        if frame is None or put is None or not self._still_current(view.pane_id, generation):
            return
        serial, whole, _width, _height, rects = frame
        known = self._shown.get(view.pane_id)
        after = put if whole or known is None or known.put != put else ""
        self._shown[view.pane_id] = _ShownWindow(outer_id, serial, put)
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
        put = self._places.get(view.pane_id)
        if frame is None or put is None or not self._still_current(view.pane_id, generation):
            return
        serial, keyframe, _width, _height, data, _rects = frame
        known = self._shown.get(view.pane_id)
        after = put if keyframe or known is None or known.put != put else ""
        self._shown[view.pane_id] = _ShownWindow(outer_id, serial, put, Codec.VP9)
        if data or after:
            await send_frame(self._answered(video_parts(outer_id, Codec.VP9, frame, after)))
        view.shown(bool(data))

    def _answered(self, parts: list[Message]) -> list[Message]:
        "Ask a client that answers frames to let its terminal answer this one. Lillecarl/pymux#643."
        if self.frame_room is not None:
            parts[-1][Field.ANSWER] = True
        return parts
