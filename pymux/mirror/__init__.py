"""
A server that shows the panes of another server, and the server that
lets it. Lillecarl/pyterm#761.

The other server owns each pane it lends: its screen, its program, its
history and its size. A peer holds a mirror, a pane whose screen shows
the rows and the images of the page the owner sends (`pyte.grid`). An
image crosses once, and the peer draws it as its own terminal can. The
peer sends a
person's keys, paste and mouse back, and the owner encodes them, runs
copy mode and keeps the history. A mirror is a pane for everything
else, so it can be lent on again, and servers chain.
"""

from __future__ import annotations
