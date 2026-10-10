"""
The layout that divides the view between the panes.

This is what pymux does unless a person asks for something else, and
what every layout it had before the strip was. The window is a tree of
splits, each split shares its rectangle out between its children by
weight, and a gap between one child and the next is where the border
goes. Lillecarl/pymux#217.

**It is a constraint, so it holds fractions and not cells.** A weight
is a share of the whole, and `measure` derives the cells from it every
time. tmux keeps absolute cells and moves them by a delta on every
resize (`layout_resize_adjust`), so its rounding drifts; ours cannot,
because nothing but a resize command ever writes a weight. The price is
that `resize-pane +1` has to say what size it wants rather than nudge a
    number, and `layout.write_sizes_into_weights` is where that is paid.

**A divided layout fills the plane exactly**, so a view as big as the
plane sees all of it and never scrolls. That is the whole difference
from `Strip`, which gives each column a width of its own and lets the
row run past the edge. It is not a promise that the view never moves:
a client smaller than the plane has one that does, and `look_at` says
by which rule.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from prompt_toolkit.data_structures import Point, Size

from . import arrangement
from .plane import GROUND, Line, Pane, Plan, Rect, View
from .tiling import Gaps, lay_out

__all__ = ["Divided"]


@dataclass(frozen=True, eq=False)
class Divided:
    """
    An exact tiling of the view, and where every pane of it is.

    **It reads the arrangement, for now.** The tree and the weights
    still live in `arrangement.Window`, so this measures them rather
    than owning them. `Strip` says the same, and both stop when the
    tree moves in here.

    **A preset is not a mode.** `select-layout main-vertical` builds a
    tree once and is then finished, which is what tmux does as well:
    `layout-set.c` says "these are one-off and generate a layout tree",
    and nothing re-applies one when a pane opens or closes. So this
    class does not remember which preset made the tree it is
    measuring, and there is no `MainVertical` layout to write.
    Inheritance follows a rule that lasts, and an arrangement is a
    function.
    """

    window: arrangement.Window
    #: Read on every measure, for the reason `layout.layout_of` gives.
    gaps: Callable[[], Gaps] = field(default=Gaps, repr=False)

    def measure(self, available: Size) -> Plan:
        """
        Where every pane of this window is, in cells of the plane.

        The tiling starts at the origin and fills what it is given.
        Nothing here clamps: a window divided between more panes than
        it has rows runs past the bottom, because every pane keeps at
        least one row (`tiling.shares`) and the plane is unbounded.
        What a person sees is then cut by the view, which is the
        honest answer and what tmux does as well -- its own comment
        says a window may be smaller than its layout.

        Insertion order is reading order: the walk takes each split's
        children in the order they sit in, so the panes come out the
        way `Window.panes` lists them and a person reads them.
        Lillecarl/pymux#210.

        A border runs the whole way across the split that left its
        gap, so a border between two columns stops where the split
        holding them stops. A horizontal one is covered wherever the
        bars above and below a gap are drawn, and seen when they are
        off.
        """
        rects: list[tuple] = []
        lines: list[Line] = []

        lay_out(
            self.window.root,
            Rect(x=0, y=0, width=available.columns, height=available.rows),
            self.gaps(),
            rects,
            lines,
        )

        return Plan({GROUND: rects}, lines)

    def look_at(self, plan: Plan, view: View, focus: Pane | None) -> Point:
        """
        The origin, whenever the view is as big as the plane.

        A tiling is measured to fit the plane, so most of the time
        there is nothing off the edge to scroll to and this answers
        the origin. Two cases are not most of the time, and both are
        real:

        - A client smaller than the plane. `window-size largest` says
          the plane is the biggest client's, so a smaller one moves
          its view over it instead of being stuck at the top left,
          which is what tmux leaves a person with.
        - A window divided between more panes than it has rows. Every
          pane keeps a row (`tiling.shares`), so the tiling runs past
          the bottom, and the panes down there are worth reaching.

        `View.moved_onto` holds the rules, and `Strip` uses the same
        ones over a wider row.
        """
        return view.moved_onto(None if focus is None else plan.rect_of(focus), plan.bounds)
