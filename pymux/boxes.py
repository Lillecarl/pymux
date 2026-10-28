"""
Where a box floats on the screen: the keys pop-up, the palette and the
choosers share it.
"""

from __future__ import annotations

#: How far a box floats from the top of the screen and from each
#: side. The keys pop-up chose these, and the command palette takes
#: the same ones because it is the same kind of box.
#: Lillecarl/pymux#158.
BOX_TOP = 5
BOX_SIDE = 3

#: What a box leaves under itself, at the foot of the screen.
BOX_BOTTOM = 5

#: What the palette holds above its completions: a title and the line
#: a person types on.
PALETTE_HEADER = 2
