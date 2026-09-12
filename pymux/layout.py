# encoding: utf-8
"""
The layout engine. This builds the prompt_toolkit layout.
"""

import argparse
import datetime
import weakref
from functools import partial
from typing import TYPE_CHECKING, Callable, Dict, List, Tuple

from prompt_toolkit.application import Application, get_app
from prompt_toolkit.clipboard import ClipboardData
from prompt_toolkit.data_structures import Point, Size
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
from .plane import Plan, Side, View, bounding_box
from .strip import Strip
from .tiling import BORDER_WIDTH, Gaps
from .zoomed import Zoomed
from .titlebar import PaneTitleBar

if TYPE_CHECKING:
    from prompt_toolkit.layout.controls import BufferControl, NotImplementedOrNone

    from pymux.main import Pymux

__all__ = ["LayoutManager"]


def option_value_of(pymux, name: str) -> str:
    """
    What one option holds, spelled the way set-option with no value
    reads it back. A window option reads the window that is active.

    Imported here, and not at the top: options.py reads this module
    for Justify, so this module may not read options.py while it
    loads.
    """
    from pymux.commands.common import option_as_written
    from pymux.options import ALL_WINDOW_OPTIONS

    window_kind = name in ALL_WINDOW_OPTIONS
    table = pymux.window_options if window_kind else pymux.options
    option = table.get(name)
    if option is None:
        return ""
    return option_as_written(pymux, option, argparse.Namespace(g=False), window=window_kind)


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

#: What marks a pane that runs off the edge of the view. It goes into
#: the style of the pane itself, so every cell the program left at the
#: default background takes the tint the theme names (`style.py`) and
#: every cell it coloured itself keeps that colour: a parent's class is
#: less precise than a cell's own `bg:`.
#:
#: **That is the cheap half of a choice, and it is deliberate.** The
#: other reading is to blend the tint into whatever each cell holds,
#: which costs arithmetic on every cell of the column on every frame:
#: marking them by hand came to 14,620 bytecode instructions a frame,
#: 43% on top of the frame it marked. Lillecarl/pymux#222.
CUT_IS_TINTED = "class:cut"

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

    `paint_screen` colours the whole area with the theme's background as
    well; the dots keep their colour, because the pattern is what says a
    client here disagrees. Lillecarl/pymux#273.
    """

    def __init__(self, painted=lambda: False) -> None:
        self._painted = painted

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
        whole = "class:background"
        if self._painted():
            whole += " class:painted"
        default_char = Char(" ", whole)
        dot = Char(".", whole)

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

    def __init__(self, pymux: "Pymux", on_click: Callable[[], None]):
        self.pymux = pymux
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

            # Display time. Test-mode pins the clock, so a picture
            # of a pane in clock-mode cannot race it.
            now = self.pymux.displayed_now()
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

        #: What a `DynamicContainer` gets when there is nothing to draw.
        #:
        #: **One window, and never a fresh one.** A bare `Window()`
        #: makes a `DummyControl` of its own, and prompt_toolkit keys
        #: the key bindings of the whole application by the set of
        #: controls it can find (`_CombinedRegistry._key_bindings`).
        #: A new control on every walk is a cache key nothing matches,
        #: so every key press rebuilt the whole binding table.
        #: Lillecarl/pymux#233.
        self._nothing_to_draw = Window()

        # The window a person types a command into, and the box that
        # holds it when the palette is on. Both are built once and kept.
        # A fresh `BufferControl` on every render is one that the layout
        # never focused, so the cursor is drawn nowhere and the person
        # cannot see where they are typing. Lillecarl/pymux#158.
        self._command_window: Window | None = None
        self._palette: Container | None = None

        # The keys a prefix leads to, when `which-key` is on. Built
        # once, like the two above: a window built fresh each frame is
        # one the layout never focused. Nothing here takes focus, but
        # the same rule keeps the box one object. Lillecarl/pymux#29.
        self._which_key: Container | None = None

        # The windows of the session, to choose from. Built once,
        # like the boxes above, for the same reason: a window built
        # fresh each frame is one the layout never focused.
        # Lillecarl/pymux#295.
        self._chooser: Container | None = None
        self._chooser_rows: Window | None = None
        self._chooser_search: Container | None = None
        self._menu: Container | None = None
        self._menu_rows: Window | None = None

        # And the same two for the prompt: the line a person answers a
        # question on, and the box that holds it when the question
        # knows its answers. Lillecarl/pymux#220.
        self._prompt_window_container: Window | None = None
        self._key_prompt: Container | None = None

        # The part of the screen that holds the windows. It is kept
        # because it is what knows the container that drew the panes,
        # and a title bar drawn inside that frame asks for its plan.
        self._body = DynamicBody(self.pymux)

        #: The plan of the frame being drawn: the window, the size it
        #: was measured for, and the plan itself. Everything drawn in
        #: one frame asks the same question, so it is worked out once.
        #: `forget_the_plan` says when it goes. Lillecarl/pymux#217.
        self._plan_of_the_frame: "Tuple[object, Size, Plan] | None" = None

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

    @property
    def room_this_client_has(self) -> Size:
        """
        How much of this client's own terminal a window may use.

        The status line comes off the bottom, the way it does for the
        plane. **This is not the plane**: the plane is one size for
        every client watching a window, and this is one client's
        screen. They differ as soon as two clients of different sizes
        watch the same window, and everything a person sees at their
        own size -- the box the command palette draws in, the overlay,
        how much of the plane fits -- reads this one.
        """
        size = self.client_state.app.output.get_size()
        rows = size.rows - (1 if self.pymux.show_status else 0)
        return Size(rows=max(1, rows), columns=size.columns)

    def room_for_the_body(self) -> Size:
        """
        How big the part of the screen that holds the windows is.

        The smaller of the plane and this client, on each axis. A
        client bigger than the plane draws background around it, which
        is what every client but the smallest one does today. A client
        smaller than the plane shows part of it and its view scrolls,
        which is what `window-size largest` is for.
        """
        plane = self.pymux.size_of_the_plane()
        mine = self.room_this_client_has

        return Size(
            rows=min(plane.rows, mine.rows),
            columns=min(plane.columns, mine.columns),
        )

    def pane_container(self) -> "PlanContainer | None":
        """
        The container that drew the panes of the window this client
        shows, or `None` before it has built one.

        It holds the plan of the frame it drew, which is what a title
        bar reads instead of measuring the window again.
        """
        return self._body.panes()

    def plan_of_this_frame(self, window, size: Size) -> "Plan | None":
        """
        The plan already worked out for this window, this frame.

        `None` when there is none, or when it was worked out for
        another window or another size. A frame asks this question
        four times for every pane it draws -- what is to my left, my
        right, above and below -- and the answer is one plan.
        """
        if self._plan_of_the_frame is None:
            return None

        drawn_for, drawn_at, plan = self._plan_of_the_frame
        if drawn_for is not window or drawn_at != size:
            return None

        return plan

    def remember_the_plan(self, window, size: Size, plan: Plan) -> None:
        "Keep this plan for the rest of the frame."
        self._plan_of_the_frame = (window, size, plan)

    def forget_the_plan(self) -> None:
        """
        Throw away the plan of the frame just drawn.

        **Anything that changes a window calls this**, through
        `Pymux.invalidate`, and so does the start of every frame. A
        plan is only an answer while the window it measured is the
        window that is there.
        """
        self._plan_of_the_frame = None

    def before_a_frame(self) -> None:
        """
        Get ready to draw.

        There are no write positions to clear: every render writes to a
        new screen and the positions go with it. What does have to go
        is the plan of the frame before this one, so that the first
        question of this frame measures the window as it is now.
        """
        self.forget_the_plan()

    def display_popup(self, title: str, content: str) -> None:
        """
        Display a pop-up dialog.
        """
        self.popup_dialog.title = title
        self._popup_textarea.text = content
        self.client_state.display_popup = True
        get_app().layout.focus(self._popup_textarea)

    def display_chooser(self, template: str = "") -> None:
        """
        Show the windows of the session, to choose from.

        It opens with the chooser on the window this client looks
        at, the way tmux's tree opens on the current one. A template
        is the tmux `%%`: the command to run on the window the
        chooser points at, instead of switching to it.
        Lillecarl/pymux#295.
        """
        windows = self.pymux.arrangement.windows
        active = self.pymux.arrangement.get_active_window()
        self.client_state.display_popup = False
        self.client_state.choose_window = True
        self.client_state.choose_buffer = False
        self.client_state.choose_options = False
        self.client_state.menu_entries = []
        self.client_state.choose_window_command = template
        self.client_state.choose_window_filter.reset()
        self.client_state.choose_window_index = (
            windows.index(active) if active in windows else 0
        )
        self._chooser_box()
        get_app().layout.focus(self._chooser_rows)

    def display_buffer_chooser(self) -> None:
        """
        Show the named buffers, to choose from.

        Enter makes the chosen buffer the session's one buffer, which
        is what `paste-buffer` pastes -- the thing tmux's
        choose-buffer marks current. Lillecarl/pymux#304.
        """
        self.client_state.display_popup = False
        self.client_state.choose_buffer = True
        self.client_state.choose_window = False
        self.client_state.choose_options = False
        self.client_state.menu_entries = []
        self.client_state.choose_window_command = ""
        self.client_state.choose_window_filter.reset()
        self.client_state.choose_window_index = 0
        self._chooser_box()
        get_app().layout.focus(self._chooser_rows)

    def display_options_chooser(self) -> None:
        """
        The options of the session, to choose from: what
        `customize-mode` is here. Enter asks for a value on the
        prompt, with the option named and what it holds as the
        default, which is the editing half of what tmux's
        customize-mode does. Lillecarl/pymux#297.
        """
        self.client_state.display_popup = False
        self.client_state.choose_options = True
        self.client_state.choose_window = False
        self.client_state.choose_buffer = False
        self.client_state.menu_entries = []
        self.client_state.choose_window_command = ""
        self.client_state.choose_window_filter.reset()
        self.client_state.choose_window_index = 0
        self._chooser_box()
        get_app().layout.focus(self._chooser_rows)

    def display_menu(self, entries: list, title: str = "") -> None:
        """
        The menu of `display-menu`: a line per entry, with the key
        that runs it beside the name. The box draws where the other
        boxes draw; tmux takes a position, and -x and -y are accepted
        for it and change nothing. Lillecarl/pymux#297.
        """
        self.client_state.display_popup = False
        self.client_state.choose_window = False
        self.client_state.choose_buffer = False
        self.client_state.menu_entries = entries
        self.client_state.menu_title = title
        self._menu_box()
        get_app().layout.focus(self._menu_rows)

    def close_menu(self) -> None:
        "The menu goes, without running anything."
        self.client_state.menu_entries = []

    def menu_key_pressed(self, key, data=None) -> None:
        """
        A key while the menu shows: the entry whose key it is runs,
        and a key that is nobody's stays in the menu, which is modal.
        The spelling of a named key is tmux's -- Enter, Space, Tab.
        """
        if isinstance(key, str) and key in ("<enter>", "<space>", "<tab>"):
            spelling = {"<enter>": "Enter", "<space>": "Space", "<tab>": "Tab"}[key]
        elif data:
            spelling = data
        else:
            spelling = key if isinstance(key, str) else str(key)

        for entry in self.client_state.menu_entries:
            if entry[0].lower() == spelling.lower():
                self._run_menu_entry(entry)
                return

    def _run_menu_entry(self, entry) -> None:
        _key, _name, command = entry
        self.close_menu()
        self.pymux.handle_command(command)

    def _create_menu_click_handler(self, entry) -> Callable[[MouseEvent], "NotImplementedOrNone"]:
        "Return a mouse handler that runs the entry when clicking."

        def handler(mouse_event: MouseEvent) -> "NotImplementedOrNone":
            if mouse_event.event_type == MouseEventType.MOUSE_DOWN:
                self._run_menu_entry(entry)
                return None
            else:
                return NotImplemented  # Event not handled here.

        return handler

    def _menu_tokens(self) -> StyleAndTextTuples:
        """
        The entries of the menu, one row each: the key that runs it,
        and its name. A row answers a click the way a chooser's row
        does. Lillecarl/pymux#297.
        """
        result: StyleAndTextTuples = []
        for key, name, _command in self.client_state.menu_entries:
            handler = self._create_menu_click_handler((key, name, _command))
            result.append(("class:chooser.hint", " %s " % (key or " "), handler))
            result.append(("", "%s\n" % name, handler))
        return result

    def _menu_box(self) -> Container:
        """
        The menu, in a box. Built once, for the reason
        `_command_line_window` gives; the title is asked each frame,
        because the command that opened the menu names it.
        Lillecarl/pymux#297.
        """
        if self._menu is not None:
            return self._menu

        self._menu_rows = Window(
            content=FormattedTextControl(
                self._menu_tokens,
                focusable=True,
                show_cursor=False,
            ),
            style="class:commandpalette",
        )
        self._menu = HSplit(
            [
                Window(
                    height=1,
                    align=WindowAlign.CENTER,
                    content=FormattedTextControl(
                        lambda: [
                            (
                                "class:commandpalette.title",
                                " %s " % (self.client_state.menu_title or "Menu"),
                            )
                        ]
                    ),
                    style="class:commandpalette.titlebar",
                ),
                self._menu_rows,
            ],
        )
        return self._menu

    def chooser_matches(self) -> list:
        """
        The rows the chooser lists. The search narrows them: a row
        stays when what was typed is in its name or in its index,
        without case. Lillecarl/pymux#295.
        """
        text = self.client_state.choose_window_filter.text.lower()
        if self.client_state.choose_options:
            from pymux.options import ALL_OPTIONS, ALL_WINDOW_OPTIONS

            names = sorted(set(ALL_OPTIONS) | set(ALL_WINDOW_OPTIONS))
            if not text:
                return names
            return [
                name
                for name in names
                if text in name.lower()
                or text in option_value_of(self.pymux, name).lower()
            ]
        if self.client_state.choose_buffer:
            buffers = self.pymux.named_buffers
            if not text:
                return sorted(buffers)
            return [
                name
                for name in sorted(buffers)
                if text in name.lower() or text in str(len(buffers[name]))
            ]
        windows = self.pymux.arrangement.windows
        if not text:
            return list(windows)
        return [
            w for w in windows if text in w.name.lower() or text in str(w.index)
        ]

    def choose_the_pointed_buffer(self) -> None:
        """
        Make the buffer the chooser points at the session's one
        buffer, which is what `paste-buffer` pastes. The chooser
        closes. Lillecarl/pymux#304.
        """
        matches = self.chooser_matches()
        self.client_state.choose_buffer = False
        self.client_state.choose_options = False
        if not matches:
            return
        index = min(self.client_state.choose_window_index, len(matches) - 1)
        text = self.pymux.named_buffers[matches[index]]
        self.pymux.clipboard.set_data(ClipboardData(text))
        self.pymux.invalidate(Woke.CLICK_CHOSE_A_BUFFER)

    def choose_the_pointed_window(self) -> None:
        """
        Switch to the window the chooser points at, or run the
        template of the `choose-window` command on it. The chooser
        closes either way. Lillecarl/pymux#295.
        """
        matches = self.chooser_matches()
        self.client_state.choose_window = False
        self.client_state.choose_options = False
        if not matches:
            return
        index = min(self.client_state.choose_window_index, len(matches) - 1)
        window = matches[index]
        template = self.client_state.choose_window_command
        if template:
            self.pymux.handle_command(template.replace("%%", ":%i" % window.index))
        else:
            self.pymux.arrangement.set_active_window(window)
            self.pymux.invalidate(Woke.CLICK_CHOSE_A_WINDOW)

    def choose_the_pointed_option(self) -> None:
        """
        Ask what the option the chooser points at should hold, on the
        prompt, with the command named and what it holds as the
        default answer. The chooser closes.
        Lillecarl/pymux#297.
        """
        matches = self.chooser_matches()
        self.client_state.choose_options = False
        self.client_state.choose_window = False
        self.client_state.choose_buffer = False
        if not matches:
            return
        index = min(self.client_state.choose_window_index, len(matches) - 1)
        name = matches[index]
        self.pymux.handle_command(
            "command-prompt -p 'set-option %s' -I '%s' 'set-option %s %%'"
            % (name, option_value_of(self.pymux, name), name)
        )

    def _create_select_window_handler(
        self, window: arrangement.Window
    ) -> Callable[[MouseEvent], "NotImplementedOrNone"]:
        "Return a mouse handler that selects the given window when clicking."

        def handler(mouse_event: MouseEvent) -> "NotImplementedOrNone":
            if mouse_event.event_type == MouseEventType.MOUSE_DOWN:
                self.pymux.arrangement.set_active_window(window)
                self.pymux.invalidate(Woke.CLICK_CHOSE_A_WINDOW)
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

        **The panes are this client's window's, and no others.** It
        used to read `#T` for every pane of every window. That was free
        on the four second tick and is not: a pane's write asks the
        other clients this question now (Lillecarl/pymux#224), so a
        session of eight windows read eight windows of titles every
        time any pane wrote.

        Narrowing it loses nothing, because `_get_status_tokens` above
        already formats `window-status-format` for every window. So
        whatever this client draws *about* another window -- its name,
        its flags, and `#T` if a person put it in that format -- is
        already in `parts`. What is left is the titlebars of the panes
        this client can see, and a `clock-mode` pane, and both of those
        are in the window it looks at. Lillecarl/pymux#251.
        """
        pymux = self.pymux
        parts: List[str] = []

        if pymux.show_status:
            parts.append(self._get_status_left_tokens())
            parts.append(self._get_status_right_tokens())
            parts += [text for _, text, *_ in self._get_status_tokens()]

        for pane in self._panes_in_view():
            if pane.clock_mode:
                parts.append(pymux.displayed_now().strftime(CLOCK_FORMAT))
            elif pymux.show_pane_status:
                parts.append(format_pymux_string(pymux, PANE_TITLE_FORMAT, pane=pane))

        return tuple(parts)

    def _panes_in_view(self) -> List:
        "The panes of the window this client looks at, or none."
        try:
            return list(self.pymux.arrangement.get_active_window().panes)
        except IndexError:
            # `get_active_window_for` ends at `windows[0]`, and there
            # may be no window at all: the refresh ticks before the
            # first one is made.
            return []

    def _get_align(self) -> WindowAlign:
        if self.pymux.status_justify == Justify.RIGHT:
            return WindowAlign.RIGHT
        elif self.pymux.status_justify == Justify.CENTER:
            return WindowAlign.CENTER
        else:
            return WindowAlign.LEFT

    def _before_prompt_command_tokens(self) -> StyleAndTextTuples:
        if self.client_state.prompt_completer is not None:
            # The box says what it is asking for, on the row above the
            # line. Saying it twice is one row wasted and one thing to
            # read. Lillecarl/pymux#220.
            return []
        return [("class:commandline.prompt", "%s " % (self.client_state.prompt_text,))]

    def _overlay_container(self) -> Container:
        """
        The overlay pane of this client, with a title bar above it.

        The container is built again when the pane changes, and kept
        while it stays, so that the terminal keeps its state.
        """
        pane = self.pymux.overlay_pane

        if pane is None:
            return self._nothing_to_draw

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

    def _prompt_window(self) -> Container:
        """
        The window that holds the answer to a `command-prompt`.

        The bar at the bottom and the box in the middle draw the same
        window, for the same reason the ":" line does, and it is built
        once for the same reason as well.
        """
        if self._prompt_window_container is not None:
            return self._prompt_window_container

        self._prompt_window_container = Window(
            height=1,
            style="class:commandline",
            content=BufferControl(
                buffer=self.client_state.prompt_buffer,
                input_processors=[
                    BeforeInput(self._before_prompt_command_tokens),
                    AppendAutoSuggestion(),
                    HighlightSelectionProcessor(),
                ],
            ),
            z_index=Z_INDEX.COMMAND_LINE,
        )
        return self._prompt_window_container

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

        self._palette = self._box(lambda: " Command ", self._command_line_window())
        return self._palette

    def _box(self, title, window: Container) -> Container:
        """
        A box in the middle of the screen: a title, a line to type in,
        and the completions filling what is left.

        Two things draw one. The ":" command line does, when a person
        asked for a palette; and a prompt that knows its answers does,
        which today is the box that composes a key.
        Lillecarl/pymux#220.

        `title` is asked each frame, so a box can say what it is asking
        for rather than what kind of box it is.
        """
        return HSplit(
            [
                Window(
                    height=1,
                    align=WindowAlign.CENTER,
                    content=FormattedTextControl(
                        lambda: [("class:commandpalette.title", title())]
                    ),
                    style="class:commandpalette.titlebar",
                ),
                window,
                self._palette_completions(),
            ],
            style="class:commandpalette",
        )

    def _key_box(self) -> Container:
        """
        The prompt as a box, for a question that knows its answers.

        The line inside it is the same `prompt_buffer` the bottom
        toolbar uses. Only the room around it is different, and the
        room is what the completions need: a person composing
        "ctrl+shift+f5" is reading the list, not typing from memory.

        Built once, for the reason `_command_line_window` gives.
        """
        if self._key_prompt is not None:
            return self._key_prompt

        self._key_prompt = self._box(
            lambda: " %s " % (self.client_state.prompt_text or "Key",),
            self._prompt_window(),
        )
        return self._key_prompt

    def _which_key_box(self) -> Container:
        """
        The keys the prefix leads to, in a box.

        Built once, for the reason `_command_line_window` gives.
        Lillecarl/pymux#29.
        """
        if self._which_key is not None:
            return self._which_key

        self._which_key = HSplit(
            [
                Window(
                    height=1,
                    align=WindowAlign.CENTER,
                    content=FormattedTextControl(
                        lambda: [("class:commandpalette.title", " Prefix ")]
                    ),
                    style="class:commandpalette.titlebar",
                ),
                Window(
                    content=FormattedTextControl(self._which_key_tokens),
                    height=lambda: D(min=1, max=self._palette_rows() - 1),
                    style="class:commandpalette",
                ),
            ],
        )
        return self._which_key

    def _which_key_tokens(self) -> StyleAndTextTuples:
        """
        The keys the prefix leads to, one row each.

        Two columns, the key and the command it runs, the key column
        one width for the whole list so the meanings line up under
        each other. A prefix that leads nowhere says so rather than
        drawing an empty box. Lillecarl/pymux#29.
        """
        rows = self.pymux.key_bindings_manager.keys_a_prefix_leads_to()
        if not rows:
            return [("class:commandpalette", " No keys follow the prefix. ")]

        width = max(len(key) for key, _meaning in rows) + 2
        tokens: StyleAndTextTuples = []
        for key, meaning in rows:
            tokens.append(("class:which-key.key", key.ljust(width)))
            tokens.append(("class:commandpalette", meaning + "\n"))
        return tokens

    def _chooser_box(self) -> Container:
        """
        What a chooser lists, in a box: the windows, the named
        buffers or the options, by the kind that is showing. One box
        for the three of them, because they are one thing: the
        title, the rows and the search are shared, and only what the
        rows say and what taking a row does differ.
        Lillecarl/pymux#295. Lillecarl/pymux#304. Lillecarl/pymux#297.

        Built once, for the reason `_command_line_window` gives.
        """
        if self._chooser is not None:
            return self._chooser

        rows = Window(
            content=FormattedTextControl(
                self._chooser_tokens,
                focusable=True,
                show_cursor=False,
                get_cursor_position=lambda: Point(
                    0, self.client_state.choose_window_index
                ),
            ),
            height=lambda: D(min=1, max=self._palette_rows() - 1),
            style="class:commandpalette",
        )
        search = VSplit(
            [
                Window(
                    width=2,
                    height=1,
                    content=FormattedTextControl(
                        lambda: [("class:chooser.hint", "/ ")]
                    ),
                    style="class:commandpalette",
                ),
                Window(
                    content=BufferControl(
                        buffer=self.client_state.choose_window_filter
                    ),
                    height=1,
                    style="class:commandpalette",
                ),
            ]
        )
        self._chooser = HSplit(
            [
                Window(
                    height=1,
                    align=WindowAlign.CENTER,
                    content=FormattedTextControl(
                        lambda: [
                            ("class:commandpalette.title", " %s " % self._chooser_title())
                        ]
                    ),
                    style="class:commandpalette.titlebar",
                ),
                rows,
                search,
            ],
        )
        self._chooser_rows = rows
        self._chooser_search = search
        return self._chooser

    def _chooser_title(self) -> str:
        "What the box of the chooser says it is."
        if self.client_state.choose_options:
            return "Customize"
        if self.client_state.choose_buffer:
            return "Choose a buffer"
        return "Choose a window"

    def _chooser_tokens(self) -> StyleAndTextTuples:
        "The rows of the chooser, by the kind that shows."
        if self.client_state.choose_options:
            return self._choose_options_tokens()
        if self.client_state.choose_buffer:
            return self._choose_buffer_tokens()
        return self._choose_window_tokens()

    def _choose_options_tokens(self) -> StyleAndTextTuples:
        """
        The options of the session, one row each: the name, and what
        it holds. A window option says which window it reads.
        Lillecarl/pymux#297.
        """
        from pymux.options import ALL_WINDOW_OPTIONS

        matches = self.chooser_matches()
        if not matches:
            return [("class:chooser.hint", " No option matches. ")]

        chosen = min(self.client_state.choose_window_index, len(matches) - 1)
        tokens: StyleAndTextTuples = []
        for i, name in enumerate(matches):
            style = "class:chooser.selected" if i == chosen else "class:commandpalette"
            value = option_value_of(self.pymux, name)
            kind = " (window)" if name in ALL_WINDOW_OPTIONS else ""
            tokens.append(
                (
                    style,
                    "%s%s = %s%s\n"
                    % ("> " if i == chosen else "  ", name, value, kind),
                    self._create_chooser_click_handler(i),
                )
            )
        return tokens

    def _choose_buffer_tokens(self) -> StyleAndTextTuples:
        """
        The named buffers, one row each: the name, and how much it
        holds. The row the chooser points at carries the gutter arrow
        and stands out, and a row answers a click the way a window's
        row does. Lillecarl/pymux#304.
        """
        matches = self.chooser_matches()
        if not matches:
            return [("class:chooser.hint", " No buffer matches. ")]

        chosen = min(self.client_state.choose_window_index, len(matches) - 1)
        tokens: StyleAndTextTuples = []
        for i, name in enumerate(matches):
            style = "class:chooser.selected" if i == chosen else "class:commandpalette"
            tokens.append(
                (
                    style,
                    "%s%-24s %i\n"
                    % (
                        "> " if i == chosen else "  ",
                        name,
                        len(self.pymux.named_buffers[name]),
                    ),
                    self._create_buffer_click_handler(i),
                )
            )
        return tokens

    def _create_buffer_click_handler(
        self, row: int
    ) -> Callable[[MouseEvent], "NotImplementedOrNone"]:
        "Return a mouse handler that chooses the buffer on this row."

        def handler(mouse_event: MouseEvent) -> "NotImplementedOrNone":
            if mouse_event.event_type == MouseEventType.MOUSE_DOWN:
                self.client_state.choose_window_index = row
                self.choose_the_pointed_buffer()
                return None
            return NotImplemented

        return handler

    def _choose_window_tokens(self) -> StyleAndTextTuples:
        """
        The windows of the session, one row each.

        The row the chooser points at carries the gutter arrow and
        stands out, and the window this client already looks at says
        so, the way `list-windows` does. A window that holds more
        panes than one says how many. A row answers a click the way
        a column of the strip does. Lillecarl/pymux#295.
        """
        matches = self.chooser_matches()
        if not matches:
            return [("class:chooser.hint", " No window matches. ")]

        active = self.pymux.arrangement.get_active_window()
        chosen = min(self.client_state.choose_window_index, len(matches) - 1)
        tokens: StyleAndTextTuples = []
        for i, window in enumerate(matches):
            style = "class:chooser.selected" if i == chosen else "class:commandpalette"
            suffix = ""
            if len(window.panes) > 1:
                suffix = " (%i panes)" % len(window.panes)
            if window == active:
                suffix += " (active)"
            tokens.append(
                (
                    style,
                    "%s%2i %s%s\n"
                    % (
                        "> " if i == chosen else "  ",
                        window.index,
                        window.name,
                        suffix,
                    ),
                    self._create_chooser_click_handler(i),
                )
            )
        return tokens

    def _create_chooser_click_handler(
        self, row: int
    ) -> Callable[[MouseEvent], "NotImplementedOrNone"]:
        """
        Return a mouse handler that takes the row: the window it
        names, the buffer it names, or the option it names, by the
        kind that shows.
        """

        def handler(mouse_event: MouseEvent) -> "NotImplementedOrNone":
            if mouse_event.event_type == MouseEventType.MOUSE_DOWN:
                self.client_state.choose_window_index = row
                if self.client_state.choose_options:
                    self.choose_the_pointed_option()
                elif self.client_state.choose_buffer:
                    self.choose_the_pointed_buffer()
                else:
                    self.choose_the_pointed_window()
                return None
            return NotImplemented

        return handler

    def _cursor_on_the_view(self) -> Point:
        """
        Where the cursor of the focused pane sits on this client's
        view, in the coordinates a float is placed in.

        prompt_toolkit records one cursor position per window on the
        screen it drew -- `cursor_positions`, keyed by the window --
        already in the view's coordinates. While the prefix waits the
        pane is the window the layout has focused, so it is that
        window's position the popup steps away from. A screen that
        has drawn no cursor for the window, and the frame before the
        first one, answer the top-left corner.
        Lillecarl/pymux#29.
        """
        app = self.client_state.app
        screen = getattr(getattr(app, "renderer", None), "_last_screen", None)
        window = getattr(getattr(app, "layout", None), "current_window", None)
        if screen is None or window is None:
            return Point(0, 0)
        return screen.get_cursor_position(window)

    def _the_cursor_is_in_the_top_right(self) -> bool:
        """
        Whether the cursor sits in the quadrant the box would rather
        draw in. The half the status bar takes is part of the lower
        half; it is where a cursor below the middle sits either way.
        Lillecarl/pymux#29.
        """
        point = self._cursor_on_the_view()
        room = self.room_this_client_has
        return point.y * 2 < room.rows and point.x * 2 >= room.columns

    def _palette_rows(self) -> int:
        """
        How many rows of completions fit under the box.

        The box is drawn on this client's own screen, so it is this
        client's rows that bound it and not the plane's. The box
        starts `BOX_TOP` rows down and holds a title and the input, so
        what is left is what the completions may take.
        """
        rows = self.room_this_client_has.rows
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
        # A prompt that knows its answers draws in a box, because the
        # completions are what need the room. Lillecarl/pymux#220.
        asks_for_a_key = Condition(
            lambda: self.client_state.prompt_completer is not None
        )
        in_a_box = (has_focus(self.client_state.command_buffer) & palette) | (
            has_focus(self.client_state.prompt_buffer) & asks_for_a_key
        )
        which_key_shows = Condition(lambda: self.pymux.which_key) & Condition(
            lambda: self.client_state.has_prefix
        )

        return FloatContainer(
            content=HSplit(
                [
                    # The main window.
                    FloatContainer(
                        Background(painted=Condition(lambda: self.pymux.paint_screen)),
                        floats=[
                            Float(
                                width=lambda: self.room_for_the_body().columns,
                                height=lambda: self.room_for_the_body().rows,
                                content=self._body,
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
                                content=self._prompt_window(),
                                filter=has_focus(self.client_state.prompt_buffer)
                                & ~asks_for_a_key,
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
                # The prompt as a box, when the question knows its
                # answers. The same inset as the two boxes above,
                # because it is the same kind of box.
                # Lillecarl/pymux#220.
                Float(
                    content=ConditionalContainer(
                        content=DynamicContainer(self._key_box),
                        filter=has_focus(self.client_state.prompt_buffer)
                        & asks_for_a_key,
                    ),
                    left=BOX_SIDE,
                    right=BOX_SIDE,
                    top=BOX_TOP,
                    z_index=Z_INDEX.POPUP,
                ),
                # The menu that hangs off the cursor. A box holds its
                # own, so this one steps aside for either of them.
                Float(
                    xcursor=True,
                    ycursor=True,
                    content=ConditionalContainer(
                        content=CompletionsMenu(max_height=12),
                        filter=~in_a_box,
                    ),
                ),
                # The windows of the session, to choose from. The
                # same inset as the keys pop-up, because it is the
                # same kind of box, and the same z order. It takes
                # focus, which is what keeps the keys of the pane out
                # of the way while it shows. Lillecarl/pymux#295.
                Float(
                    content=ConditionalContainer(
                        content=DynamicContainer(self._chooser_box),
                        filter=Condition(
                            lambda: self.client_state.choose_window
                            or self.client_state.choose_buffer
                            or self.client_state.choose_options
                        ),
                    ),
                    left=BOX_SIDE,
                    right=BOX_SIDE,
                    top=BOX_TOP,
                    bottom=5,
                    z_index=Z_INDEX.POPUP,
                ),

                # The menu that `display-menu` opened. The same
                # kind of box as the two choosers above, one at a
                # time with them, and no bottom of its own: it takes
                # the height of what is in it. Lillecarl/pymux#297.
                Float(
                    content=ConditionalContainer(
                        content=DynamicContainer(self._menu_box),
                        filter=Condition(lambda: bool(self.client_state.menu_entries)),
                    ),
                    left=BOX_SIDE,
                    right=BOX_SIDE,
                    top=BOX_TOP,
                    z_index=Z_INDEX.POPUP,
                ),
                # The keys a prefix leads to, while `which-key` is on
                # and the prefix waits. The box prefers the top right,
                # which holds the least of what a person has on the
                # screen -- a prompt fills a terminal from the bottom,
                # so the top right is empty more often than any other
                # corner -- and steps aside to the corner diagonally
                # opposite only when the cursor is in its own way. One
                # float per place, because a float's sides are fixed at
                # build time. It takes no focus, so the key after the
                # prefix reaches the bindings as it always did and the
                # popup is gone by the time the frame for that key
                # draws. Lillecarl/pymux#29.
                Float(
                    content=ConditionalContainer(
                        content=DynamicContainer(self._which_key_box),
                        filter=which_key_shows
                        & Condition(
                            lambda: not self._the_cursor_is_in_the_top_right()
                        ),
                    ),
                    top=0,
                    right=1,
                    z_index=Z_INDEX.POPUP,
                ),
                Float(
                    content=ConditionalContainer(
                        content=DynamicContainer(self._which_key_box),
                        filter=which_key_shows
                        & Condition(lambda: self._the_cursor_is_in_the_top_right()),
                    ),
                    # The status line keeps its row.
                    bottom=2,
                    left=1,
                    z_index=Z_INDEX.POPUP,
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
                        self.room_this_client_has.columns,
                    ),
                    height=lambda: overlay_size(
                        self.pymux.overlay_height,
                        self.room_this_client_has.rows,
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

        #: The container that draws the panes, per client. It holds the
        #: plan of the frame it drew, and everything drawn inside that
        #: frame reads it rather than measuring again.
        #: Lillecarl/pymux#217.
        self._panes_for_app: weakref.WeakKeyDictionary[Application, PlanContainer] = (
            weakref.WeakKeyDictionary()
        )

        #: Where this client looks at each window's plane, by window.
        #:
        #: **A view outlives the container that moves it.** The
        #: containers are built again whenever the arrangement changes
        #: shape, and a view held by one of them went back to the
        #: origin every time: a person who scrolled a strip and then
        #: split a pane found the row somewhere else. The view belongs
        #: to the client and the window, which is what it is a view of.
        #:
        #: One per window, because a window is a plane. Switching
        #: window and switching back leaves the strip where it was.
        self._views: weakref.WeakKeyDictionary[object, View] = (
            weakref.WeakKeyDictionary()
        )

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

    def panes(self) -> "PlanContainer | None":
        """
        The container that draws the panes of this client's window.

        **It is asked for through `_get_body`**, so a body whose
        arrangement changed shape is rebuilt first and what comes back
        is never a container for a window that has moved on. A fresh
        container has drawn nothing, so its plan is `None` and a
        caller measures instead.
        """
        if not self.pymux.arrangement.windows:
            return None

        self._get_body()
        return self._panes_for_app.get(get_app())

    def view_of(self, window) -> View:
        """
        Where this client looks at that window's plane.

        A fresh one is at the origin with no size, and the first frame
        writes the size. Nothing else makes one, so a window a person
        never looked at costs nothing.
        """
        view = self._views.get(window)
        if view is None:
            view = View()
            self._views[window] = view
        return view

    def _build_layout(self) -> Container:
        "Rebuild a new Container object and return that."
        logger.debug("Rebuilding layout.")

        if not self.pymux.arrangement.windows:
            # No Pymux windows in the arrangement.
            return Window()

        window = self.pymux.arrangement.get_active_window()

        # Every layout draws the same way: it says where the panes are
        # and one container puts them there. Zoom included, which is a
        # layout that wraps another and shows one pane of it, so a
        # zoomed pane keeps the row its title bar hangs in and a zoomed
        # strip is still a strip. Lillecarl/pymux#215.
        panes = _create_the_panes(self.pymux, window, self.view_of(window))
        self._panes_for_app[get_app()] = panes

        return HSplit(
            [
                # Some spacing for the top status bar.
                ConditionalContainer(
                    content=Window(height=1),
                    filter=Condition(lambda: self.pymux.show_pane_status),
                ),
                # The actual content.
                panes,
                # And the row the bottom pane hangs its bar below in.
                # Only when there is a stack, because only then is
                # there anything to name. Lillecarl/pymux#211.
                ConditionalContainer(
                    content=Window(height=1),
                    filter=Condition(
                        lambda: _bar_below_is_drawn(self.pymux, window)
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


def _create_the_panes(pymux: "Pymux", window, view: View) -> Container:
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

    **The view is given, not made here.** It belongs to the client and
    the window, and it outlives every container this builds, so a
    strip a person scrolled stays where they left it when a pane opens
    or closes.
    """
    containers = {
        pane: _create_container_for_process(pymux, window, pane)
        for pane in window.panes
    }

    return PlanContainer(
        layout_of(pymux, window),
        containers,
        _tell_the_pane_its_size,
        view=view,
        room=partial(room_for_the_panes, pymux, window),
    )


def _tell_the_pane_its_size(pane: arrangement.Pane, rect) -> None:
    """
    Give a pane the size of its rectangle.

    **The plan sizes a pane, and the drawing no longer has to.** A
    pane that is drawn hears the same numbers again from
    prompt_toolkit, which costs nothing because neither the pty nor
    the screen acts on a size it already has. A pane that is *not*
    drawn -- scrolled out of the view, or behind another in a stack --
    hears them only here, and the program in it needs them: it writes
    for the screen it thinks it has. Lillecarl/pymux#224.
    """
    pane.terminal.set_size(rect.width, rect.height)


def layout_of(pymux: "Pymux", window):
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

    **Zoom wraps whichever of them the window is in**, and that is why
    it is a wrapper. The branch here used to test `window.zoom` first,
    so a zoomed strip was not a strip: the row stopped existing for
    that frame instead of being covered. Lillecarl/pymux#215.

    The gaps are passed as a callable, because the gap between two
    stacked panes is two rows when a bar is drawn under a pane and one
    when it is not, and an option turns that on while the layout built
    here is still standing.
    """
    gaps = partial(gaps_of, pymux, window)
    inner = Strip(window, gaps) if window.strip else Divided(window, gaps)

    if window.zoom and window.active_pane is not None:
        return Zoomed(inner, window.active_pane)
    return inner


def _bar_below_is_drawn(pymux: "Pymux", window) -> bool:
    """
    Whether every pane of this window keeps a row under it.

    The bar under a pane names the pane above and the pane below it, so
    a window with no stack in it has nothing to put there and does not
    pay the row. A single pane and a plain row of panes look exactly as
    they did. Lillecarl/pymux#211.

    **A zoomed window has no stack on the screen**, whatever its tree
    holds: one pane covers everything, and nothing is above or below
    it. So it does not pay the row either.

    **Three places have to give the same answer**, or the rows drift:
    the gap between stacked panes, the row kept under the whole
    layout, and the float that draws the bar.
    """
    return pymux.show_pane_status and not window.zoom and window.has_a_stack()


def gaps_of(pymux: "Pymux", window) -> Gaps:
    """
    The cells this window leaves between the things in it.

    One for the border a column owns, and two rows between stacked
    panes when a pane draws a bar below it and the pane under it draws
    one above. `_create_split` leaves the same rows for the same
    reason. Lillecarl/pymux#211.
    """
    return Gaps(
        between_columns=BORDER_WIDTH,
        between_panes=2 if _bar_below_is_drawn(pymux, window) else 1,
    )


def room_for_the_panes(pymux: "Pymux", window) -> Size:
    """
    How much of the plane the panes get.

    The rows the chrome takes come off first, because a plan holds
    panes and the chrome draws in the gaps between them: one row for
    the bar above the top pane, and one for the bar below the bottom
    one where that is drawn. Lillecarl/pymux#217.

    **This is the size a plan is measured for**, and it is the plane's
    and not a client's. A client smaller than this sees part of the
    plan through a view of its own.
    """
    size = pymux.size_of_the_plane(window)

    rows = size.rows
    if pymux.show_pane_status:
        rows -= 1
    if _bar_below_is_drawn(pymux, window):
        rows -= 1

    return Size(rows=max(1, rows), columns=size.columns)


def plan_of(pymux: "Pymux", window) -> Plan:
    """
    Where the panes of this window are, in cells.

    **The frame's own plan, when there is one.** A title bar is drawn
    inside the frame that the container measured, so the answer it
    wants is the plan already on the container -- and asking for it is
    the difference between one measurement per frame and one per
    question. A window of sixteen panes asks sixty-four of them (four
    sides each), and measuring each time cost seventeen times the whole
    frame: 950k bytecode instructions against 55k, paid on every
    frame. `tests/measure_a_frame.py` is where that number comes from.

    **Measured fresh when there is no frame to read**, which is what
    makes a direction key work before anything is drawn. That is the
    property slice 2 of Lillecarl/pymux#217 added, and it is held by
    `test_a_key_moves_the_focus_before_anything_is_drawn`.
    """
    size = room_for_the_panes(pymux, window)

    try:
        manager = pymux.get_client_state().layout_manager
    except ValueError:
        # No client, so no frame and nowhere to keep an answer. A
        # command from the command line runs on a client like that.
        return layout_of(pymux, window).measure(size)

    known = manager.plan_of_this_frame(window, size)
    if known is not None:
        return known

    plan = _plan_of_the_frame(manager, window, size)
    if plan is None:
        plan = layout_of(pymux, window).measure(size)

    manager.remember_the_plan(window, size, plan)
    return plan


def pane_is_cut(pymux: "Pymux", pane: arrangement.Pane) -> bool:
    """
    Whether this pane runs off the edge of the view this client has.

    **A cut pane is the one case where what a person sees is not what
    the program wrote**, and the cells give nothing away: a pane whose
    program wrote nothing past the cut looks exactly like a pane that
    ends there. So the pane is tinted, and this is the question.
    Lillecarl/pymux#222.

    It reads the frame that is being drawn, because that is the only
    frame whose answer is worth anything: the view has a size then,
    and a plan to hold it against. Before the first frame, and for a
    pane of another window, the answer is no -- nothing is drawn, so
    nothing is cut.
    """
    try:
        manager = pymux.get_client_state().layout_manager
    except ValueError:
        return False

    container = manager.pane_container()
    if container is None or container.plan is None:
        return False

    try:
        rect = container.plan.rect_of(pane)
    except KeyError:
        return False

    return container.view.cuts(rect)


def _plan_of_the_frame(manager, window, size: Size) -> "Plan | None":
    """
    The plan the container of this frame measured, when it is still the
    answer.

    A title bar is drawn inside that frame, so this is the plan it is
    being drawn against, and reading it means the frame is measured
    once rather than once per pane.

    Two things have to hold, and each rules out a plan that would lie:

    - The container is the one for *this* window, and it measured for
      this size. A client that resized between two frames has a plan
      of the size it was.
    - The arrangement has not changed shape since. That one is free:
      `DynamicBody` rebuilds its body when the invalidation hash
      moves, and a fresh container has drawn nothing, so a pane
      opening, closing or zooming falls through to a fresh
      measurement.
    """
    container = manager.pane_container()
    if container is None or container.measured_for != size:
        return None

    if getattr(container.layout, "window", None) is not window:
        return None

    return container.plan


def write_sizes_into_weights(pymux: "Pymux", window) -> None:
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
    plan = plan_of(pymux, window)

    for split in window.splits:
        sideways = isinstance(split, arrangement.VSplit)

        for child in split:
            held = [plan.rect_of(pane) for pane in arrangement.panes_of(child)]
            if not held:
                continue

            box = bounding_box(held)
            split.weights[child] = box.width if sideways else box.height


def change_pane_size(
    pymux: "Pymux", window, pane: arrangement.Pane, up=0, right=0, down=0, left=0
) -> None:
    """
    Make one pane bigger or smaller, by that many cells on each side.

    **One door**, because a resize is two steps and the first one is
    easy to forget: the weights have to say what the panes measure now
    before a delta means anything. `resize-pane` and a program asking
    for a size both come through here.
    """
    write_sizes_into_weights(pymux, window)
    window.change_size_for_pane(pane, up=up, right=right, down=down, left=left)


def pane_beside(
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

    plan = plan_of(pymux, window)

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
        if terminal_is_focused():
            result = "class:terminal.focused"
        else:
            result = "class:terminal"

        if pymux.paint_screen:
            # The theme's background behind every cell the program left
            # at a default one, so the terminal's own colours are never
            # seen. Lillecarl/pymux#273.
            result += " class:painted"

        if pane_is_cut(pymux, arrangement_pane):
            # A pane that runs off the edge of the view is tinted, so
            # that a person can see it is cut. Lillecarl/pymux#222.
            result += " " + CUT_IS_TINTED

        return result

    def get_titlebar_text_fragments() -> StyleAndTextTuples:
        result: StyleAndTextTuples = []

        if is_zoomed():
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

    def neighbour(on_the_left: bool) -> StyleAndTextTuples:
        """
        The name of the pane on one side of this one.

        Nothing when there is none. A zoomed pane covers the window,
        so nothing is beside it either, and `pane_beside` says so.
        """
        pane = pane_beside(
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
        return neighbour(on_the_left=True)

    def get_the_right_of_the_bar() -> StyleAndTextTuples:
        return neighbour(on_the_left=False)

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
        arrangement_pane.clock_mode = False
        pymux.arrangement.get_active_window().active_pane = arrangement_pane
        pymux.invalidate(Woke.CLICK_LEFT_THE_CLOCK)

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
                        filter=Condition(lambda: bool(get_the_bar_below())),
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
                        BigClock(pymux, on_click), filter=clock_is_visible
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

    beside = pane_beside(pymux, window, window.active_pane, side)
    if beside is not None:
        window.active_pane = beside
