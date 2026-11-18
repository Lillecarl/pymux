"""
What a bell, activity or silence in a window does for the people at a
session's clients: tmux's `alerts_set_message`. `*-action` says which
clients, `visual-*` whether each one's terminal rings, shows a message,
or both. Lillecarl/pyterm#1035.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..enums import Alert, AlertAction, Visual

if TYPE_CHECKING:
    from ..connection.client_state import ClientState
    from .session import Session
    from .window_tree import Window

__all__ = ["alert"]


def alert(clients: list[ClientState], session: Session, window: Window, kind: Alert) -> bool:
    "Ring and tell the clients of `session` that `kind` covers. True when a message went up."
    options = session.options
    match kind:
        case Alert.BELL:
            # tmux's alerts_check_bell: monitor-bell off rings nobody.
            if not window.options.monitor_bell:
                return False
            action, visual = options.bell_action, options.visual_bell
        case Alert.ACTIVITY:
            action, visual = options.activity_action, options.visual_activity
        case Alert.SILENCE:
            action, visual = options.silence_action, options.visual_silence
    told = False
    for client in clients:
        if client.session is not session:
            continue
        current = session.arrangement.get_active_window_for(client.app) is window
        if not _applies(action, current):
            continue
        if visual is not Visual.ON:
            client.app.output.bell()
        if visual is not Visual.OFF:
            where = "current window" if current else "window %s" % window.index
            client.message = "%s in %s" % (kind, where)
            told = True
    return told


def _applies(action: AlertAction, current: bool) -> bool:
    match action:
        case AlertAction.ANY:
            return True
        case AlertAction.NONE:
            return False
        case AlertAction.CURRENT:
            return current
        case AlertAction.OTHER:
            return not current
