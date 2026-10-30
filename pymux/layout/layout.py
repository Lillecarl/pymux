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
from prompt_toolkit.layout.screen import Screen
from prompt_toolkit.mouse_events import MouseEvent, MouseEventType
from prompt_toolkit.widgets import Dialog, SearchToolbar, TextArea

from pymux.core import window_tree

from ..chooser import ChooserView
from ..enums import Chooser, EditingKeys
from ..filters import WaitsForConfirmation
from ..format import format_pymux_string
from ..log import logger
from ..wayland.window_pane import WindowWidget
from .border_drag import DragCapture, holds_window_panes, press_on_line
from .boxes import BOX_BOTTOM, BOX_SIDE, BOX_TOP, PALETTE_HEADER
from .geometry import bar_below_is_drawn, layout_of, pane_beside, room_for_panes, tell_pane_its_size
from .menu import Menu
from .pane_container import TracePaneWritePosition, create_container_for_process, pane_containers, pane_write_positions
from .plan_container import PlanContainer
from .plane import Plan, Rect, Side, View
from .status_line import StatusLine
from .widgets import Background, ConfirmationToolbar, MessageToolbar, UrlLabels
from .z_index import Z_INDEX

if TYPE_CHECKING:
    from prompt_toolkit.filters import Filter
    from prompt_toolkit.key_binding.key_bindings import NotImplementedOrNone
    from prompt_toolkit.layout.controls import BufferControl

    from pymux.connection.client_state import ClientState
    from pymux.core.main import Pymux
    from pymux.notify.notifications import Notification

__all__ = ["LayoutManager"]

#: A notification box, in cells: a width that fits a title, and the
#: rows of body it shows. Lillecarl/pymux#826.
POPUP_WIDTH = 44
MIN_POPUP_WIDTH = 12
POPUP_BODY_ROWS = 3

#: What `clock-mode` draws inside a pane, as text. `BigClock` paints the
#: hour and the minute in big numbers, and nothing else of it moves.
CLOCK_FORMAT = "%H:%M"


#: What the frame drew over each pane, on that screen, once something
#: asked.
_PANE_COVERS = "pymux_pane_covers"


def pane_covers(screen: Screen) -> dict[window_tree.Pane, tuple[Rect, ...]]:
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
    covers: dict[window_tree.Pane, tuple[Rect, ...]] | None = getattr(screen, _PANE_COVERS, None)
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
        self._overlay_for_pane: tuple[window_tree.Pane, Container] | None = None

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
        self.frame_plans: dict[window_tree.Window, Plan] | None = None

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
    def pane_write_positions(self) -> dict[window_tree.Pane, WritePosition]:
        """
        Where each pane was drawn, in the last render of this client.

        The positions live on the screen that carried them, so a client
        reads its own and never another's. Before the first render, and
        on a frame that the renderer threw away, there are none.
        """
        screen = self.client_state.app.renderer.last_rendered_screen
        if screen is None:
            return {}
        return pane_write_positions(screen)

    @property
    def pane_covers(self) -> dict[window_tree.Pane, tuple[Rect, ...]]:
        "What the last render of this client drew over each pane: `pane_covers`."
        screen = self.client_state.app.renderer.last_rendered_screen
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
        plane = cast(Size, self.pymux.planes.size())
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
                    height=lambda: D(min=1, max=self.palette_rows() - 1),
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
        return bool(self.client_state.popups) and self.cursor_is_in_top_right() is not top_right

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

    def cursor_on_view(self) -> Point:
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
        screen = app.renderer.last_rendered_screen
        if screen is None:
            return Point(0, 0)
        window = app.layout.current_window
        return screen.get_cursor_position(window)

    def cursor_is_in_top_right(self) -> bool:
        """
        Whether the cursor sits in the quadrant the box would rather
        draw in. The half the status bar takes is part of the lower
        half; it is where a cursor below the middle sits either way.
        Lillecarl/pymux#29.
        """
        point = self.cursor_on_view()
        room = self.room_this_client_has
        return point.y * 2 < room.rows and point.x * 2 >= room.columns

    def palette_rows(self) -> int:
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
                height=lambda: D(min=1, max=self.palette_rows()),
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
                    filter=which_key_shows & Condition(lambda: self.cursor_is_in_top_right()),
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
                        filter=which_key_shows & Condition(lambda: not self.cursor_is_in_top_right()),
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
    The dynamic part, which is different for each client. It
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

    def view_of(self, window: window_tree.Window) -> View:
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
                    filter=Condition(lambda: bar_below_is_drawn(self.pymux, window)),
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


def _create_panes(pymux: Pymux, window: window_tree.Window, view: View) -> PlanContainer:
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
    containers = {pane: create_container_for_process(pymux, window, pane) for pane in window.panes}

    def tell_its_size(pane: window_tree.Pane, rect: Rect) -> None:
        if isinstance(pane.terminal, WindowWidget) and holds_window_panes(pymux, window):
            return
        tell_pane_its_size(pane, rect)

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
