"""
The image on the clipboard of the machine the client runs on.

A pane on another server cannot reach this machine's clipboard, and a
terminal's OSC 52 carries text only. So the client reads the image
itself, the way a program on this machine would, and sends it ahead of
the Ctrl-V that pastes it. Lillecarl/pymux#956.
"""

from __future__ import annotations

import os
import re
import sys
from subprocess import DEVNULL

import anyio

__all__ = ["clipboard_image", "pastes_an_image"]

#: Ctrl-V as a legacy terminal sends it. Claude Code pastes an image on it.
CTRL_V = "\x16"

#: v under the kitty keyboard protocol, in every form its flags give it:
#: alternate keys after the code, an event type after the modifiers.
#: The modifiers are one more than their bits, and Control is 4.
_KITTY_V = re.compile(r"\x1b\[118(?::\d*)*;(\d+)(?::(\d+))?(?:;[\d:]*)?u")

#: kitty's event type for a release, which pastes nothing.
_RELEASE = "3"

#: How long a clipboard read may hold the key behind it.
READ_SECONDS = 2.0

#: `osascript`'s answer for PNG data on the clipboard: `«data PNGf…»`.
_PNG_DATA = re.compile(r"«data PNGf([0-9A-Fa-f]*)»")


def pastes_an_image(data: str) -> bool:
    "Whether typed `data` holds a Ctrl-V, in whatever keyboard mode the pane's program set."
    if CTRL_V in data:
        return True
    return any((int(found.group(1)) - 1) & 4 and found.group(2) != _RELEASE for found in _KITTY_V.finditer(data))


async def clipboard_image() -> bytes | None:
    "The PNG on this machine's clipboard, or None for none, or no way to read it in time."
    with anyio.move_on_after(READ_SECONDS):
        try:
            if sys.platform == "darwin":
                return await _macos()
            return await _linux()
        except OSError:
            return None
    return None


async def _run(*command: str) -> bytes | None:
    done = await anyio.run_process(list(command), check=False, stderr=DEVNULL)
    return done.stdout if done.returncode == 0 else None


async def _macos() -> bytes | None:
    said = await _run("osascript", "-e", "the clipboard as «class PNGf»")
    found = _PNG_DATA.search(said.decode("utf-8", "replace")) if said else None
    return bytes.fromhex(found.group(1)) if found and found.group(1) else None


async def _linux() -> bytes | None:
    if os.environ.get("WAYLAND_DISPLAY"):
        types = await _run("wl-paste", "--list-types")
        if types and b"image/png" in types.split():
            return await _run("wl-paste", "--type", "image/png")
        return None
    if os.environ.get("DISPLAY"):
        types = await _run("xclip", "-selection", "clipboard", "-t", "TARGETS", "-o")
        if types and b"image/png" in types.split():
            return await _run("xclip", "-selection", "clipboard", "-t", "image/png", "-o")
    return None
