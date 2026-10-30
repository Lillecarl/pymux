"""
A notice in a corner that an error was logged, and where to read it.

A server logs an exception and keeps going, so an error is invisible to
the person using it: they see a thing not work, and nothing tells them
where to look. The notice says so for a few seconds, over the top right
of every attached screen, and takes neither the keyboard nor the
cursor. Lillecarl/pymux#241.

**Counted, not stacked.** A loop that raises every frame raises a
hundred times a second, so every error while the notice shows adds to
its count and keeps it up, and it goes when the errors stop.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import TYPE_CHECKING, override

import anyio

from ..log import default_logfile, logfile, logger

if TYPE_CHECKING:
    from pymux.core.main import Pymux

__all__ = ["SHOWN_FOR", "ErrorNotice"]

#: How long the notice stays after the last error, in seconds. Long
#: enough to read one line, short enough not to live in the corner.
SHOWN_FOR = 5.0


class ErrorNotice:
    "What the corner says, and the handler that makes it say it."

    def __init__(self, pymux: Pymux) -> None:
        self.pymux = pymux
        #: Errors since the notice opened. Nothing shows at zero.
        self.count = 0
        self._until = 0.0
        self._closing = False
        self._handler = _Handler(self)
        self._loop_thread: int | None = None

    def install(self) -> None:
        "Start noticing what the server logs. `Pymux.running` calls it."
        self._loop_thread = threading.get_ident()
        logger.addHandler(self._handler)

    def uninstall(self) -> None:
        logger.removeHandler(self._handler)

    def text(self) -> str:
        "The one line the corner shows, or nothing."
        if not self.count:
            return ""
        what = "an error was" if self.count == 1 else "%d errors were" % self.count
        path = str(logfile() or default_logfile())
        home = os.path.expanduser("~")
        if path.startswith(home + os.sep):
            path = "~" + path[len(home) :]
        return " %s logged: %s " % (what, path)

    def noticed(self) -> None:
        """
        One more error. On the event loop's thread only: a frame is
        asked for here, and an application is not thread safe.
        """
        self.count += 1
        self._until = time.monotonic() + SHOWN_FOR
        self.pymux.invalidate()
        tasks = self.pymux.tasks
        if tasks is not None and not self._closing:
            self._closing = True
            tasks.start_soon(self._close_when_quiet)

    async def _close_when_quiet(self) -> None:
        try:
            while (left := self._until - time.monotonic()) > 0:
                await anyio.sleep(left)
            self.count = 0
            self.pymux.invalidate()
        finally:
            self._closing = False

    def _from_any_thread(self) -> None:
        if threading.get_ident() == self._loop_thread:
            self.noticed()
            return
        loop = getattr(self.pymux, "loop", None)
        if loop is not None:
            loop.call_soon_threadsafe(self.noticed)


class _Handler(logging.Handler):
    """
    Notices every record of ERROR and above.

    **An error while noticing one notices nothing.** Drawing the notice
    can raise too, and a notice of that would ask for the frame that
    raises again.
    """

    def __init__(self, notice: ErrorNotice) -> None:
        super().__init__(level=logging.ERROR)
        self.notice = notice
        self._busy = False

    @override
    def emit(self, record: logging.LogRecord) -> None:
        if self._busy:
            return
        self._busy = True
        # A handler that raises turns one logged error into a crash of
        # whatever logged it, which is the opposite of what logging is
        # for; `logging.Handler.handleError` swallows the same way.
        try:
            self.notice._from_any_thread()
        except Exception:
            pass
        finally:
            self._busy = False
