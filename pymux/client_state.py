"""
One attached client: the application it draws, what it shows, and
what its person is in the middle of.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, ClassVar, override

import anyio
import anyio.abc
from prompt_toolkit.application import Application
from prompt_toolkit.application.current import get_app, set_app
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.clipboard import DynamicClipboard, InMemoryClipboard
from prompt_toolkit.completion import Completer, DynamicCompleter
from prompt_toolkit.cursor_shapes import CursorShape, CursorShapeConfig
from prompt_toolkit.document import Document
from prompt_toolkit.enums import EditingMode
from prompt_toolkit.filters import Condition
from prompt_toolkit.key_binding.vi_state import InputMode
from prompt_toolkit.layout.layout import Layout
from prompt_toolkit.selection import SelectionState, SelectionType
from prompt_toolkit.styles import (
    BaseStyle,
    ConditionalStyleTransformation,
    DynamicStyle,
    SwapLightAndDarkStyleTransformation,
)
from ptterm import Terminal
from pyte.keep import Keep

from .actions import Origin, started_by
from .arrangement import Pane, Window
from .client_options import ClientOptions
from .colors import DefaultColors
from .commands.completer import create_command_completer
from .enums import (
    CHOOSE,
    COMMAND,
    PROMPT,
    Chooser,
    EditingKeys,
    Woke,
)
from .graphics import PaneView
from .ids import PaneId
from .layout import LayoutManager
from .log import logger
from .nearest import NEAREST
from .notifications import (
    Notification,
)
from .plane import RESIZE_SHAPES
from .session import Session
from .style import DEFAULT_THEME, theme
from .window_pane import WindowWidget

if TYPE_CHECKING:
    from prompt_toolkit.input import Input
    from prompt_toolkit.layout.screen import WritePosition
    from prompt_toolkit.output import ColorDepth, Output

    from .border_drag import BorderDrag
    from .graphics import WindowView
    from .main import Pymux
    from .server import ServerConnection
    from .url_mode import UrlMode

__all__ = ["BEHIND_INTERVAL", "ClientState", "PaneCursor"]

#: The shapes of DECSCUSR, as prompt_toolkit names them. The odd numbers
#: blink. Number 0 means "the shape this terminal starts with", and
#: `ptterm` turns it into 1 before it gets here.
CURSOR_SHAPES = {
    1: CursorShape.BLINKING_BLOCK,
    2: CursorShape.BLOCK,
    3: CursorShape.BLINKING_UNDERLINE,
    4: CursorShape.UNDERLINE,
    5: CursorShape.BLINKING_BEAM,
    6: CursorShape.BEAM,
}


class PaneCursor(CursorShapeConfig):
    """
    The cursor of a client is the cursor of the pane it looks at.

    A pane keeps its own shape, because a program in it writes DECSCUSR
    or mode 12 and expects the terminal to obey. Two clients on one
    session can look at different panes, so each one asks its own
    question here.

    prompt_toolkit hands the application to `get_cursor_shape`. That
    matters: `get_app()` names the client that is current, and during a
    render of one client another may be current.
    """

    def __init__(self, pymux: Pymux) -> None:
        self.pymux = pymux

    @override
    def get_cursor_shape(self, application: Application[Any]) -> CursorShape:
        session = self.pymux.session_of(application)
        pane = session.overlay_pane
        if pane is None:
            pane = session.arrangement.get_active_pane_for(application)
        if pane is None:
            return CursorShape._NEVER_CHANGE

        # A pane that never asked for a shape gives the cursor back.
        # The person running pymux chose the cursor of their terminal,
        # and a pane that names a shape anyway takes that choice away: a
        # blinking block would land on top of the beam they set, for no
        # reason but that a pane has to hold some value.
        #
        # `DEFAULT` and not `_NEVER_CHANGE`, because the pane before
        # this one may have asked. Then the shape it set is on the
        # terminal now, and saying nothing would leave it there.
        # A window pane shows the person's own cursor nowhere: the
        # window draws its own.
        if not isinstance(pane.terminal, Terminal):
            return CursorShape.DEFAULT
        screen = pane.terminal.screen
        if not screen.cursor_style_asked:
            return CursorShape.DEFAULT

        return CURSOR_SHAPES.get(screen.cursor_style, CursorShape._NEVER_CHANGE)


#: How often a client whose terminal fell behind looks again whether it
#: caught up, in seconds: its next frame draws within this of it.
#: Lillecarl/pymux#585.
BEHIND_INTERVAL = 0.1


class ClientState:
    """
    State information that is independent for each client.
    """

    #: What a hot upgrade does with each attribute; `pyte.keep` says.
    #: This is the record a reconnecting client gets back
    #: (Lillecarl/pymux#409): what a person set and what they were in
    #: the middle of. The application around it is built again. The
    #: strip views live on `layout_manager`, keyed by window, and are
    #: saved by window id.
    KEEP: ClassVar[dict[str, Keep]] = {
        "session": Keep.SAVED,
        "previous_session": Keep.SAVED,
        "options": Keep.SAVED,
        "last_used": Keep.SAVED,
        "terminal_focused": Keep.DROPPED,
        "read_only": Keep.SAVED,
        "watched_clipboard": Keep.DROPPED,
        "ignore_size": Keep.SAVED,
        "matched": Keep.REBUILT,  # the nearest theme to the colours the terminal answers
        # Where the person was: a prefix held, a key table, a prompt,
        # a question, a menu, a chooser.
        "has_prefix": Keep.SAVED,
        "url_mode": Keep.DROPPED,
        "border_drag": Keep.DROPPED,
        "key_tables": Keep.SAVED,
        "message": Keep.SAVED,
        "confirmations": Keep.SAVED,
        "prompt_text": Keep.SAVED,
        "prompt_command": Keep.SAVED,
        "prompt_buffer": Keep.SAVED,
        "command_buffer": Keep.SAVED,
        "display_popup": Keep.SAVED,
        "menu_entries": Keep.SAVED,
        "menu_title": Keep.SAVED,
        "chooser_return_to": Keep.SAVED,
        "chooser": Keep.SAVED,
        "choose_window_index": Keep.SAVED,
        "choose_window_along": Keep.DROPPED,
        "choose_window_filter": Keep.SAVED,
        "choose_window_command": Keep.SAVED,
        "popups": Keep.DROPPED,  # a few seconds each
        # The application and what it is made of.
        "pymux": Keep.REBUILT,
        "connection": Keep.REBUILT,
        "app": Keep.REBUILT,
        "input": Keep.REBUILT,
        "output": Keep.REBUILT,
        "color_depth": Keep.REBUILT,
        "layout_manager": Keep.REBUILT,
        "prompt_completer": Keep.REBUILT,
        "last_time_text": Keep.REBUILT,
        "input_tick": Keep.DROPPED,
        "schedule_tick": Keep.DROPPED,
        "_waiting_for_drain": Keep.DROPPED,
        "_paced_while_behind": Keep.DROPPED,
        "_frame_waiters": Keep.DROPPED,
        "frame_interval": Keep.DROPPED,
        # A command's client lives for one command, and no command
        # spans an upgrade.
        "temporary": Keep.DROPPED,
        "caller_pane_id": Keep.DROPPED,
    }

    def __init__(
        self,
        pymux: Pymux,
        input: Input,
        output: Output,
        color_depth: ColorDepth,
        connection: ServerConnection,
        session: Session,
    ) -> None:
        self.pymux = pymux
        self.input = input
        self.output = output
        self.color_depth = color_depth
        #: Every route builds a `ServerConnection`, so its attributes
        #: are read plainly. A test's stand-in carries them too.
        #: Lillecarl/pymux#726.
        self.connection = connection

        #: The session this client looks at. `attach-session` and
        #: `switch-client` move it to another one. Every render path
        #: reads the windows through this, and never through
        #: `get_app()`. Lillecarl/pymux#323.
        self.session = session

        #: Where this client was before, for `switch-client -l`.
        self.previous_session: Session | None = None

        #: What a person set for this terminal. `pymux/client_options.py`
        #: says why each is the client's and not the server's.
        self.options = ClientOptions()
        self.options.owner = self

        #: What the search found for this terminal, or None while it
        #: has said nothing. It is not what a person wrote, so it is
        #: kept beside `theme` rather than in it: a client whose
        #: terminal reports a new background is matched again, and
        #: `show-client-options` says both. Lillecarl/pymux#346.
        self.matched: str | None = None

        #: Whether this client only watches. `attach-session -r` sets
        #: it, and nothing clears it: tmux's `-r` only sets as well
        #: (`cmd-attach-session.c:118`).
        #:
        #: Two things read it. A pane asks it for every key, every
        #: paste and every mouse event, through `may_type` on the
        #: widget. And a command refuses to run for such a client
        #: unless `add_command` marked it read only.
        #: Lillecarl/pymux#467.
        self.read_only = False

        #: What copy mode copies while `read_only`, in place of the
        #: paste buffer of the session.
        self.watched_clipboard = InMemoryClipboard()

        #: Whether this client is left out of the size of the plane.
        #: `attach-session -r` sets this too: a person who watches a
        #: session from a small terminal must not shrink it for the
        #: people working in it. tmux calls it `CLIENT_IGNORESIZE`.
        self.ignore_size = False

        #: True when the prefix key (Ctrl-B) has been pressed.
        self.has_prefix = False

        #: The labels `show-urls` put over a pane, while they are up.
        self.url_mode: UrlMode | None = None

        #: The line between two panes this client is dragging, from the
        #: press to the release. Lillecarl/pymux#772.
        self.border_drag: BorderDrag | None = None

        #: Ticks of the input this client sent against ticks of the
        #: frames it asked for. A redraw is scheduled, and input
        #: arrives before it runs: the frame it would draw predates
        #: the input, so the renderer skips it and asks for another
        #: one instead, which the input's own handling brings. Every
        #: schedule records the tick, so equality is the steady state
        #: and a frame whose input has been handled always draws.
        #: Lillecarl/pymux#524.
        self.input_tick = 0
        self.schedule_tick = 0

        #: A wait for this client's terminal to take its bytes is out,
        #: and redraws the window panes after. Lillecarl/pymux#629.
        self._waiting_for_drain = False

        #: Whether this client's redraws are paced because its terminal
        #: fell behind; `should_skip_render` sets and clears it.
        self._paced_while_behind = False

        #: Set by the next frame this client draws: `drawn_again`.
        self._frame_waiters: anyio.Event | None = None

        #: The cap the window's `frame-rate` sets, in seconds per frame.
        #: `sync_frame_rate` sets it and `pace` combines it with the
        #: pacing above.
        self.frame_interval: float | None = None

        #: The mode tables this client sits in, the innermost last.
        #: Empty when every key is read from root or prefix. A mode is
        #: client state, the way the chooser is: two people on one
        #: session manage panes separately.
        #: `enter-mode` and `leave-mode` move the top.
        #: Lillecarl/pymux#394.
        self.key_tables: list[str] = []

        #: Error/info message.
        self.message: str | None = None

        #: The questions waiting for a yes or a no, oldest first, each
        #: one a (text, command) pair.
        #:
        #: **A list and not one pair.** With one pair a second question
        #: silently replaces the first: a pane waiting on
        #: `confirm-before kill-pane` has its question taken by an
        #: `open-url` ask, and a `y` then opens the URL while the kill
        #: is lost with nothing said about either. Ask mode exists so
        #: that nothing happens without a yes, and losing a yes is the
        #: same surprise the other way round. Lillecarl/pymux#266.
        self.confirmations: list[tuple[str, str]] = []

        # When a "command-prompt" command is running.
        self.prompt_text: str | None = None
        self.prompt_command: str | None = None

        #: What completes the prompt, when anything does.
        #:
        #: A prompt that asks for a key knows what the answers are, and
        #: a prompt that asks for a window name does not. So the
        #: completer belongs to the question and not to the buffer, and
        #: `command-prompt -K` is what puts one there.
        #:
        #: **It also says the prompt draws in a box.** The completions
        #: are what needs the room: the menu that hangs off the cursor
        #: stops at twelve rows, and in the box it takes the height of
        #: the box. That is the argument `_command_palette` makes for
        #: the ":" line, and it is the same argument here.
        #: Lillecarl/pymux#220.
        self.prompt_completer: Completer | None = None

        # Popup.
        self.display_popup = False

        # Window chooser. The index is the row the chooser points
        # at, into the rows it lists; the command is the template a
        # `choose-window` command may carry, and the filter is what
        # its search types into. Typing narrows the list and puts
        # the point back on the first row. The filter and the index
        # are the chooser's, whichever kind shows -- a buffer chooser
        # runs the same keys over its own rows.
        # Lillecarl/pymux#295. Lillecarl/pymux#304.
        self.chooser: Chooser | None = None
        #: The notifications this client shows as boxes, newest last.
        #: Lillecarl/pymux#826.
        self.popups: list[Notification] = []
        self.choose_window_index = 0
        self.choose_window_command = ""
        #: The place along a line that j and k aim for. A shorter line
        #: clamps the point but not the aim, so walking back over it
        #: lands where the walk began. Any other move forgets it.
        self.choose_window_along: int | None = None

        #: Where Escape puts this client back, as (session, window).
        #: The window chooser has no preview of its own: it switches
        #: this client to the window it points at, so a person reads
        #: the real thing. Leaving it has to undo that.
        #: Lillecarl/pymux#327.
        self.chooser_return_to: tuple[Session, Window | None] | None = None
        self.choose_window_filter = Buffer(
            name=CHOOSE,
            multiline=False,
            accept_handler=self._accept_chooser,
            # Typing narrows the list, so the point goes back to the
            # first match -- and the window chooser switches to it,
            # which is how a search previews. Lillecarl/pymux#327.
            on_text_changed=lambda buffer: self.layout_manager.chooser.point_at(0),
        )

        # The menu that `display-menu` opened: a line per entry, as
        # (key, name, command), and what its title bar says. The key
        # of an entry is what runs it, and Escape leaves the menu.
        # Lillecarl/pymux#297.
        self.menu_entries: list[tuple[str, str, str]] = []
        self.menu_title = ""

        #: When a person last used this client, as a turn of
        #: `Pymux.client_was_used`. `window-size latest` reads it.
        self.last_used = 0

        #: Whether this client's terminal window has the focus, as its
        #: DEC 1004 reports say. A terminal that never reports has it.
        #: Lillecarl/pymux#795.
        self.terminal_focused = True

        #: True for the fake CLI that runs a command that arrived over
        #: a socket: it draws nothing, nobody uses it, and it is gone
        #: the moment the command answers. It must not win "the client
        #: a person used last", or a URL that `open-url` opens goes to
        #: a connection that closes before it can ask a browser to do
        #: anything.
        self.temporary = False

        #: The pane a socket command arrived from, by id, or None.
        #: `$PYMUX` names it ("socket,pane-id"), and the run-command
        #: packet carries it. An untargeted command defaults to this
        #: pane instead of the active one, so a program in a pane
        #: reaches its own pane without naming it -- tmux answers
        #: `TMUX_PANE` the same way. Only temporary clients carry
        #: one: a real client looks at its focus, and a pane that died
        #: since resolves to nothing, which falls back as if no pane
        #: was named at all.
        self.caller_pane_id: PaneId | None = None

        # What the last frame of this client drew of the strings that
        # time moves. The auto refresh compares against it, and asks
        # for a frame only when they differ. Lillecarl/pymux#154.
        self.last_time_text: tuple[str, ...] = ()

        # Input buffers.
        self.command_buffer = Buffer(
            name=COMMAND,
            accept_handler=self._handle_command,
            auto_suggest=AutoSuggestFromHistory(),
            history=pymux.prompt_history,
            multiline=False,
            complete_while_typing=False,
            completer=create_command_completer(pymux),
        )

        self.prompt_buffer = Buffer(
            name=PROMPT,
            accept_handler=self._handle_prompt_command,
            history=pymux.prompt_history,
            multiline=False,
            auto_suggest=AutoSuggestFromHistory(),
            # A person composing a key wants to see the keys while they
            # type, which is the whole point of the box. A prompt with
            # no completer completes nothing, so this costs the others
            # nothing.
            complete_while_typing=True,
            completer=DynamicCompleter(lambda: self.prompt_completer),
        )

        # Layout.
        self.layout_manager = LayoutManager(self.pymux, self)

        self.app = self._create_app()

        # Draw the images of the panes right after rendering. (The text
        # is on the screen by then; kitty draws images over it.)
        def after_render(_):
            # What this frame drew of the text that time moves, so that
            # the next refresh wakes this client for a change it has not
            # drawn, and not again for one it drew on its own.
            with set_app(self.app):
                self.last_time_text = self.layout_manager.what_time_moves()

            if self._frame_waiters is not None:
                self._frame_waiters.set()
                self._frame_waiters = None

            # A client that runs in the process that started pymux has
            # no connection, and there is nothing to draw images on.
            if self.connection is None:
                return

            # A line that moved away from a still pointer leaves its shape.
            if self.connection.pointer_shape_sent in RESIZE_SHAPES:
                self.pymux.terminals.sync_pointer_shape_of(self.connection)

            graphics = self.connection.graphics
            if not graphics.supported:
                return
            # Taken, so a frame the renderer skipped does not count them twice.
            drawn = self.app.renderer.last_rendered_screen
            if drawn is not None and drawn.scrolls_sent:
                graphics.scrolled(drawn.scrolls_sent)
                drawn.scrolls_sent = []
            try:
                graphics.render(self.graphics_views())
            except Exception:
                logger.exception("Drawing the pane images failed.")
            if self.connection.pipe_connection.outstanding:
                self._windows_after_drain()
                return
            try:
                graphics.render_windows(self._window_views())
            except Exception:
                logger.exception("Drawing the window panes failed.")

        self.app.before_render += self.layout_manager.frame_started
        self.app.after_render += after_render
        # Last, so the images above still read the frame's arrangement.
        self.app.after_render += self.layout_manager.frame_ended

    def should_skip_render(self) -> bool:
        """
        Whether a frame this client asked for is not wanted now, and
        when the next one is: the renderer skips it and asks for none.

        **A frame input arrived after is stale**, and the next one is
        asked for at once. Equality is the steady state: every scheduled
        redraw records the tick, so input that has been handled reads
        equal and the frame draws. Lillecarl/pymux#524.

        **The terminal has not read the last frame**, on any route.
        Over ssh the bytes wait where no route sees them, in sshd's
        buffers and the link, and only the terminal's answer to the
        status report behind the frame says they arrived. So a slow
        link carries the newest screen, a little late, and never a
        backlog of old ones; a keystroke's frame waits for one frame and
        not for thirty. Lillecarl/pymux#259. Nothing asks again here:
        the answer asks for the next frame. A skip that asked again
        spun the loop for as long as a key's urgency outran the pacing,
        a frame's cost each turn, and the request the answer made then
        waited for the pacing's timer. Lillecarl/pymux#824.

        **A terminal that fell behind on a route where the bytes pile up
        in this process** has no answer to wait for, so its skip asks
        again, paced until it caught up. The next frame is a diff
        against the last one drawn, so a skipped one loses nothing.
        Lillecarl/pymux#585.
        """
        if self.input_tick != self.schedule_tick:
            self.app.invalidate()
            return True
        if self.connection.pacing.waits_for_terminal:
            self.connection.pacing.frame_held = True
            return True
        pipe = self.connection.pipe_connection
        if pipe.holds_frames_while_behind and pipe.outstanding:
            if not self._paced_while_behind:
                self._paced_while_behind = True
                self.pace()
            self.app.invalidate()
            return True
        if self._paced_while_behind:
            self._paced_while_behind = False
            self.pace()
        return False

    def pace(self) -> None:
        """
        Set the redraw cap from the two things that ask for one.

        **One writer, because both write one knob.** `sync_frame_rate`
        runs on every invalidate, and a pane that animates invalidates
        many times a second. If it wrote the knob alone, it would clear
        the pacing a terminal that is behind needs, and the client would
        ask for a frame a thousand times a second. Lillecarl/pymux#811.
        """
        interval = self.frame_interval
        if self._paced_while_behind:
            interval = max(interval or 0.0, BEHIND_INTERVAL)
        self.app.min_redraw_interval = interval

    @property
    def active_key_table(self) -> str:
        """
        The table this client's next key is read from.

        The prefix first, because a prefix pressed inside a mode sits
        on top of it: one key is read from `prefix`, and the mode is
        still there when that key is done. Then the innermost mode,
        and `root` when no mode is on the stack.
        Lillecarl/pymux#394.
        """
        if self.has_prefix:
            return "prefix"
        if self.key_tables:
            return self.key_tables[-1]
        return "root"

    @property
    def default_colors(self) -> DefaultColors:
        """
        The two colours this client's own terminal draws with.

        Either may be `None`: a terminal that does not answer `OSC 11`
        says nothing, and so does a client with no connection, which is
        the one that runs in the process that started pymux.

        **This is a fact about one terminal**, which is why a client
        holds it and the session does not. Two people on one session
        can be on a light terminal and a dark one.
        Lillecarl/pymux#223.
        """
        if self.connection is None:
            return DefaultColors()
        return self.connection.default_colors

    def graphics_views(self) -> list[PaneView]:
        """
        One `PaneView` per pane whose images the outer terminal should
        show.

        A pane in clock mode or copy mode shows something else than its
        terminal content, and a popup covers the panes. Those show no
        images. (The list is empty then, so the previous images go
        away.) What this client drew over a pane goes with it, and its
        images are cut around that. Lillecarl/pymux#816.
        """
        if self.display_popup:
            return []

        covers = self.layout_manager.pane_covers
        result = []
        for pane, write_position in self.layout_manager.pane_write_positions.items():
            terminal = pane.terminal
            # A window pane's picture goes through `_window_views`.
            if pane.clock_mode or not isinstance(terminal, Terminal) or pane.is_copying_for(self.app):
                continue
            window = terminal.terminal_window
            result.append(
                PaneView(
                    pane_id=pane.pane_id,
                    x=write_position.xpos,
                    y=write_position.ypos,
                    width=write_position.width,
                    height=write_position.height,
                    vertical_scroll=window.vertical_scroll,
                    horizontal_scroll=window.horizontal_scroll,
                    graphics=terminal.screen.graphics,
                    screen=terminal.screen,
                    covered=covers.get(pane, ()),
                )
            )
        return result

    def refresh_window_pane(self, pane: Pane) -> None:
        """
        Send this client the new frame of a window pane it shows, and
        draw nothing else: the window drew, and the cells around it did
        not move. Lillecarl/pymux#636.
        """
        if self.connection is None or self.display_popup:
            return
        if self.connection.pipe_connection.outstanding:
            self._windows_after_drain()
            return
        write_position = self.layout_manager.pane_write_positions.get(pane)
        if write_position is None or not isinstance(pane.terminal, WindowWidget):
            return
        drained = self.connection.pacing.drained
        self.connection.graphics.refresh_window(
            self.pymux.wayland.view_of(pane.pane_id, pane.terminal, write_position, drained)
        )

    def _windows_after_drain(self) -> None:
        """
        Draw the window panes again once this client's terminal has
        taken what it holds. A window frame can be megabytes, and frames
        that queue behind each other only arrive late. Lillecarl/pymux#629.
        """
        if self._waiting_for_drain or self.connection is None:
            return
        self._waiting_for_drain = True
        connection = self.connection

        async def wait() -> None:
            try:
                await connection.pipe_connection.drained()
            finally:
                self._waiting_for_drain = False
            self.app.invalidate()

        connection._spawn(wait())

    async def drawn_again(self, timeout: float) -> None:
        """
        Draw this client again, and wait until it has. What a command
        changed shows then: the command line that ran it is closed.
        A client that draws nothing within `timeout` keeps its last frame,
        as one whose terminal fell behind does. Lillecarl/pymux#820.
        """
        if self._frame_waiters is None:
            self._frame_waiters = anyio.Event()
        drawn = self._frame_waiters
        self.app.invalidate()
        with anyio.move_on_after(timeout):
            await drawn.wait()

    def window_panes(self) -> list[tuple[Pane, WindowWidget, WritePosition]]:
        "Each window pane whose picture this client shows, and where. A popup covers them all."
        if self.display_popup:
            return []
        return [
            (pane, pane.terminal, write_position)
            for pane, write_position in self.layout_manager.pane_write_positions.items()
            if isinstance(pane.terminal, WindowWidget) and not pane.clock_mode
        ]

    def _window_views(self) -> list[WindowView]:
        "One `WindowView` per window pane this client can see."
        if self.connection is None:
            return []
        drained = self.connection.pacing.drained
        return [
            self.pymux.wayland.view_of(pane.pane_id, widget, write_position, drained)
            for pane, widget, write_position in self.window_panes()
        ]

    @property
    def command_mode(self):
        return get_app().layout.has_focus(COMMAND)

    # The question on screen is the oldest one waiting. These two read
    # it, so a filter and the bar below draw what a `y` will answer.
    @property
    def confirm_text(self) -> str | None:
        return self.confirmations[0][0] if self.confirmations else None

    @property
    def confirm_command(self) -> str | None:
        return self.confirmations[0][1] if self.confirmations else None

    def ask(self, text: str, command: str) -> None:
        "Put a question at the back of the queue."
        self.confirmations.append((text, command))

    def answer(self) -> str | None:
        "Take the question on screen away, and give back its command."
        if not self.confirmations:
            return None
        _text, command = self.confirmations.pop(0)
        return command

    def forget_questions(self) -> None:
        "Drop every question, answered by nobody."
        self.confirmations.clear()

    def _handle_command(self, buffer: Buffer) -> bool:
        "When text is accepted in the command line."
        text = buffer.text

        # First leave command mode. We want to make sure that the working
        # pane is focused again before executing the command handers.
        self.pymux.leave_command_mode(append_to_history=True)

        with started_by(Origin.PROMPT):
            self.pymux.handle_command(text)
        return False

    def _handle_prompt_command(self, buffer: Buffer) -> bool:
        "When a command-prompt command is accepted."
        text = buffer.text
        prompt_command = self.prompt_command

        # Leave command mode and handle command.
        self.pymux.leave_command_mode(append_to_history=True)
        if prompt_command is None:
            return False
        with started_by(Origin.PROMPT):
            self.pymux.handle_command(prompt_command.replace("%%", text))
        return False

    def _accept_chooser(self, buffer: Buffer) -> bool:
        "When the search of a chooser is accepted: it takes the row."
        self.layout_manager.chooser.choose_pointed()
        return False

    def _create_app(self):
        """
        Create `Application` instance for this .
        """
        pymux = self.pymux

        app = Application(
            output=self.output,
            input=self.input,
            # One buffer for the whole session. Copy mode in one client
            # and "paste-buffer" in another are the same buffer, and a
            # pane that writes the clipboard fills it as well. A client
            # that only watches writes nothing the others share, so its
            # copy mode copies into a buffer of its own.
            # Lillecarl/pymux#470.
            clipboard=DynamicClipboard(lambda: self.watched_clipboard if self.read_only else pymux.clipboard),
            # Read on every render: the detection of the outer
            # terminal can raise the depth after the app started.
            color_depth=lambda: self.color_depth,
            # The cursor belongs to the pane, so the client wears the
            # shape of the pane it looks at.
            cursor=PaneCursor(pymux),
            layout=Layout(container=self.layout_manager.layout),
            key_bindings=pymux.key_bindings_manager.key_bindings,
            mouse_support=Condition(lambda: self.session.options.mouse),
            full_screen=True,
            # Read on every render, so a theme chosen while a client is
            # attached reaches it without rebuilding the application.
            # This client's own theme, not the server's: two people on
            # one session may be at very different screens.
            # Lillecarl/pymux#223.
            style=DynamicStyle(lambda: self.style),
            style_transformation=ConditionalStyleTransformation(
                SwapLightAndDarkStyleTransformation(),
                Condition(lambda: self.options.swap_light_and_dark_colors),
            ),
            on_invalidate=pymux.client_asked_for_frame,
        )

        # Synchronize the Vi state with the CLI object.
        # (This is stored in the current class, but expected to be in the
        # CommandLineInterface.)
        def sync_vi_state(_):
            VI = EditingMode.VI
            EMACS = EditingMode.EMACS

            # Helix keys sit on top of the emacs ones, which is what
            # prompt_toolkit's insert mode is.
            if self.confirm_text or self.prompt_command or self.command_mode:
                app.editing_mode = VI if self.session.options.status_keys is EditingKeys.VI else EMACS
            else:
                app.editing_mode = VI if self.session.options.mode_keys is EditingKeys.VI else EMACS

        app.key_processor.before_key_press += sync_vi_state
        app.key_processor.after_key_press += sync_vi_state
        app.key_processor.after_key_press += self.sync_focus

        # Set render postpone time. (.1 instead of 0).
        # This small change ensures that if for a split second a process
        # outputs a lot of information, we don't give the highest priority to
        # rendering output. (Nobody reads that fast in real-time.)
        app.max_render_postpone_time = 0.1  # Second.

        # Hide message when a key has been pressed, and note that a
        # person is using this client: `window-size latest` follows
        # whichever terminal somebody last typed in.
        def key_pressed(_):
            self.message = None
            pymux.client_was_used(self)

        app.key_processor.before_key_press += key_pressed

        # A frame this client asked for is already stale when input
        # arrived after it was scheduled: the renderer skips it and
        # asks for another one instead, so the input is handled first.
        # Lillecarl/pymux#524.
        app.should_skip_render = self.should_skip_render

        # The frame that answers these keys goes now: postponing it
        # would hold the answer behind a busy loop for the whole wait.
        # Stamped when the keys are handled, and not when they arrive:
        # terminal replies ride the same packets and want no fast
        # frame. The stamp expires by itself, so a loop with no input
        # behind it postpones as always. Lillecarl/pymux#524.
        def rushed(_):
            app._urgent_until = time.time() + 0.2

        app.key_processor.before_key_press += rushed

        # The following code needs to run with the application active.
        # Especially, `startup` needs to know what the current
        # application is, in order to focus the new pane.
        with set_app(app):
            # Redraw all CLIs. (Adding a new client could mean that the others
            # change size, so everything has to be redrawn.)
            pymux.invalidate(Woke.CLIENT_ATTACHED)

            # Not here: making the first window waits for a process,
            # and this runs in `__init__`. Each route awaits `startup`
            # after `add_client` instead, under this same application.

        return app

    def sync_focus(self, *_):
        """
        Focus the focused window from the pymux arrangement.

        **Under this client's own application.** Focus is synced for
        every client at once, so the current one is rarely this one,
        and the layout this walks is built per application:
        `DynamicBody` keys its body on `get_app()`. Without this a
        client would be focused into the body of the client that asked.
        Lillecarl/pymux#323.
        """
        with set_app(self.app):
            self._sync_focus()
        self.pymux.sync_pane_focus()

    def modes(self) -> dict[str, Any]:
        """
        What this client is in the middle of, as plain values: what a
        snapshot holds and `take_modes` opens again.
        Lillecarl/pymux#551.
        """
        layout = self.layout_manager
        going_back = self.chooser_return_to
        with set_app(self.app):
            command_mode = self.command_mode
        return {
            "has_prefix": self.has_prefix,
            "key_tables": list(self.key_tables),
            "confirmations": [list(one) for one in self.confirmations],
            "prompt": [self.prompt_text, self.prompt_command, self.prompt_buffer.text] if self.prompt_command else None,
            "command": self.command_buffer.text if command_mode else None,
            "popup": (
                [layout.popup_dialog.title, layout._popup_textarea.text]
                if self.display_popup and layout.popup_dialog is not None and layout._popup_textarea is not None
                else None
            ),
            "menu": [self.menu_title, [list(entry) for entry in self.menu_entries]] if self.menu_entries else None,
            "chooser": self.chooser,
            "choose_window_index": self.choose_window_index,
            "choose_window_command": self.choose_window_command,
            "choose_window_filter": self.choose_window_filter.text,
            "chooser_return_to": (
                [going_back[0].session_id, going_back[1].window_id]
                if going_back is not None and going_back[1] is not None
                else None
            ),
            "copying": self._copy_modes(),
        }

    def _copy_modes(self) -> list[list[Any]]:
        """
        Each pane this client reads the history of: the pane, the caret,
        and where the selection starts and what it selects.

        The copy itself is not saved. It is read from the screen again,
        which has moved on by then, so a caret can land on another line
        than the one it stood on. Lillecarl/pymux#470.
        """
        copying = []
        for pane_id, pane in sorted(self.pymux.panes_by_id.items()):
            view = pane.terminal.copy_views.of(self.app)
            if view is None:
                continue
            selection = view.buffer.selection_state
            copying.append(
                [
                    pane_id,
                    view.buffer.cursor_position,
                    selection.original_cursor_position if selection is not None else None,
                    selection.type.value if selection is not None else None,
                ]
            )
        return copying

    def take_modes(self, modes: dict[str, Any]) -> None:
        """
        Open again what `modes` says this client was in the middle of,
        through the same calls a key opens each with, so each takes the
        keyboard the way it does then. A prompt or a command line goes
        last: it holds the keyboard over a chooser it was asked from.
        """
        layout = self.layout_manager
        with set_app(self.app):
            self.has_prefix = modes["has_prefix"]
            self.key_tables = list(modes["key_tables"])
            self.confirmations = [(text, command) for text, command in modes["confirmations"]]

            # A record from the build before holds none.
            for pane_id, caret, start, kind in modes.get("copying", []):
                pane = self.pymux.panes_by_id.get(pane_id)
                view = pane.terminal.enter_copy_mode() if pane is not None else None
                if view is None:
                    continue
                buffer = view.buffer
                buffer.cursor_position = min(caret, len(buffer.text))
                if start is not None:
                    buffer.selection_state = SelectionState(min(start, len(buffer.text)), SelectionType(kind))

            if modes["popup"] is not None:
                layout.display_popup(*modes["popup"])
            if modes["menu"] is not None:
                title, entries = modes["menu"]
                layout.menu.show([tuple(entry) for entry in entries], title)

            chooser = modes["chooser"]
            if chooser is not None:
                if chooser == Chooser.WINDOW:
                    layout.chooser.display_chooser(modes["choose_window_command"])
                else:
                    layout.chooser.display_box_chooser(Chooser(chooser))
                self.choose_window_filter.text = modes["choose_window_filter"]
                self.choose_window_index = modes["choose_window_index"]
                going_back = modes["chooser_return_to"]
                if going_back is not None:
                    session_id, window_id = going_back
                    for session in self.pymux.sessions:
                        for window in session.arrangement.windows:
                            if session.session_id == session_id and window.window_id == window_id:
                                self.chooser_return_to = (session, window)

            if modes["prompt"] is not None:
                self.prompt_text, self.prompt_command, text = modes["prompt"]
                self.prompt_buffer.reset(Document(text))
                self.app.layout.focus(self.prompt_buffer)
                self.app.vi_state.input_mode = InputMode.INSERT
            elif modes["command"] is not None:
                self.prompt_text = ""
                self.prompt_command = ""
                self.command_buffer.reset(Document(modes["command"]))
                self.app.layout.focus(self.command_buffer)
                self.app.vi_state.input_mode = InputMode.INSERT

    @property
    def theme_in_use(self) -> str:
        """
        The theme this client really draws with.

        The one a person named, or -- when that is `nearest` -- the
        known theme the search found for this terminal. A terminal
        that has said nothing yet leaves the default standing, so the
        first frame draws in it and a later frame draws in the match.
        Lillecarl/pymux#346.
        """
        if self.options.theme != NEAREST:
            return self.options.theme
        return self.matched or DEFAULT_THEME

    @property
    def style(self) -> BaseStyle:
        "The colour scheme of the theme this client is on."
        return theme(self.theme_in_use)

    def _sync_focus(self):
        # Pop-up displayed?
        if self.display_popup:
            dialog = self.layout_manager.popup_dialog
            if dialog is not None:
                self.app.layout.focus(dialog)
            return

        # A question takes no focus: its keys are global bindings, and
        # they come after the pane's, so they win. The pane keeps the
        # keyboard, and a window switched to under a question draws its
        # active pane. Lillecarl/pymux#551.

        # Custom prompt.
        if self.prompt_command:
            return  # Focus prompt

        # Command mode.
        if self.command_mode:
            return  # Focus command

        # A chooser owns the keyboard while it shows. It moves its own
        # focus -- the rows hold it, and `/` gives it to the search
        # line -- and this hook runs after every key press, so without
        # this branch the focus goes straight back to the pane and the
        # search line can never be typed into. Every other key of the
        # chooser works, because its bindings ask what shows and not
        # what has the focus, which is why only the search broke.
        # Lillecarl/pymux#161, Lillecarl/pymux#337.
        if self.chooser is not None:
            return

        # An overlay pane takes the keyboard while it is open.
        if self.session.overlay_pane is not None:
            self.app.layout.focus(self.session.overlay_pane.terminal)
            return

        # No windows left, return. We will quit soon.
        if not self.session.arrangement.windows:
            return

        pane = self.session.arrangement.get_active_pane_for(self.app)
        if pane is None:
            return
        self.app.layout.focus(pane.terminal)
