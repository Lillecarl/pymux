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
"""

from __future__ import annotations

import base64
import contextlib
import itertools
import os
import zlib
from collections.abc import Iterable
from multiprocessing import shared_memory

__all__ = ["SHM_PROBE_ID", "Base64Carrier", "ShmCarrier", "frame_commands", "shm_probe"]

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
            memory.buf[: len(pixels)] = pixels
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
    image_id: int, whole: bool, width: int, height: int, rects: Iterable[Rect], carrier: Base64Carrier | ShmCarrier
) -> str:
    "The commands that bring image `image_id` up to date with these rectangles."
    out = []
    for x, y, w, h, pixels in rects:
        if whole:
            keys = "a=t,i=%d,s=%d,v=%d,q=2" % (image_id, width, height)
        else:
            keys = "a=f,i=%d,r=1,x=%d,y=%d,s=%d,v=%d,X=1,q=2" % (image_id, x, y, w, h)
        out.append(carrier.command(keys, pixels))
    return "".join(out)


def shm_probe(carrier: ShmCarrier) -> str:
    """
    The query that asks a terminal whether it reads shared memory: one
    black pixel under `SHM_PROBE_ID`. kitty answers `OK`, or an error
    when it cannot open the object; a terminal without the protocol says
    nothing.
    """
    return carrier.command("a=q,i=%d,s=1,v=1" % SHM_PROBE_ID, bytes(3))
