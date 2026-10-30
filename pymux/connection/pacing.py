"""
How fast a client gets frames: a status report behind each frame, whose
answer says the terminal read it (Lillecarl/pymux#259, #825), the window
pane frames a client has not answered yet (Lillecarl/pymux#641), and the
counts `#{client_fps}` reads.
"""

from __future__ import annotations

from collections import deque
from typing import (
    TYPE_CHECKING,
    ClassVar,
)

import anyio
from pyte.keep import Keep

from ..log import logger
from ..options.client_options import ClientOptions
from .rate import Rate

if TYPE_CHECKING:
    from .server import ServerConnection

__all__ = ["FramePacing"]

#: A status report and its answer: 0 is ready, 3 a malfunction, and
#: either says the terminal read everything sent before the report.
#: Lillecarl/pymux#259.
STATUS_REPORT = "\x1b[5n"

#: How long a window pane's frame may go unanswered before the next one
#: goes all the same. Lillecarl/pymux#643.
FRAME_ANSWER_SECONDS = 2.0


class FramePacing:
    """
    One connection's frames on their way to its terminal.
    """

    KEEP: ClassVar[dict[str, Keep]] = {
        "connection": Keep.REBUILT,
        "paced": Keep.REBUILT,
        # A frame in flight belongs to the connection it went out on.
        "frames_out": Keep.DROPPED,
        # A new server asks its own reports, and learns again whether
        # the terminal answers them.
        "unread": Keep.DROPPED,
        "frame_held": Keep.DROPPED,
        "answers_reports": Keep.DROPPED,
        "frames_shown": Keep.DROPPED,
        "frames_sent": Keep.DROPPED,
        "rate_frames": Keep.DROPPED,
        "window_frames_sent": Keep.DROPPED,
        "rate_window_frames": Keep.DROPPED,
    }

    def __init__(self, connection: ServerConnection) -> None:
        self.connection = connection

        #: Window frames sent and not yet answered with `frame-shown`,
        #: for a client that answers; set while there are none.
        #: Lillecarl/pymux#641.
        self.frames_out = 0
        self.frames_shown = anyio.Event()
        self.frames_shown.set()
        #: Whether this client said its terminal answers a status
        #: report, the size of each frame whose report is not answered
        #: yet, oldest first, and whether one ever was. Lillecarl/pymux#259.
        self.paced = False
        self.unread: deque[int] = deque()
        self.answers_reports = False
        #: A frame was skipped for the terminal, so its answer asks for
        #: the next one. Lillecarl/pymux#824.
        self.frame_held = False
        #: The frames this client was sent, and what `#{client_fps}`
        #: reads off them. Lillecarl/pymux#596.
        self.frames_sent = 0
        self.rate_frames = Rate()
        #: The window panes' frames, which go out apart from the frames
        #: above, and what `#{client_window_fps}` reads off them.
        self.window_frames_sent = 0
        self.rate_window_frames = Rate()

    def frame_answered(self) -> None:
        self.frames_out = max(0, self.frames_out - 1)
        if not self.frames_out:
            self.frames_shown.set()

    async def frames_all_shown(self) -> None:
        """
        Wait until the client answered every window frame it was sent, or
        went. A terminal that never answers is let go after
        `FRAME_ANSWER_SECONDS`, so it costs frames and never the window.
        """
        with anyio.move_on_after(FRAME_ANSWER_SECONDS) as waited:
            while self.frames_out and not self.connection._closed:
                await self.frames_shown.wait()
        if waited.cancelled_caught:
            logger.warning(
                "Client %s did not answer %d window frames.", self.connection.client_id or "?", self.frames_out
            )
            self.frames_out = 0
            self.frames_shown.set()

    async def drained(self) -> None:
        """
        Wait until this client's terminal took what it was sent: every
        window frame answered, for a client that answers, and the
        socket empty. Lillecarl/pymux#641.
        """
        await self.connection.pipe_connection.drained()
        if self.connection.graphics.windows.frame_room is not None:
            await self.frames_all_shown()

    def frame_went_out(self) -> None:
        self.frames_sent += 1

    def window_frame_went_out(self) -> None:
        self.window_frames_sent += 1
        if self.connection.graphics.windows.frame_room is not None:
            self.frames_out += 1
            if self.frames_shown.is_set():
                self.frames_shown = anyio.Event()

    def report_after_frame(self, size: int) -> str:
        """
        What goes behind a frame of `size` bytes: a status report, for a
        client whose terminal answers one. The answer arrives once the
        terminal read everything before it, which over ssh is the only
        thing that says a frame reached the person. Lillecarl/pymux#259.
        """
        if not self.paced:
            return ""
        self.unread.append(size + len(STATUS_REPORT))
        return STATUS_REPORT

    def status_answered(self) -> None:
        """
        The terminal read a frame. A frame held back for it can draw.
        """
        if self.unread:
            self.unread.popleft()
        self.answers_reports = True
        if self.frame_held and not self.waits_for_terminal and self.connection.client_state is not None:
            self.frame_held = False
            self.connection.client_state.app.invalidate()

    @property
    def waits_for_terminal(self) -> bool:
        """
        Whether the next frame waits: as many frames, or as many bytes,
        are on their way to a terminal that has answered before as this
        client's `frames-in-flight` and `bytes-in-flight` allow. A frame
        goes whenever none is, so a big one on a slow link waits for the
        answer and never builds a backlog. One that never answered is
        never waited for, so a terminal that ignores the report costs
        nothing; one that answered and went quiet holds a few frames and
        no more, which is the dead link of a phone that lost its signal.
        Lillecarl/pymux#825.
        """
        if not self.answers_reports or not self.unread:
            return False
        options = self.connection.client_state.options if self.connection.client_state is not None else ClientOptions()
        frames, size = options.frames_in_flight, options.bytes_in_flight
        return bool(frames and len(self.unread) >= frames) or bool(size and sum(self.unread) >= size)
