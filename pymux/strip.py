"""
A row of panes that can be wider than the screen.

Every other layout pymux has divides a fixed budget, so adding a pane
makes every other pane smaller. Lillecarl/pymux#198 asks for the
opposite, the way niri's scrollable tiling works: a pane keeps its
width, the row grows past the edge of the screen, and the view scrolls
to the pane a person is on.

**This file says where the panes are, and nothing draws here.**
`Strip.measure` hands over a plan, `Strip.chrome` says what fills the
gaps it left, and `Strip.look_at` says where the view goes.
`PlanContainer` draws all three. Lillecarl/pymux#217.

**The row is drawn straight onto the real screen, at a negative
offset.** `Screen.data_buffer` is a sparse dictionary of rows of cells,
and the renderer reads only the rectangle it is going to show. So a
pane laid out to the left of the screen is clipped for nothing: its
cells are written and never read.

That is the whole trick, and it is worth saying why the obvious other
way is wrong. prompt_toolkit's `ScrollablePane` renders onto a screen
of its own and copies the visible part over. The container that drew
this row did too, and every fault it had came from that: **a render
hangs things on the screen it drew on**, and each of them has to be
found and carried across by hand. Six of them were, one at a time, each
after something broke -- the cells, the zero width escapes, the mouse
handlers, where each window went, the cursor, the menus. Then pymux's
own record of where it drew each pane turned out to be a seventh, and
moving between panes did nothing at all. A title bar is a float one row
above its pane, which is outside the copy again.

None of that exists here. There is one coordinate space, the real one,
and nothing to translate. A pane that has scrolled off the left is
recorded at a negative position, which is exactly what a caller wants:
the pane to the right of the one a person is on is the same pane
whether it is on the screen or not.

**The view moves only when it has to**, which is the property that was
missing for a long time. A column already wholly on screen leaves the
view alone, so walking right and back again is a round trip. The policy
before it was a clamp recomputed from the focused pane every frame,
with a peek offset on each side, and the offset dragged the view two
cells sideways every time the focus landed on a column that was already
perfectly visible. Lillecarl/pymux#209.

There are no peek offsets now. A sliver of the next column said that
something was out there and nothing about what; the title bar names it
instead. Lillecarl/pymux#207.
"""

from prompt_toolkit.data_structures import Point, Size

from . import arrangement
from .plane import Line, Pane, Plan, Rect, Side, Slot
from .tiling import BORDER_HORIZONTAL, BORDER_VERTICAL, Gaps, lay_out

__all__ = ["Strip"]


class Strip:
    """
    A row of columns, and where every pane of it is.

    This is the first layout that answers with a `Plan`, and it is the
    hardest one pymux has: the row may be wider than the view, so
    "which pane is beside this one" cannot be answered by the screen.
    Lillecarl/pymux#217, and `docs/layout-engine-plan.md`.

    **It reads the arrangement, for now.** The tree, the column widths
    and the weights still live in `arrangement.Window`, so this
    measures them rather than owning them. `Divided` reads the same
    tree, and the plan file says the tree moves in here later.

    **The plan is the truth.** `PlanContainer` draws it, so nothing
    works the numbers out a second time and nothing can disagree with
    it. `tests/test_the_plan_and_the_frame.py` holds what was drawn
    against what was measured.
    """

    def __init__(self, window: "arrangement.Window", gaps=Gaps()) -> None:
        self.window = window
        self._gaps = gaps

    @property
    def gaps(self) -> Gaps:
        """
        The cells left between things, now.

        It may be given as a callable, because the gap between two
        stacked panes is two rows when a bar is drawn under a pane and
        one when it is not, and an option turns that on while a layout
        that was already built is still standing.
        """
        return self._gaps() if callable(self._gaps) else self._gaps

    def __repr__(self) -> str:
        return "Strip(%r)" % (self.window,)

    def content_width(self, column, columns: int) -> int:
        """
        How many cells one column's content takes.

        The fraction is of the window, and the border the column owns
        comes out of that share, so a column takes the same room
        whatever the other columns do. `layout._create_strip` measures
        it the same way, and Lillecarl/pymux#206 says why the border is
        inside the share.
        """
        share = round(self.window.column_width(column) * columns)
        return max(1, share - self.gaps.between_columns)

    def measure(self, available: Size) -> Plan:
        """
        Where every pane of this window is, in cells of the plane.

        The row starts at the origin and runs right, however far that
        is: the plane is unbounded, so a strip wider than the view is
        not a special case here. A view is what is bounded, and that
        comes later.
        """
        rects: list[tuple[Slot, Rect]] = []
        x = 0

        for column in self.window.root:
            width = self.content_width(column, available.columns)
            lay_out(
                column,
                Rect(x=x, y=0, width=width, height=available.rows),
                self.gaps,
                rects,
            )
            x += width + self.gaps.between_columns

        # Insertion order is reading order here, because the columns go
        # on left to right and a column's panes go on top to bottom.
        # Decision 9: a strip numbers its panes the way a person reads
        # them. Lillecarl/pymux#210.
        return Plan(rects)

    def chrome(self, plan: Plan) -> list[Line]:
        """
        The lines this strip draws, in the gaps it left.

        **A pane knows nothing about borders**, so the layout that left
        the gap is what fills it. A vertical line runs down the right
        of every column, the one the column owns and paid for out of
        its own share (Lillecarl/pymux#206), and a horizontal one runs
        across every gap inside a column.

        The horizontal ones are covered wherever a pane draws a bar
        above it and the pane over it draws one below, which is what
        the second row of the gap is for. They are drawn anyway, and
        seen only when the bars are off.
        """
        lines = []
        box = plan.plane

        # One line down the right of each column, the whole height of
        # the row. A column is a stack of slots that share an edge, so
        # the columns are the edges the slots share.
        for right in dict.fromkeys(rect.right for rect in plan.rects.values()):
            lines.append(
                Line(
                    Rect(
                        x=right,
                        y=box.y,
                        width=self.gaps.between_columns,
                        height=box.height,
                    ),
                    BORDER_VERTICAL,
                )
            )

        for slot, rect in plan.rects.items():
            below = plan.neighbour(slot, Side.BELOW)
            if below is None:
                continue

            lines.append(
                Line(
                    Rect(
                        x=rect.x,
                        y=rect.bottom,
                        width=rect.width,
                        height=plan.rects[below].y - rect.bottom,
                    ),
                    BORDER_HORIZONTAL,
                )
            )

        return lines

    def look_at(
        self, plan: Plan, offset: Point, size: Size, focus: "Pane | None"
    ) -> Point:
        """
        Where the view goes: the focused column's left edge at the
        left of the view, and never past the end of the row.

        **A column already wholly on screen leaves the view alone.**
        That is what makes moving the focus a round trip: walk right
        and back, and the strip is where it started.
        Lillecarl/pymux#207.

        A column is its panes and the border it owns, so the column
        ends one cell past the pane's rectangle. The view never passes
        the end of the row either, whatever the focus asks for: a
        column that closes can leave it out there.

        **A column wider than the view shows its left edge**, and the
        right of it is cut. Carl: "left should generally be preferred
        for terminals since that's where ~100% of applications begin
        writing text, it's even likely that a missing right column
        doesn't miss anything." Lillecarl/pymux#218. Nothing on the
        screen says that the column is cut, which is
        Lillecarl/pymux#222.
        """
        x = offset.x

        if focus is not None:
            rect = plan.rect_of(focus)
            start, end = rect.x, rect.right + self.gaps.between_columns

            if start < x or end > x + size.columns:
                # The column's left edge, at the left of the view.
                #
                # That is what this did already for every column that
                # fits, because `max(start, end - size)` is `start`
                # whenever the column is narrower than the view. The
                # only column it treated differently was one too wide
                # to show whole, and that one is the case the rule is
                # about. Lillecarl/pymux#218.
                x = start

        row = plan.plane.width + self.gaps.between_columns
        return Point(x=max(0, min(x, max(0, row - size.columns))), y=0)
