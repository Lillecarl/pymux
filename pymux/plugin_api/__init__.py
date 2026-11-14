"""
Event plugins: a class a plugin implements, the events it hears, and
the host it reaches the server through. `plugin.py` says how a module
names its plugin. Lillecarl/pyterm#982.
"""

from __future__ import annotations

from ..mirror.plugin_messages import PeerMessage, PeerUnanswered
from .events import (
    EVENTS,
    BellRang,
    CommandFinished,
    Event,
    Notified,
    PaneEnded,
    ProgramReported,
    WindowActive,
    WindowSilent,
)
from .plugin import Host, NoSocket, Plugin

__all__ = [
    "EVENTS",
    "BellRang",
    "CommandFinished",
    "Event",
    "Host",
    "NoSocket",
    "Notified",
    "PaneEnded",
    "PeerMessage",
    "PeerUnanswered",
    "Plugin",
    "ProgramReported",
    "WindowActive",
    "WindowSilent",
]
