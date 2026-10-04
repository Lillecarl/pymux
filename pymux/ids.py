"""
Identity and position of everything a server numbers.

A pane id, a window id and a session id are three kinds of number that
all look like `int`, and a pane index is a fourth: the slot a pane
holds in its window, recycled on every close, where an id never comes
back. `NewType` names the kinds so pyrefly tells a mixup apart, and
costs nothing at runtime -- every value stays a plain int on the
wire, in JSON and in `-t %/@` spellings.

Mint once, wrap once: counters and the two parsing sites (`-t`
spellings, client packets) construct these, and everything else only
carries them. Lillecarl/pymux#508.
"""

from typing import NewType

#: What `Pane.pane_id` is: server-unique, never reused, spelled `%id`.
PaneId = NewType("PaneId", int)

#: What `Window.window_id` is: server-unique, never reused, spelled `@id`.
WindowId = NewType("WindowId", int)

#: What `Session.session_id` is: per-server, counted from zero.
SessionId = NewType("SessionId", int)

#: A pane's slot in its window. Recycled, 0-based, display only.
PaneIndex = NewType("PaneIndex", int)

#: A window's slot in its session. Recycled, display only.
WindowIndex = NewType("WindowIndex", int)
