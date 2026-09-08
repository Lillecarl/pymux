"""
A row of panes that can be wider than the screen.

Every layout pymux has divides a fixed budget. `_create_split` wraps
each child in a `SizedBox` with a weight, and the tree always renders to
exactly fill the write position, so adding a pane makes every other pane
smaller. Lillecarl/pymux#198 asks for the opposite, the way niri's
scrollable tiling works: a pane keeps its width, the row grows past the
edge of the screen, and the view scrolls to the pane a person is on.

**This is prompt_toolkit's `ScrollablePane` turned on its side.** That
class renders its content onto a screen taller than the viewport and
copies a `vertical_scroll`-offset slice of it onto the real one. Its own
comment says it scrolls vertically and nothing else
(`prompt_toolkit/layout/scrollable_pane.py:90`), and a strip scrolls
horizontally, so it cannot be used as it stands.

It is written here rather than as a patch to that class on purpose.
`CLAUDE.md` asks for the interaction first and an upstreamable patch
second, and what pymux needs of a horizontal scroll is not settled until
pymux uses one. When it is, this belongs upstream, as a counterpart of
`ScrollablePane` or as a generalisation of it. It is written to look
like that class so the move is mechanical rather than a rewrite.
"""

from typing import Callable

from prompt_toolkit.application import get_app
from prompt_toolkit.data_structures import Point
from prompt_toolkit.filters import FilterOrBool, to_filter
from prompt_toolkit.key_binding import KeyBindingsBase
from prompt_toolkit.layout.containers import Container, ScrollOffsets
from prompt_toolkit.layout.dimension import Dimension as D
from prompt_toolkit.layout.mouse_handlers import MouseHandler, MouseHandlers
from prompt_toolkit.layout.screen import Char, Screen, WritePosition
from prompt_toolkit.mouse_events import MouseEvent

__all__ = ["ScrollableStrip"]

#: Never lay out a strip wider than this. `ScrollablePane` caps its own
#: height for the same reason: the cost of the off-screen render is the
#: whole virtual area, and a runaway width would be felt.
MAX_AVAILABLE_WIDTH = 10_000


class ScrollableStrip(Container):
    """
    Show a window onto content that may be wider than the write position.

    :param content: The content container. Its preferred width is the
        width of the strip, and it is asked for that width rather than
        for the width of the screen.
    :param scroll_offsets: How much of the strip to keep beyond the
        focused pane, so that the next one peeks at the edge. Only
        `left` and `right` are read.
    :param keep_focused_window_visible: Scroll so the focused window is
        on screen, or as much of it as fits.
    :param max_available_width: The cap above.
    :param top_margin: How many rows at the top of the strip belong to
        what hangs above a pane rather than to the panes themselves.

        **A strip may not draw outside itself.** A pane's title bar is
        a `Float` at `top=-1`, one row above the pane. Every other
        layout lets it land on a row that something else reserved,
        because a float is drawn onto the real screen last of all. A
        strip draws onto a screen of its own and copies the result, so
        by the time it copies, that row has not been drawn yet -- and
        whatever owns it draws over the copy afterwards. A picture
        found this; no test that reads a container on its own can.
        Lillecarl/pymux#161.

        So the row belongs to the strip. The content is laid out this
        many rows down on the screen of its own, which puts a title bar
        inside the strip's own area, and the caller reserves nothing.
    """

    def __init__(
        self,
        content: Container,
        scroll_offsets: ScrollOffsets | None = None,
        keep_focused_window_visible: FilterOrBool = True,
        max_available_width: int = MAX_AVAILABLE_WIDTH,
        top_margin: Callable[[], int] | int = 0,
    ) -> None:
        self.content = content
        self.scroll_offsets = scroll_offsets or ScrollOffsets(left=0, right=0)
        self.keep_focused_window_visible = to_filter(keep_focused_window_visible)
        self.max_available_width = max_available_width
        self.top_margin = top_margin

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

    def margin(self) -> int:
        "How many rows at the top belong to what hangs above a pane."
        if callable(self.top_margin):
            return max(0, self.top_margin())
        return max(0, self.top_margin)

    def strip_width(self, visible_width: int) -> int:
        """
        How wide the whole strip is: what its columns add up to.

        It may be narrower than the screen, and then the rest of the
        screen stays blank. That is what niri does with one window at
        half width, and it is why this is the columns' own total rather
        than the width of the window: a content laid out wider than its
        columns fills the difference with whatever it pads with, which
        drew a border down the middle of an empty half.
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
        """
        Render the strip, and copy the part of it that is on screen.

        The content is drawn onto a screen of its own, as wide as the
        whole strip. Only then is it known where the focused pane sits,
        which is what says where to scroll to.
        """
        virtual_width = self.strip_width(write_position.width)
        margin = self.margin()

        temp_screen = Screen(default_char=Char(char=" ", style=parent_style))
        temp_screen.show_cursor = screen.show_cursor
        temp_mouse_handlers = MouseHandlers()

        self.content.write_to_screen(
            temp_screen,
            temp_mouse_handlers,
            WritePosition(
                xpos=0,
                ypos=margin,
                width=virtual_width,
                height=max(0, write_position.height - margin),
            ),
            parent_style,
            erase_bg,
            z_index,
        )
        temp_screen.draw_all_floats()

        focused_window = get_app().layout.current_window
        try:
            visible_win_write_pos = temp_screen.visible_windows_to_write_positions[
                focused_window
            ]
        except KeyError:
            pass  # Nothing here has the focus. Leave the scroll alone.
        else:
            self._make_window_visible(
                write_position.width, virtual_width, visible_win_write_pos
            )

        # The scroll can be left past the end by a pane that closed or a
        # window that grew, and neither of those runs through
        # `_make_window_visible`.
        self.horizontal_scroll = max(
            0, min(self.horizontal_scroll, virtual_width - write_position.width)
        )

        self._copy_over_screen(screen, temp_screen, write_position)
        self._copy_over_mouse_handlers(
            mouse_handlers, temp_mouse_handlers, write_position
        )
        self._copy_over_write_positions(screen, temp_screen, write_position)

        screen.width = max(screen.width, write_position.xpos + write_position.width)
        screen.height = max(screen.height, write_position.ypos + write_position.height)

        if temp_screen.show_cursor:
            screen.show_cursor = True

        for window, point in temp_screen.cursor_positions.items():
            if (
                0 <= point.y < write_position.height
                and self.horizontal_scroll
                <= point.x
                < write_position.width + self.horizontal_scroll
            ):
                screen.cursor_positions[window] = Point(
                    x=point.x + write_position.xpos - self.horizontal_scroll,
                    y=point.y + write_position.ypos,
                )

        for window, point in temp_screen.menu_positions.items():
            screen.menu_positions[window] = self._clip_point_to_visible_area(
                Point(
                    x=point.x + write_position.xpos - self.horizontal_scroll,
                    y=point.y + write_position.ypos,
                ),
                write_position,
            )

    def _clip_point_to_visible_area(
        self, point: Point, write_position: WritePosition
    ) -> Point:
        "Keep a reported position inside the part that is on screen."
        if point.x < write_position.xpos:
            point = point._replace(x=write_position.xpos)
        if point.y < write_position.ypos:
            point = point._replace(y=write_position.ypos)
        if point.x >= write_position.xpos + write_position.width:
            point = point._replace(x=write_position.xpos + write_position.width - 1)
        if point.y >= write_position.ypos + write_position.height:
            point = point._replace(y=write_position.ypos + write_position.height - 1)
        return point

    def _copy_over_screen(
        self, screen: Screen, temp_screen: Screen, write_position: WritePosition
    ) -> None:
        """
        The cells that are on screen, and the escapes that ride with them.

        Row for row: the content was laid out `margin` rows down on the
        screen of its own, and this strip's own write position starts
        at the same place. `top_margin` says why.
        """
        xpos = write_position.xpos
        ypos = write_position.ypos

        for y in range(write_position.height):
            temp_row = temp_screen.data_buffer[y]
            row = screen.data_buffer[y + ypos]
            temp_zero_width_escapes = temp_screen.zero_width_escapes[y]
            zero_width_escapes = screen.zero_width_escapes[y + ypos]

            for x in range(write_position.width):
                row[x + xpos] = temp_row[x + self.horizontal_scroll]

                if x + self.horizontal_scroll in temp_zero_width_escapes:
                    zero_width_escapes[x + xpos] = temp_zero_width_escapes[
                        x + self.horizontal_scroll
                    ]

    def _copy_over_mouse_handlers(
        self,
        mouse_handlers: MouseHandlers,
        temp_mouse_handlers: MouseHandlers,
        write_position: WritePosition,
    ) -> None:
        "The handlers of the cells that are on screen, with the click moved back."
        xpos = write_position.xpos
        ypos = write_position.ypos

        # One wrapper per handler. The same handler is registered for
        # every cell of a pane, and a pane is thousands of cells.
        wrappers: dict[MouseHandler, MouseHandler] = {}

        def wrap(handler: MouseHandler) -> MouseHandler:
            if handler not in wrappers:

                def moved(event: MouseEvent) -> None:
                    handler(
                        MouseEvent(
                            position=Point(
                                x=event.position.x - xpos + self.horizontal_scroll,
                                y=event.position.y - ypos,
                            ),
                            event_type=event.event_type,
                            button=event.button,
                            modifiers=event.modifiers,
                        )
                    )

                wrappers[handler] = moved
            return wrappers[handler]

        handlers = mouse_handlers.mouse_handlers
        temp_handlers = temp_mouse_handlers.mouse_handlers

        for y in range(write_position.height):
            if y in temp_handlers:
                temp_row = temp_handlers[y]
                row = handlers[y + ypos]
                for x in range(write_position.width):
                    if x + self.horizontal_scroll in temp_row:
                        row[x + xpos] = wrap(temp_row[x + self.horizontal_scroll])

    def _copy_over_write_positions(
        self, screen: Screen, temp_screen: Screen, write_position: WritePosition
    ) -> None:
        """
        Where each window ended up on the real screen.

        A window that scrolled off keeps its width, so what is reported
        can start left of the strip. `ScrollablePane` has the same hole
        and says so: truncating a partly visible one matters once these
        nest, and neither of them nests yet.
        """
        xpos = write_position.xpos
        ypos = write_position.ypos

        for win, pos in temp_screen.visible_windows_to_write_positions.items():
            screen.visible_windows_to_write_positions[win] = WritePosition(
                xpos=pos.xpos + xpos - self.horizontal_scroll,
                ypos=pos.ypos + ypos,
                width=pos.width,
                height=pos.height,
            )

    def _make_window_visible(
        self,
        visible_width: int,
        virtual_width: int,
        visible_win_write_pos: WritePosition,
    ) -> None:
        """
        Scroll so that the focused window is on screen.

        A window wider than the screen cannot be shown whole, so its
        left edge wins: that is where a prompt is, and where a person
        reading a pane starts.
        """
        min_scroll = 0
        max_scroll = virtual_width - visible_width

        if self.keep_focused_window_visible():
            offsets = self.scroll_offsets

            if visible_win_write_pos.width <= visible_width:
                on_the_right = (
                    visible_win_write_pos.xpos
                    + visible_win_write_pos.width
                    - visible_width
                )
                on_the_left = visible_win_write_pos.xpos

                # Room for the next pane to peek, but never so much that
                # the focused one is pushed off itself.
                on_the_right = min(
                    on_the_right + offsets.right,
                    visible_win_write_pos.xpos,
                )
                on_the_left = max(on_the_left - offsets.left, 0)
            else:
                on_the_right = visible_win_write_pos.xpos
                on_the_left = visible_win_write_pos.xpos

            min_scroll = max(min_scroll, on_the_right)
            max_scroll = min(max_scroll, on_the_left)

        # A window wider than the strip can leave these crossed.
        if min_scroll > max_scroll:
            max_scroll = min_scroll

        self.horizontal_scroll = max(
            min_scroll, min(max_scroll, self.horizontal_scroll)
        )

    def is_modal(self) -> bool:
        return self.content.is_modal()

    def get_key_bindings(self) -> KeyBindingsBase | None:
        return self.content.get_key_bindings()

    def get_children(self) -> list[Container]:
        return [self.content]
