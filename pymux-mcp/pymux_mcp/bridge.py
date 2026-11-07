"""
The client half of `qutebridge`: one request to a qutebrowser's bridge,
and its answer. Lillecarl/pyterm#944.

A qutebrowser that pymux starts serves the bridge from the moment it
runs, at `BRIDGE` in its profile. A person's own qutebrowser serves it
once they load `qutebridge` in their config.py and type
`:pymux-bridge on`, or at once if they load it with `start=True`.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Final

import anyio

from .qutebridge import SOCKET

__all__ = ["BRIDGE", "ask", "own_socket", "socket_for"]

#: The bridge's socket in a pymux profile, at its root.
BRIDGE: Final = "bridge.sock"
#: The longest socket path: sockaddr_un.sun_path on macOS, less its NUL.
#: Linux allows four bytes more.
SOCKET_PATH_MAX: Final = 103


def socket_for(profile: Path, name: str = BRIDGE) -> Path:
    """
    Where a browser pymux starts on `profile` serves the socket `name`:
    in the profile, or, when that path is too long for a socket, under a
    name from the profile's hash in the runtime directory, which only
    this user can enter.
    """
    inside = profile / name
    if len(os.fsencode(inside)) <= SOCKET_PATH_MAX:
        return inside
    runtime = Path(os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir())
    digest = hashlib.sha256(os.fsencode(profile)).hexdigest()[:16]
    return runtime / ("pymux-%s-%s.sock" % (Path(name).stem, digest))


def own_socket() -> Path:
    "Where the person's own qutebrowser serves its bridge: in qutebrowser's data directory."
    if sys.platform == "darwin":
        data = Path.home() / "Library" / "Application Support"
    else:
        data = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return data / "qutebrowser" / SOCKET


async def ask(path: Path, request: dict[str, Any], timeout: float) -> dict[str, Any]:
    "Send one request to the bridge at `path`, and answer what it said."
    if not path.exists():
        raise LookupError(
            "no qutebrowser bridge at %s: a qutebrowser that loads pymux-mcp's qutebridge serves it"
            " once `:pymux-bridge on` is typed in it" % (path,)
        )
    try:
        with anyio.fail_after(timeout):
            async with await anyio.connect_unix(path) as stream:
                await stream.send(json.dumps(request).encode() + b"\n")
                raw = b""
                async for chunk in stream:
                    raw += chunk
    except ConnectionRefusedError:
        raise LookupError("the qutebrowser bridge at %s does not answer: its browser ended" % (path,)) from None
    except TimeoutError:
        raise LookupError("the qutebrowser bridge at %s did not answer in %gs" % (path, timeout)) from None
    return json.loads(raw)
