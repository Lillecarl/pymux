"""
A server that shows the panes of another server, and the server that
lets it. Lillecarl/pymux#759.

The other server owns each pane it lends: its screen, its program, its
history and its size. A peer holds a mirror, a pane whose screen shows
the rows of the page the owner sends (`pyte.grid`). The peer sends a
person's keys, paste and mouse back, and the owner encodes them, runs
copy mode and keeps the history. A mirror is a pane for everything
else, so it can be lent on again, and servers chain.
"""

from __future__ import annotations
