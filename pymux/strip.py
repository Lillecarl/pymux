"""
A row of panes that can be wider than the screen.

**Two things live here, and they are the same row twice.** `Strip`
says where every pane of the row is, as a `Plan` on the plane;
`ScrollableStrip` draws the row and scrolls it. They are separate
today because prompt_toolkit still divides the row, so the plan is a
second opinion about a frame it does not draw. `PlanContainer` closes
that, and then `ScrollableStrip` is `Strip.look_at` and nothing else.
Lillecarl/pymux#217.

Every layout pymux has divides a fixed budget. `_create_split` wraps
each child in a `SizedBox` with a weight, and the tree always renders to
exactly fill the write position, so adding a pane makes every other pane
smaller. Lillecarl/pymux#198 asks for the opposite, the way niri's
scrollable tiling works: a pane keeps its width, the row grows past the
edge of the screen, and the view scrolls to the pane a person is on.

**The strip draws straight onto the real screen, at a negative offset.**
`Screen.data_buffer` is a sparse dictionary of rows of cells, and the
renderer reads only the rectangle it is going to show. So content laid
out at `xpos - horizontal_scroll`, as wide as the whole row, is clipped
on both sides for nothing: the cells to the left of the screen are
written and never read.

That is the whole trick, and it is worth saying why the obvious other
way is wrong. prompt_toolkit's `ScrollablePane` renders onto a screen of
its own and copies the visible part over. This class did too, and every
fault it had came from that: **a render hangs things on the screen it
drew on**, and each of them has to be found and carried across by hand.
Six of them were, one at a time, each after something broke -- the
cells, the zero width escapes, the mouse handlers, where each window
went, the cursor, the menus. Then pymux's own record of where it drew
each pane turned out to be a seventh, and moving between panes did
nothing at all. A title bar is a float one row above its pane, which is
outside the copy again. `ScrollablePane` still carries a TODO about a
window that is only partly visible reporting its whole width.

None of that exists here. There is one coordinate space, the real one,
and nothing to translate. A pane that has scrolled off the left is
recorded at a negative position, which is exactly what a caller wants:
`select-pane -R` steps one cell past the pane a person is on and asks
which pane is drawn there, and the answer has to be the same whether
that pane is on the screen or not.

**A screen proxy would not do it.** A write position travels down the
container tree as an argument and not through the screen, so a screen
that translated cells would still leave every caller recording
positions in a second coordinate space. One space or the world.

**The strip is given its columns, and measures them.** It used to be
given one container and read the geometry back off a screen it drew to
a side: a measuring pass, so that it could find the focused pane's
write position and scroll to it. Three faults came out of that, and one
change closes all three. Lillecarl/pymux#209.

- It measured the focused **pane**, and the thing to scroll to is the
  **column**. A column is its panes and the border it owns, so the pane
  is one cell narrower, and every answer was one cell short. That is
  the off-by-one a person saw.
- Reading a render back to decide the next render is the loop
  `ScrollablePane` was doing, one level up. The strip builds the
  columns; it can ask them.
- A column may be a stack of panes, and then the focused pane is one of
  several. Walking the containers finds the column that holds it, so a
  stack needs nothing said about it.

**The scroll moves only when it has to**, which is the property that
was missing. A column already wholly on screen leaves the view alone,
so walking right and back again is a round trip. The old policy was a
clamp recomputed from the focused pane every frame, with a peek offset
on each side, and the offset dragged the view two cells sideways every
time the focus landed on a column that was already perfectly visible.

There are no peek offsets now. A sliver of the next column said that
something was out there and nothing about what; the title bar names it
instead. Lillecarl/pymux#207.
"""

from typing import NamedTuple

from prompt_toolkit.application import get_app
from prompt_toolkit.data_structures import Size
from prompt_toolkit.key_binding import KeyBindingsBase
from prompt_toolkit.layout.containers import Container, VSplit, to_container
from prompt_toolkit.layout.dimension import Dimension as D
from prompt_toolkit.layout.mouse_handlers import MouseHandlers
from prompt_toolkit.layout.screen import Screen, WritePosition

from . import arrangement
from .plane import Plan, Rect, Slot

__all__ = ["BORDER_WIDTH", "Gaps", "ScrollableStrip", "Strip"]

#: Never lay out a strip wider than this. `ScrollablePane` caps its own
#: height for the same reason: the cost of laying the content out is
#: the whole row, and a runaway width would be felt.
MAX_AVAILABLE_WIDTH = 10_000

#: The cell a column keeps for the border on its right. It lives here
#: because a column's width is measured around it, and `layout.py`
#: draws it.
BORDER_WIDTH = 1


class Gaps(NamedTuple):
    """
    The cells a layout leaves between the things it lays out.

    Both are chrome, so a plan puts a hole there rather than giving
    the cells to a pane. `layout.py` decides the numbers, because it
    is what draws in them: two rows between stacked panes when the bar
    below a pane is drawn, and one when it is not.
    """

    between_columns: int = BORDER_WIDTH
    between_panes: int = 1


class Strip:
    """
    A row of columns, and where every pane of it is.

    This is the first layout that answers with a `Plan`, and it is the
    hardest one pymux has: the row may be wider than the view, so
    "which pane is beside this one" cannot be answered by the screen.
    Lillecarl/pymux#217, and `docs/layout-engine-plan.md`.

    **It reads the arrangement, for now.** The tree, the column widths
    and the weights still live in `arrangement.Window`, so this
    measures them rather than owning them. When `Divided` lands as
    well, the tree becomes these classes' own state and this reads
    nothing.

    **The plan predicts the frame, for now.** prompt_toolkit still
    divides the row, and this works out the same numbers a second
    time, which is exactly the duplication the work is removing:
    `PlanContainer` draws the plan next, and then the plan is the truth
    and nothing can disagree with it.
    `tests/test_the_plan_matches_the_frame.py` holds the two together
    until that lands.
    """

    def __init__(self, window: "arrangement.Window", gaps: Gaps = Gaps()) -> None:
        self.window = window
        self.gaps = gaps

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
            _lay_out(
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


def _lay_out(item, rect: Rect, gaps: Gaps, into: list) -> None:
    """
    Put every pane of this item on the plane, inside that rectangle.

    A pane is one slot. A split divides its rectangle between its
    children, leaving a gap between each pair for the chrome that goes
    there. It is the same walk `layout._create_split` does, and it will
    serve `Divided` as it stands.
    """
    if isinstance(item, arrangement.Pane):
        into.append((Slot(item), rect))
        return

    sideways = isinstance(item, arrangement.VSplit)
    gap = gaps.between_columns if sideways else gaps.between_panes
    room = (rect.width if sideways else rect.height) - gap * (len(item) - 1)
    shares = _shares(room, [item.weights[child] for child in item])

    at = rect.x if sideways else rect.y
    for child, share in zip(item, shares):
        if sideways:
            _lay_out(child, Rect(at, rect.y, share, rect.height), gaps, into)
        else:
            _lay_out(child, Rect(rect.x, at, rect.width, share), gaps, into)
        at += share + gap


def _shares(total: int, weights: list[int]) -> list[int]:
    """
    How many cells each child of a split takes.

    A weight is a share of the whole, and after one frame it is the
    size that frame drew: `_create_split` writes the real width and
    height back into the weights, so that `resize-pane +1` means one
    row. So at rest this hands each child the size it already has and
    nothing moves.

    The cells that do not divide evenly go to the children the
    division shortchanged most, and a tie goes to the one nearer the
    start.

    **Every child gets at least one cell**, because a rectangle with no
    cells is a rectangle nothing can be drawn in. A stack too tall for
    the room therefore runs past it, which the plane allows: it is
    unbounded, and a view is what is bounded.
    """
    count = len(weights)
    total = max(total, count)
    whole = sum(weights) or count

    exact = [total * weight / whole for weight in weights]
    shares = [max(1, int(share)) for share in exact]

    spare = total - sum(shares)
    if spare > 0:
        wanting = sorted(range(count), key=lambda i: (int(exact[i]) - exact[i], i))
        for i in wanting[:spare]:
            shares[i] += 1

    return shares


class ScrollableStrip(Container):
    """
    Show a window onto a row of columns that may be wider than the
    write position.

    :param columns: One container for each column, in the order they
        sit in. A column is whatever it holds: one pane, a stack of
        them, and the border it owns.
    :param max_available_width: The cap above.
    """

    def __init__(
        self,
        columns: list,
        max_available_width: int = MAX_AVAILABLE_WIDTH,
    ) -> None:
        self.columns = [to_container(column) for column in columns]
        self.content = VSplit(self.columns)
        self.max_available_width = max_available_width

        #: The first column of the strip that is on screen.
        self.horizontal_scroll = 0

    def __repr__(self) -> str:
        return "ScrollableStrip(%r)" % (self.columns,)

    def reset(self) -> None:
        self.content.reset()

    def preferred_width(self, max_available_width: int) -> D:
        """
        Whatever it is given.

        The strip scrolls, so it does not ask for the width its content
        wants. Asking for it would make every parent try to supply it,
        which is the behaviour this exists to avoid.
        """
        return D(min=1)

    def preferred_height(self, width: int, max_available_height: int) -> D:
        "The height of the content, laid out at the width of the strip."
        return self.content.preferred_height(
            self.strip_width(width), max_available_height
        )

    def width_of(self, column: Container) -> int:
        "How many cells one column takes, the border it owns included."
        return column.preferred_width(self.max_available_width).preferred

    def strip_width(self, visible_width: int = 0) -> int:
        """
        How wide the whole strip is: what its columns add up to.

        It may be narrower than the screen, and then the rest of the
        screen stays as it was. That is what niri does with one window
        at half width.
        """
        wanted = sum(self.width_of(column) for column in self.columns)
        return max(1, min(wanted, self.max_available_width))

    def write_to_screen(
        self,
        screen: Screen,
        mouse_handlers: MouseHandlers,
        write_position: WritePosition,
        parent_style: str,
        erase_bg: bool,
        z_index: int | None,
    ) -> None:
        "Scroll to the focused column, then draw the row where it goes."
        virtual_width = self.strip_width()

        self._scroll_to_the_focus(write_position.width, virtual_width)

        self.content.write_to_screen(
            screen,
            mouse_handlers,
            self._where_the_row_goes(write_position, virtual_width),
            parent_style,
            erase_bg,
            z_index,
        )

    def _where_the_row_goes(
        self, write_position: WritePosition, virtual_width: int
    ) -> WritePosition:
        """
        The whole row, placed so that the part on screen is on screen.

        The left edge goes `horizontal_scroll` cells left of the strip,
        which is off the screen when anything is scrolled past. That is
        the point: what falls outside is written into a dictionary that
        the renderer never reads.
        """
        return WritePosition(
            xpos=write_position.xpos - self.horizontal_scroll,
            ypos=write_position.ypos,
            width=virtual_width,
            height=write_position.height,
        )

    # ------------------------------------------------------------------
    # Where the focus is, and where the view goes because of it.

    def _the_focused_column(self) -> int | None:
        """
        Which column holds the focus, by its place in the row.

        `None` when nothing here has it, and then the view stays where
        it is. That is the case while a command line or a dialog holds
        the keyboard.
        """
        focused = get_app().layout.current_window

        for index, column in enumerate(self.columns):
            if _holds(column, focused):
                return index
        return None

    def _where_the_column_is(self, index: int) -> tuple[int, int]:
        """
        One column's edges, counted from the start of the strip.

        The columns before it say where it starts, which is exact
        because every column asks for a width and gets it.
        """
        start = sum(self.width_of(column) for column in self.columns[:index])
        return start, start + self.width_of(self.columns[index])

    def _scroll_to_the_focus(self, visible_width: int, virtual_width: int) -> None:
        """
        Move the view, if the focused column is not wholly inside it.

        **A column already on screen leaves the view alone.** That is
        what makes moving the focus a round trip: walk right and back,
        and the strip is where it started. The old policy re-derived
        the scroll from the focused pane on every frame, so it could
        not say "nothing to do", and a peek offset pulled the view
        sideways every time.
        """
        index = self._the_focused_column()
        if index is not None:
            start, end = self._where_the_column_is(index)

            if start < self.horizontal_scroll:
                self.horizontal_scroll = start
            elif end > self.horizontal_scroll + visible_width:
                # A column wider than the view cannot be shown whole,
                # and then its left edge wins: that is where a prompt
                # is, and where a person reading a pane starts.
                self.horizontal_scroll = max(start, end - visible_width)

        # Never past the end of the strip, whatever the focus asked
        # for. A narrowing can leave the view beyond it.
        self.horizontal_scroll = max(
            0, min(self.horizontal_scroll, max(0, virtual_width - visible_width))
        )

    def is_modal(self) -> bool:
        return self.content.is_modal()

    def get_key_bindings(self) -> KeyBindingsBase | None:
        return self.content.get_key_bindings()

    def get_children(self) -> list[Container]:
        return [self.content]


def _holds(container: Container, window) -> bool:
    "Whether this container is that window, or holds it somewhere below."
    if container is window:
        return True
    return any(_holds(child, window) for child in container.get_children())
