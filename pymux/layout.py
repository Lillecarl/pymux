# encoding: utf-8
"""
The layout engine. This builds the prompt_toolkit layout.
"""

import datetime
import weakref
from functools import partial
from typing import TYPE_CHECKING, Callable, Dict, List, Tuple

from prompt_toolkit.application import Application, get_app
from prompt_toolkit.data_structures import Size
from prompt_toolkit.filters import (
    Condition,
    has_completions,
    has_focus,
    is_done,
)
from prompt_toolkit.formatted_text import HTML, FormattedText, StyleAndTextTuples
from prompt_toolkit.layout.containers import (
    ConditionalContainer,
    Container,
    DynamicContainer,
    Float,
    FloatContainer,
    HSplit,
    VSplit,
    Window,
    WindowAlign,
    ScrollOffsets,
    WritePosition,
    to_container,
)
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension as D
from prompt_toolkit.layout.margins import ScrollbarMargin
from prompt_toolkit.layout.menus import CompletionsMenu, CompletionsMenuControl
from prompt_toolkit.layout.mouse_handlers import MouseHandlers
from prompt_toolkit.layout.processors import (
    AppendAutoSuggestion,
    BeforeInput,
    HighlightSelectionProcessor,
    ShowArg,
)
from prompt_toolkit.layout.screen import Char, Screen
from prompt_toolkit.mouse_events import MouseEvent, MouseEventType
from prompt_toolkit.widgets import Dialog, SearchToolbar, TextArea

import pymux.arrangement as arrangement

from .enums import Woke
from .filters import WaitsForConfirmation
from .format import format_pymux_string
from .log import logger
from .divided import Divided
from .plan_container import PlanContainer
from .plane import Plan, Side, bounding_box
from .strip import Strip
from .tiling import BORDER_WIDTH, Gaps
from .titlebar import PaneTitleBar

if TYPE_CHECKING:
    from prompt_toolkit.layout.controls import BufferControl, NotImplementedOrNone

    from pymux.main import Pymux

__all__ = ["LayoutManager"]


#: How far a box floats from the top of the screen and from each
#: side. The keys pop-up chose these, and the command palette takes
#: the same ones because it is the same kind of box.
#: Lillecarl/pymux#158.
BOX_TOP = 5
BOX_SIDE = 3

#: What the palette holds above its completions: a title and the line
#: a person types on.
PALETTE_HEADER = 2

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

#: What `clock-mode` draws inside a pane, as text. `BigClock` paints the
#: hour and the minute in big numbers, and nothing else of it moves.
CLOCK_FORMAT = "%H:%M"


class Justify:
    "Justify enum for the status bar."

    LEFT = "left"
    CENTER = "center"
    RIGHT = "right"

    _ALL = [LEFT, CENTER, RIGHT]


class Z_INDEX:
    HIGHLIGHTED_BORDER = 2
    STATUS_BAR = 5
    COMMAND_LINE = 6
    MESSAGE_TOOLBAR = 7
    WINDOW_TITLE_BAR = 8
    POPUP = 9
    OVERLAY = 10

    #: A clock and a pane number are drawn inside the pane they belong
    #: to, over the content of that pane. Nothing else draws in those
    #: cells, so the only thing this height decides is what happens
    #: when one of them meets the chrome above: a popup or an overlay
    #: covers the panes, and a clock drawn at a hundred paints over
    #: one. They are left where they were found, well clear of the
    #: list, because no case has been seen where the two meet.
    CLOCK = 100
    PANE_NUMBER = 100


#: Where the panes of one render were drawn. It hangs on the screen of
#: that render, so two clients never read each other's.
_PANE_POSITIONS = "pymux_pane_write_positions"


def pane_write_positions(screen) -> dict:
    "Where each pane was drawn on this screen."
    positions = getattr(screen, _PANE_POSITIONS, None)
    if positions is None:
        positions = {}
        setattr(screen, _PANE_POSITIONS, positions)
    return positions


#: How much of the screen an overlay pane takes when nobody says.
DEFAULT_OVERLAY_SIZE = "60%"

#: The smallest overlay that still shows something.
MIN_OVERLAY_SIZE = 3


def overlay_size(given: str | None, available: int) -> int:
    """
    How many cells an overlay pane takes along one axis.

    A plain number is a number of cells. A number with a percent sign
    is a share of the screen, which is what `display-popup -w 60%`
    gives. Anything that is not a number falls back to the default.
    The answer never passes the screen.
    """
    text = (given or DEFAULT_OVERLAY_SIZE).strip()

    try:
        if text.endswith("%"):
            cells = available * int(text[:-1]) // 100
        else:
            cells = int(text)
    except ValueError:
        cells = available * int(DEFAULT_OVERLAY_SIZE[:-1]) // 100

    return max(MIN_OVERLAY_SIZE, min(cells, available))


class Background(Container):
    """
    Generate the background of dots, which becomes visible when several clients
    are attached and not all of them have the same size.

    (This is implemented as a Container, rather than a UIControl wrapped in a
    Window, because it can be done very effecient this way.)
    """

    def reset(self) -> None:
        pass

    def preferred_width(self, max_available_width: int) -> D:
        return D()

    def preferred_height(self, width: int, max_available_height: int) -> D:
        return D()

    def write_to_screen(
        self,
        screen: Screen,
        mouse_handlers: MouseHandlers,
        write_position: WritePosition,
        parent_style: str,
        erase_bg: bool,
        z_index: int | None,
    ) -> None:
        "Fill the whole area of write_position with dots."
        default_char = Char(" ", "class:background")
        dot = Char(".", "class:background")

        ypos = write_position.ypos
        xpos = write_position.xpos
        columns = range(xpos, xpos + write_position.width)

        # The pattern repeats every three rows. Build one row of each of
        # the three, and copy it. `dict.update` runs in C, which makes a
        # render of a wide screen about a tenth cheaper.
        rows = [
            {x: (dot if (x + phase) % 3 == 0 else default_char) for x in columns}
            for phase in range(3)
        ]

        data_buffer = screen.data_buffer

        for y in range(ypos, ypos + write_position.height):
            data_buffer[y].update(rows[y % 3])

    def get_children(self) -> List[Container]:
        return []


# Numbers for the clock and pane numbering.
_numbers = list(
    zip(
        *[  # (Transpose x/y.)
            [
                "#####",
                "    #",
                "#####",
                "#####",
                "#   #",
                "#####",
                "#####",
                "#####",
                "#####",
                "#####",
            ],
            [
                "#   #",
                "    #",
                "    #",
                "    #",
                "#   #",
                "#    ",
                "#    ",
                "    #",
                "#   #",
                "#   #",
            ],
            [
                "#   #",
                "    #",
                "#####",
                "#####",
                "#####",
                "#####",
                "#####",
                "    #",
                "#####",
                "#####",
            ],
            [
                "#   #",
                "    #",
                "#    ",
                "    #",
                "    #",
                "    #",
                "#   #",
                "    #",
                "#   #",
                "    #",
            ],
            [
                "#####",
                "    #",
                "#####",
                "#####",
                "    #",
                "#####",
                "#####",
                "    #",
                "#####",
                "#####",
            ],
        ]
    )
)


def _draw_number(
    screen, x_offset, y_offset, number, style="class:clock", transparent=False
):
    "Write number at position."
    fg = Char(" ", "class:clock")
    bg = Char(" ", "")

    for y, row in enumerate(_numbers[number]):
        screen_row = screen.data_buffer[y + y_offset]
        for x, n in enumerate(row):
            if n == "#":
                screen_row[x + x_offset] = fg
            elif not transparent:
                screen_row[x + x_offset] = bg


class BigClock(Container):
    """
    Display a big clock.
    """

    WIDTH = 28
    HEIGHT = 5

    def __init__(self, on_click: Callable[[], None]):
        self.on_click = on_click

    def reset(self):
        pass

    def write_to_screen(
        self,
        screen: Screen,
        mouse_handlers: MouseHandlers,
        write_position: WritePosition,
        parent_style: str,
        erase_bg: bool,
        z_index: int | None,
    ) -> None:
        xpos = write_position.xpos
        ypos = write_position.ypos

        # Erase background.
        bg = Char(" ", "")

        def draw_func() -> None:
            for y in range(ypos, self.HEIGHT + ypos):
                row = screen.data_buffer[y]
                for x in range(xpos, xpos + self.WIDTH):
                    row[x] = bg

            # Display time.
            now = datetime.datetime.now()
            _draw_number(screen, xpos + 0, ypos, now.hour // 10)
            _draw_number(screen, xpos + 6, ypos, now.hour % 10)
            _draw_number(screen, xpos + 16, ypos, now.minute // 10)
            _draw_number(screen, xpos + 23, ypos, now.minute % 10)

            # Add a colon
            screen.data_buffer[ypos + 1][xpos + 13] = Char(" ", "class:clock")
            screen.data_buffer[ypos + 3][xpos + 13] = Char(" ", "class:clock")

            screen.width = self.WIDTH
            screen.height = self.HEIGHT

            mouse_handlers.set_mouse_handler_for_range(
                x_min=xpos,
                x_max=xpos + write_position.width,
                y_min=ypos,
                y_max=ypos + write_position.height,
                handler=self._mouse_handler,
            )

        screen.draw_with_z_index(z_index=Z_INDEX.CLOCK, draw_func=draw_func)

    def _mouse_handler(self, mouse_event: MouseEvent) -> None:
        "Click callback."
        if mouse_event.event_type == MouseEventType.MOUSE_UP:
            self.on_click()

    def preferred_width(self, max_available_width: int) -> D:
        return D.exact(BigClock.WIDTH)

    def preferred_height(self, width: int, max_available_height: int) -> D:
        return D.exact(BigClock.HEIGHT)

    def get_children(self) -> List[Container]:
        return []


class PaneNumber(Container):  # XXX: make FormattedTextControl
    """
    Number of panes, to be drawn in the middle of the pane.
    """

    WIDTH = 5
    HEIGHT = 5

    def __init__(self, pymux: "Pymux", arrangement_pane: arrangement.Pane) -> None:
        self.pymux = pymux
        self.arrangement_pane = arrangement_pane

    def reset(self) -> None:
        pass

    def _get_index(self):
        window = self.pymux.arrangement.get_active_window()
        try:
            return window.get_pane_index(self.arrangement_pane)
        except ValueError:
            return 0

    def preferred_width(self, max_available_width: int) -> D:
        # Enough to display all the digits.
        return D.exact(6 * len("%s" % self._get_index()) - 1)

    def preferred_height(self, width: int, max_available_height: int) -> D:
        return D.exact(self.HEIGHT)

    def write_to_screen(
        self,
        screen: Screen,
        mouse_handlers: MouseHandlers,
        write_position: WritePosition,
        parent_style: str,
        erase_bg: bool,
        z_index: int | None,
    ) -> None:
        style = "class:panenumber"

        def draw_func():
            for i, d in enumerate("%s" % (self._get_index(),)):
                _draw_number(
                    screen,
                    write_position.xpos + i * 6,
                    write_position.ypos,
                    int(d),
                    style=style,
                    transparent=True,
                )

        screen.draw_with_z_index(z_index=Z_INDEX.PANE_NUMBER, draw_func=draw_func)

    def get_children(self) -> List[Container]:
        return []


#: How many rows a message may take. It is a pop-up over the panes, so
#: it may not become a page: past this the end of a very long message
#: is cut, which is the end a reader reaches last.
MAX_MESSAGE_ROWS = 5


class MessageToolbar(Window):
    """
    Pop-up (at the bottom) for showing error/status messages.

    **It wraps, and it does not park a cursor at the end.** This was a
    `FormattedTextToolbar`, which is a `Window` that does not wrap, and
    the message carried a `[SetCursorPosition]` marker after the text. A
    window scrolls to keep its cursor visible whether it has the focus
    or not (`Window._scroll_without_linewrapping`), so a message wider
    than the terminal was shown from its *end*.

    That lost the front of it. `Pymux.report_startup_errors` joins every
    failed line of a configuration file into one message, and each one
    names the file and the line, so two of them pass the width of a
    normal terminal easily -- and only the last could be read. A person
    fixed that one, ran pymux again, and met the first.
    Lillecarl/pymux#205, and the last step of Lillecarl/pymux#38.

    Nothing is typed into a message, so the cursor had no work to do
    there. The y/n toolbar keeps its marker: that one is a prompt, and
    it is short.
    """

    def __init__(self, client_state):
        def get_message():
            # If there is a message to be shown for this client, show that.
            if client_state.message:
                return client_state.message
            else:
                return ""

        def get_tokens():
            message = get_message()
            if message:
                return FormattedText([("class:message", message + " ")])
            else:
                return ""

        # The style rides on the fragments, the way it did before: the
        # toolbar itself carried none, so the rows past the end of the
        # message are the background and not a coloured bar.
        super().__init__(
            FormattedTextControl(get_tokens),
            wrap_lines=True,
            dont_extend_height=True,
            height=D(min=1, max=MAX_MESSAGE_ROWS),
        )


class LayoutManager:
    """
    The main layout class, that contains the whole Pymux layout.
    """

    def __init__(self, pymux: "Pymux", client_state) -> None:
        self.pymux = pymux
        self.client_state = client_state

        # Popup dialog for displaying keys, etc...
        search_textarea = SearchToolbar()
        self._popup_textarea = TextArea(
            scrollbar=True, read_only=True, search_field=search_textarea
        )
        self.popup_dialog = Dialog(
            title="Keys",
            body=HSplit(
                [
                    Window(FormattedTextControl(text=""), height=1),  # 1 line margin.
                    self._popup_textarea,
                    search_textarea,
                    Window(
                        FormattedTextControl(
                            text=HTML(
                                "Press [<b>q</b>] to quit or [<b>/</b>] for searching."
                            )
                        ),
                        align=WindowAlign.CENTER,
                        height=1,
                    ),
                ]
            ),
        )

        # The container of the overlay pane, and the pane it belongs to.
        self._overlay_for_pane: Tuple[arrangement.Pane, Container] | None = None

        # The window a person types a command into, and the box that
        # holds it when the palette is on. Both are built once and kept.
        # A fresh `BufferControl` on every render is one that the layout
        # never focused, so the cursor is drawn nowhere and the person
        # cannot see where they are typing. Lillecarl/pymux#158.
        self._command_window: Window | None = None
        self._palette: Container | None = None

        self.layout = self._create_layout()

        # Keep track of render information.

    @property
    def pane_write_positions(self) -> Dict[arrangement.Pane, WritePosition]:
        """
        Where each pane was drawn, in the last render of this client.

        The positions live on the screen that carried them, so a client
        reads its own and never another's. Before the first render, and
        on a frame that the renderer threw away, there are none.
        """
        renderer = getattr(self.client_state.app, "renderer", None)
        screen = getattr(renderer, "_last_screen", None)
        if screen is None:
            return {}
        return pane_write_positions(screen)

    def reset_write_positions(self) -> None:
        """
        Nothing to clear: every render writes to a new screen, and the
        positions go with it. Kept because the render hook calls it.
        """

    def display_popup(self, title: str, content: str) -> None:
        """
        Display a pop-up dialog.
        """
        self.popup_dialog.title = title
        self._popup_textarea.text = content
        self.client_state.display_popup = True
        get_app().layout.focus(self._popup_textarea)

    def _create_select_window_handler(
        self, window: arrangement.Window
    ) -> Callable[[MouseEvent], "NotImplementedOrNone"]:
        "Return a mouse handler that selects the given window when clicking."

        def handler(mouse_event: MouseEvent) -> "NotImplementedOrNone":
            if mouse_event.event_type == MouseEventType.MOUSE_DOWN:
                self.pymux.arrangement.set_active_window(window)
                self.pymux.invalidate(Woke.A_CLICK_CHOSE_A_WINDOW)
                return None
            else:
                return NotImplemented  # Event not handled here.

        return handler

    def _get_status_tokens(self) -> StyleAndTextTuples:
        "The tokens for the status bar."
        result: StyleAndTextTuples = []

        # Display panes.
        for i, w in enumerate(self.pymux.arrangement.windows):
            if i > 0:
                result.append(("", " "))

            if w == self.pymux.arrangement.get_active_window():
                style = "class:window.current"
                format_str = self.pymux.window_status_current_format

            else:
                style = "class:window"
                format_str = self.pymux.window_status_format

            result.append(
                (
                    style,
                    format_pymux_string(self.pymux, format_str, window=w),
                    self._create_select_window_handler(w),
                )
            )

        return result

    def _get_status_left_tokens(self) -> str:
        return format_pymux_string(self.pymux, self.pymux.status_left)

    def _get_status_right_tokens(self) -> str:
        return format_pymux_string(self.pymux, self.pymux.status_right)

    def what_time_moves(self) -> Tuple[str, ...]:
        """
        Every string on this client's screen that changes on its own.

        The status line holds the clock, and a `#{...}` variable in it
        can hold anything. `clock-mode` draws a clock inside a pane.
        Nothing else on the screen changes unless something asks for a
        frame, so a caller that finds this the same as the last frame
        drew has nothing to draw. Lillecarl/pymux#154.

        The caller sets its own application first. A `#{...}` variable
        asks which window this client looks at.

        An empty answer means this client shows nothing that time
        moves. `full-screen on` is that case.
        """
        pymux = self.pymux
        parts: List[str] = []

        if pymux.show_status:
            parts.append(self._get_status_left_tokens())
            parts.append(self._get_status_right_tokens())
            parts += [text for _, text, *_ in self._get_status_tokens()]

        for window in pymux.arrangement.windows:
            for pane in window.panes:
                if pane.clock_mode:
                    parts.append(datetime.datetime.now().strftime(CLOCK_FORMAT))
                elif pymux.show_pane_status:
                    parts.append(
                        format_pymux_string(pymux, PANE_TITLE_FORMAT, pane=pane)
                    )

        return tuple(parts)

    def _get_align(self) -> WindowAlign:
        if self.pymux.status_justify == Justify.RIGHT:
            return WindowAlign.RIGHT
        elif self.pymux.status_justify == Justify.CENTER:
            return WindowAlign.CENTER
        else:
            return WindowAlign.LEFT

    def _before_prompt_command_tokens(self) -> StyleAndTextTuples:
        return [("class:commandline.prompt", "%s " % (self.client_state.prompt_text,))]

    def _overlay_container(self) -> Container:
        """
        The overlay pane of this client, with a title bar above it.

        The container is built again when the pane changes, and kept
        while it stays, so that the terminal keeps its state.
        """
        pane = self.pymux.overlay_pane

        if pane is None:
            return Window()

        if self._overlay_for_pane is not None and self._overlay_for_pane[0] is pane:
            return self._overlay_for_pane[1]

        def get_title() -> StyleAndTextTuples:
            return [("class:overlay.title", " %s " % self.pymux.overlay_title)]

        container = HSplit(
            [
                Window(
                    height=1,
                    align=WindowAlign.CENTER,
                    content=FormattedTextControl(get_title),
                    style="class:overlay.titlebar",
                ),
                TracePaneWritePosition(self.pymux, pane, content=pane.terminal),
            ],
            style="class:overlay",
        )

        self._overlay_for_pane = (pane, container)
        return container

    def _command_line_window(self) -> Window:
        """
        The window that holds what a person types after ":".

        The bar at the bottom and the palette in the middle draw the
        same window. Only where it is drawn differs, so the key
        bindings of command mode and everything that reads the buffer
        stay as they were. Lillecarl/pymux#158.

        **It is built once.** The layout focuses a control, and a fresh
        one on every render is one it never focused: the cursor lands
        nowhere and a person cannot see where they are typing. Only one
        of the two places draws it, because the filter of each reads
        the option.
        """
        if self._command_window is not None:
            return self._command_window

        self._command_window = Window(
            # Can be more if the command is multiline.
            height=D(min=1),
            style="class:commandline",
            dont_extend_height=True,
            content=BufferControl(
                buffer=self.client_state.command_buffer,
                preview_search=True,
                input_processors=[
                    AppendAutoSuggestion(),
                    BeforeInput(":", style="class:commandline-prompt"),
                    ShowArg(),
                    HighlightSelectionProcessor(),
                ],
            ),
            z_index=Z_INDEX.COMMAND_LINE,
        )
        return self._command_window

    def _command_palette(self) -> Container:
        """
        The ":" command line as a box in the middle of the screen.

        **The completions are inside the box.** The menu that a bottom
        bar uses hangs off the cursor and stops at twelve rows, so a
        palette that only moved the text would be a nicer box around
        the same thin content. In here the menu takes the height of the
        box, which is what makes the room worth having.

        A title row says what the box is, the way the overlay pane says
        what it holds. tmux has no such thing; this is the room that
        Lillecarl/pymux#147, #148 and #48 are asking for.

        Built once, for the reason `_command_line_window` gives.
        """
        if self._palette is not None:
            return self._palette

        self._palette = HSplit(
            [
                Window(
                    height=1,
                    align=WindowAlign.CENTER,
                    content=FormattedTextControl(
                        lambda: [("class:commandpalette.title", " Command ")]
                    ),
                    style="class:commandpalette.titlebar",
                ),
                self._command_line_window(),
                self._palette_completions(),
            ],
            style="class:commandpalette",
        )
        return self._palette

    def _palette_rows(self) -> int:
        """
        How many rows of completions fit under the box.

        `get_window_size` answers the rows a pane may use, which is the
        screen without the status line. The box starts `BOX_TOP`
        rows down and holds a title and the input, so what is left is
        what the completions may take.
        """
        rows = self.pymux.get_window_size().rows
        return max(1, rows - BOX_TOP - PALETTE_HEADER)

    def _palette_completions(self) -> Container:
        """
        The completions of the palette, filling the box.

        This is `CompletionsMenu` with two of its numbers changed, so
        it is written out rather than wrapped. That widget draws for a
        menu that hangs under the cursor: as narrow as its longest
        completion, and twelve rows at the most. Both are wrong in a
        box. The box has a width to fill, which is where a completion
        and what it means will sit side by side, and it has to stop
        above the status line rather than run off the screen.
        """
        return ConditionalContainer(
            content=Window(
                content=CompletionsMenuControl(),
                height=lambda: D(min=1, max=self._palette_rows()),
                scroll_offsets=ScrollOffsets(top=1, bottom=1),
                right_margins=[ScrollbarMargin(display_arrows=True)],
                style="class:completion-menu",
                z_index=Z_INDEX.POPUP,
            ),
            filter=has_completions & ~is_done,
        )

    def _create_layout(self) -> Container:
        """
        Generate the main prompt_toolkit layout.
        """
        waits_for_confirmation = WaitsForConfirmation(self.pymux)
        palette = Condition(lambda: self.pymux.command_palette)

        return FloatContainer(
            content=HSplit(
                [
                    # The main window.
                    FloatContainer(
                        Background(),
                        floats=[
                            Float(
                                width=lambda: self.pymux.get_window_size().columns,
                                height=lambda: self.pymux.get_window_size().rows,
                                content=DynamicBody(self.pymux),
                            )
                        ],
                    ),
                    # Status bar.
                    ConditionalContainer(
                        content=VSplit(
                            [
                                # Left.
                                Window(
                                    height=1,
                                    width=(
                                        lambda: D(max=self.pymux.status_left_length)
                                    ),
                                    dont_extend_width=True,
                                    content=FormattedTextControl(
                                        self._get_status_left_tokens
                                    ),
                                ),
                                # List of windows in the middle.
                                Window(
                                    height=1,
                                    char=" ",
                                    align=self._get_align,
                                    content=FormattedTextControl(
                                        self._get_status_tokens
                                    ),
                                ),
                                # Right.
                                Window(
                                    height=1,
                                    width=(
                                        lambda: D(max=self.pymux.status_right_length)
                                    ),
                                    dont_extend_width=True,
                                    align=WindowAlign.RIGHT,
                                    content=FormattedTextControl(
                                        self._get_status_right_tokens
                                    ),
                                ),
                            ],
                            z_index=Z_INDEX.STATUS_BAR,
                            style="class:statusbar",
                        ),
                        filter=Condition(lambda: self.pymux.show_status),
                    ),
                ]
            ),
            floats=[
                Float(
                    bottom=1,
                    left=0,
                    z_index=Z_INDEX.MESSAGE_TOOLBAR,
                    content=MessageToolbar(self.client_state),
                ),
                Float(
                    left=0,
                    right=0,
                    bottom=0,
                    content=HSplit(
                        [
                            # Wait for confirmation toolbar.
                            ConditionalContainer(
                                content=Window(
                                    height=1,
                                    content=ConfirmationToolbar(
                                        self.pymux, self.client_state
                                    ),
                                    z_index=Z_INDEX.COMMAND_LINE,
                                ),
                                filter=waits_for_confirmation,
                            ),
                            # ':' prompt toolbar.
                            ConditionalContainer(
                                content=self._command_line_window(),
                                filter=has_focus(self.client_state.command_buffer)
                                & ~palette,
                            ),
                            # Other command-prompt commands toolbar.
                            ConditionalContainer(
                                content=Window(
                                    height=1,
                                    style="class:commandline",
                                    content=BufferControl(
                                        buffer=self.client_state.prompt_buffer,
                                        input_processors=[
                                            BeforeInput(
                                                self._before_prompt_command_tokens
                                            ),
                                            AppendAutoSuggestion(),
                                            HighlightSelectionProcessor(),
                                        ],
                                    ),
                                    z_index=Z_INDEX.COMMAND_LINE,
                                ),
                                filter=has_focus(self.client_state.prompt_buffer),
                            ),
                        ]
                    ),
                ),
                # Keys pop-up.
                Float(
                    content=ConditionalContainer(
                        content=self.popup_dialog,
                        filter=Condition(lambda: self.client_state.display_popup),
                    ),
                    left=BOX_SIDE,
                    right=BOX_SIDE,
                    top=BOX_TOP,
                    bottom=5,
                    z_index=Z_INDEX.POPUP,
                ),
                # The ":" command line as a box in the middle, when the
                # person asked for one. The same inset as the keys
                # pop-up above, because it is the same kind of box.
                # Lillecarl/pymux#158.
                Float(
                    content=ConditionalContainer(
                        content=DynamicContainer(self._command_palette),
                        filter=has_focus(self.client_state.command_buffer) & palette,
                    ),
                    left=BOX_SIDE,
                    right=BOX_SIDE,
                    top=BOX_TOP,
                    # No bottom. A float that is given both ends fills
                    # the space between them, and a box of two rows of
                    # text in a slab fourteen rows tall is what that
                    # looks like. With one end it takes the height of
                    # what is in it and grows as the completions
                    # arrive.
                    z_index=Z_INDEX.POPUP,
                ),
                # The menu that hangs off the cursor. The palette holds
                # its own, so this one steps aside for it.
                Float(
                    xcursor=True,
                    ycursor=True,
                    content=ConditionalContainer(
                        content=CompletionsMenu(max_height=12),
                        filter=~(has_focus(self.client_state.command_buffer) & palette),
                    ),
                ),
                # The overlay pane, in the middle of the screen. A
                # `Float` with no side given is centred.
                Float(
                    content=ConditionalContainer(
                        content=DynamicContainer(self._overlay_container),
                        filter=Condition(lambda: self.pymux.overlay_pane is not None),
                    ),
                    width=lambda: overlay_size(
                        self.pymux.overlay_width,
                        self.pymux.get_window_size().columns,
                    ),
                    height=lambda: overlay_size(
                        self.pymux.overlay_height,
                        self.pymux.get_window_size().rows,
                    ),
                    z_index=Z_INDEX.OVERLAY,
                ),
            ],
        )


class ConfirmationToolbar(FormattedTextControl):
    """
    Window that displays the yes/no confirmation dialog.
    """

    def __init__(self, pymux, client_state):
        def get_tokens():
            return [
                ("class:question", " "),
                (
                    "class:question",
                    format_pymux_string(pymux, client_state.confirm_text or ""),
                ),
                ("class:question", " "),
                ("class:yesno", "  y/n"),
                ("[SetCursorPosition]", ""),
                ("class:yesno", "  "),
            ]

        super().__init__(get_tokens, style="class:confirmationtoolbar")


class DynamicBody(Container):
    """
    The dynamic part, which is different for each CLI (for each client). It
    depends on which window/pane is active.

    This makes it possible to have just one main layout class, and
    automatically rebuild the parts that change if the windows/panes
    arrangement changes, without doing any synchronisation.
    """

    def __init__(self, pymux: "Pymux") -> None:
        self.pymux = pymux
        self._bodies_for_app: weakref.WeakKeyDictionary[
            Application, Tuple[str, Container]
        ] = weakref.WeakKeyDictionary()  # Maps Application to (hash, Container)

    def _get_body(self) -> Container:
        "Return the Container object for the current CLI."
        new_hash = self.pymux.arrangement.invalidation_hash()

        # Return existing layout if nothing has changed to the arrangement.
        app = get_app()

        if app in self._bodies_for_app:
            existing_hash, container = self._bodies_for_app[app]
            if existing_hash == new_hash:
                return container

        # The layout changed. Build a new layout when the arrangement changed.
        new_layout = self._build_layout()
        self._bodies_for_app[app] = (new_hash, new_layout)
        return new_layout

    def _build_layout(self) -> Container:
        "Rebuild a new Container object and return that."
        logger.info("Rebuilding layout.")

        if not self.pymux.arrangement.windows:
            # No Pymux windows in the arrangement.
            return Window()

        active_window = self.pymux.arrangement.get_active_window()

        # When zoomed, only show the current pane, otherwise show all of them.
        if active_window.zoom:
            return to_container(
                _create_container_for_process(
                    self.pymux, active_window, active_window.active_pane, zoom=True
                )
            )
        else:
            window = self.pymux.arrangement.get_active_window()

            # Every layout draws the same way: it says where the panes
            # are and one container puts them there. A strip draws onto
            # this screen, in this screen's coordinates, so the row
            # reserved below is the row its title bars hang in, exactly
            # as in every other layout.
            content = _create_the_panes(self.pymux, window)

            return HSplit(
                [
                    # Some spacing for the top status bar.
                    ConditionalContainer(
                        content=Window(height=1),
                        filter=Condition(lambda: self.pymux.show_pane_status),
                    ),
                    # The actual content.
                    content,
                    # And the row the bottom pane hangs its bar below
                    # in. Only when there is a stack, because only then
                    # is there anything to name. Lillecarl/pymux#211.
                    ConditionalContainer(
                        content=Window(height=1),
                        filter=Condition(
                            lambda: _the_bar_below_is_drawn(self.pymux, window)
                        ),
                    ),
                ]
            )

    def reset(self) -> None:
        for invalidation_hash, body in self._bodies_for_app.values():
            body.reset()

    def preferred_width(self, max_available_width: int) -> D:
        body = self._get_body()
        return body.preferred_width(max_available_width)

    def preferred_height(self, width: int, max_available_height: int) -> D:
        body = self._get_body()
        return body.preferred_height(width, max_available_height)

    def write_to_screen(
        self,
        screen: Screen,
        mouse_handlers: MouseHandlers,
        write_position: WritePosition,
        parent_style: str,
        erase_bg: bool,
        z_index: int | None,
    ) -> None:
        body = self._get_body()
        body.write_to_screen(
            screen, mouse_handlers, write_position, parent_style, erase_bg, z_index
        )

    def get_children(self) -> List[Container]:
        # (Required for prompt_toolkit.layout.utils.find_window_for_buffer_name.)
        body = self._get_body()
        return [body]


def _create_the_panes(pymux: "Pymux", window) -> Container:
    """
    The container that draws every pane of a window.

    **The layout says where every pane is, and this draws them.**
    `measure` hands over a plan -- a rectangle for each pane, on a
    plane that may be bigger than the screen -- and `PlanContainer`
    puts each pane's container at its rectangle, less the offset of the
    view. Nothing here divides anything, and there is one of these
    whatever layout the window is in. Lillecarl/pymux#217.

    **A pane knows nothing about borders**, and the layout draws them:
    Carl, "individual panes should not be aware of borders ... the
    layout is responsible for drawing the borders either way". In a
    strip the border a column owns comes out of that column's share of
    the window rather than being added on top of it, so a column takes
    the same room whatever the other columns do. Without that, two
    columns of half a window came to one cell more than the window,
    every time. Lillecarl/pymux#206.
    """
    containers = {
        pane: _create_container_for_process(pymux, window, pane)
        for pane in window.panes
    }

    return PlanContainer(the_layout_of(pymux, window), containers)


def the_layout_of(pymux: "Pymux", window):
    """
    What says where the panes of this window are.

    One object, and everything asks it: the container that draws, the
    title bars, and the key that moves the focus. That is the whole of
    Lillecarl/pymux#217 -- "which pane is beside this one" had two
    answers, one read off the last frame and one walked out of the
    tree, and they did not agree.

    A strip is not a sixth layout: every other one divides the window
    between the panes, and a strip gives each column a width of its own
    and scrolls. Lillecarl/pymux#198.

    The gaps are passed as a callable, because the gap between two
    stacked panes is two rows when a bar is drawn under a pane and one
    when it is not, and an option turns that on while the layout built
    here is still standing.
    """
    gaps = partial(the_gaps_of, pymux, window)

    if window.strip:
        return Strip(window, gaps)
    return Divided(window, gaps)


def _the_bar_below_is_drawn(pymux: "Pymux", window) -> bool:
    """
    Whether every pane of this window keeps a row under it.

    The bar under a pane names the pane above and the pane below it, so
    a window with no stack in it has nothing to put there and does not
    pay the row. A single pane and a plain row of panes look exactly as
    they did. Lillecarl/pymux#211.

    **Three places have to give the same answer**, or the rows drift:
    the padding between stacked panes, the row kept under the whole
    layout, and the float that draws the bar.
    """
    return pymux.show_pane_status and window.has_a_stack()


def the_gaps_of(pymux: "Pymux", window) -> Gaps:
    """
    The cells this window leaves between the things in it.

    One for the border a column owns, and two rows between stacked
    panes when a pane draws a bar below it and the pane under it draws
    one above. `_create_split` leaves the same rows for the same
    reason. Lillecarl/pymux#211.
    """
    return Gaps(
        between_columns=BORDER_WIDTH,
        between_panes=2 if _the_bar_below_is_drawn(pymux, window) else 1,
    )


def the_plan_of(pymux: "Pymux", window) -> Plan:
    """
    Where the panes of this window are, in cells.

    The rows the chrome takes come off the window first, because a plan
    holds panes and the chrome draws in the gaps between them.
    Lillecarl/pymux#217.

    **Measured on demand**, so a frame that asks about four sides of
    ten panes measures forty times. It is cheap arithmetic over a few
    rectangles, and it is the same arithmetic the container that draws
    does, over the same window: what it draws is what a title bar
    reads.
    """
    size = pymux.get_window_size()

    rows = size.rows
    if pymux.show_pane_status:
        rows -= 1
    if _the_bar_below_is_drawn(pymux, window):
        rows -= 1

    return the_layout_of(pymux, window).measure(
        Size(rows=max(1, rows), columns=size.columns),
    )


def the_weights_become_the_cells(pymux: "Pymux", window) -> None:
    """
    Write the size each thing has now into the weight that decides it.

    **This is what makes `resize-pane -U 1` mean one row.** A weight is
    a share of a split and not a number of cells, so a delta of one
    added to a share of one means nothing on a screen. Measure first,
    write the cells each child holds into its weight, and then a delta
    of one is one cell -- the weights add up to the room, so each
    child's share of it comes out as the number itself.

    The render used to do this on every frame
    (`report_write_position_callback`), which is how the arrangement
    came to hold four kinds of geometry at once. The plan decides now,
    and this is the one place that writes a weight back.

    tmux does the opposite and pays for it: `layout_resize_adjust`
    keeps absolute cells and moves them by a delta, so its rounding
    drifts over repeated resizes.
    """
    plan = the_plan_of(pymux, window)

    for split in window.splits:
        sideways = isinstance(split, arrangement.VSplit)

        for child in split:
            held = [plan.rect_of(pane) for pane in arrangement.panes_of(child)]
            if not held:
                continue

            box = bounding_box(held)
            split.weights[child] = box.width if sideways else box.height


def the_pane_resizes(
    pymux: "Pymux", window, pane: arrangement.Pane, up=0, right=0, down=0, left=0
) -> None:
    """
    Make one pane bigger or smaller, by that many cells on each side.

    **One door**, because a resize is two steps and the first one is
    easy to forget: the weights have to say what the panes measure now
    before a delta means anything. `resize-pane` and a program asking
    for a size both come through here.
    """
    the_weights_become_the_cells(pymux, window)
    window.change_size_for_pane(pane, up=up, right=right, down=down, left=left)


def the_pane_beside(
    pymux: "Pymux", window, pane: arrangement.Pane, side: Side
) -> "arrangement.Pane | None":
    """
    The pane on one side of this one. **One answer, for one window.**

    Two mechanisms answered this and they did not agree.
    `select-pane -L` read where the panes were drawn last frame, and a
    title bar cannot use that because it is drawn *during* a frame, so
    it walked the arrangement's tree instead. The tree named the top of
    a stack beside us; the frame named whichever pane the active one's
    own row ran into. Lillecarl/pymux#217.

    The plan answers, which is neither: it names the slot that shares
    most of our edge, and then the pane that slot shows.

    **A zoomed pane has nothing beside it**, because it covers the
    window. That is what both mechanisms already did, and saying it
    once here keeps them saying it.
    """
    if window.zoom:
        return None

    plan = the_plan_of(pymux, window)

    try:
        slot = plan.slot_of(pane)
    except KeyError:
        # A pane that is not in this window's plan: it closed, or it
        # belongs to another window. A bar is drawn on every frame, so
        # it may not be the thing that finds that out.
        return None

    beside = plan.neighbour(slot, side)
    return None if beside is None else beside.shown


def _short_name_of(pymux: "Pymux", pane: arrangement.Pane) -> str:
    """
    A pane in a few cells, for the title bar of the pane beside it.

    The program running in it, which is what `Pane.name` is, and a
    person chose it if they renamed the pane. Its title is the fallback,
    because a pane whose process has ended has no name left.
    """
    return pane.name or format_pymux_string(pymux, PANE_TITLE_FORMAT, pane=pane).strip()


def _create_container_for_process(
    pymux: "Pymux",
    window: arrangement.Window,
    arrangement_pane: arrangement.Pane,
    zoom: bool = False,
):
    """
    Create a `Container` with a titlebar for a process.
    """

    @Condition
    def clock_is_visible() -> bool:
        return arrangement_pane.clock_mode

    @Condition
    def pane_numbers_are_visible() -> bool:
        return pymux.display_pane_numbers

    terminal_is_focused = has_focus(arrangement_pane.terminal)

    def get_terminal_style() -> str:
        if terminal_is_focused():
            result = "class:terminal.focused"
        else:
            result = "class:terminal"
        return result

    def get_titlebar_text_fragments() -> StyleAndTextTuples:
        result: StyleAndTextTuples = []

        if zoom:
            result.append(("class:titlebar-zoom", " Z "))

        if arrangement_pane.process.is_terminated:
            result.append(("class:terminated", " Terminated "))

        # A pane whose program is suspended while a person reads its
        # history says so. Where in the history they are is drawn in
        # the corner of the pane itself, by the widget that knows.
        if arrangement_pane.is_copying:
            result.append(("class:copymode", " Copy "))

        if arrangement_pane.name:
            result.append(("class:name", " %s " % arrangement_pane.name))

        # **Padded on both sides, and not on one.** The bar centres
        # this, so a space that hangs off the end of it moves the title
        # off the middle of the pane by half of that space. The title
        # used to be drawn from the left, where a trailing space costs
        # nothing. Lillecarl/pymux#207.
        title = format_pymux_string(
            pymux, PANE_TITLE_FORMAT, pane=arrangement_pane
        ).strip()

        if title:
            result.append(("", " %s " % title))

        return result

    def get_pane_index() -> str:
        try:
            w = pymux.arrangement.get_active_window()
            index = w.get_pane_index(arrangement_pane)
        except ValueError:
            index = "/"

        return "%3s " % index

    def a_neighbour(on_the_left: bool) -> StyleAndTextTuples:
        """
        The name of the pane on one side of this one.

        Nothing when there is none. A zoomed pane covers the window,
        so nothing is beside it either.
        """
        if zoom:
            return []

        pane = the_pane_beside(
            pymux,
            window,
            arrangement_pane,
            Side.LEFT if on_the_left else Side.RIGHT,
        )

        if pane is None:
            return []

        name = _short_name_of(pymux, pane)
        if on_the_left:
            return [("class:neighbour", "%s %s " % (LEFT_MARK, name))]
        return [("class:neighbour", " %s %s" % (name, RIGHT_MARK))]

    def get_the_number_of_the_pane() -> StyleAndTextTuples:
        """
        The pane's own number, at the far left of its bar.

        **It moved there from the right edge**, because the right edge
        now belongs to the right neighbour. Lillecarl/pymux#207.
        """
        return [("class:paneindex", get_pane_index())]

    def get_the_left_of_the_bar() -> StyleAndTextTuples:
        return a_neighbour(on_the_left=True)

    def get_the_right_of_the_bar() -> StyleAndTextTuples:
        return a_neighbour(on_the_left=False)

    def get_the_bar_below() -> StyleAndTextTuples:
        """
        What is above this pane and what is below it, in the middle.

        The bar over a pane names the panes beside it at its two edges,
        and there is no room on it for two more names. So the panes a
        stack puts above and below go on a bar of their own, under the
        pane, and they go in the middle of it together: two marks that
        point the way they mean, and one gap between them.
        Lillecarl/pymux#211.
        """
        if zoom:
            return []

        above = the_pane_beside(pymux, window, arrangement_pane, Side.ABOVE)
        below = the_pane_beside(pymux, window, arrangement_pane, Side.BELOW)
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
        arrangement_pane.clock_mode = False
        pymux.arrangement.get_active_window().active_pane = arrangement_pane
        pymux.invalidate(Woke.A_CLICK_LEFT_THE_CLOCK)

    return HighlightBordersIfActive(
        window,
        arrangement_pane,
        get_terminal_style,
        FloatContainer(
            HSplit(
                [
                    # The terminal.
                    TracePaneWritePosition(
                        pymux, arrangement_pane, content=arrangement_pane.terminal
                    ),
                ]
            ),
            #
            floats=[
                # The title bar: this pane's title in the middle, and
                # the panes beside it named at the edges.
                # Lillecarl/pymux#207.
                Float(
                    content=ConditionalContainer(
                        content=Window(
                            height=1,
                            content=PaneTitleBar(
                                get_number=get_the_number_of_the_pane,
                                get_left=get_the_left_of_the_bar,
                                get_middle=get_titlebar_text_fragments,
                                get_right=get_the_right_of_the_bar,
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
                            content=PaneTitleBar(
                                get_number=nothing,
                                get_left=nothing,
                                get_middle=get_the_bar_below,
                                get_right=nothing,
                            ),
                            style="class:titlebar",
                        ),
                        filter=Condition(
                            lambda: _the_bar_below_is_drawn(pymux, window)
                        ),
                    ),
                    left=0,
                    right=0,
                    bottom=-1,
                    height=1,
                    z_index=Z_INDEX.WINDOW_TITLE_BAR,
                ),
                # The clock.
                Float(
                    content=ConditionalContainer(
                        BigClock(on_click), filter=clock_is_visible
                    )
                ),
                # Pane number.
                Float(
                    content=ConditionalContainer(
                        content=PaneNumber(pymux, arrangement_pane),
                        filter=pane_numbers_are_visible,
                    )
                ),
            ],
        ),
    )


class _ContainerProxy(Container):
    def __init__(self, content: Container) -> None:
        self.content = content

    def reset(self) -> None:
        self.content.reset()

    def preferred_width(self, max_available_width: int) -> D:
        return self.content.preferred_width(max_available_width)

    def preferred_height(self, width: int, max_available_height: int) -> D:
        return self.content.preferred_height(width, max_available_height)

    def write_to_screen(
        self,
        screen: Screen,
        mouse_handlers: MouseHandlers,
        write_position: WritePosition,
        parent_style: str,
        erase_bg: bool,
        z_index: int | None,
    ) -> None:
        self.content.write_to_screen(
            screen, mouse_handlers, write_position, parent_style, erase_bg, z_index
        )

    def get_children(self) -> List[Container]:
        return [self.content]


_focused_border_titlebar = "┃"
_focused_border_vertical = "┃"
_focused_border_horizontal = "━"
_focused_border_left_top = "┏"
_focused_border_right_top = "┓"
_focused_border_left_bottom = "┗"
_focused_border_right_bottom = "┛"

_border_left_bottom = "└"
_border_right_bottom = "┘"
_border_left_top = "┌"
_border_right_top = "┐"


class HighlightBordersIfActive:
    """
    Put borders around this control if active.
    """

    def __init__(self, window, pane, style, content):
        @Condition
        def is_selected() -> bool:
            return window.active_pane == pane

        def conditional_float(
            char, left=None, right=None, top=None, bottom=None, width=None, height=None
        ):
            return Float(
                content=ConditionalContainer(
                    Window(char=char, style="class:border"), filter=is_selected
                ),
                left=left,
                right=right,
                top=top,
                bottom=bottom,
                width=width,
                height=height,
                z_index=Z_INDEX.HIGHLIGHTED_BORDER,
            )

        self.container = FloatContainer(
            content,
            style=style,
            floats=[
                # Sides.
                conditional_float(
                    _focused_border_vertical, left=-1, top=0, bottom=0, width=1
                ),
                conditional_float(
                    _focused_border_vertical, right=-1, top=0, bottom=0, width=1
                ),
                # conditional_float(
                #    _focused_border_horizontal, left=0, right=0, top=-1, height=1
                # ),
                # conditional_float(
                #    _focused_border_horizontal, left=0, right=0, bottom=-1, height=1
                # ),
                # Corners.
                conditional_float(
                    _focused_border_left_top, left=-1, top=-1, width=1, height=1
                ),
                conditional_float(
                    _focused_border_right_top, right=-1, top=-1, width=1, height=1
                ),
                # conditional_float(
                #    _focused_border_left_bottom, left=-1, bottom=-1, width=1, height=1
                # ),
                # conditional_float(
                #    _focused_border_right_bottom, right=-1, bottom=-1, width=1, height=1
                # ),
            ],
        )

    def __pt_container__(self) -> Container:
        return self.container


class TracePaneWritePosition(_ContainerProxy):  # XXX: replace with SizedBox
    "Trace the write position of this pane."

    def __init__(
        self, pymux: "Pymux", arrangement_pane: arrangement.Pane, content
    ) -> None:
        content = to_container(content)
        _ContainerProxy.__init__(self, content)

        self.pymux = pymux
        self.arrangement_pane = arrangement_pane

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


def focus_left(pymux: "Pymux") -> None:
    "Move focus to the left."
    _move_focus(pymux, Side.LEFT)


def focus_right(pymux: "Pymux") -> None:
    "Move focus to the right."
    _move_focus(pymux, Side.RIGHT)


def focus_down(pymux: "Pymux") -> None:
    "Move focus down."
    _move_focus(pymux, Side.BELOW)


def focus_up(pymux: "Pymux") -> None:
    "Move focus up."
    _move_focus(pymux, Side.ABOVE)


def _move_focus(pymux: "Pymux", side: Side) -> None:
    """
    Move the focus one pane that way, and stay where it is at the edge.

    **The plan says which pane that is, and the same words as the title
    bar do.** This read the last frame instead: where each pane was
    drawn, then two cells past its edge, then whichever pane held that
    cell. A key and a bar that name different panes are two bugs
    waiting, and before the first frame the key did nothing at all.
    Lillecarl/pymux#217.
    """
    window = pymux.arrangement.get_active_window()

    if window.active_pane is None:
        return

    beside = the_pane_beside(pymux, window, window.active_pane, side)
    if beside is not None:
        window.active_pane = beside
