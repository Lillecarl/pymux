"""
A Wayland compositor the server holds, for the panes that have none.

It is optional twice over. `pysmithay` is an extra, Rust and Linux only,
and this module imports it or does nothing; `wayland-server` turns it
off where it loads. Lillecarl/pymux#383.

What it serves today is the clipboard. A pane whose session names no
`WAYLAND_DISPLAY` is given this one, so wl-copy and wl-paste work there
and share one clipboard across the panes. A session that has a display
of its own -- a desktop attached, an ssh client forwarding one -- keeps
it: a program started there belongs on that screen, not on one nobody
looks at.

The clipboard keeps `set-clipboard`'s promise. What a pane copies
reaches the paste buffer and the person's terminal only under "on", as
OSC 52 does; and a pane reads what the person copied only under "on".
Between the panes it is always shared, because that is all a Wayland
clipboard is.
"""

from __future__ import annotations

import contextlib
import errno
import logging
import os
import sys
from typing import TYPE_CHECKING

import anyio
import anyio.abc
from prompt_toolkit.clipboard import ClipboardData

from pymux.options import Clipboard

if TYPE_CHECKING:
    from pysmithay import Compositor

    from pymux.main import Pymux

try:
    import pysmithay
except ImportError:
    pysmithay = None

logger = logging.getLogger(__name__)

#: The mime types that are text, best first. wl-copy offers the first
#: two for text it was given; X11 programs bridged in offer the rest.
TEXT_TYPES = ("text/plain;charset=utf-8", "UTF8_STRING", "text/plain", "STRING", "TEXT")

#: How long a client may take to hand its selection over, and how much
#: of it is read. A clipboard is a person's copy, not a file transfer.
READ_SECONDS = 5.0
READ_LIMIT = 16 << 20


def available() -> bool:
    "Whether a compositor can run here at all."
    return pysmithay is not None and sys.platform == "linux"


def socket_path_of(server_socket: str | None) -> str | None:
    """
    Where the compositor listens: beside the server's own socket, so the
    path stays the same across `upgrade-server` and a pane keeps a
    working `WAYLAND_DISPLAY` through one. With no socket, the first
    free `wayland-N` in `$XDG_RUNTIME_DIR`.

    The name ends in ".sock" because Wayland's lock file replaces the
    extension, and a dotted server name would otherwise share one.
    """
    if not server_socket:
        return None
    directory, name = os.path.split(server_socket)
    return os.path.join(directory, "wayland-%s.sock" % (name,))


class WaylandServer:
    "The compositor of one pymux server, while `wayland-server` is on."

    def __init__(self, pymux: Pymux) -> None:
        self.pymux = pymux
        self.compositor: Compositor | None = None
        #: What `WAYLAND_DISPLAY` says to reach it, while it runs.
        self.display: str | None = None
        #: The text a pane last copied, so the paste buffer it fills is
        #: not handed back and does not take the selection from its
        #: owner.
        self._from_panes: str | None = None
        self._changed = anyio.Event()
        self._scope: anyio.CancelScope | None = None

    def option_changed(self) -> None:
        "`wayland-server` moved: start or stop on the next turn."
        self._changed.set()
        if not self.pymux.wayland_server and self._scope is not None:
            self._scope.cancel()

    async def keep(self) -> None:
        "Run the compositor whenever the option is on. Never returns."
        while True:
            if self.pymux.wayland_server and available():
                with anyio.CancelScope() as self._scope:
                    await self._serve()
                self._scope = None
            await self._changed.wait()
            self._changed = anyio.Event()

    async def _serve(self) -> None:
        if pysmithay is None:
            return
        path = socket_path_of(self.pymux.socket_name)
        try:
            compositor = pysmithay.Compositor(path)
        except OSError as error:
            # No XDG_RUNTIME_DIR, or a path taken: the panes simply get
            # no display, as without pysmithay.
            logger.warning("No Wayland compositor: %s", error)
            return

        self.compositor = compositor
        self.display = compositor.socket_name
        self.pymux.clipboard.watchers.append(self._buffer_changed)
        logger.info("Wayland compositor on %s", self.display)
        try:
            # What the person copied before it started is there to paste.
            self._buffer_changed(self.pymux.clipboard.get_data())
            async with anyio.create_task_group() as tasks:
                for fd in compositor.fds():
                    tasks.start_soon(self._dispatch_on, compositor, fd, tasks)
        finally:
            self.pymux.clipboard.watchers.remove(self._buffer_changed)
            self.compositor = None
            self.display = None
            if path:
                for leftover in (path, os.path.splitext(path)[0] + ".lock"):
                    with contextlib.suppress(FileNotFoundError):
                        os.unlink(leftover)

    async def _dispatch_on(self, compositor: Compositor, fd: int, tasks: anyio.abc.TaskGroup) -> None:
        while True:
            await anyio.wait_readable(fd)
            for event in compositor.dispatch():
                if event[0] == "selection":
                    _, target, mime_types = event
                    if target == "clipboard" and isinstance(mime_types, list) and mime_types:
                        tasks.start_soon(self._take_selection, compositor, mime_types)

    async def _take_selection(self, compositor: Compositor, mime_types: list[str]) -> None:
        "A pane copied: read it, and pass it on as `set-clipboard` allows."
        mime_type = next((one for one in TEXT_TYPES if one in mime_types), None)
        if mime_type is None:
            return
        try:
            fd = compositor.request_selection("clipboard", mime_type)
        except ValueError:
            return  # Replaced or gone before it was asked for.
        data = await _read_all(fd)
        if data is None:
            return
        text = data.decode("utf-8", "replace")
        self._from_panes = text
        if self.pymux.clipboard_mode is not Clipboard.ON or not text:
            return
        self.pymux.clipboard.set_data(ClipboardData(text))
        self.pymux.write_user_clipboard(text)

    def _buffer_changed(self, data: ClipboardData) -> None:
        "The paste buffer moved: offer it to the panes, if they may read it."
        if self.compositor is None or not data.text or data.text == self._from_panes:
            return
        if self.pymux.clipboard_mode is not Clipboard.ON:
            return
        encoded = data.text.encode("utf-8")
        self.compositor.set_selection("clipboard", {mime_type: encoded for mime_type in TEXT_TYPES[:3]})


async def _read_all(fd: int) -> bytes | None:
    "A pipe read to its end, or None when the writer stalls or floods it."
    os.set_blocking(fd, False)
    chunks: list[bytes] = []
    size = 0
    try:
        with anyio.move_on_after(READ_SECONDS):
            while True:
                await anyio.wait_readable(fd)
                try:
                    chunk = os.read(fd, 1 << 16)
                except OSError as error:
                    if error.errno == errno.EAGAIN:
                        continue
                    raise
                if not chunk:
                    return b"".join(chunks)
                size += len(chunk)
                if size > READ_LIMIT:
                    return None
                chunks.append(chunk)
        return None
    finally:
        os.close(fd)
