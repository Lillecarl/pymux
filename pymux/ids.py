"""
Identity and position of everything a server numbers.

Two families. An id is an opaque token: a pane id, a window id and a
session id never do arithmetic, so they are `NewType`s and pyrefly
tells a cross-kind pass apart. An index is a position: the slot a
window holds in its session or a pane in its window, recycled on
every close, where an id never comes back. Indexes add and subtract
-- the next slot, the previous one -- so they are `int` subclasses
whose arithmetic stays typed, and `WindowIndex("3")` reads like
`int("3")`, which is what lets argparse build one straight away.

Both cost nothing at runtime: every value stays an int on the wire,
in JSON and in `-t %/@` spellings. Mint once, wrap once: counters
and the two parsing sites (`-t` spellings, client packets) construct
these, and everything else only carries them. Lillecarl/pymux#508.
"""

from typing import NewType

from typing_extensions import override

#: What `Pane.pane_id` is: server-unique, never reused, spelled `%id`.
PaneId = NewType("PaneId", int)

#: What `Window.window_id` is: server-unique, never reused, spelled `@id`.
WindowId = NewType("WindowId", int)

#: What `Session.session_id` is: per-server, counted from zero.
SessionId = NewType("SessionId", int)


class PaneIndex(int):
    """A pane's slot in its window. Recycled, 0-based, display only."""

    @override
    def __add__(self, other: int) -> "PaneIndex":
        return PaneIndex(super().__add__(other))

    @override
    def __sub__(self, other: int) -> "PaneIndex":
        return PaneIndex(super().__sub__(other))


class WindowIndex(int):
    """A window's slot in its session. Recycled, display only."""

    @override
    def __add__(self, other: int) -> "WindowIndex":
        return WindowIndex(super().__add__(other))

    @override
    def __sub__(self, other: int) -> "WindowIndex":
        return WindowIndex(super().__sub__(other))
