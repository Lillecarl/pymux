"""
The image on the clipboard of the machine the client runs on.

A pane on another server cannot reach this machine's clipboard, and a
terminal's OSC 52 carries text only. So the client reads the image
itself, the way a program on this machine would, and sends it ahead of
the Ctrl-V that pastes it. Lillecarl/pyterm#958.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from subprocess import DEVNULL, PIPE

import anyio

__all__ = ["clipboard_image", "clipboard_image_now", "pastes_an_image"]

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
    commands = _commands()
    if commands is None:
        return None
    listing, reading = commands
    with anyio.move_on_after(READ_SECONDS):
        try:
            if listing is not None and not _lists_png(await _run(listing)):
                return None
            return _png_of(await _run(reading))
        except OSError:
            return None
    return None


def clipboard_image_now() -> bytes | None:
    """
    `clipboard_image`, for the socket client, which runs no event loop.
    It holds that client's `select` for the read, and nothing else runs
    meanwhile, so the image is ahead of the key with no queue.
    """
    commands = _commands()
    if commands is None:
        return None
    listing, reading = commands
    try:
        if listing is not None and not _lists_png(_run_now(listing)):
            return None
        return _png_of(_run_now(reading))
    except OSError, subprocess.TimeoutExpired:
        return None


def _commands() -> tuple[list[str] | None, list[str]] | None:
    "What lists the clipboard's types, if anything must, and what reads its PNG; None where nothing can."
    if sys.platform == "darwin":
        return None, ["osascript", "-e", "the clipboard as «class PNGf»"]
    if os.environ.get("WAYLAND_DISPLAY"):
        return ["wl-paste", "--list-types"], ["wl-paste", "--type", "image/png"]
    if os.environ.get("DISPLAY"):
        return (
            ["xclip", "-selection", "clipboard", "-t", "TARGETS", "-o"],
            ["xclip", "-selection", "clipboard", "-t", "image/png", "-o"],
        )
    return None


def _lists_png(types: bytes | None) -> bool:
    return bool(types) and b"image/png" in types.split()


def _png_of(read: bytes | None) -> bytes | None:
    "The PNG a read gave: `osascript` writes it as hex."
    if not read:
        return None
    if sys.platform != "darwin":
        return read
    found = _PNG_DATA.search(read.decode("utf-8", "replace"))
    return bytes.fromhex(found.group(1)) if found and found.group(1) else None


async def _run(command: list[str]) -> bytes | None:
    done = await anyio.run_process(command, check=False, stderr=DEVNULL)
    return done.stdout if done.returncode == 0 else None


def _run_now(command: list[str]) -> bytes | None:
    done = subprocess.run(command, check=False, stdin=DEVNULL, stdout=PIPE, stderr=DEVNULL, timeout=READ_SECONDS)
    return done.stdout if done.returncode == 0 else None
