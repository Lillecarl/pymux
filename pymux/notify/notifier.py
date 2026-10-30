"""
How a finished notification reaches each person: a box on a client that
shows none of its own, and the OSC each client's terminal shows. A look
at its window takes it down again. `notifications.py` holds what a
notification is; this delivers one.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import anyio
from pyte.osc import Osc

from ..enums import Chooser, NotificationPopups, Woke
from ..ids import PaneId
from .notifications import Notification, NotificationSource, close_of, osc99_of
from .osc import build_osc, message, simple_notification

if TYPE_CHECKING:
    from ..connection.client_state import ClientState
    from ..core.main import Pymux

__all__ = ["MAX_POPUPS", "Notifier"]

#: The notifications a terminal got as the program wrote them, in the
#: forward of the pane's OSC: nothing is spelled again for it.
_WENT_AS_IT_CAME = frozenset(
    {
        (Osc.NOTIFICATION, NotificationSource.OSC_99),
        (Osc.MESSAGE, NotificationSource.ATTENTION),
    }
)

#: How many notification boxes one client stacks; a new one pushes the
#: oldest off. Lillecarl/pymux#826.
MAX_POPUPS = 5


class Notifier:
    "Delivers each notification the hub records to every client."

    def __init__(self, pymux: Pymux) -> None:
        self.pymux = pymux

    def notified(self, record: Notification) -> None:
        "Mark the window a notification came from, and tell each client. Lillecarl/pymux#767."
        self._pop_up(record)
        self._emit(record)
        found = self.pymux.place_of_pane_id(record.pane_id)
        if found is None:
            return
        session, window, _pane = found
        if session.arrangement.notify(window, self.pymux.apps):
            self.pymux.invalidate(Woke.WINDOW_MARKED)

    def _pop_up(self, record: Notification) -> None:
        """
        Show a notification as a box on each client that asks for one,
        for `notification-popup-seconds`. Not for a pane a client with
        the focus already shows, as a desktop notification is not.
        Lillecarl/pymux#826.
        """
        if record.pane_id is not None and record.pane_id in self.panes_in_sight():
            return
        shown = False
        for connection, client_state in self.pymux._client_states.items():
            if client_state.temporary:
                continue
            wanted = client_state.options.notification_popups
            if wanted is NotificationPopups.OFF or (
                wanted is NotificationPopups.AUTO
                and connection is not None
                and connection.replies.notification_osc is not None
            ):
                continue
            client_state.popups = [*client_state.popups, record][-MAX_POPUPS:]
            if self.pymux.tasks is not None:
                self.pymux.tasks.start_soon(
                    self._take_down, client_state, record, client_state.options.notification_popup_seconds
                )
            shown = True
        if shown:
            self.pymux.invalidate(Woke.NOTIFICATION_SENT)

    async def _take_down(self, client_state: ClientState, record: Notification, seconds: float) -> None:
        await anyio.sleep(seconds)
        self.drop_popup(client_state, record)

    def drop_popup(self, client_state: ClientState, record: Notification) -> None:
        if record in client_state.popups:
            client_state.popups = [one for one in client_state.popups if one is not record]
            self.pymux.invalidate(Woke.NOTIFICATION_SENT)

    def drop_seen_popups(self) -> None:
        "A box goes once its pane is seen, as a desktop notification does."
        showing = [state for state in self.pymux._client_states.values() if state.popups]
        if not showing:
            return
        seen = self.panes_in_sight()
        for client_state in showing:
            for record in client_state.popups:
                if record.pane_id in seen:
                    self.drop_popup(client_state, record)

    def _emit(self, record: Notification) -> None:
        """
        Send a finished notification to each client's terminal, spelled
        in the OSC that terminal shows, whichever OSC the program used.
        OSC 99 to an OSC 99 terminal already went as it came, chunk by
        chunk, with everything the program asked of it; and iTerm2 got
        an attention request as it came. Lillecarl/pymux#497.

        As OSC 99 it carries an identifier of pymux's own, so a click
        shows the pane. Lillecarl/pymux#779.
        """
        if record.pane_id is not None and record.pane_id in self.panes_in_sight():
            return
        spelled: dict[Osc, list[str]] = {}
        for connection, client_state in self.pymux._client_states.items():
            if connection is None or client_state.temporary:
                continue
            osc = connection.replies.notification_osc
            if osc is None or (osc, record.source) in _WENT_AS_IT_CAME:
                continue
            if osc not in spelled:
                spelled[osc] = self._spell(osc, record)
            for sequence in spelled[osc]:
                connection.forward_osc(sequence)
        if spelled:
            self.pymux.invalidate(Woke.NOTIFICATION_SENT)

    def _spell(self, osc: Osc, record: Notification) -> list[str]:
        "The sequences that say `record` in `osc`."
        if osc is Osc.NOTIFICATION:
            ours = self.pymux.notifications.own(record.pane_id)
            params = osc99_of(ours, record.title, record.body, record.urgency)
        elif osc is Osc.SIMPLE_NOTIFICATION:
            params = [simple_notification(record.title, record.body)]
        else:
            params = [message(record.title, record.body)]
        return [sequence for param in params if (sequence := build_osc(osc, param)) is not None]

    def panes_in_sight(self) -> set[PaneId]:
        """
        The panes of every window a client shows on a terminal that has the focus.

        Not while the window chooser is open: it switches to each window
        it points at, as a preview, and a window walked past is not seen.
        """
        shown: set[PaneId] = set()
        for client_state in self.pymux._client_states.values():
            if (
                client_state.terminal_focused
                and not client_state.temporary
                and client_state.chooser is not Chooser.WINDOW
            ):
                window = client_state.session.arrangement.get_active_window_for(client_state.app)
                if window is not None:
                    shown.update(pane.pane_id for pane in window.panes)
        return shown

    def close_seen_notifications(self) -> None:
        """
        Take the desktop notifications of a window off each terminal that
        can close one, once a client whose terminal has the focus shows
        that window. Lillecarl/pymux#799.
        """
        if not self.pymux.notifications.any_shown:
            return
        closed = self.pymux.notifications.seen(self.panes_in_sight())
        if not closed:
            return
        for connection in self.pymux._client_states:
            if connection is not None and connection.replies.notification_closes:
                for ours in closed:
                    connection.forward_osc(build_osc(Osc.NOTIFICATION, close_of(ours)))
