"""
Which pane has the keyboard. A pane has the focus while it is the active
pane of a client whose terminal window has it, and an open overlay takes
it over. This answers that for a client, and tells the panes that asked
(DEC 1004) when they gain or lose it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from prompt_toolkit.application.current import set_app
from pyte.keep import Keep
from pyte.sequences.reports import FocusReport

from ..connection.client_state import ClientState
from ..ids import PaneId
from ..log import logger
from ..wayland.window_pane import WindowProcess, WindowScreen
from .pane import Pane

if TYPE_CHECKING:
    from .main import Pymux

__all__ = ["Focus"]


class Focus:
    "Finds the pane each client's keyboard reaches, and tells a pane when that changes."

    KEEP: ClassVar[dict[str, Keep]] = {
        "pymux": Keep.REBUILT,
        # Told again at the first focus sync, which a program reads as one more focus-in.
        "_focused_panes": Keep.DROPPED,
    }

    def __init__(self, pymux: Pymux) -> None:
        self.pymux = pymux
        #: The ids of the panes that have the focus. Lillecarl/pymux#795.
        self._focused_panes: set[PaneId] = set()

    def sync_panes(self) -> None:
        """
        Send focus-in and focus-out to the panes that asked (DEC 1004).

        A pane has the focus while it is the active pane of a client
        whose terminal window has it. Several clients can give several
        panes the focus at once; tmux has one client and one focus.
        Lillecarl/pymux#795.
        """
        self.pymux.notifier.close_seen_notifications()
        self.pymux.notifier.drop_seen_popups()
        focused = set()
        for client_state in self.pymux._client_states.values():
            if client_state.terminal_focused and not client_state.temporary:
                pane = client_state.session.arrangement.get_active_pane_for(client_state.app)
                if pane is not None:
                    focused.add(pane.pane_id)
        if focused == self._focused_panes:
            return
        for pane_id, report in [(one, str(FocusReport(True))) for one in focused - self._focused_panes] + [
            (one, str(FocusReport(False))) for one in self._focused_panes - focused
        ]:
            pane = self.pymux.panes_by_id.get(pane_id)
            if pane is None:
                continue
            # A window pane has no program to read the report.
            screen, process = pane.screen, pane.process
            if isinstance(screen, WindowScreen) or isinstance(process, WindowProcess):
                continue
            if screen.reports_focus and not process.is_terminated:
                process.write_input(report)
        self._focused_panes = focused

    def sync_clients(self) -> None:
        "Give every client the focus that its state asks for."
        for client_state in list(self.pymux._client_states.values()):
            try:
                with set_app(client_state.app):
                    client_state.sync_focus()
            except Exception:
                # An application that never ran has no layout to focus.
                logger.exception("Could not sync the focus of a client.")

    def focused_pane(self):
        """
        The pane that the keyboard of the active client reaches.

        An overlay pane takes the keyboard while it is open, so it is
        not always the active pane of the arrangement.

        Never raises: this runs on the invalidate path, also for
        headless servers without a running prompt_toolkit application.
        """
        if self.pymux.overlay_pane is not None:
            return self.pymux.overlay_pane

        try:
            return self.pymux.arrangement.get_active_pane()
        except Exception:
            # `get_active_window` needs a running application.
            return None

    def focused_kitty_flags(self) -> int:
        """
        The kitty keyboard protocol flags requested by the process in
        the focused pane. (Zero when the process did not request
        anything or when there is no focused pane.)

        Never raises: this runs on the invalidate path, also for
        headless servers without a running prompt_toolkit application.
        """
        pane = self.focused_pane()
        if pane is None:
            return 0
        return pane.screen.kitty_keyboard_flags

    def pane_of(self, client_state: ClientState) -> Pane | None:
        """
        The pane that this client looks at, or `None`.

        Never raises: this runs on the invalidate path, also for
        headless servers without a running prompt_toolkit application.
        """
        if client_state.session.overlay_pane is not None:
            return client_state.session.overlay_pane

        try:
            with set_app(client_state.app):
                window = client_state.session.arrangement.get_active_window()
        except Exception:
            # `get_active_window` needs a running application.
            return None
        return window.active_pane if window is not None else None
