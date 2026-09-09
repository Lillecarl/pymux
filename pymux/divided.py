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
number, and `layout.the_weights_of` is where that is paid.

**A divided layout fills the view exactly**, so it never scrolls and
`look_at` is the origin. That is the whole difference from `Strip`,
which gives each column a width of its own and lets the row run past
the edge.
"""

from prompt_toolkit.data_structures import Point, Size

from . import arrangement
from .plane import Line, Pane, Plan, Rect
from .tiling import Gaps, lay_out

__all__ = ["Divided"]


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

    def __init__(self, window: "arrangement.Window", gaps=Gaps()) -> None:
        self.window = window
        self._gaps = gaps

        #: The lines of the last `measure`, which `chrome` hands back.
        #: They come out of the same walk, because a gap and the line
        #: in it are the same fact and working it out twice is how the
        #: two drift apart.
        self._lines: list[Line] = []

    @property
    def gaps(self) -> Gaps:
        """
        The cells left between things, now.

        It may be given as a callable, for the reason `Strip.gaps`
        gives: the gap between two stacked panes is two rows when a bar
        is drawn under a pane and one when it is not, and an option
        turns that on while a layout that is already standing draws.
        """
        return self._gaps() if callable(self._gaps) else self._gaps

    def __repr__(self) -> str:
        return "Divided(%r)" % (self.window,)

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
        """
        rects: list[tuple] = []
        lines: list[Line] = []

        lay_out(
            self.window.root,
            Rect(x=0, y=0, width=available.columns, height=available.rows),
            self.gaps,
            rects,
            lines,
        )

        self._lines = lines
        return Plan(rects)

    def chrome(self, plan: Plan) -> list[Line]:
        """
        The lines this layout draws, in the gaps it left.

        **A pane knows nothing about borders**, so the layout that left
        the gap is what fills it. Carl: "individual panes should not be
        aware of borders ... the layout is responsible for drawing the
        borders either way."

        A line runs the whole way across the split that left the gap,
        which is what the padding of a `VSplit` did when prompt_toolkit
        divided the window. So a border between two columns stops where
        the split holding them stops, and the border of the split
        outside it carries on.

        The horizontal ones are covered wherever a pane draws a bar
        above it and the pane over it draws one below, which is what
        the second row of that gap is for. They are drawn anyway, and
        seen only when the bars are off.
        """
        return self._lines

    def look_at(
        self, plan: Plan, offset: Point, size: Size, focus: "Pane | None"
    ) -> Point:
        """
        The origin, always.

        A tiling is measured to fit the view, so there is nothing off
        the edge to scroll to. `Strip` is the layout that answers this
        question with work.
        """
        return Point(x=0, y=0)
