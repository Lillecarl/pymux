"""
One pane as the layout draws it: the terminal widget with its title bar,
its clock or number, the mark of the focused pane and the shade at a cut
edge, and where each pane landed this frame.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import partial
from typing import TYPE_CHECKING, override

from prompt_toolkit.application import get_app
from prompt_toolkit.filters import (
    Condition,
    has_focus,
)
from prompt_toolkit.formatted_text import StyleAndTextTuples
from prompt_toolkit.layout.containers import (
    AnyContainer,
    ConditionalContainer,
    Container,
    Float,
    FloatContainer,
    HSplit,
    Window,
    WritePosition,
    to_container,
)
from prompt_toolkit.layout.dimension import Dimension as D
from prompt_toolkit.layout.mouse_handlers import MouseHandlers
from prompt_toolkit.layout.screen import Char, Screen

from pymux import arrangement

from .actions import Kind, Origin, note, started_by
from .cut_shade import Hidden, lifts, shade
from .enums import Woke
from .format import format_pymux_string
from .geometry import (
    cut_backdrop,
    cut_shade_of,
    hidden_of_pane,
    pane_beside,
)
from .plane import Side
from .titlebar import PaneTitleBar
from .widgets import BigClock, PaneNumber
from .z_index import Z_INDEX

if TYPE_CHECKING:
    from pymux.main import Pymux

__all__ = [
    "ABOVE_MARK",
    "BELOW_MARK",
    "LEFT_MARK",
    "PANE_TITLE_FORMAT",
    "RIGHT_MARK",
    "TracePaneWritePosition",
    "create_container_for_process",
    "pane_containers",
    "pane_write_positions",
]


#: The text on the titlebar of a pane. XXX: Make configurable.
PANE_TITLE_FORMAT = " #T "

#: How a title bar marks the name of the pane on each side of its own.
#: The mark points outward, at the edge it sits on, so the name reads
#: as "that way". Lillecarl/pymux#207.
#:
#: They are one cell wide. Every glyph of this kind is "ambiguous"
#: width in Unicode, which a terminal in a CJK locale may draw as two,
#: and then a bar is a cell wider than the arithmetic that laid it out.
#: The ellipsis of a cut name is the same kind of character, so this
#: adds no risk that the bar did not already take.
LEFT_MARK = "◂"
RIGHT_MARK = "▸"

#: And the same two, turned. They name the pane above and the pane
#: below on the bar under a pane. Lillecarl/pymux#211.
ABOVE_MARK = "▴"
BELOW_MARK = "▾"

#: Where the panes of one render were drawn. It hangs on the screen of
#: that render, so two clients never read each other's.
_PANE_POSITIONS = "pymux_pane_write_positions"


def pane_write_positions(screen: Screen) -> dict[arrangement.Pane, WritePosition]:
    "Where each pane was drawn on this screen."
    positions: dict[arrangement.Pane, WritePosition] | None = getattr(screen, _PANE_POSITIONS, None)
    if positions is None:
        positions = {}
        setattr(screen, _PANE_POSITIONS, positions)
    return positions


#: The container each pane was drawn from, on that screen.
_PANE_CONTAINERS = "pymux_pane_containers"


def pane_containers(screen: Screen) -> dict[arrangement.Pane, Container]:
    "The container each pane was drawn from on this screen."
    containers: dict[arrangement.Pane, Container] | None = getattr(screen, _PANE_CONTAINERS, None)
    if containers is None:
        containers = {}
        setattr(screen, _PANE_CONTAINERS, containers)
    return containers


def _short_name_of(pymux: Pymux, pane: arrangement.Pane) -> str:
    """
    A pane in a few cells, for the title bar of the pane beside it.

    The program running in it, which is what `Pane.name` is, and a
    person chose it if they renamed the pane. Its title is the fallback,
    because a pane whose process has ended has no name left.
    """
    return pane.name or format_pymux_string(pymux, PANE_TITLE_FORMAT, pane=pane).strip()


def create_container_for_process(
    pymux: Pymux,
    window: arrangement.Window,
    arrangement_pane: arrangement.Pane,
):
    """
    Create a `Container` with a titlebar for a process.
    """

    def is_zoomed() -> bool:
        """
        Whether this pane is the one filling the window now.

        **Asked every frame, and not fixed when the container is
        built.** Zoom is a layout that wraps another one, so the same
        container draws whether the window is zoomed or not, and the
        mark on the bar has to follow.
        """
        return window.zoom and window.active_pane is arrangement_pane

    @Condition
    def clock_is_visible() -> bool:
        return arrangement_pane.clock_mode

    @Condition
    def pane_numbers_are_visible() -> bool:
        return pymux.display_pane_numbers

    terminal_is_focused = has_focus(arrangement_pane.terminal)

    def get_terminal_style() -> str:
        result = "class:terminal.focused" if terminal_is_focused() else "class:terminal"

        if pymux.current_session.options.paint_screen:
            # The theme's background behind every cell the program left
            # at a default one, so the terminal's own colours are never
            # seen. Lillecarl/pymux#273.
            result += " class:painted"

        return result

    def get_titlebar_text_fragments() -> StyleAndTextTuples:
        result: StyleAndTextTuples = []

        if is_zoomed():
            result.append(("class:titlebar-zoom", " Z "))

        if arrangement_pane.process.is_terminated:
            result.append(("class:terminated", " Terminated "))

        # A pane this client reads the history of says so. Where in the
        # history it is is drawn in the corner of the pane itself, by
        # the widget that knows.
        if arrangement_pane.is_copying:
            result.append(("class:copymode", " Copy "))

        if arrangement_pane.recording is not None:
            result.append(("class:recording", " ● REC %s " % arrangement_pane.recording.name))

        if arrangement_pane.name:
            result.append(("class:name", " %s " % arrangement_pane.name))

        # **Padded on both sides, and not on one.** The bar centres
        # this, so a space that hangs off the end of it moves the title
        # off the middle of the pane by half of that space.
        # Lillecarl/pymux#207.
        title = format_pymux_string(
            pymux, window.options.pane_title_format, window=window, pane=arrangement_pane
        ).strip()

        if title:
            result.append(("", " %s " % title))

        return result

    def get_pane_index() -> str:
        window = pymux.arrangement.get_active_window()
        if window is None:
            return "%3s " % "/"

        try:
            index = window.get_pane_index(arrangement_pane)
        except ValueError:
            index = "/"

        return "%3s " % index

    def neighbour(on_left: bool) -> StyleAndTextTuples:
        """
        The name of the pane on one side of this one.

        Nothing when there is none. A zoomed pane covers the window,
        so nothing is beside it either, and `pane_beside` says so.
        """
        pane = pane_beside(
            pymux,
            window,
            arrangement_pane,
            Side.LEFT if on_left else Side.RIGHT,
        )

        if pane is None:
            return []

        name = _short_name_of(pymux, pane)
        if on_left:
            return [("class:neighbour", "%s %s " % (LEFT_MARK, name))]
        return [("class:neighbour", " %s %s" % (name, RIGHT_MARK))]

    def get_pane_number() -> StyleAndTextTuples:
        """
        The pane's own number, at the far left of its bar.

        **It moved there from the right edge**, because the right edge
        now belongs to the right neighbour. Lillecarl/pymux#207.
        """
        return [("class:paneindex", get_pane_index())]

    def get_bar_left() -> StyleAndTextTuples:
        return neighbour(on_left=True)

    def get_bar_right() -> StyleAndTextTuples:
        return neighbour(on_left=False)

    def get_bar_below() -> StyleAndTextTuples:
        """
        What is above this pane and what is below it, in the middle.

        The bar over a pane names the panes beside it at its two edges,
        and there is no room on it for two more names. So the panes a
        stack puts above and below go on a bar of their own, under the
        pane, and they go in the middle of it together: two marks that
        point the way they mean, and one gap between them.
        Lillecarl/pymux#211.
        """
        above = pane_beside(pymux, window, arrangement_pane, Side.ABOVE)
        below = pane_beside(pymux, window, arrangement_pane, Side.BELOW)
        result: StyleAndTextTuples = []

        if above is not None:
            name = _short_name_of(pymux, above)
            result.append(("class:neighbour", "%s %s" % (ABOVE_MARK, name)))

        if above is not None and below is not None:
            result.append(("", "   "))

        if below is not None:
            name = _short_name_of(pymux, below)
            result.append(("class:neighbour", "%s %s" % (BELOW_MARK, name)))

        return result

    def nothing() -> StyleAndTextTuples:
        "The three parts the bar below does not have."
        return []

    def on_click() -> None:
        "Click handler for the clock. When clicked, select this pane."
        with started_by(Origin.MOUSE):
            note(pymux, Kind.CLICK, "leave-clock")
        arrangement_pane.clock_mode = False
        window = pymux.arrangement.get_active_window()
        if window is not None:
            window.active_pane = arrangement_pane
        pymux.invalidate(Woke.CLICK_LEFT_THE_CLOCK)

    return _PaneMark(
        FloatContainer(
            HSplit(
                [
                    # The terminal.
                    TracePaneWritePosition(pymux, arrangement_pane, content=arrangement_pane.terminal),
                ]
            ),
            floats=[
                # The title bar: this pane's title in the middle, and
                # the panes beside it named at the edges.
                # Lillecarl/pymux#207.
                Float(
                    content=ConditionalContainer(
                        content=Window(
                            height=1,
                            # A window that fills its row owns every
                            # cell of it, which is what lets an unchanged
                            # bar write its last cells again. The bar
                            # pads to the width, so this changes no cell.
                            # Lillecarl/pymux#578.
                            char=" ",
                            content=PaneTitleBar(
                                get_number=get_pane_number,
                                get_left=get_bar_left,
                                get_middle=get_titlebar_text_fragments,
                                get_right=get_bar_right,
                            ),
                            style="class:titlebar",
                        ),
                        filter=Condition(lambda: pymux.show_pane_status),
                    ),
                    left=0,
                    right=0,
                    top=-1,
                    height=1,
                    z_index=Z_INDEX.WINDOW_TITLE_BAR,
                ),
                # The bar below: the panes above and below this one.
                # There is no room for them on the bar above, which has
                # four parts already. Lillecarl/pymux#211.
                Float(
                    content=ConditionalContainer(
                        content=Window(
                            height=1,
                            char=" ",
                            content=PaneTitleBar(
                                get_number=nothing,
                                get_left=nothing,
                                get_middle=get_bar_below,
                                get_right=nothing,
                            ),
                            style="class:titlebar",
                        ),
                        # **Only where this pane has a neighbour above
                        # or below it.** The row belongs to the window,
                        # because the bottom pane of a stack needs one
                        # under it, but a pane with nothing to name has
                        # nothing to draw: an empty bar still fills its
                        # row with the title bar style, which put a
                        # coloured band under a pane that is not
                        # stacked at all. Carl, on the picture: "we
                        # only want that bar if the rectangle is
                        # split". Lillecarl/pymux#219.
                        filter=Condition(lambda: bool(get_bar_below())),
                    ),
                    left=0,
                    right=0,
                    bottom=-1,
                    height=1,
                    z_index=Z_INDEX.WINDOW_TITLE_BAR,
                ),
                # The clock.
                Float(content=ConditionalContainer(BigClock(pymux, on_click), filter=clock_is_visible)),
                # Pane number.
                Float(
                    content=ConditionalContainer(
                        content=PaneNumber(pymux, arrangement_pane),
                        filter=pane_numbers_are_visible,
                    )
                ),
            ],
        ),
        window,
        arrangement_pane,
        get_terminal_style,
        # Whether the row below this pane is the bar that names a stack's
        # neighbours, which is the row the mark's foot is drawn on.
        # Lillecarl/pymux#401.
        has_bar_below=lambda: bool(get_bar_below()),
        hidden=lambda: hidden_of_pane(pymux, arrangement_pane),
        backdrop=lambda: cut_backdrop(pymux),
        fade=lambda: lifts(*cut_shade_of(pymux)),
    )


class _ContainerProxy(Container):
    def __init__(self, content: Container) -> None:
        self.content = content

    @override
    def reset(self) -> None:
        self.content.reset()

    @override
    def preferred_width(self, max_available_width: int) -> D:
        return self.content.preferred_width(max_available_width)

    @override
    def preferred_height(self, width: int, max_available_height: int) -> D:
        return self.content.preferred_height(width, max_available_height)

    @override
    def write_to_screen(
        self,
        screen: Screen,
        mouse_handlers: MouseHandlers,
        write_position: WritePosition,
        parent_style: str,
        erase_bg: bool,
        z_index: int | None,
    ) -> None:
        self.content.write_to_screen(screen, mouse_handlers, write_position, parent_style, erase_bg, z_index)

    @override
    def get_children(self) -> list[Container]:
        return [self.content]


_focused_border_vertical = "┃"
_focused_border_left_top = "┏"
_focused_border_right_top = "┓"
_focused_border_left_bottom = "┗"
_focused_border_right_bottom = "┛"


class _PaneMark(_ContainerProxy):
    """
    The mark of the focused pane, written beside its cells.

    The mark sits one cell outside the pane: a side down the column on
    either hand, and the four corners between them. It is registered as
    a deferred draw at the z-index a `Float` there would have inherited,
    so a pane drawn later cannot cover it and it still lands over a bar
    it shares an edge with.

    **A corner needs a chrome row to sit on.** The row above a pane is
    its title bar, and the row below is the bar that names a stack's
    neighbours -- there is none for a pane in no stack, whose lower
    edge is the window's, where a mark would land on the status line.
    So the foot of the mark is drawn only where that bar is.
    Lillecarl/pymux#401.

    **Six cells are not six Floats.** A `Float` per cell draws
    through the whole container machinery to land one character, and
    the two corners of the foot cost almost six thousand instructions
    a frame in `checks.pymux-keystroke`. Lillecarl/pymux#488.
    """

    #: What a mark cell is styled as.
    STYLE = "class:border"

    def __init__(
        self,
        content: Container,
        window: arrangement.Window,
        pane: arrangement.Pane,
        style: Callable[[], str],
        has_bar_below: Callable[[], bool],
        hidden: Callable[[], Hidden | None] = lambda: None,
        backdrop: Callable[[], str | None] = lambda: None,
        fade: Callable[[], list[float]] = list,
    ) -> None:
        super().__init__(content)
        self.window = window
        self.pane = pane
        #: The pane's style, under everything it draws. Read once a
        #: frame here: a `FloatContainer` reads its own style again
        #: for each of its floats.
        self.style = style
        self.has_bar_below = has_bar_below
        #: What the view hides of this pane, and what a default cell
        #: shows: the two things the shade at a cut edge needs.
        #: Lillecarl/pymux#616.
        self.hidden = hidden
        self.backdrop = backdrop
        self.fade = fade
        # The cells the mark is made of, built once. A frame draws
        # close to two hundred of them on a big screen, and a `Char`
        # built per cell per frame was most of a millisecond of it.
        self._cells = {
            glyph: Char(glyph, self.STYLE)
            for glyph in {
                _focused_border_vertical,
                _focused_border_left_top,
                _focused_border_right_top,
                _focused_border_left_bottom,
                _focused_border_right_bottom,
            }
        }

    @override
    def write_to_screen(
        self,
        screen: Screen,
        mouse_handlers: MouseHandlers,
        write_position: WritePosition,
        parent_style: str,
        erase_bg: bool,
        z_index: int | None,
    ) -> None:
        style = parent_style + " " + self.style()
        super().write_to_screen(screen, mouse_handlers, write_position, style, erase_bg, z_index)
        hidden = self.hidden()
        if hidden is not None:
            screen.draw_with_z_index(
                (z_index or 0) + Z_INDEX.CUT_SHADE,
                partial(
                    shade,
                    screen,
                    write_position,
                    hidden,
                    self.fade(),
                    get_app().renderer.style.get_attrs_for_style_str,
                    self.backdrop(),
                ),
            )
        # **The z is the parent's, plus the mark's**, as a `Float` in a
        # `FloatContainer` inherits it. A body drawn late -- one whose
        # own floats wait on a cursor -- runs its panes again, and a mark
        # left at a bare `HIGHLIGHTED_BORDER` would be erased by that
        # second pass. Adding the parent's z keeps the mark after it.
        screen.draw_with_z_index(
            (z_index or 0) + Z_INDEX.HIGHLIGHTED_BORDER,
            partial(self._draw_mark, screen, write_position),
        )

    def _draw_mark(self, screen: Screen, rect: WritePosition) -> None:
        "The six cells, once the pane's rectangle is known."
        if self.window.active_pane != self.pane:
            return

        left = rect.xpos - 1
        right = rect.xpos + rect.width
        top = rect.ypos - 1
        bottom = rect.ypos + rect.height

        cells = self._cells

        def put(x: int, y: int, glyph: str) -> None:
            # A cell outside the screen is a key the diff never reads, so
            # a mark at an edge needs no bound and simply draws nothing.
            screen.data_buffer[y][x] = cells[glyph]

        for y in range(rect.ypos, bottom):
            put(left, y, _focused_border_vertical)
            put(right, y, _focused_border_vertical)

        put(left, top, _focused_border_left_top)
        put(right, top, _focused_border_right_top)

        if self.has_bar_below():
            put(left, bottom, _focused_border_left_bottom)
            put(right, bottom, _focused_border_right_bottom)

        # The columns the mark reaches, for the renderer's trim: these
        # cells land after the copies measured their rows, and a row
        # that ends before them would erase what it just drew. Only
        # past what shows: a mark outside the screen is a key the diff
        # never reads, and measuring it would walk every blank between
        # the pane and the edge on every frame.
        width = screen.visible_width
        if width is None or right < width:
            max_index = screen.max_column_index
            for y in range(top, bottom + 1):
                if max_index.get(y, -1) < right:
                    max_index[y] = right


class TracePaneWritePosition(_ContainerProxy):  # XXX: replace with SizedBox
    "Trace the write position of this pane."

    def __init__(self, pymux: Pymux, arrangement_pane: arrangement.Pane, content: AnyContainer) -> None:
        content = to_container(content)
        _ContainerProxy.__init__(self, content)

        self.pymux = pymux
        self.arrangement_pane = arrangement_pane

    @override
    def write_to_screen(
        self,
        screen: Screen,
        mouse_handlers: MouseHandlers,
        write_position: WritePosition,
        parent_style: str,
        erase_bg: bool,
        z_index: int | None,
    ) -> None:
        _ContainerProxy.write_to_screen(
            self,
            screen,
            mouse_handlers,
            write_position,
            parent_style,
            erase_bg,
            z_index,
        )
        # Where this pane was drawn, kept on the screen that was drawn
        # and not on whichever client `get_app()` happens to name. With
        # two clients attached, a render of one can carry the context of
        # the other, and the positions then land on the wrong client.
        pane_write_positions(screen)[self.arrangement_pane] = write_position
        pane_containers(screen)[self.arrangement_pane] = self
