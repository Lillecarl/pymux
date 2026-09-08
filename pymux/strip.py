"""
A row of panes that can be wider than the screen.

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
nothing at all, because that record is kept on the screen too and the
screen it was kept on was thrown away. A title bar is a float one row
above its pane, which is outside the copy again. `ScrollablePane` still
carries a TODO about a window that is only partly visible reporting its
whole width.

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

The cost, and it is real: the content is laid out twice. The scroll
follows the focused pane, and where that pane is can only be known by
laying the row out. So a measuring pass draws onto a screen that is
thrown away, purely to read positions off it, and then the row is drawn
once for real. It happens on a strip window and nowhere else.

The renderer clips what it reads to the size of the output, in both
directions, so a cell off the screen costs the write and nothing more.
It does compare a whole row when it looks for what changed, so a column
that is off the screen and busy marks its rows as changed. The inner
loop then finds every visible cell equal and emits nothing.
"""

from prompt_toolkit.application import get_app
from prompt_toolkit.filters import FilterOrBool, to_filter
from prompt_toolkit.key_binding import KeyBindingsBase
from prompt_toolkit.layout.containers import Container, ScrollOffsets
from prompt_toolkit.layout.dimension import Dimension as D
from prompt_toolkit.layout.mouse_handlers import MouseHandlers
from prompt_toolkit.layout.screen import Char, Screen, WritePosition

__all__ = ["ScrollableStrip"]

#: Never lay out a strip wider than this. `ScrollablePane` caps its own
#: height for the same reason: the cost of laying the content out is
#: the whole row, and a runaway width would be felt.
MAX_AVAILABLE_WIDTH = 10_000


class ScrollableStrip(Container):
    """
    Show a window onto content that may be wider than the write position.

    :param content: The content container. Its preferred width is the
        width of the strip, and it is laid out at that width rather
        than at the width of the screen.
    :param scroll_offsets: How much of the strip to keep beyond the
        focused pane, so that the next one peeks at the edge. Only
        `left` and `right` are read.
    :param keep_focused_window_visible: Scroll so the focused window is
        on screen, or as much of it as fits.
    :param max_available_width: The cap above.
    """

    def __init__(
        self,
        content: Container,
        scroll_offsets: ScrollOffsets | None = None,
        keep_focused_window_visible: FilterOrBool = True,
        max_available_width: int = MAX_AVAILABLE_WIDTH,
    ) -> None:
        self.content = content
        self.scroll_offsets = scroll_offsets or ScrollOffsets(left=0, right=0)
        self.keep_focused_window_visible = to_filter(keep_focused_window_visible)
        self.max_available_width = max_available_width

        #: The first column of the strip that is on screen.
        self.horizontal_scroll = 0

    def __repr__(self) -> str:
        return "ScrollableStrip(%r)" % (self.content,)

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

    def strip_width(self, visible_width: int) -> int:
        """
        How wide the whole strip is: what its columns add up to.

        It may be narrower than the screen, and then the rest of the
        screen stays as it was. That is what niri does with one window
        at half width.
        """
        wanted = self.content.preferred_width(self.max_available_width).preferred
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
        "Scroll to the focused pane, then draw the row where it belongs."
        virtual_width = self.strip_width(write_position.width)

        self._scroll_to_the_focus(write_position, virtual_width, parent_style)

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

    def _scroll_to_the_focus(
        self, write_position: WritePosition, virtual_width: int, parent_style: str
    ) -> None:
        """
        Move the scroll so that the focused pane is on screen.

        Where a pane sits is only known once the row has been laid out,
        and the row cannot be laid out until the scroll is known. So
        this lays it out on a screen that is thrown away, reads the
        position off that, and leaves the real drawing to the caller.

        Nothing of this pass is kept. It draws cells nobody reads and
        hangs its bookkeeping on a screen nobody keeps, which is
        exactly what makes it safe to do twice.
        """
        if not self.keep_focused_window_visible():
            return

        focused = get_app().layout.current_window

        measuring = Screen(default_char=Char(char=" ", style=parent_style))
        self.content.write_to_screen(
            measuring,
            MouseHandlers(),
            WritePosition(
                xpos=0, ypos=0, width=virtual_width, height=write_position.height
            ),
            parent_style,
            False,
            None,
        )

        try:
            where = measuring.visible_windows_to_write_positions[focused]
        except KeyError:
            return  # Nothing here has the focus. Leave the scroll alone.

        self._make_window_visible(
            write_position.width, virtual_width, where.xpos, where.width
        )

    def _make_window_visible(
        self, visible_width: int, virtual_width: int, xpos: int, width: int
    ) -> None:
        """
        Choose the scroll that shows the window at `xpos`, `width`.

        The position is the window's place in the row, counted from the
        start of the row.

        A window wider than the screen cannot be shown whole, so its
        left edge wins: that is where a prompt is, and where a person
        reading a pane starts.
        """
        min_scroll = 0
        max_scroll = max(0, virtual_width - visible_width)

        offsets = self.scroll_offsets

        if width <= visible_width:
            # Room for the next pane to peek, but never so much that
            # the focused one is pushed off itself.
            on_the_right = min(xpos + width - visible_width + offsets.right, xpos)
            on_the_left = max(xpos - offsets.left, 0)
        else:
            on_the_right = xpos
            on_the_left = xpos

        min_scroll = max(min_scroll, on_the_right)
        max_scroll = min(max_scroll, on_the_left)

        # Asking to peek past the last column crosses these. The end of
        # the strip wins: there is nothing beyond it to show.
        if min_scroll > max_scroll:
            min_scroll = max_scroll

        self.horizontal_scroll = max(
            min_scroll, min(max_scroll, self.horizontal_scroll)
        )

    def is_modal(self) -> bool:
        return self.content.is_modal()

    def get_key_bindings(self) -> KeyBindingsBase | None:
        return self.content.get_key_bindings()

    def get_children(self) -> list[Container]:
        return [self.content]
