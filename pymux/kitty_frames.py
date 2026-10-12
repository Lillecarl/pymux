"""
A window pane's frame as kitty graphics commands, encoded where the
terminal is. Lillecarl/pymux#634.

The server sends raw RGB rectangles (`pysmithay.Compositor.frame_rects`),
and whoever writes to the terminal turns them into commands: the whole
frame as `a=t`, and the damage after it as edits of the root frame
(`a=f,r=1,X=1`), the keys `pysmithay/src/kitty.rs` writes.

**Two ways to carry the pixels.** A kitty on the same machine reads them
from POSIX shared memory (`t=s`): one `memcpy` here, and none of the
compression and base64 that cost 12 ms a frame at 1600x900. Any other
terminal gets them zlib compressed and base64 encoded in the command
itself (`t=d`, `o=z`). Which one a terminal takes is asked once,
with `SHM_PROBE`.

A window pane in a codec (`window-codec`) comes as encoded frames, which
`decoders` turns back into RGB rectangles here. Lillecarl/pymux#639.
"""

from __future__ import annotations

import base64
import contextlib
import itertools
import os
import zlib
from collections.abc import Callable, Iterable
from multiprocessing import shared_memory
from typing import Any

from libpymux.protocol import Codec

__all__ = ["SHM_PROBE_ID", "Base64Carrier", "Pictures", "ShmCarrier", "decoders", "frame_commands", "shm_probe"]

#: kitty takes at most this many bytes of base64 in one command.
CHUNK = 4096

#: The image id of the shm probe: one no pane ever takes.
SHM_PROBE_ID = 0x7FFF_FF31

#: macOS refuses a POSIX shm name longer than this. kitty says so too
#: (`data-types.c`, `SHM_NAME_MAX`).
SHM_NAME_MAX = 30

Rect = tuple[int, int, int, int, bytes]


class Base64Carrier:
    "Pixels inside the command: zlib, then base64, in chunks kitty takes."

    def command(self, keys: str, pixels: bytes) -> str:
        payload = base64.b64encode(zlib.compress(pixels, 1)).decode("ascii")
        chunks = [payload[start : start + CHUNK] for start in range(0, len(payload), CHUNK)] or [""]
        out = []
        for index, chunk in enumerate(chunks):
            more = int(index + 1 < len(chunks))
            if index == 0:
                out.append("\x1b_G%s,f=24,o=z,m=%d;%s\x1b\\" % (keys, more, chunk))
            else:
                out.append("\x1b_Gm=%d;%s\x1b\\" % (more, chunk))
        return "".join(out)


class ShmCarrier:
    """
    Pixels in a POSIX shared memory object of their own, which kitty
    maps, reads and unlinks.

    **A new object for every command**: kitty reads it when it gets to
    the command, so one written again before then would show the wrong
    frame. `close` unlinks what kitty never got to, as at exit.
    """

    def __init__(self) -> None:
        self._prefix = "pmx%x-" % os.getpid()
        self._counter = itertools.count()
        self._made: list[str] = []

    def _name(self) -> str:
        # The counter wraps well inside the length macOS allows: a name
        # is used once, and read before thousands more are made.
        name = "%s%x" % (self._prefix, next(self._counter) % 0x1000000)
        assert len(name) + 1 <= SHM_NAME_MAX
        return name

    def write(self, pixels: bytes) -> str:
        "Put `pixels` in a new object, and answer the name kitty opens."
        name = self._name()
        memory = shared_memory.SharedMemory(name, create=True, size=max(1, len(pixels)), track=False)
        try:
            buffer = memory.buf
            assert buffer is not None, "a SharedMemory that is open has a buffer"
            buffer[: len(pixels)] = pixels
        finally:
            memory.close()
        self._made.append(name)
        # Names kitty unlinked are gone already; keep the list short.
        if len(self._made) > 256:
            del self._made[:128]
        return "/" + name

    def command(self, keys: str, pixels: bytes) -> str:
        # `S=` always: kitty maps the size of the object when none is
        # given, and macOS rounds an object up to a page.
        name = self.write(pixels)
        return "\x1b_G%s,f=24,t=s,S=%d;%s\x1b\\" % (keys, len(pixels), base64.b64encode(name.encode()).decode("ascii"))

    def close(self) -> None:
        for name in self._made:
            with contextlib.suppress(FileNotFoundError):
                shared_memory.SharedMemory(name, track=False).unlink()
        self._made.clear()


def frame_commands(
    image_id: int,
    whole: bool,
    width: int,
    height: int,
    rects: Iterable[Rect],
    carrier: Base64Carrier | ShmCarrier,
    answer: bool = False,
) -> str:
    """
    The commands that bring image `image_id` up to date with these
    rectangles. With `answer`, the terminal answers the last one once it
    processed it; every other command is quiet.
    """
    rects = list(rects)
    out = []
    for index, (x, y, w, h, pixels) in enumerate(rects):
        quiet = "" if answer and index == len(rects) - 1 else ",q=2"
        if whole:
            keys = "a=t,i=%d,s=%d,v=%d%s" % (image_id, width, height, quiet)
        else:
            keys = "a=f,i=%d,r=1,x=%d,y=%d,s=%d,v=%d,X=1%s" % (image_id, x, y, w, h, quiet)
        out.append(carrier.command(keys, pixels))
    return "".join(out)


class Pictures:
    """
    The client's copy of each window pane's picture, so a frame of many
    rectangles goes to the terminal as one edit: their bounding box.

    kitty uploads the whole image to the GPU again for every edit
    command (`graphics.c`, `update_current_frame`). A Firefox frame is a
    strip per line of text, and on Carl's Mac about 250 strips a second
    were 250 uploads of a Retina-sized image: macOS converts each on the
    CPU, and the GPU starved the whole desktop. Lillecarl/pymux#644.
    """

    def __init__(self) -> None:
        self._held: dict[int, tuple[int, int, bytearray]] = {}

    def clear(self) -> None:
        self._held.clear()

    def merged(self, image: int, whole: bool, width: int, height: int, rects: list) -> list:
        "Take the frame into the copy, and answer the rectangles to send: one, when the copy allows."
        if whole:
            if len(rects) == 1 and len(rects[0][4]) == width * height * 3:
                self._held[image] = (width, height, bytearray(rects[0][4]))
            else:
                self._held.pop(image, None)
            return rects
        held = self._held.get(image)
        if held is None or held[:2] != (width, height):
            return rects
        picture = held[2]
        row = width * 3
        for x, y, w, h, pixels in rects:
            for line in range(h):
                at = (y + line) * row + x * 3
                picture[at : at + w * 3] = pixels[line * w * 3 : (line + 1) * w * 3]
        if len(rects) <= 1:
            return rects
        left = min(rect[0] for rect in rects)
        top = min(rect[1] for rect in rects)
        right = max(rect[0] + rect[2] for rect in rects)
        bottom = max(rect[1] + rect[3] for rect in rects)
        box = b"".join(picture[line * row + left * 3 : line * row + right * 3] for line in range(top, bottom))
        return [(left, top, right - left, bottom - top, box)]


def decoders() -> dict[str, Callable[[], Any]]:
    """
    The codecs this machine decodes, and what makes a decoder of each:
    `decoder.decode(data, rects)` answers `(width, height, [(x, y, w, h,
    rgb)])`. None without pymux_rs, which builds them.
    """
    try:
        from pymux_rs import Vp9Decoder
    except ImportError:
        return {}
    return {Codec.VP9: Vp9Decoder}


def shm_probe(carrier: ShmCarrier) -> str:
    """
    The query that asks a terminal whether it reads shared memory: one
    black pixel under `SHM_PROBE_ID`. kitty answers `OK`, or an error
    when it cannot open the object; a terminal without the protocol says
    nothing.
    """
    return carrier.command("a=q,i=%d,s=1,v=1" % SHM_PROBE_ID, bytes(3))
