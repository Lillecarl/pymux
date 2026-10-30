"""
A session's overlay: one pane in the middle of the screen, over the
windows, that every client of the session sees. It runs a program or
shows a job, and it takes the keyboard while it is open. A job also
opens here as a window of its own, or runs again to be taken over.
Lillecarl/pymux#324, Lillecarl/pymux#528.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..enums import Woke
from ..jobs import Job, describe
from .planes import remember_the_size_made_at
from .session import Session
from .window_tree import Window

if TYPE_CHECKING:
    from .main import Pymux
    from .window_tree import Pane

__all__ = ["Overlays"]


class Overlays:
    "Opens and closes the overlay of each session, and the windows a job opens in."

    def __init__(self, pymux: Pymux) -> None:
        self.pymux = pymux

    async def display(
        self,
        command: str | None = None,
        width: str | None = None,
        height: str | None = None,
        title: str | None = None,
        session: Session | None = None,
    ):
        """
        Open an overlay pane in the middle of the screen.

        It runs `command`, or the default shell, and closes itself when
        that finishes. It takes the keyboard while it is open.

        The overlay belongs to the session, like a window does, so
        every client of that session sees the same one and a second
        call replaces the first. That also means a command from the
        command line reaches it: the temporary client that runs such a
        command is gone before the next render, and it is on the
        session of the pane the command was typed in.
        Lillecarl/pymux#324.
        """
        if session is None:
            session = self.pymux.current_session
        self.close(session)

        pane: Pane | None = None

        def done() -> None:
            self.ended(session, pane)

        try:
            window = session.arrangement.get_active_window()
        except Exception:
            window = None

        pane = await self.pymux.panes.create_pane(window=window, command=command, on_done=done)
        self._show(session, pane, title or command or "overlay", width, height)
        return pane

    def ended(self, session: Session, pane: Pane | None) -> None:
        "The program of an overlay finished, so the overlay goes."
        if session.overlay_pane is pane:
            session.overlay_pane = None
            self.pymux.sync_focus_everywhere()

    def close(self, session: Session | None = None) -> None:
        """
        Close the overlay pane of one session, and kill what runs in
        it. Without a session, the one this client is on.
        """
        if session is None:
            session = self.pymux.current_session

        pane = session.overlay_pane
        if pane is None:
            return

        session.overlay_pane = None

        if not pane.process.is_terminated:
            self.pymux.panes.hang_up(pane)

        self.pymux.sync_focus_everywhere()
        self.pymux.invalidate(Woke.OVERLAY_CLOSED)

    async def display_job(
        self,
        job: Job,
        session: Session | None = None,
        width: str | None = None,
        height: str | None = None,
    ) -> Pane:
        """
        Show a job in the overlay of a session.

        The tail replays and the rest follows, read-only, until
        Escape closes it: a viewer never ends on its own, so unlike a
        program's overlay this one stays past the end of the job,
        saying how it ended. Closing stops the follow and never
        touches the job, which runs on until it ends on its own. A
        second call replaces the first, the way overlays do.
        Lillecarl/pymux#528.
        """
        if session is None:
            session = self.pymux.current_session
        self.close(session)

        pane = await self.pymux.panes.create_pane(session=session, job=job)
        self._show(session, pane, describe(job), width, height)
        return pane

    def _show(self, session: Session, pane: Pane, title: str, width: str | None, height: str | None) -> None:
        "Put `pane` up as the session's overlay, and give it the keyboard."
        session.overlay_pane = pane
        session.overlay_title = title
        session.overlay_width = width
        session.overlay_height = height
        self.pymux.sync_focus_everywhere()
        self.pymux.invalidate(Woke.OVERLAY_OPENED)

    async def open_job_in_pane(self, job: Job, session: Session | None = None) -> Window:
        """
        Open a job as a pane in a new window of a session.

        A viewer, read-only, that replays the tail and follows the
        rest; nothing forks and no program runs. Closing the window
        never touches the job. Without a session it is the session of
        the client that asks. Lillecarl/pymux#528.
        """
        if session is None:
            session = self.pymux.current_session

        pane = await self.pymux.panes.create_pane(session=session, job=job)
        remember_the_size_made_at(session.arrangement.create_window(pane, name="job %d" % job.job_id), pane)
        if session is self.pymux.current_session:
            pane.focus()
        self.pymux.invalidate(Woke.WINDOW_OPENED)

        window = self.pymux._window_holding(pane)
        assert window is not None  # The arrangement just took the pane.
        return window

    async def take_over_job(self, job: Job, session: Session | None = None) -> None:
        """
        Run a job's command again, interactively, in a new window.

        A terminal the person can type in, from the job's directory:
        that is what taking over means for a job that holds no stdin
        (`run` starts one on `/dev/null`). The recorded job is
        untouched -- it runs on, and detaching never kills it -- and
        the new program answers to the session's environment, not the
        job's. Lillecarl/pymux#528.
        """
        if session is None:
            session = self.pymux.current_session
        await self.pymux.panes.create_window(command=job.command, start_directory=job.directory, session=session)
