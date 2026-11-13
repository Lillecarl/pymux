"""
What a plugin hears: one frozen record per event, with its facts taken
when it happened.

A plugin hears an event later, on its own task, and by then the pane
can be gone: `take_ended_pane` runs before the end is announced. So an
event holds the facts, and never a live object to look them up on.
Lillecarl/pyterm#982.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from pyte.program_status import Report

from ..ids import PaneId, WindowId
from ..notify.notifications import Notification

__all__ = [
    "EVENTS",
    "BellRang",
    "CommandFinished",
    "Event",
    "Notified",
    "PaneEnded",
    "ProgramReported",
    "WindowActive",
    "WindowSilent",
]


@dataclass(frozen=True, slots=True)
class Event:
    #: The `Plugin` method that hears this event.
    HANDLER: ClassVar[str]


@dataclass(frozen=True, slots=True)
class Notified(Event):
    "The hub recorded a notification, from a pane or from `pymux notify`."

    HANDLER: ClassVar[str] = "on_notified"
    notification: Notification


@dataclass(frozen=True, slots=True)
class ProgramReported(Event):
    "A program said what it is doing (OSC 7501): working, blocked, done."

    HANDLER: ClassVar[str] = "on_program_reported"
    pane_id: PaneId
    report: Report


@dataclass(frozen=True, slots=True)
class CommandFinished(Event):
    "A shell said its command ended (OSC 133 D). `exit_status` is None for an abort."

    HANDLER: ClassVar[str] = "on_command_finished"
    pane_id: PaneId
    exit_status: int | None


@dataclass(frozen=True, slots=True)
class BellRang(Event):
    HANDLER: ClassVar[str] = "on_bell"
    pane_id: PaneId


@dataclass(frozen=True, slots=True)
class PaneEnded(Event):
    "The program of a pane ended, or the Wayland window of a window pane closed."

    HANDLER: ClassVar[str] = "on_pane_ended"
    pane_id: PaneId


@dataclass(frozen=True, slots=True)
class WindowActive(Event):
    "A window nobody looks at showed something new: `monitor-activity` raised its mark."

    HANDLER: ClassVar[str] = "on_window_active"
    window_id: WindowId


@dataclass(frozen=True, slots=True)
class WindowSilent(Event):
    "A window nobody looks at went quiet for `monitor-silence` seconds."

    HANDLER: ClassVar[str] = "on_window_silent"
    window_id: WindowId


EVENTS: tuple[type[Event], ...] = (
    Notified,
    ProgramReported,
    CommandFinished,
    BellRang,
    PaneEnded,
    WindowActive,
    WindowSilent,
)
