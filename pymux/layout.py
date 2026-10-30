"""
The layout engine. This builds the prompt_toolkit layout.
"""

from __future__ import annotations

import textwrap
import weakref
from collections.abc import Callable
from functools import partial
from typing import TYPE_CHECKING, Any, cast, override

from prompt_toolkit.application import Application, get_app
from prompt_toolkit.data_structures import Point, Size
from prompt_toolkit.enums import EditingMode
from prompt_toolkit.filters import (
    Condition,
    has_completions,
    has_focus,
    is_done,
)
from prompt_toolkit.formatted_text import HTML, StyleAndTextTuples
from prompt_toolkit.key_binding.vi_state import InputMode
from prompt_toolkit.layout.containers import (
    AnyContainer,
    ConditionalContainer,
    Container,
    DynamicContainer,
    Float,
    FloatContainer,
    HSplit,
    ScrollOffsets,
    VSplit,
    Window,
    WindowAlign,
    WritePosition,
    to_container,
)
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension as D
from prompt_toolkit.layout.layout import walk
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

from pymux import arrangement

from .actions import Kind, Origin, note, started_by
from .border_drag import DragCapture, holds_window_panes, press_on_line
from .boxes import BOX_BOTTOM, BOX_SIDE, BOX_TOP, PALETTE_HEADER
from .chooser import ChooserView
from .cut_shade import Hidden, hex_of, hidden_of, lifts, shade
from .divided import Divided
from .enums import Chooser, EditingKeys, Woke
from .filters import WaitsForConfirmation
from .format import format_pymux_string
from .log import logger
from .menu import Menu
from .plan_container import PlanContainer
from .plane import Plan, Rect, Side, View, bounding_box
from .status_line import StatusLine
from .strip import Strip
from .tiling import BORDER_WIDTH, Gaps
from .titlebar import PaneTitleBar
from .widgets import Background, BigClock, ConfirmationToolbar, MessageToolbar, PaneNumber, UrlLabels
from .window_pane import WindowWidget
from .z_index import Z_INDEX
from .zoomed import Zoomed

if TYPE_CHECKING:
    from prompt_toolkit.filters import Filter
    from prompt_toolkit.key_binding.key_bindings import NotImplementedOrNone
    from prompt_toolkit.layout.controls import BufferControl

    from pymux.client_state import ClientState
    from pymux.main import Pymux
    from pymux.notifications import Notification

__all__ = ["LayoutManager"]

#: A notification box, in cells: a width that fits a title, and the
#: rows of body it shows. Lillecarl/pymux#826.
POPUP_WIDTH = 44
MIN_POPUP_WIDTH = 12
POPUP_BODY_ROWS = 3

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


#: The container each pane was drawn from, on that screen, and what the
#: frame drew over each pane, once something asked.
_PANE_CONTAINERS = "pymux_pane_containers"
_PANE_COVERS = "pymux_pane_covers"


def pane_containers(screen: Screen) -> dict[arrangement.Pane, Container]:
    "The container each pane was drawn from on this screen."
    containers: dict[arrangement.Pane, Container] | None = getattr(screen, _PANE_CONTAINERS, None)
    if containers is None:
        containers = {}
        setattr(screen, _PANE_CONTAINERS, containers)
    return containers


def pane_covers(screen: Screen) -> dict[arrangement.Pane, tuple[Rect, ...]]:
    """
    What this screen drew over each pane, in cells of the screen: every
    window it drew that overlaps the pane and is not one of the pane's
    own. A popup, a menu, the which-key box, the command line. An image
    of the pane belongs under them. Lillecarl/pymux#816.

    **Not by paint order.** prompt_toolkit records a window when it
    paints, but a pane can paint twice in one frame (a body drawn late
    runs its panes again), and a window that paints again keeps its first
    place in the record. Nothing paints under the panes as a window --
    the background is a container -- so a window that is not the pane's
    and overlaps it is over it.

    Worked out once per screen, on the first ask.
    """
    covers: dict[arrangement.Pane, tuple[Rect, ...]] | None = getattr(screen, _PANE_COVERS, None)
    if covers is not None:
        return covers
    drawn = [
        (window, Rect(at.xpos, at.ypos, at.width, at.height))
        for window, at in screen.visible_windows_to_write_positions.items()
    ]
    containers = pane_containers(screen)
    covers = {}
    for pane, at in pane_write_positions(screen).items():
        area = Rect(at.xpos, at.ypos, at.width, at.height)
        container = containers.get(pane)
        own = set(walk(container)) if container is not None else set()
        covers[pane] = tuple(rect for window, rect in drawn if window not in own and rect.overlaps(area))
    setattr(screen, _PANE_COVERS, covers)
    return covers


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
        cells = available * int(text[:-1]) // 100 if text.endswith("%") else int(text)
    except ValueError:
        cells = available * int(DEFAULT_OVERLAY_SIZE[:-1]) // 100

    return max(MIN_OVERLAY_SIZE, min(cells, available))


class LayoutManager:
    """
    The main layout class, that contains the whole Pymux layout.
    """

    def __init__(self, pymux: Pymux, client_state: ClientState) -> None:
        self.pymux = pymux
        self.client_state = client_state

        #: The pop-up dialog that `list-keys` and `show-buffer` fill,
        #: and the text area inside it. **Neither exists until
        #: something asks for a pop-up.**
        #:
        #: A `Dialog` is a frame in a shadow around a text area with a
        #: search bar: 31 containers and 21 windows, which
        #: `Layout.find_all_controls` crossed on every key press for a
        #: box a person opens once an hour. Measured, with one pane and
        #: nothing open, the walk went from 135 containers to 106.
        #: Lillecarl/pymux#366.
        self.popup_dialog: Dialog | None = None
        self._popup_textarea: TextArea | None = None

        # The container of the overlay pane, and the pane it belongs to.
        self._overlay_for_pane: tuple[arrangement.Pane, Container] | None = None

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
        self._popups: Window | None = None

        # And the same two for the prompt: the line a person answers a
        # question on, and the box that holds it when the question
        # knows its answers. Lillecarl/pymux#220.
        self._prompt_window_container: Window | None = None
        self._key_prompt: Container | None = None

        # The part of the screen that holds the windows. It is kept
        # because it is what knows the container that drew the panes,
        # and a title bar drawn inside that frame asks for its plan.
        self._body = DynamicBody(self.pymux)

        #: `plan_of`'s answer for each window, while this client renders.
        #: Every title bar asks it for three sides, and the room for the
        #: panes walks every client. None between renders, where a
        #: handler may change what it measures.
        self.frame_plans: dict[arrangement.Window, Plan] | None = None

        self.chooser = ChooserView(self)
        self.status = StatusLine(self)
        self.menu = Menu(self)

        self.layout = self._create_layout()

    def frame_started(self, app: Application[Any]) -> None:
        "A render of this client starts: the arrangement holds still until it ends."
        self._body.frame_started()
        self.frame_plans = {}
        self.status.frame_changed()

    def frame_ended(self, app: Application[Any]) -> None:
        self._body.frame_ended()
        self.frame_plans = None
        self.status.frame_changed()

    @property
    def pane_write_positions(self) -> dict[arrangement.Pane, WritePosition]:
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
    def pane_covers(self) -> dict[arrangement.Pane, tuple[Rect, ...]]:
        "What the last render of this client drew over each pane: `pane_covers`."
        renderer = getattr(self.client_state.app, "renderer", None)
        screen = getattr(renderer, "_last_screen", None)
        if screen is None:
            return {}
        return pane_covers(screen)

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
        return Size(rows=max(1, size.rows - self.status.rows()), columns=size.columns)

    def room_for_body(self) -> Size:
        """
        How big the part of the screen that holds the windows is.

        The smaller of the plane and this client, on each axis. A
        client bigger than the plane draws background around it, which
        is what every client but the smallest one does today. A client
        smaller than the plane shows part of it and its view scrolls,
        which is what `window-size largest` is for.
        """
        plane = cast(Size, self.pymux.plane_size())
        mine = self.room_this_client_has

        return Size(
            rows=min(plane.rows, mine.rows),
            columns=min(plane.columns, mine.columns),
        )

    def pane_container(self) -> PlanContainer | None:
        """
        The container that drew the panes of the window this client
        shows, or `None` before it has built one.

        It holds the plan of the frame it drew, which is what a title
        bar reads instead of measuring the window again.
        """
        return self._body.panes()

    def _popup_box(self) -> AnyContainer:
        "The pop-up, or nothing while nobody has asked for one."
        return self.popup_dialog or self._nothing_to_draw

    def _build_popup(self) -> None:
        """
        Make the pop-up dialog, once.

        **Before anything focuses it.** `Layout.focus` refuses a
        control it cannot reach, and the control is only reachable
        after `_popup_box` has one to answer with.
        """
        if self.popup_dialog is not None:
            return

        search_textarea = SearchToolbar()
        self._popup_textarea = TextArea(scrollbar=True, read_only=True, search_field=search_textarea)
        self.popup_dialog = Dialog(
            title="Keys",
            body=HSplit(
                [
                    Window(FormattedTextControl(text=""), height=1),  # 1 line margin.
                    self._popup_textarea,
                    search_textarea,
                    Window(
                        FormattedTextControl(text=HTML("Press [<b>q</b>] to quit or [<b>/</b>] for searching.")),
                        align=WindowAlign.CENTER,
                        height=1,
                    ),
                ]
            ),
        )

    def display_popup(self, title: str, content: str) -> None:
        """
        Display a pop-up dialog.
        """
        self._build_popup()
        assert self.popup_dialog is not None
        assert self._popup_textarea is not None
        self.popup_dialog.title = title
        self._popup_textarea.text = content
        self.client_state.display_popup = True
        get_app().layout.focus(self._popup_textarea)

    def what_time_moves(self) -> tuple[str, ...]:
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

        **The panes are this client's window's, and no others.** A
        pane's write asks the other clients this question
        (Lillecarl/pymux#224), so reading `#T` for every pane of every
        window reads eight windows of titles each time any pane of a
        session of eight writes.

        Narrowing it loses nothing, because `StatusLine.window_list`
        already formats `window-status-format` for every window. So
        whatever this client draws *about* another window -- its name,
        its flags, and `#T` if a person put it in that format -- is
        already in `parts`. What is left is the titlebars of the panes
        this client can see, and a `clock-mode` pane, and both of those
        are in the window it looks at. Lillecarl/pymux#251.
        """
        pymux = self.pymux
        parts: list[str] = []

        if pymux.show_status:
            parts.append(self.status.left())
            parts.append(self.status.right())
            parts += [text for _, text, *_ in self.status.window_list()]

        window = self.client_state.session.arrangement.get_active_window()
        if window is None:
            return tuple(parts)
        for pane in window.panes:
            if pane.clock_mode:
                parts.append(pymux.displayed_now().strftime(CLOCK_FORMAT))
            elif pymux.show_pane_status:
                parts.append(
                    format_pymux_string(
                        pymux,
                        window.options.pane_title_format,
                        window=window,
                        pane=pane,
                        session=self.client_state.session,
                        client=self.client_state,
                    )
                )

        return tuple(parts)

    def _vi_mode_tokens(self) -> StyleAndTextTuples:
        """
        Which vi mode the line a person types on is in.

        Nothing with emacs status keys: there is one mode there, and a
        tag that never changes says nothing.

        **With vi status keys the mode decides what a key does**, and
        Escape most of all: the first one leaves insert mode and the
        second closes the line. A person has to be able to see which
        press they are about to make. Lillecarl/pymux#255.

        The tag is one width whatever it says, so the line a person is
        typing on never moves under them.
        """
        if self.client_state.session.options.status_keys is EditingKeys.HELIX:
            return self._helix_mode_tokens()
        if self.client_state.session.options.status_keys is not EditingKeys.VI:
            return []

        app = get_app()
        if app.editing_mode is not EditingMode.VI:
            return []

        mode = app.vi_state.input_mode
        if mode in (InputMode.REPLACE, InputMode.REPLACE_SINGLE):
            name = "REPLACE"
        elif mode is InputMode.NAVIGATION:
            name = "NORMAL"
        else:
            name = "INSERT"

        return [("class:commandline.mode", " %-7s " % (name,))]

    def _helix_mode_tokens(self) -> StyleAndTextTuples:
        """
        Helix's mode, as Helix spells it, and what is half typed: a
        count, a menu key, or the regex `s` reads, which shows nowhere
        else. The mode is one width; the half-typed part grows only
        while it is typed. Lillecarl/pymux#749.
        """
        helix = self.pymux.key_bindings_manager.helix
        buffer = get_app().current_buffer
        tokens: StyleAndTextTuples = [("class:commandline.mode", " %s " % (helix.mode_of(buffer).label,))]
        pending = helix.pending_of(buffer)
        if pending:
            tokens.append(("class:commandline.pending", " %s " % (pending,)))
        return tokens

    def _before_command_tokens(self) -> StyleAndTextTuples:
        'What stands before what a person types after ":".'
        return [*self._vi_mode_tokens(), ("class:commandline-prompt", ":")]

    def _before_prompt_command_tokens(self) -> StyleAndTextTuples:
        if self.client_state.prompt_completer is not None:
            # The box says what it is asking for, on the row above the
            # line. Saying it twice is one row wasted and one thing to
            # read. Lillecarl/pymux#220.
            return []
        return [*self._vi_mode_tokens(), ("class:commandline.prompt", "%s " % (self.client_state.prompt_text,))]

    def _overlay_container(self) -> Container:
        """
        The overlay pane of this client, with a title bar above it.

        The container is built again when the pane changes, and kept
        while it stays, so that the terminal keeps its state.
        """
        pane = self.client_state.session.overlay_pane

        if pane is None:
            return self._nothing_to_draw

        if self._overlay_for_pane is not None and self._overlay_for_pane[0] is pane:
            return self._overlay_for_pane[1]

        def get_title() -> StyleAndTextTuples:
            return [("class:overlay.title", " %s " % self.client_state.session.overlay_title)]

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
                    BeforeInput(self._before_command_tokens),
                    ShowArg(),
                    HighlightSelectionProcessor(),
                    # Paints nothing unless the status keys are Helix's.
                    self.pymux.key_bindings_manager.helix.processor(),
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
                    self.pymux.key_bindings_manager.helix.processor(),
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

    def _box(self, title: Callable[[], str], window: Container) -> Container:
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

        def get_title() -> StyleAndTextTuples:
            return [("class:commandpalette.title", title())]

        return HSplit(
            [
                Window(
                    height=1,
                    align=WindowAlign.CENTER,
                    content=FormattedTextControl(get_title),
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
                    content=FormattedTextControl([("class:commandpalette.title", " Prefix ")]),
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
        rows = self.pymux.key_bindings_manager.prefix_keys()
        if not rows:
            return [("class:commandpalette", " No keys follow the prefix. ")]

        width = max(len(key) for key, _meaning in rows) + 2
        tokens: StyleAndTextTuples = []
        for key, meaning in rows:
            tokens.append(("class:which-key.key", key.ljust(width)))
            tokens.append(("class:commandpalette", meaning + "\n"))
        return tokens

    def _popups_box(self) -> Container:
        """
        The notifications this client shows as boxes, newest on top.
        Built once, for the reason `_command_line_window` gives.
        Lillecarl/pymux#826.
        """
        if self._popups is None:
            self._popups = Window(
                FormattedTextControl(self._popup_tokens, focusable=False),
                width=lambda: D.exact(self._popup_width()),
                dont_extend_height=True,
                style="class:notification-popup",
            )
        return self._popups

    def _popups_show(self, top_right: bool) -> bool:
        "Whether the boxes draw in this corner. The list goes first: it is empty on almost every frame."
        return bool(self.client_state.popups) and self._cursor_is_in_top_right() is not top_right

    def _popup_width(self) -> int:
        return max(MIN_POPUP_WIDTH, min(POPUP_WIDTH, self.room_this_client_has.columns - 2))

    def _popup_tokens(self) -> StyleAndTextTuples:
        """
        One box per notification: the title, then up to `POPUP_BODY_ROWS`
        rows of the body. A click shows the pane it came from, as a
        click on a desktop notification does, and closes the box.
        """
        inner = self._popup_width() - 4
        tokens: StyleAndTextTuples = []
        border = "class:notification-popup.border"
        for number, record in enumerate(reversed(self.client_state.popups)):
            click = self._popup_click(record)
            rows: list[str] = textwrap.wrap(record.body, inner)[:POPUP_BODY_ROWS]
            if number:
                tokens.append((border, "\n", click))
            tokens.append((border, "╭" + "─" * (inner + 2) + "╮\n", click))
            for style, text in [("class:notification-popup.title", record.title[:inner])] + [("", row) for row in rows]:
                tokens.append((border, "│ ", click))
                tokens.append((style, text.ljust(inner), click))
                tokens.append((border, " │\n", click))
            tokens.append((border, "╰" + "─" * (inner + 2) + "╯", click))
        return tokens

    def _popup_click(self, record: Notification) -> Callable[[MouseEvent], NotImplementedOrNone]:
        def click(mouse_event: MouseEvent) -> NotImplementedOrNone:
            if mouse_event.event_type != MouseEventType.MOUSE_UP:
                return NotImplemented
            pane = self.pymux.panes_by_id.get(record.pane_id) if record.pane_id is not None else None
            if pane is not None:
                self.pymux.show_pane_on_click(self.client_state, pane)
            self.pymux.notifier.drop_popup(self.client_state, record)
            return None

        return click

    def _cursor_on_view(self) -> Point:
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

    def _cursor_is_in_top_right(self) -> bool:
        """
        Whether the cursor sits in the quadrant the box would rather
        draw in. The half the status bar takes is part of the lower
        half; it is where a cursor below the middle sits either way.
        Lillecarl/pymux#29.
        """
        point = self._cursor_on_view()
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

    def _floats_above_the_status_line(self, waits_for_confirmation: Filter, which_key_shows: Filter) -> list[Float]:
        "The floats placed from the bottom of the body, which is the top of the status line."
        return [
            Float(
                bottom=0,
                left=0,
                z_index=Z_INDEX.MESSAGE_TOOLBAR,
                content=MessageToolbar(self.client_state),
            ),
            # A question waiting for an answer sits above the status
            # line, where a message sits, and not over it. The status
            # line is where a person reads which window they are on,
            # and "kill-window #W?" is the moment they want it: the
            # question names something the window list is the context
            # for. Lillecarl/pymux#339.
            # **No `right`.** A float given both sides is as wide as the
            # screen, and an opaque one paints that row even when its
            # content is empty: it erased a row of the pane under it,
            # which `checks.pymux-alacritty` caught as a changed cell
            # colour. The message toolbar beside it takes its width from
            # its content, and so does this.
            Float(
                left=0,
                bottom=0,
                z_index=Z_INDEX.CONFIRMATION,
                content=ConditionalContainer(
                    content=Window(
                        height=1,
                        content=ConfirmationToolbar(self.pymux, self.client_state),
                    ),
                    filter=waits_for_confirmation,
                ),
            ),
            # The notification boxes, in the same corner as the keys
            # below, for the same reason. Lillecarl/pymux#826.
            Float(
                content=ConditionalContainer(
                    content=DynamicContainer(self._popups_box),
                    filter=Condition(lambda: self._popups_show(top_right=False)),
                ),
                bottom=1,
                left=1,
                z_index=Z_INDEX.NOTIFICATION_POPUP,
            ),
            # The keys a prefix leads to, in the corner diagonally
            # opposite the top right. `_create_layout` holds the other
            # corner and says why there are two.
            Float(
                content=ConditionalContainer(
                    content=DynamicContainer(self._which_key_box),
                    filter=which_key_shows & Condition(lambda: self._cursor_is_in_top_right()),
                ),
                # A row clear of the status line.
                bottom=1,
                left=1,
                z_index=Z_INDEX.POPUP,
            ),
        ]

    def _create_layout(self) -> Container:
        """
        Generate the main prompt_toolkit layout.
        """
        waits_for_confirmation = WaitsForConfirmation(self.pymux)
        palette = Condition(lambda: self.client_state.session.options.command_palette)
        # A prompt that knows its answers draws in a box, because the
        # completions are what need the room. Lillecarl/pymux#220.
        asks_for_key = Condition(lambda: self.client_state.prompt_completer is not None)
        in_box = (has_focus(self.client_state.command_buffer) & palette) | (
            has_focus(self.client_state.prompt_buffer) & asks_for_key
        )
        which_key_shows = Condition(lambda: self.client_state.session.options.which_key) & Condition(
            lambda: self.client_state.has_prefix
        )

        return FloatContainer(
            content=HSplit(
                [
                    # The main window.
                    # **The floats that sit above the status line are
                    # here, and not on the outer container.** A float's
                    # sides are numbers fixed at build time, and the
                    # status line's height is not: anchored to the
                    # bottom of the body, they rise with it.
                    # Lillecarl/pymux#723.
                    FloatContainer(
                        Background(painted=Condition(lambda: self.client_state.session.options.paint_screen)),
                        floats=[
                            Float(
                                width=lambda: self.room_for_body().columns,
                                height=lambda: self.room_for_body().rows,
                                content=self._body,
                            ),
                            *self._floats_above_the_status_line(waits_for_confirmation, which_key_shows),
                        ],
                    ),
                    # Status bar.
                    ConditionalContainer(
                        content=VSplit(
                            [
                                # Left.
                                Window(
                                    height=1,
                                    width=(lambda: D(max=self.client_state.session.options.status_left_length)),
                                    dont_extend_width=True,
                                    content=FormattedTextControl(self.status.left),
                                ),
                                # List of windows in the middle.
                                Window(
                                    height=lambda: D.exact(max(1, self.status.rows())),
                                    char=" ",
                                    align=self.status.align,
                                    content=FormattedTextControl(self.status.window_list),
                                ),
                                # Right.
                                Window(
                                    height=1,
                                    width=lambda: D(max=self.status.right_length()),
                                    dont_extend_width=True,
                                    align=WindowAlign.RIGHT,
                                    content=FormattedTextControl(self.status.right),
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
                # It draws where each link is, and not in its own box.
                Float(top=0, left=0, transparent=True, content=UrlLabels(self.client_state)),
                Float(
                    top=0,
                    left=0,
                    right=0,
                    bottom=0,
                    transparent=True,
                    z_index=Z_INDEX.DRAG_CAPTURE,
                    content=DragCapture(self.pymux, self.client_state),
                ),
                # That an error was logged, and where. It takes no focus
                # and hides no cursor: a person typing in the pane under
                # it keeps both. Lillecarl/pymux#241.
                Float(
                    top=0,
                    right=0,
                    z_index=Z_INDEX.ERROR_NOTICE,
                    content=ConditionalContainer(
                        content=Window(
                            FormattedTextControl(lambda: self.pymux.error_notice.text(), focusable=False),
                            height=1,
                            dont_extend_width=True,
                            style="class:error-notice",
                        ),
                        filter=Condition(lambda: bool(self.pymux.error_notice.count)),
                    ),
                ),
                Float(
                    left=0,
                    right=0,
                    bottom=0,
                    content=HSplit(
                        [
                            # ':' prompt toolbar.
                            ConditionalContainer(
                                content=self._command_line_window(),
                                filter=has_focus(self.client_state.command_buffer) & ~palette,
                            ),
                            # Other command-prompt commands toolbar.
                            ConditionalContainer(
                                content=self._prompt_window(),
                                filter=has_focus(self.client_state.prompt_buffer) & ~asks_for_key,
                            ),
                        ]
                    ),
                ),
                # Keys pop-up.
                Float(
                    content=ConditionalContainer(
                        content=DynamicContainer(self._popup_box),
                        filter=Condition(lambda: self.client_state.display_popup),
                    ),
                    left=BOX_SIDE,
                    right=BOX_SIDE,
                    top=BOX_TOP,
                    bottom=BOX_BOTTOM,
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
                        filter=has_focus(self.client_state.prompt_buffer) & asks_for_key,
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
                        filter=~in_box,
                    ),
                ),
                # The buffers or the options, to choose from. A box,
                # the same inset as the keys pop-up, because it is the
                # same kind of thing and neither can be switched to.
                # It takes focus, which is what keeps the keys of the
                # pane out of the way while it shows.
                # Lillecarl/pymux#295. Lillecarl/pymux#304.
                Float(
                    content=ConditionalContainer(
                        content=DynamicContainer(self.chooser._chooser_box),
                        filter=Condition(self.chooser.box_chooser_shows),
                    ),
                    left=BOX_SIDE,
                    right=BOX_SIDE,
                    top=BOX_TOP,
                    bottom=BOX_BOTTOM,
                    z_index=Z_INDEX.POPUP,
                ),
                # The windows, to choose from: a bar across the top,
                # over the window it has switched to. Not a box, and
                # no inset: it covers as few rows of that window as
                # the entries need. Lillecarl/pymux#327.
                Float(
                    content=ConditionalContainer(
                        content=DynamicContainer(self.chooser._window_bar),
                        filter=Condition(lambda: self.client_state.chooser is Chooser.WINDOW),
                    ),
                    left=0,
                    right=0,
                    top=0,
                    z_index=Z_INDEX.POPUP,
                ),
                # The menu that `display-menu` opened. The same
                # kind of box as the two choosers above, one at a
                # time with them, and no bottom of its own: it takes
                # the height of what is in it. Lillecarl/pymux#297.
                Float(
                    content=ConditionalContainer(
                        content=DynamicContainer(self.menu.box),
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
                        filter=which_key_shows & Condition(lambda: not self._cursor_is_in_top_right()),
                    ),
                    top=0,
                    right=1,
                    z_index=Z_INDEX.POPUP,
                ),
                # Notifications as boxes, where the keys above go and
                # under them: away from the cursor. Lillecarl/pymux#826.
                Float(
                    content=ConditionalContainer(
                        content=DynamicContainer(self._popups_box),
                        filter=Condition(lambda: self._popups_show(top_right=True)),
                    ),
                    top=0,
                    right=1,
                    z_index=Z_INDEX.NOTIFICATION_POPUP,
                ),
                # The overlay pane, in the middle of the screen. A
                # `Float` with no side given is centred.
                Float(
                    content=ConditionalContainer(
                        content=DynamicContainer(self._overlay_container),
                        filter=Condition(lambda: self.client_state.session.overlay_pane is not None),
                    ),
                    width=lambda: overlay_size(
                        self.client_state.session.overlay_width,
                        self.room_this_client_has.columns,
                    ),
                    height=lambda: overlay_size(
                        self.client_state.session.overlay_height,
                        self.room_this_client_has.rows,
                    ),
                    z_index=Z_INDEX.OVERLAY,
                ),
            ],
        )


class DynamicBody(Container):
    """
    The dynamic part, which is different for each CLI (for each client). It
    depends on which window/pane is active.

    This makes it possible to have just one main layout class, and
    automatically rebuild the parts that change if the windows/panes
    arrangement changes, without doing any synchronisation.
    """

    def __init__(self, pymux: Pymux) -> None:
        self.pymux = pymux
        # Each client's `LayoutManager` makes its own, so everything
        # here is that client's and needs no key. Lillecarl/pymux#712.

        #: The arrangement's hash the body was built for, and the body.
        self._body: tuple[tuple[object, ...], Container] | None = None

        #: The container that draws the panes. It holds the plan of the
        #: frame it drew, and everything drawn inside that frame reads
        #: it rather than measuring again. Lillecarl/pymux#217.
        self._panes: PlanContainer | None = None

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
        self._views: weakref.WeakKeyDictionary[object, View] = weakref.WeakKeyDictionary()

        #: The arrangement's hash while this client renders. Every title
        #: bar asks for the plan, and every ask hashed the arrangement
        #: again: 0.8 ms a frame with eight panes. Nothing changes the
        #: arrangement inside a render, and outside one a handler may,
        #: so the hash is kept from `frame_started` to `frame_ended` and
        #: never across them.
        self._framing = False
        self._frame_hash: tuple[object, ...] | None = None

    def frame_started(self) -> None:
        self._framing = True
        self._frame_hash = None

    def frame_ended(self) -> None:
        self._framing = False
        self._frame_hash = None

    def _get_body(self) -> Container:
        "The container for the arrangement as it is, built again when it changed shape."
        if not self._framing:
            new_hash = self.pymux.arrangement.invalidation_hash()
        elif self._frame_hash is None:
            new_hash = self._frame_hash = self.pymux.arrangement.invalidation_hash()
        else:
            new_hash = self._frame_hash

        if self._body is not None and self._body[0] == new_hash:
            return self._body[1]

        new_layout = self._build_layout()
        self._body = (new_hash, new_layout)
        return new_layout

    def panes(self) -> PlanContainer | None:
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
        return self._panes

    def view_of(self, window: arrangement.Window) -> View:
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

        window = self.pymux.arrangement.get_active_window()
        if window is None:
            # No Pymux windows in the arrangement.
            return Window()

        # Every layout draws the same way: it says where the panes are
        # and one container puts them there. Zoom included, which is a
        # layout that wraps another and shows one pane of it, so a
        # zoomed pane keeps the row its title bar hangs in and a zoomed
        # strip is still a strip. Lillecarl/pymux#215.
        panes = _create_panes(self.pymux, window, self.view_of(window))
        self._panes = panes

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
                    filter=Condition(lambda: _bar_below_is_drawn(self.pymux, window)),
                ),
            ]
        )

    @override
    def reset(self) -> None:
        if self._body is not None:
            self._body[1].reset()

    @override
    def preferred_width(self, max_available_width: int) -> D:
        body = self._get_body()
        return body.preferred_width(max_available_width)

    @override
    def preferred_height(self, width: int, max_available_height: int) -> D:
        body = self._get_body()
        return body.preferred_height(width, max_available_height)

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
        body = self._get_body()
        body.write_to_screen(screen, mouse_handlers, write_position, parent_style, erase_bg, z_index)

    @override
    def get_children(self) -> list[Container]:
        # (Required for prompt_toolkit.layout.utils.find_window_for_buffer_name.)
        body = self._get_body()
        return [body]


def _create_panes(pymux: Pymux, window: arrangement.Window, view: View) -> PlanContainer:
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
    containers = {pane: _create_container_for_process(pymux, window, pane) for pane in window.panes}

    def tell_its_size(pane: arrangement.Pane, rect: Rect) -> None:
        if isinstance(pane.terminal, WindowWidget) and holds_window_panes(pymux, window):
            return
        _tell_pane_its_size(pane, rect)

    return PlanContainer(
        layout_of(pymux, window),
        containers,
        tell_its_size,
        view=view,
        room=partial(room_for_panes, pymux, window),
        glide_time=lambda: pymux.current_session.options.strip_animation_time / 1000,
        on_line=partial(press_on_line, pymux, window),
        line_z_index=Z_INDEX.BORDER_LINE,
    )


def _tell_pane_its_size(pane: arrangement.Pane, rect: Rect) -> None:
    """
    Give a pane the size of its rectangle.

    **The plan sizes a pane, and the drawing does not have to.** A
    pane that is drawn hears the same numbers again from
    prompt_toolkit, which costs nothing because neither the pty nor
    the screen acts on a size it already has. A pane that is *not*
    drawn -- scrolled out of the view, or behind another in a stack --
    hears them only here, and the program in it needs them: it writes
    for the screen it thinks it has. Lillecarl/pymux#224.
    """
    pane.terminal.set_size(rect.width, rect.height)


def layout_of(pymux: Pymux, window: arrangement.Window) -> Divided | Strip | Zoomed:
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
    it is a wrapper. A branch that tests `window.zoom` before
    `window.options.strip` makes a zoomed strip not a strip: the row stops
    existing for that frame instead of being covered.
    Lillecarl/pymux#215.

    The gaps are passed as a callable, because the gap between two
    stacked panes is two rows when a bar is drawn under a pane and one
    when it is not, and an option turns that on while the layout built
    here is still standing.
    """
    gaps = partial(gaps_of, pymux, window)
    inner = Strip(window, gaps) if window.options.strip else Divided(window, gaps)

    if window.zoom and window.active_pane is not None:
        return Zoomed(inner, window.active_pane)
    return inner


def _bar_below_is_drawn(pymux: Pymux, window: arrangement.Window) -> bool:
    """
    Whether every pane of this window keeps a row under it.

    The bar under a pane names the pane above and the pane below it, so
    a window with no stack in it has nothing to put there and does not
    pay the row. A single pane and a plain row of panes look exactly as
    they did. Lillecarl/pymux#211.

    **A zoomed window has no stack on the screen**, whatever its tree
    holds: one pane covers everything, and nothing is above or below
    it. So it does not pay the row either.

    **Two places have to give the same answer**, or the rows drift: the
    row this client keeps under the whole layout, and the float that
    draws the bar. Both are this client's own drawing.

    The third is the gap between stacked panes, and that one is the
    plane's: `_the_plane_keeps_a_bar_below`. A full-screen client
    sharing a stacked window draws nothing in those gaps rather than
    closing them up, because the gap is geometry and a pane has one
    pty. Lillecarl/pymux#471.
    """
    return pymux.show_pane_status and not window.zoom and window.has_stack()


def _the_plane_keeps_a_bar_below(pymux: Pymux, window: arrangement.Window) -> bool:
    "The same rows, asked of every client watching. Lillecarl/pymux#471."
    return pymux.any_watcher_shows_pane_status(window) and not window.zoom and window.has_stack()


def gaps_of(pymux: Pymux, window: arrangement.Window) -> Gaps:
    """
    The cells this window leaves between the things in it.

    One for the border a column owns, and two rows between stacked
    panes when a pane draws a bar below it and the pane under it draws
    one above. `_create_split` leaves the same rows for the same
    reason. Lillecarl/pymux#211.

    **The plane's answer**, because a plan is one plan for every client
    watching. Lillecarl/pymux#471.
    """
    return Gaps(
        between_columns=BORDER_WIDTH,
        between_panes=2 if _the_plane_keeps_a_bar_below(pymux, window) else 1,
    )


def room_for_panes(pymux: Pymux, window: arrangement.Window) -> Size:
    """
    How much of the plane the panes get.

    The rows the chrome takes come off first, because a plan holds
    panes and the chrome draws in the gaps between them: one row for
    the bar above the top pane, and one for the bar below the bottom
    one where that is drawn. Lillecarl/pymux#217.

    **This is the size a plan is measured for**, and it is the plane's
    and not a client's. A client smaller than this sees part of the
    plan through a view of its own.

    So the rows come off for any client watching that draws the
    chrome, and never for the client asking: a full-screen client draws
    background where another one's titlebars go, rather than making a
    plan of its own and resizing everybody's programs.
    Lillecarl/pymux#471.
    """
    size = cast(Size, pymux.plane_size(window))

    rows = size.rows
    if pymux.any_watcher_shows_pane_status(window):
        rows -= 1
    if _the_plane_keeps_a_bar_below(pymux, window):
        rows -= 1

    return Size(rows=max(1, rows), columns=size.columns)


def plan_of(pymux: Pymux, window: arrangement.Window) -> Plan:
    """
    Where the panes of this window are, in cells.

    **The frame's own plan, when there is one.** A title bar is drawn
    inside the frame that the container measured, so the answer it
    wants is the plan already on the container -- and asking for it is
    the difference between one measurement per frame and one per
    question. A window of sixteen panes asks sixty-four of them (four
    sides each), and measuring each time cost seventeen times the whole
    frame: 950k bytecode instructions against 55k, paid on every
    frame. `tests/measure_frame.py` is where that number comes from.

    **Measured fresh when there is no frame to read**, which is what
    makes a direction key work before anything is drawn. That is the
    property slice 2 of Lillecarl/pymux#217 added, and it is held by
    `test_key_moves_focus_before_anything_is_drawn`.
    """
    try:
        manager = pymux.get_client_state().layout_manager
    except ValueError:
        # No client, so no frame and nowhere to keep an answer. A
        # command from the command line runs on a client like that.
        return layout_of(pymux, window).measure(room_for_panes(pymux, window))

    plans = manager.frame_plans
    if plans is not None and window in plans:
        return plans[window]

    size = room_for_panes(pymux, window)
    plan = _frame_plan(manager, window, size)
    if plan is None:
        plan = layout_of(pymux, window).measure(size)
    if plans is not None:
        plans[window] = plan
    return plan


def size_the_panes_of(pymux: Pymux, window: arrangement.Window) -> None:
    """
    Give every pane of this window the size the plan gives it.

    **A frame does this, and a window nobody watches gets no frame.**
    `PlanContainer.write_to_screen` tells each pane its rectangle, so
    an attached client's next frame is what makes a resize reach the
    programs. With nobody attached there is no next frame: the plane
    answered a new number and the ptys kept the old one, which is a
    size the program in the pane never hears about.

    Every pane of every slot, shown or not, for the reason
    `PlanContainer` gives: a hidden pane that already has the size it
    will be shown at is revealed without a resize.
    Lillecarl/pymux#459.
    """
    plan = plan_of(pymux, window)
    for slot, rect in plan.rects.items():
        for pane in slot.panes:
            _tell_pane_its_size(cast(arrangement.Pane, pane), rect)


def hidden_of_pane(pymux: Pymux, pane: arrangement.Pane) -> Hidden | None:
    """
    How much of this pane the view this client has hides, on each side.

    **A cut pane is the one case where what a person sees is not what
    the program wrote**, and the cells give nothing away: a pane whose
    program wrote nothing past the cut looks exactly like a pane that
    ends there. So the cut edge is shaded, and this is the question.
    Lillecarl/pymux#222, Lillecarl/pymux#616.

    It reads the frame that is being drawn, where the view is, glide
    and all. Before the first frame, and for a pane of another window,
    the answer is None: nothing is drawn, so nothing is cut.
    """
    try:
        manager = pymux.get_client_state().layout_manager
    except ValueError:
        return None

    container = manager.pane_container()
    if container is None or container.plan is None:
        return None

    try:
        rect = container.plan.rect_of(pane)
    except KeyError:
        return None

    return hidden_of(container.view.shown_rect, rect)


def _cut_shade(pymux: Pymux) -> tuple[int, float]:
    "How many cells the shade at a cut fades over, and how strong it is, for the client drawn."
    options = pymux.current_session.options
    return options.cut_shade_width, options.cut_shade_strength / 100


def cut_backdrop(pymux: Pymux) -> str | None:
    """
    What a cell the program left at the default background really shows.

    **Not always the theme's colour.** With `paint-screen` off the cell
    shows the terminal's own background, which the client asked its
    terminal for (`OSC 11`, Lillecarl/pymux#223), so a shade picked
    against the theme's would lift one terminal and darken the next.
    Lillecarl/pymux#352. With `paint-screen` on, the cell's own style
    already names the theme's, and a terminal that answered nothing
    leaves the theme's as the best guess.
    """
    if not pymux.current_session.options.paint_screen:
        try:
            background = pymux.get_client_state().default_colors.background
        except ValueError, AttributeError:
            background = None
        if background is not None:
            return background.hex
    return hex_of(get_app().renderer.style.get_attrs_for_style_str("class:painted").bgcolor or "")


def _frame_plan(manager: LayoutManager, window: arrangement.Window, size: Size) -> Plan | None:
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


def write_sizes_into_weights(pymux: Pymux, window: arrangement.Window) -> None:
    """
    Write the size each thing has now into the weight that decides it.

    **This is what makes `resize-pane -U 1` mean one row.** A weight is
    a share of a split and not a number of cells, so a delta of one
    added to a share of one means nothing on a screen. Measure first,
    write the cells each child holds into its weight, and then a delta
    of one is one cell -- the weights add up to the room, so each
    child's share of it comes out as the number itself.

    The plan decides the cells, and this is the one place that
    writes a weight back. A render that writes them on every frame
    leaves the arrangement holding four kinds of geometry at once.

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
            split.set_weight(child, box.width if sideways else box.height)


def change_pane_size(
    pymux: Pymux,
    window: arrangement.Window,
    pane: arrangement.Pane,
    up: int = 0,
    right: int = 0,
    down: int = 0,
    left: int = 0,
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
    pymux: Pymux, window: arrangement.Window, pane: arrangement.Pane, side: Side
) -> arrangement.Pane | None:
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
    if beside is None:
        return None

    return cast(arrangement.Pane, beside.shown)


def _short_name_of(pymux: Pymux, pane: arrangement.Pane) -> str:
    """
    A pane in a few cells, for the title bar of the pane beside it.

    The program running in it, which is what `Pane.name` is, and a
    person chose it if they renamed the pane. Its title is the fallback,
    because a pane whose process has ended has no name left.
    """
    return pane.name or format_pymux_string(pymux, PANE_TITLE_FORMAT, pane=pane).strip()


def _create_container_for_process(
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
        fade=lambda: lifts(*_cut_shade(pymux)),
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


def move_focus(pymux: Pymux, side: Side) -> None:
    """
    Move the focus one pane that way, and stay where it is at the edge.

    **The plan says which pane that is**, in the same words as the
    title bar, so a key and a bar never name different panes, and the
    key works before the first frame. Lillecarl/pymux#217.
    """
    window = pymux.arrangement.get_active_window()
    if window is None:
        return

    active = window.active_pane

    if active is None:
        return

    beside = pane_beside(pymux, window, active, side)
    if beside is not None:
        window.active_pane = beside
