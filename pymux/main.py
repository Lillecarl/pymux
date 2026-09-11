import asyncio
import base64
import contextvars
import os
import shlex
import signal
import sys
import tempfile
import time
import traceback
import weakref
from typing import Callable, Tuple

from prompt_toolkit.application import Application
from prompt_toolkit.application.current import get_app, set_app
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.clipboard import ClipboardData, InMemoryClipboard
from prompt_toolkit.completion import Completer, DynamicCompleter
from prompt_toolkit.cursor_shapes import CursorShape, CursorShapeConfig
from prompt_toolkit.data_structures import Size
from prompt_toolkit.enums import EditingMode
from prompt_toolkit.filters import Condition
from prompt_toolkit.input.defaults import create_input
from prompt_toolkit.key_binding.vi_state import InputMode
from prompt_toolkit.layout.layout import Layout
from prompt_toolkit.output.defaults import create_output
from prompt_toolkit.styles import (
    BaseStyle,
    ConditionalStyleTransformation,
    DynamicStyle,
    SwapLightAndDarkStyleTransformation,
)
from ptterm import Terminal
from pyte.environment import terminal_name
from pyte.keys import KeyboardFlag
from pyte.osc import Osc

from .arrangement import Arrangement, Pane, Window
from .colors import DefaultColors
from .commands.commands import call_command_handler, handle_command
from .commands.completer import create_command_completer
from .enums import COMMAND, PROMPT, WindowSize, Woke
from .graphics import PaneView
from . import introspect
from .key_bindings import PymuxKeyBindings
from .key_spelling import why_a_pane_cannot_read
from .layout import Justify, LayoutManager, change_pane_size
from . import log
from .log import logger
from .notifications import NotificationRoutes
from .options import ALL_OPTIONS, ALL_WINDOW_OPTIONS, ExtendedKeys
from .osc import build_osc, open_url_of
from .pipes import bind_and_listen_on_socket, connect_in_memory
from .prompt_toolkit_compat import apply_prompt_toolkit_compat_fixes
from .rc import STARTUP_COMMANDS
from .server import ServerConnection
from .style import DEFAULT_THEME, THEMES, theme
from .utils import get_default_shell

__all__ = [
    "Pymux",
]

apply_prompt_toolkit_compat_fixes()


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

    def __init__(self, pymux: "Pymux") -> None:
        self.pymux = pymux

    def get_cursor_shape(self, application) -> CursorShape:
        pane = self.pymux.overlay_pane
        if pane is None:
            pane = self.pymux.arrangement.get_active_pane_for(application)
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
        screen = pane.screen
        if not screen.cursor_style_asked:
            return CursorShape.DEFAULT

        return CURSOR_SHAPES.get(screen.cursor_style, CursorShape._NEVER_CHANGE)


#: How many keys one pane remembers saying it could not read. A held
#: key repeats, and one line per repeat is a log nobody reads. A
#: keyboard has fewer keys than this.
MAX_KEYS_TO_REMEMBER = 512


def _say_a_key_did_not_fit():
    """
    A reporter for one pane, for a key it reads as something else.

    **Once per key, not once per repeat.** The same shape
    `KittyVt100Parser._say_it_was_dropped` uses for a key pymux itself
    cannot name, so the two failures read alike. Lillecarl/pymux#238.
    """
    already_said: set = set()

    def say(event, lost: int, encoded: str) -> None:
        if event in already_said:
            return
        if len(already_said) < MAX_KEYS_TO_REMEMBER:
            already_said.add(event)
        logger.info("%s", why_a_pane_cannot_read(event, lost, encoded))

    return say


class ClientState:
    """
    State information that is independent for each client.
    """

    def __init__(self, pymux: "Pymux", input, output, color_depth, connection) -> None:
        self.pymux = pymux
        self.input = input
        self.output = output
        self.color_depth = color_depth
        self.connection = connection

        #: True when the prefix key (Ctrl-B) has been pressed.
        self.has_prefix = False

        #: Error/info message.
        self.message = None

        # When a "confirm-before" command is running,
        # Show this text in the command bar. When confirmed, execute
        # confirm_command.
        self.confirm_text = None
        self.confirm_command = None

        # When a "command-prompt" command is running.
        self.prompt_text = None
        self.prompt_command = None

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

        #: When a person last used this client, as a turn of
        #: `Pymux.client_was_used`. `window-size latest` reads it.
        self.last_used = 0

        #: True for the fake CLI that runs a command that arrived over
        #: a socket: it draws nothing, nobody uses it, and it is gone
        #: the moment the command answers. It must not win "the client
        #: a person used last", or a URL that `open-url` opens goes to
        #: a connection that closes before it can ask a browser to do
        #: anything.
        self.temporary = False

        # What the last frame of this client drew of the strings that
        # time moves. The auto refresh compares against it, and asks
        # for a frame only when they differ. Lillecarl/pymux#154.
        self.last_time_text: Tuple[str, ...] = ()

        # Input buffers.
        self.command_buffer = Buffer(
            name=COMMAND,
            accept_handler=self._handle_command,
            auto_suggest=AutoSuggestFromHistory(),
            multiline=False,
            complete_while_typing=False,
            completer=create_command_completer(pymux),
        )

        self.prompt_buffer = Buffer(
            name=PROMPT,
            accept_handler=self._handle_prompt_command,
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

        # Whatever the frame before this one left behind goes now: the
        # plan it measured is an answer about the window as it was.
        def before_render(_):
            self.layout_manager.before_a_frame()

        self.app.before_render += before_render

        # Draw the images of the panes right after rendering. (The text
        # is on the screen by then; kitty draws images over it.)
        def after_render(_):
            # A client that runs in the process that started pymux has
            # no connection, and there is nothing to draw images on.
            if self.connection is None:
                return

            graphics = self.connection.graphics
            if not graphics.supported:
                return
            try:
                graphics.render(self._graphics_views())
            except Exception:
                logger.exception("Drawing the pane images failed.")

        self.app.after_render += after_render

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

    def _graphics_views(self):
        """
        One `PaneView` per pane whose images the outer terminal should
        show.

        A pane in clock mode or copy mode shows something else than its
        terminal content, and a popup covers the panes. Those show no
        images. (The list is empty then, so the previous images go
        away.)
        """
        if self.display_popup:
            return []

        result = []
        for pane, write_position in self.layout_manager.pane_write_positions.items():
            if pane.clock_mode or pane.terminal.is_copying:
                continue
            window = pane.terminal.terminal_window
            result.append(
                PaneView(
                    pane_id=pane.pane_id,
                    x=write_position.xpos,
                    y=write_position.ypos,
                    width=write_position.width,
                    height=write_position.height,
                    vertical_scroll=window.vertical_scroll,
                    horizontal_scroll=window.horizontal_scroll,
                    graphics=pane.screen.graphics,
                    screen=pane.screen,
                )
            )
        return result

    @property
    def command_mode(self):
        return get_app().layout.has_focus(COMMAND)

    def _handle_command(self, buffer):
        "When text is accepted in the command line."
        text = buffer.text

        # First leave command mode. We want to make sure that the working
        # pane is focused again before executing the command handers.
        self.pymux.leave_command_mode(append_to_history=True)

        # Execute command.
        self.pymux.handle_command(text)

    def _handle_prompt_command(self, buffer):
        "When a command-prompt command is accepted."
        text = buffer.text
        prompt_command = self.prompt_command

        # Leave command mode and handle command.
        self.pymux.leave_command_mode(append_to_history=True)
        self.pymux.handle_command(prompt_command.replace("%%", text))

    def _create_app(self):
        """
        Create `Application` instance for this .
        """
        pymux = self.pymux

        def on_focus_changed():
            """When the focus changes to a read/write buffer, make sure to go
            to insert mode. This happens when the ViState was set to NAVIGATION
            in the copy buffer."""
            vi_state = app.vi_state

            if app.current_buffer.read_only():
                vi_state.input_mode = InputMode.NAVIGATION
            else:
                vi_state.input_mode = InputMode.INSERT

        app = Application(
            output=self.output,
            input=self.input,
            # One buffer for the whole session. Copy mode in one client
            # and "paste-buffer" in another are the same buffer, and a
            # pane that writes the clipboard fills it as well.
            clipboard=pymux.clipboard,
            # Read on every render: the detection of the outer
            # terminal can raise the depth after the app started.
            color_depth=lambda: self.color_depth,
            # The cursor belongs to the pane, so the client wears the
            # shape of the pane it looks at.
            cursor=PaneCursor(pymux),
            layout=Layout(container=self.layout_manager.layout),
            key_bindings=pymux.key_bindings_manager.key_bindings,
            mouse_support=Condition(lambda: pymux.enable_mouse_support),
            full_screen=True,
            # Read on every render, so a theme chosen while a client is
            # attached reaches it without rebuilding the application.
            style=DynamicStyle(lambda: self.pymux.style),
            style_transformation=ConditionalStyleTransformation(
                SwapLightAndDarkStyleTransformation(),
                Condition(lambda: self.pymux.swap_dark_and_light),
            ),
            on_invalidate=pymux.client_asked_for_a_frame,
        )

        # Synchronize the Vi state with the CLI object.
        # (This is stored in the current class, but expected to be in the
        # CommandLineInterface.)
        def sync_vi_state(_):
            VI = EditingMode.VI
            EMACS = EditingMode.EMACS

            if self.confirm_text or self.prompt_command or self.command_mode:
                app.editing_mode = VI if pymux.status_keys_vi_mode else EMACS
            else:
                app.editing_mode = VI if pymux.mode_keys_vi_mode else EMACS

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

        # The following code needs to run with the application active.
        # Especially, `create_window` needs to know what the current
        # application is, in order to focus the new pane.
        with set_app(app):
            # Redraw all CLIs. (Adding a new client could mean that the others
            # change size, so everything has to be redrawn.)
            pymux.invalidate(Woke.CLIENT_ATTACHED)

            pymux.startup()

        return app

    def sync_focus(self, *_):
        """
        Focus the focused window from the pymux arrangement.
        """
        # Pop-up displayed?
        if self.display_popup:
            self.app.layout.focus(self.layout_manager.popup_dialog)
            return

        # Confirm.
        if self.confirm_text:
            return

        # Custom prompt.
        if self.prompt_command:
            return  # Focus prompt

        # Command mode.
        if self.command_mode:
            return  # Focus command

        # An overlay pane takes the keyboard while it is open.
        if self.pymux.overlay_pane is not None:
            self.app.layout.focus(self.pymux.overlay_pane.terminal)
            return

        # No windows left, return. We will quit soon.
        if not self.pymux.arrangement.windows:
            return

        pane = self.pymux.arrangement.get_active_pane()
        self.app.layout.focus(pane.terminal)


class Pymux:
    """
    The main Pymux application class.

    Usage:

        p = Pymux()
        p.listen_on_socket()
        p.run_server()

    Or, a server and one client in this process:

        p = Pymux()
        p.run_integrated(color_depth)

    Or, no client and no protocol at all:

        p = Pymux()
        p.run_standalone(color_depth)
    """

    def __init__(
        self,
        source_file=None,
        startup_command=None,
        session_name: str | None = None,
    ):
        self._client_states = {}  # connection -> client_state

        #: How many times a client has been used, over the session.
        #: `client_was_used` bumps it and stamps the client, and
        #: `window-size latest` reads the stamps.
        self._uses = 0

        # Options
        self.enable_mouse_support = True
        self.enable_status = True
        self.enable_pane_status = True  # False
        # Full screen: one pane takes every cell, and pymux draws
        # nothing of its own. It hides the status line and the pane
        # titlebar without changing what the person asked for, so
        # turning it off gives them both back.
        self.full_screen = False
        # Where the ":" command line is drawn. A box in the middle of
        # the screen has room for a completion beside what it means,
        # for a usage line, and for more than the twelve rows a menu
        # under the cursor can reach. tmux has no such thing.
        # Lillecarl/pymux#158.
        self.command_palette = False
        self.enable_bell = True
        self.enable_clipboard = True
        self.open_url_target = "last"
        self.open_url_mode = "open"
        self.open_url_shim = False
        self._open_url_shim_dir = None

        # The paste buffer of the session. Copy mode writes it, a pane
        # that writes the clipboard of the user writes it too, and
        # "paste-buffer" reads it. Every client shares this one.
        self.clipboard = InMemoryClipboard()
        self.remain_on_exit = False
        self.status_keys_vi_mode = False
        self.mode_keys_vi_mode = False
        # How many lines above the screen a pane keeps. tmux keeps two
        # thousand by default and this keeps the same, so a person who
        # moves over finds the depth they had. People do configure
        # more: ten thousand is common and kitty is often set to fifty
        # thousand, which is why `ptterm/tests/measure_instructions.py`
        # measures at all three. Lillecarl/pymux#8.
        self.history_limit = 2000

        # How many seconds between two redraws of the status bar. Four
        # is tmux's `status-interval`, and this keeps the same. It is
        # what the clock in the status bar costs when nothing else is
        # happening.
        self.status_interval = 4
        # What a pane is told it is. The entry of pyte describes what a
        # pane really does; a build without one falls back to xterm.
        #
        # `ptterm` already tells a pane this, so the option is here to
        # let a person say something else. It is `default-terminal` in
        # tmux, and it keeps that job.
        self.default_terminal = terminal_name()
        self.status_left = "[#S] "
        self.status_left_length = 20
        self.status_right = " %H:%M %d-%b-%y "
        self.status_right_length = 20
        self.window_status_current_format = "#I:#W#F"
        self.window_status_format = "#I:#W#F"
        self.session_name = "0"
        self.status_justify = Justify.LEFT
        self.default_shell = get_default_shell()
        self.swap_dark_and_light = False
        self.paint_screen = False

        self.options = ALL_OPTIONS
        self.window_options = ALL_WINDOW_OPTIONS

        #: Which pane a desktop notification came from. The terminal of
        #: the user answers a notification by its identifier, and every
        #: pane names its own without knowing about the others.
        self.notifications = NotificationRoutes()

        # When no panes are available.
        self.original_cwd = os.getcwd()

        self.display_pane_numbers = False

        #: List of clients.
        self._runs_standalone = False
        self.connections = []

        #: Kitty keyboard protocol flags last sent to the clients. (The
        #: flags of the focused pane; clients enable the protocol on
        #: their outer terminals accordingly.)
        self._kitty_flags_sent = None

        #: Make up the halves of a key event that the keyboard of a
        #: client cannot send: the release of a key, and the shifted
        #: key of a letter. With this, a pane gets what it asked for
        #: from any terminal; without it, a pane hears that it does
        #: not have it. ("synthesize-key-events".)
        self.synthesize_key_events = True

        #: How much of the keyboard this session uses.
        #: ("extended-keys".) `OFF` is the escape hatch, for a program
        #: that misbehaves under the extended encodings and for a
        #: person attaching with a terminal that claims more than it
        #: does. Lillecarl/pymux#173.
        self.extended_keys = ExtendedKeys.ON

        # May a program inside a pane resize that pane? DECSLPP and the
        # window resize sequences ask for it. Off, because a pane sits
        # in a layout that the person arranged.
        self.allow_program_resize = False

        # What the panes were last told about the keyboards of the
        # clients: the flags and the switch above. (None: nothing was
        # told yet.)
        self._keyboard_state_sent = None
        # Event loop for this server. (Python 3.14 doesn't have a global
        # "current event loop" anymore. Keep our own reference.)
        try:
            self.loop = asyncio.get_running_loop()
        except RuntimeError:
            self.loop = asyncio.new_event_loop()
            # ptterm still uses `asyncio.Future()` without an explicit loop,
            # which requires a current event loop. Set ours.
            asyncio.set_event_loop(self.loop)
        self.done_f = self.loop.create_future()

        # Command output, for commands that were entered from the command
        # line. (E.g. `pymux list-panes -F ...`.) When a run-command packet is
        # handled, this is a list where commands can append their output.
        # The server sends it back to the client before the connection is
        # closed. It's `None` for commands entered interactively.
        self.command_output: list | None = None
        self.command_error: list | None = None

        # The lines of the configuration file that failed, until a
        # client is there to be told about them.
        self.startup_errors: list[str] = []

        # The file and the line that `source-file` is reading now, so
        # that a failure can say which line it was.
        self.sourcing: str | None = None

        self._startup_done = False
        self.source_file = source_file
        self.startup_command = startup_command

        # Time when this server was started.
        self.created = time.time()

        #: What this server has done since it started. `pymux counters`
        #: reads it, and `pymux/introspect.py` says why the reasons
        #: matter more than the totals.
        self.counters = introspect.Counters()

        #: Whether another process of this user may attach a debugger.
        #: Set through `set-option allow-remote-debugging`, and the
        #: property below is what tells the kernel.
        self._allow_remote_debugging = False

        if session_name is not None:
            self.session_name = session_name

        # Keep track of all the panes, by ID. (For quick lookup.)
        self.panes_by_id = weakref.WeakValueDictionary()

        # Socket information.
        self.socket = None
        self.socket_name = None

        # Key bindings manager.
        self.key_bindings_manager = PymuxKeyBindings(self)

        self.arrangement = Arrangement()

        # The overlay pane: a pane that floats in the middle of the
        # screen over the layout, like the popup of tmux. It belongs to
        # the session, so every client sees the same one, and it takes
        # the keyboard while it is open.
        self.overlay_pane = None
        self.overlay_title = ""
        self.overlay_width = None
        self.overlay_height = None

        # Which colour scheme every client draws with.
        # `set-option theme <name>` picks another one. The name is what
        # is kept, because that is what a person set and can read back;
        # the scheme is derived from it. Lillecarl/pymux#194.
        self.theme = DEFAULT_THEME

    @property
    def style(self) -> BaseStyle:
        "The colour scheme of the theme this session is on."
        return theme(self.theme)

    @property
    def show_status(self) -> bool:
        "Whether a client draws the status line."
        return self.enable_status and not self.full_screen

    @property
    def show_pane_status(self) -> bool:
        "Whether a client draws the titlebar of a pane."
        return self.enable_pane_status and not self.full_screen

    def refresh_what_time_moves(self, but_not=None) -> None:
        """
        Ask for a frame from each client whose screen moved by itself.

        The clock in the status line is the reason the auto refresh
        exists. `#{...}` variables in the status line and in the
        titlebar of a pane are the rest of it, and `clock-mode` draws a
        clock inside a pane.

        A frame is 279,645 bytecode instructions, and the text that
        time moves is 2,371 of them. So this reads the text and draws
        only when it differs from what the last frame drew. A clock
        that says `%H:%M` changes once a minute, and the interval is
        four seconds, so fourteen of every fifteen ticks stop here.
        Lillecarl/pymux#154.

        A pane that writes invalidates its own client. It never needed
        the clock. Lillecarl/pymux#117.

        `but_not` is the application that already asked for a frame, so
        that `client_asked_for_a_frame` can ask about the others and
        leave that one alone.
        """
        for client_state in self._client_states.values():
            if client_state.app is but_not:
                continue

            # A `#{...}` variable asks which window this client looks
            # at, so each client reads its own text.
            with set_app(client_state.app):
                text = client_state.layout_manager.what_time_moves()

            if text != client_state.last_time_text:
                client_state.last_time_text = text
                # Named here and not through `invalidate`, because this
                # wakes the one client whose text moved and not all of
                # them. `Woke` says why the log carries the reason.
                logger.debug(
                    "Drawing 1 of the clients: the text that time moves changed to %r",
                    text,
                )
                client_state.app.invalidate()

    def client_asked_for_a_frame(self, app) -> None:
        """
        What one client's own invalidate means for the other clients.

        A pane that writes arrives here. prompt_toolkit invalidates the
        application whose layout holds that pane, and no other one:
        `Application._update_invalidate_events` attaches its handler to
        the controls it walks, and a client's layout holds the window
        it looks at. So the wake of the clients that can see the change
        already happened before this runs.

        Two things are left for the clients that cannot see it, and
        both are why this is not simply nothing. Lillecarl/pymux#224.

        - Every client's status line names the other windows, through
          `window-status-format`. `what_time_moves` reads exactly that
          text, so the clients whose window list moved wake and the
          rest do not.
        - The three syncs below. A program pushes kitty keyboard flags
          or asks for a pointer by writing, and this is the path that
          notices.

        With one client there are no others, so the common case pays
        for the syncs and nothing else.
        """
        self.counters.invalidated(Woke.APPLICATION)
        # DEBUG: this is the line that says a pane is animating. A
        # server drew eleven frames a second with nobody typing, and a
        # line each is what made one log 86 MB in four days.
        # Lillecarl/pymux#248.
        logger.debug("Drawing 1 of the clients: %s", Woke.APPLICATION)

        self.refresh_what_time_moves(but_not=app)

        self.sync_kitty_flags()
        self.sync_pointer_shape()
        self.sync_keyboard_source_flags()

    @property
    def allow_remote_debugging(self) -> bool:
        return self._allow_remote_debugging

    @allow_remote_debugging.setter
    def allow_remote_debugging(self, allowed: bool) -> None:
        """
        A setting the kernel has to be told about, so it is a property
        and not an attribute. `introspect.let_a_debugger_attach` says
        what it costs and why it is off.
        """
        self._allow_remote_debugging = introspect.let_a_debugger_attach(allowed)

    @property
    def log_level(self) -> str:
        return log.level()

    @log_level.setter
    def log_level(self, name: str) -> None:
        """
        Held by the logger and not by this object, so it is a property.

        A server can be told this while it runs, which is the only time
        it helps: `--log-level` is read before the server starts, and by
        then the thing worth logging has not happened yet.
        Lillecarl/pymux#252.
        """
        log.set_level(name)
        logger.info("The log level is %s from now on.", name)

    def server_starts(self) -> None:
        """
        What every route does before its loop turns.

        Three of them start a server -- the socket, `integrated` and
        `standalone` -- and each one needs the clock ticking and the
        signal taken.
        """
        self._start_auto_refresh()
        introspect.answer_a_signal()

    def _start_auto_refresh(self) -> None:
        """
        Refresh the clients every `status_interval` seconds, on the loop.

        On the loop, and not on a thread of its own. The refresh reads
        which window each client looks at, and `set_app` answers that
        by writing an `AppSession` that every client shares. A thread
        that writes it while a render reads it hands that render
        another client's application. Lillecarl/pymux#155.

        The callback arms the next one, so a `set-option
        status-interval` reaches the tick after this one. The loop does
        not have to run yet: the three callers arm this before they
        start it.
        """

        def tick() -> None:
            self.refresh_what_time_moves()
            self.loop.call_later(self.status_interval, tick)

        self.loop.call_later(self.status_interval, tick)

    @property
    def apps(self):
        return [c.app for c in self._client_states.values()]

    def get_client_state(self):
        "Return the active ClientState instance."
        app = get_app()
        for client_state in self._client_states.values():
            if client_state.app == app:
                return client_state

        raise ValueError("Client state for app %r not found" % (app,))

    def get_connection(self):
        "Return the active Connection instance."
        app = get_app()
        for connection, client_state in self._client_states.items():
            if client_state.app == app:
                return connection

        raise ValueError("Connection for app %r not found" % (app,))

    def startup(self):
        # Handle start-up comands.
        # (Does initial key bindings.)
        if not self._startup_done:
            self._startup_done = True

            # Execute default config.
            for cmd in STARTUP_COMMANDS.splitlines():
                self.handle_command(cmd)

            # Source the given file.
            if self.source_file:
                call_command_handler("source-file", self, [self.source_file])

            # Make sure that there is one window created.
            self.create_window(command=self.startup_command)

    def get_title(self):
        """
        The title to be displayed in the titlebar of the terminal.
        """
        w = self.arrangement.get_active_window()

        if w and w.active_pane:
            title = w.active_pane.screen.titles.window
        else:
            title = ""

        if title:
            return "%s - Pymux" % (title,)
        else:
            return "Pymux"

    def size_of_the_plane(self, window=None):
        """
        How big the plane of that window is, in cells.

        **A plane is shared and a view is not.** A pane has one pty, so
        it has one size however many clients look at it (decision 10 of
        `docs/layout-engine-plan.md`), and this is that one size. A
        client bigger than the plane draws background around it, and a
        client smaller than it moves its view over it.

        **The window's `window-size` option decides which client.**
        `smallest` is the default and what pymux always did; `largest`
        gives the plane to the biggest client watching, `latest` to
        whoever used one last, and `manual` to nobody. tmux has the
        same four, and leaves a client too small to see the whole
        window stuck at the top left of it; here that client moves its
        view instead.

        The status line comes off the bottom of a client's terminal,
        because it is not part of any window.
        `layout.room_for_the_panes` takes the rows the chrome
        around the panes wants off what is left. **A manual size is
        already the window's own**, so nothing comes off it.

        **Nobody watching is eighty by twenty**, and no status row
        comes off it. A window exists before a client attaches to it,
        and the program in a pane needs a size from the first byte it
        writes.
        """
        if window is None:
            window = self.arrangement.get_active_window()

        if window.window_size is WindowSize.MANUAL and window.manual_size is not None:
            return window.manual_size

        clients = self.clients_watching(window)

        if not clients:
            return Size(rows=20, columns=80)

        if window.window_size is WindowSize.LATEST:
            newest = max(clients, key=lambda client: client.last_used)
            size = newest.app.output.get_size()
        else:
            pick = max if window.window_size is WindowSize.LARGEST else min
            sizes = [client.app.output.get_size() for client in clients]
            size = Size(
                rows=pick(one.rows for one in sizes),
                columns=pick(one.columns for one in sizes),
            )

        return Size(
            rows=size.rows - (1 if self.show_status else 0),
            columns=size.columns,
        )

    def client_was_used(self, client_state) -> None:
        """
        Note that a person just used this client.

        `window-size latest` reads it: the plane belongs to whichever
        terminal somebody last typed in. A counter and not a clock,
        because two key presses in one millisecond still have an order
        and a clock may not say which came first.
        """
        self._uses += 1
        client_state.last_used = self._uses

    def clients_watching(self, window=None) -> "list[ClientState]":
        """
        Every client that is looking at that window.

        The active window of the session by default. A client shows one
        window at a time, and a client on another window says nothing
        about how big this one should be.
        """

        def active_window_for_app(app):
            with set_app(app):
                return self.arrangement.get_active_window()

        if window is None:
            window = self.arrangement.get_active_window()

        return [
            client_state
            for client_state in self._client_states.values()
            if not client_state.temporary
            and active_window_for_app(client_state.app) == window
        ]

    def _create_pane(
        self,
        window: Window | None = None,
        command: str | None = None,
        start_directory: str | None = None,
        on_done: Callable[[], None] | None = None,
    ):
        """
        Create a new :class:`pymux.arrangement.Pane` instance. (Don't put it in
        a window yet.)

        :param window: If a window is given, take the CWD of the current
            process of that window as the start path for this pane.
        :param command: If given, run this command instead of `self.default_shell`.
        :param start_directory: If given, use this as the CWD.
        """

        def done_callback():
            "When the process finishes."
            if on_done is not None:
                on_done()

            if not self.remain_on_exit:
                # Remove pane from layout.
                self.arrangement.remove_pane(pane)

                # No panes left? -> Quit.
                if not self.arrangement.has_panes:
                    self.stop()

                # Make sure the right pane is focused for each client.
                for client_state in self._client_states.values():
                    client_state.sync_focus()

            self.invalidate(Woke.PANE_ENDED)

        def bell():
            "Sound bell on all clients."
            if self.enable_bell:
                for c in self.apps:
                    c.output.bell()

        def forward_osc(code: str, param: str) -> None:
            "Pass an OSC sequence of this pane to the terminals of the clients."
            self.forward_osc(pane, code, param)

        def resize(lines: int | None, columns: int | None) -> None:
            "Give a pane the size that the program inside it asks for."
            self.resize_pane_for_program(pane, lines, columns)

        def may_resize() -> bool:
            """
            Would such an ask be granted?

            Read every time, because a person can turn the option on and
            off while the pane runs. A pane that answers "yes" and then
            gives no room sends a program to lay its output out for a
            width it does not have.
            """
            return self.allow_program_resize

        # Start directory.
        path: str | None

        if start_directory:
            path = start_directory
        elif window and window.active_process:
            # When the path of the active process is known,
            # start the new process at the same location.
            path = window.active_process.get_cwd()
        else:
            path = None

        def before_exec():
            "Called in the process fork (in the child process)."
            # Go to this directory.
            try:
                os.chdir(path or self.original_cwd)
            except OSError:
                pass  # No such file or directory.

            # A pane is not the terminal that the client attached
            # from: it answers the protocol queries for itself. ptterm
            # has already said so here, because it is the layer that
            # owns both the screen and the process
            # (Lillecarl/pymux#125). What is left is what only pymux
            # knows.
            #
            # The name is one of those. `default-terminal` is an option
            # a person can set, and this hook runs after ptterm's, so
            # what they set wins.
            os.environ["TERM"] = self.default_terminal

            # Make sure to set the PYMUX environment variable.
            if self.socket_name:
                os.environ["PYMUX"] = "%s,%i" % (self.socket_name, pane.pane_id)

            # The shim, when it is on, reaches the opener of this
            # session before anything else on the PATH of the pane,
            # and names it for what reads $BROWSER.
            self._shim_the_environment_of_a_pane()

        if command:
            # `shlex.split` and not `str.split`: a command reaches this as
            # one string, and the quoting inside it is what says where one
            # argument ends. A plain split on whitespace tears
            # `sh -c 'echo one two'` into seven words, and `sh` then reads
            # `'echo` as the whole of its script.
            #
            # `run_pymux.run` builds the string with `shlex.quote`, so this
            # undoes exactly what that did.
            command_list = shlex.split(command)
        else:
            command_list = [self.default_shell]

        # The shim directory has to exist in this process, before the
        # fork: a directory made in the child is made once per pane,
        # and two panes starting together would race for it.
        if self.open_url_shim:
            self._ensure_the_open_url_shim()

        # Create new pane and terminal.
        terminal = Terminal(
            done_callback=done_callback,
            bell_func=bell,
            osc_func=forward_osc,
            resize_func=resize,
            may_resize=may_resize,
            before_exec_func=before_exec,
            command=command_list,
            # The `history-limit` option, which said how far copy mode
            # could scroll and never reached the screen that holds the
            # rows. A pane kept two thousand whatever the option said.
            get_history_limit=lambda: self.history_limit,
            unreadable_key_func=_say_a_key_did_not_fit(),
        )
        pane = Pane(terminal)

        # ptterm starts the process only when the terminal is rendered for
        # the first time. Start it right away, so that panes in detached
        # sessions also run and produce output. (Like tmux does.)
        terminal_control = terminal.terminal_control
        if not terminal_control._running:
            process = terminal_control.process
            # Give the terminal a default size until a client attaches.
            process.set_size(80, 24)
            process.start()
            terminal_control._running = True
            # Now that the pty exists, apply the size to it as well.
            process.set_size(80, 24)

        # Keep track of panes. This is a WeakKeyDictionary, we only add, but
        # don't remove.
        self.panes_by_id[pane.pane_id] = pane

        # A pane that starts now missed the last walk of the panes.
        self.tell_pane_about_the_keyboard(pane)

        logger.info("Created process %r.", command_list)

        return pane

    def display_overlay(
        self,
        command: str | None = None,
        width: str | None = None,
        height: str | None = None,
        title: str | None = None,
    ):
        """
        Open an overlay pane in the middle of the screen.

        It runs `command`, or the default shell, and closes itself when
        that finishes. It takes the keyboard while it is open.

        The overlay belongs to the session, like a window does, so
        every client sees the same one and a second call replaces the
        first. That also means a command from the command line reaches
        it: the temporary client that runs such a command is gone
        before the next render.
        """
        self.close_overlay()

        pane: "arrangement.Pane" | None = None

        def done() -> None:
            "The program of the overlay finished, so the overlay goes."
            if self.overlay_pane is pane:
                self.overlay_pane = None
                self._sync_focus_everywhere()

        try:
            window = self.arrangement.get_active_window()
        except Exception:
            window = None

        pane = self._create_pane(window=window, command=command, on_done=done)

        self.overlay_pane = pane
        self.overlay_title = title or command or "overlay"
        self.overlay_width = width
        self.overlay_height = height
        self._sync_focus_everywhere()
        self.invalidate(Woke.OVERLAY_OPENED)

        return pane

    def close_overlay(self) -> None:
        """
        Close the overlay pane, and kill what runs in it.
        """
        pane = self.overlay_pane
        if pane is None:
            return

        self.overlay_pane = None

        process = pane.process
        if not process.is_terminated:
            process.kill()

        self._sync_focus_everywhere()
        self.invalidate(Woke.OVERLAY_CLOSED)

    def _sync_focus_everywhere(self) -> None:
        "Give every client the focus that its state asks for."
        for client_state in list(self._client_states.values()):
            try:
                with set_app(client_state.app):
                    client_state.sync_focus()
            except Exception:
                # An application that never ran has no layout to focus.
                logger.exception("Could not sync the focus of a client.")

    def invalidate(self, reason: str = Woke.APPLICATION):
        """
        Ask every client for a frame. `Woke` says why the reason is
        here, and holds every reason but the one that carries a name.
        """
        self.counters.invalidated(reason)
        # DEBUG: a server draws eleven frames a second when a pane
        # animates, and a line each is what made one log 86 MB in four
        # days. `pymux counters` holds the same reasons with nothing
        # written down. Lillecarl/pymux#248.
        logger.debug("Drawing %s of the clients: %s", len(self.apps), reason)

        # Whatever changed may have changed where the panes are, so no
        # client may answer that from the frame it drew before this.
        # The plan is worked out again on the next frame, which is what
        # this asks for. Lillecarl/pymux#217.
        for client_state in self._client_states.values():
            client_state.layout_manager.forget_the_plan()

        # **Before the invalidates, and here rather than on the path a
        # pane's write takes.** A window switch and a `set-option`
        # both come through here, and they are the only two things
        # that change the answer. A pane that writes does not.
        self.sync_the_frame_rate()

        for app in self.apps:
            app.invalidate()

        # The focused pane can have changed, or the process inside it
        # can have pushed/popped kitty keyboard protocol flags or asked
        # for a different pointer.
        self.sync_kitty_flags()
        self.sync_pointer_shape()
        # "set-option synthesize-key-events" takes effect here.
        self.sync_keyboard_source_flags()

    def sync_the_frame_rate(self) -> None:
        """
        Cap each client at the frame rate of the window it looks at.

        `Application.min_redraw_interval` is prompt_toolkit's own knob
        and it is the right one: a redraw that arrives too soon is not
        dropped, it is held until the interval is up and then drawn. So
        a cap loses no frame, it only refuses to draw the same thing
        twice in a thirtieth of a second.

        Never raises: this runs on the invalidate path, also for
        headless servers without a running prompt_toolkit application.
        """
        for client_state in self._client_states.values():
            try:
                with set_app(client_state.app):
                    window = self.arrangement.get_active_window()
            except Exception:
                # No window yet, or no application that ever ran.
                continue

            rate = getattr(window, "frame_rate", 0)
            client_state.app.min_redraw_interval = (1.0 / rate) if rate else None

    def get_focused_pane(self):
        """
        The pane that the keyboard of the active client reaches.

        An overlay pane takes the keyboard while it is open, so it is
        not always the active pane of the arrangement.

        Never raises: this runs on the invalidate path, also for
        headless servers without a running prompt_toolkit application.
        """
        if self.overlay_pane is not None:
            return self.overlay_pane

        try:
            return self.arrangement.get_active_pane()
        except Exception:
            # `get_active_window` needs a running application.
            return None

    def get_focused_kitty_flags(self) -> int:
        """
        The kitty keyboard protocol flags requested by the process in
        the focused pane. (Zero when the process did not request
        anything or when there is no focused pane.)

        Never raises: this runs on the invalidate path, also for
        headless servers without a running prompt_toolkit application.
        """
        pane = self.get_focused_pane()
        if pane is None:
            return 0
        return pane.screen.kitty_keyboard_flags

    def resize_pane_for_program(
        self, pane, lines: int | None, columns: int | None
    ) -> None:
        """
        Give a pane the size that the program inside it asks for.

        Three sequences ask: DECSLPP, and the two forms of a window
        resize. xterm has one window and does as it is told. A pane
        does not: it sits in a layout, and making one pane taller makes
        another shorter. So a program only gets its way when the person
        says it may, with "set-option allow-program-resize on".

        The layout holds weights and not cells, and a weight of one is
        about one cell, so the change is what "resize-pane" would do.
        The other panes give up the room, and the size of the window
        holds. So a pane cannot ask for more than there is.

        Never raises: this runs on the read path of a pane, and one
        sequence may not stop it.
        """
        if not self.allow_program_resize:
            return

        try:
            window = self._window_holding(pane)
            if window is None:
                return

            process = pane.process
            if lines is not None and process.sy:
                change_pane_size(self, window, pane, down=lines - process.sy)
            if columns is not None and process.sx:
                change_pane_size(self, window, pane, right=columns - process.sx)

            self.invalidate(Woke.PANE_RESIZED)
        except Exception:
            logger.exception("Failed to resize a pane for the program in it.")

    def _window_holding(self, pane):
        "The window that holds this pane, or None when it is gone."
        for window in self.arrangement.windows:
            if pane in window.panes:
                return window
        return None

    def clients_to_open_on(self) -> "list[ClientState]":
        """
        The clients that receive what "open-url" opens.

        `open-url-target last` is the client a person used last, by the
        same stamp that `window-size latest` reads. Nothing attached
        means nobody, and the person who asked hears so. The fake CLI
        of a command that arrived over a socket is never one of them:
        it is the connection that ran the command, and it closes while
        the answer is still being sent.
        """
        clients = [
            client
            for client in self._client_states.values()
            if not client.temporary
        ]
        if self.open_url_target != "broadcast" and clients:
            return [max(clients, key=lambda client: client.last_used)]
        return clients

    def open_url(self, url: str, confirmed: bool = False) -> None:
        """
        Open a URL in the browser of a client, or ask first.

        `open-url-mode` decides: "open" sends the packet, "ask" shows
        the question in the command bar of every client it would land
        on -- a yes runs "open-url -c", so the question is asked once
        -- and "off" drops it.

        The packet goes to the client and not to the server, because
        the browser of the user runs on the machine of the client. The
        client picks the way its platform opens one: "open" on macOS,
        "xdg-open" or what $BROWSER names on Linux.
        """
        if self.open_url_mode == "off":
            logger.info("Not opening %s: open-url-mode is off.", url)
            return

        clients = self.clients_to_open_on()
        if not clients:
            self.add_command_error("Nobody is attached to open %s." % (url,))
            return

        if self.open_url_mode == "ask" and not confirmed:
            command = "open-url -c %s" % (shlex.quote(url),)
            for client_state in clients:
                client_state.confirm_text = "Open %s in the browser? (y/n)" % (url,)
                client_state.confirm_command = command
            return

        for client_state in clients:
            client_state.connection._send_packet({"cmd": "open", "data": url})
            client_state.message = "Opened %s in the browser of this machine." % (url,)

    def _ensure_the_open_url_shim(self) -> None:
        """
        Make the directory that puts the opener of this session on the
        PATH of a pane.

        It holds one script, `pymux-open-url`, which asks this session
        to open what it was given, and a link to it under the name
        `xdg-open`. A program in a pane calls `xdg-open` without
        knowing pymux, and what reads $BROWSER finds the script by
        name too. Nothing answers for `open` on macOS: too much calls
        it for files, and a name that opens files must not open URLs.

        Only a pane that starts afterwards sees it: a running pane
        keeps the PATH it was born with.
        """
        if self._open_url_shim_dir is not None:
            return
        self._open_url_shim_dir = tempfile.mkdtemp(prefix="pymux-open-url-")
        script = os.path.join(self._open_url_shim_dir, "pymux-open-url")
        with open(script, "w") as f:
            f.write("#!/bin/sh\nexec pymux open-url -- \"$@\"\n")
        os.chmod(script, 0o755)
        os.symlink("pymux-open-url", os.path.join(self._open_url_shim_dir, "xdg-open"))

    def _shim_the_environment_of_a_pane(self) -> None:
        """
        Put the shim on the PATH of a pane, and name the opener in
        $BROWSER. Runs in the fork, before the program of the pane.
        """
        if self.open_url_shim and self._open_url_shim_dir:
            os.environ["PATH"] = (
                self._open_url_shim_dir + os.pathsep + os.environ["PATH"]
            )
            os.environ["BROWSER"] = os.path.join(
                self._open_url_shim_dir, "pymux-open-url"
            )

    def forward_osc(self, pane, code: str, param: str) -> None:
        """
        Write an OSC sequence of a pane to the terminals of the
        clients. ptterm hands over the three that only the terminal of
        the user can serve: the clipboard, a desktop notification and
        the shape of the pointer.

        The payload comes from a program in a pane, so `build_osc`
        checks it before it reaches the terminal of the user.

        Never raises: this runs on the read path of a pane, and one
        sequence may not stop it.
        """
        try:
            if code == Osc.POINTER_SHAPE:
                # The shape of the pointer is not a sequence to pass on
                # as it arrives: it belongs to the pane, and the client
                # has to see the shape of the pane it looks at. The
                # screen of the pane holds it; this only asks every
                # client to look again.
                self.sync_pointer_shape()
                return

            if code == Osc.TERMINAL_EXTENSION:
                # iTerm2's namespace. The one subcommand a pane may use
                # is the one this session can serve; nothing else of
                # the namespace goes out, because a payload that was
                # not checked must not reach the terminal of the user.
                url = open_url_of(param)
                if url is not None:
                    self.open_url(url)
                return

            if code == Osc.CLIPBOARD:
                if not self.enable_clipboard:
                    return
                # What a pane copies goes into the paste buffer of the
                # session as well, so that "paste-buffer" can put it in
                # another pane. This stays one way: a pane may write the
                # clipboard, and may not read what the user copied
                # somewhere else.
                self._mirror_clipboard(param)

            if code == Osc.NOTIFICATION:
                # Give the notification an identifier that names this
                # pane, so that the answer finds its way back.
                param = self.notifications.outgoing(pane.pane_id, param)

            sequence = build_osc(code, param)
            if sequence is None:
                logger.warning("Dropped an unsafe OSC %s of a pane.", code)
                return

            # The clipboard and a notification come from any pane: a
            # build that ends in a pane out of sight is exactly what a
            # notification is for.
            for connection in self._client_states:
                connection.forward_osc(sequence)
        except Exception:
            logger.exception("Forwarding an OSC sequence failed.")

    def _mirror_clipboard(self, param: str) -> None:
        """
        Put what a pane copied into the paste buffer of the session.

        The payload is "selections ; base64". An empty payload clears a
        selection of the user, which is not a reason to throw away the
        buffer of the session.
        """
        _selections, _semicolon, data = param.partition(";")
        if not data:
            return
        try:
            text = base64.b64decode(data.encode("ascii")).decode("utf-8", "replace")
        except Exception:
            return  # Not base64. `build_osc` drops it as well.
        self.clipboard.set_data(ClipboardData(text))

    def _has_focus(self, client_state, pane) -> bool:
        "True when this client looks at this pane."
        return self.focused_pane_of(client_state) is pane

    def focused_pane_of(self, client_state):
        """
        The pane that this client looks at, or `None`.

        Never raises: this runs on the invalidate path, also for
        headless servers without a running prompt_toolkit application.
        """
        if self.overlay_pane is not None:
            return self.overlay_pane

        try:
            with set_app(client_state.app):
                window = self.arrangement.get_active_window()
        except Exception:
            # `get_active_window` needs a running application.
            return None
        return window.active_pane if window is not None else None

    def pointer_shape_of(self, pane) -> str:
        """
        The shape of the pointer that the program in a pane asks for.

        An empty string means that it asked for none, and that the
        terminal of the user picks the shape itself.
        """
        if pane is None:
            return ""
        return pane.screen.pointer_shapes.shape

    def sync_pointer_shape(self) -> None:
        """
        Give every client the shape of the pointer that the pane it
        looks at asks for.

        Two clients can look at two panes, so this is answered for each
        of them. A client that moves to a pane which asks for no shape
        is told so, or the pointer keeps the shape of a pane that the
        user left.
        """
        try:
            for connection, client_state in self._client_states.items():
                connection.set_pointer_shape(
                    self.pointer_shape_of(self.focused_pane_of(client_state))
                )
        except Exception:
            logger.exception("Sending the shape of the pointer failed.")

    def keyboard_source_flags(self) -> int:
        """
        What the terminals of the clients can report of the keyboard
        protocol, in the flags of that protocol.

        A pane may be looked at by several clients at once, and a key
        can come from any of them. So only what every attached client
        can report counts. No client at all reports nothing.

        Only an attached client counts. A connection that runs one
        command and leaves has no terminal of the user behind it, and
        it never asked one what it does.
        """
        # A client that runs in the process that started pymux has no
        # connection, so nothing ever asked its terminal what it can
        # report. It counts as reporting nothing, which is what it
        # already did.
        masks = [
            0 if connection is None else connection.kitty_source_flags
            for connection in self._client_states
        ]
        if not masks:
            return 0
        common = masks[0]
        for mask in masks[1:]:
            common &= mask
        return common

    def sync_keyboard_source_flags(self) -> None:
        """
        Tell every pane what the keyboards of the clients can report.

        A pane answers the query of its program with the flags that it
        really gets, and that answer needs this. (Only walks the panes
        when something changed.)
        """
        state = (
            self.keyboard_source_flags(),
            self.synthesize_key_events,
            self.extended_keys,
        )
        if state == self._keyboard_state_sent:
            return
        self._keyboard_state_sent = state
        for pane in list(self.panes_by_id.values()):
            self.tell_pane_about_the_keyboard(pane)

    def sync_the_keyboard(self) -> None:
        """
        Tell the panes and the clients everything about the keyboard.

        `set-option extended-keys` calls this, because that option
        moves both halves at once: what a pane may ask for, and what
        the terminal of a client is put into. Lillecarl/pymux#173.
        """
        self.sync_keyboard_source_flags()
        self.sync_kitty_flags()

    def tell_pane_about_the_keyboard(self, pane) -> None:
        """
        Tell one pane what the keyboards of the clients can report.

        Never raises: this runs when a client attaches and when a pane
        starts, and neither may stop for it.
        """
        try:
            screen = pane.screen
        except Exception:
            return
        try:
            screen.keyboard_source_flags = self.keyboard_source_flags()
            screen.synthesize_key_events = self.synthesize_key_events
            screen.extended_keys_allowed = self.extended_keys is not ExtendedKeys.OFF
        except AttributeError:
            # An older ptterm knows nothing about the keyboard of the
            # host. It then claims what a pane asks for, as before.
            pass

    def keyboard_flags_for_a_client(self) -> int:
        """
        What the terminal of a client is asked to report.

        Two things ask. The pane in front of the person asks for what
        its program pushed, and that has to reach the terminal or the
        program does not get the keys it wants. **pymux asks as well.**
        A bare Escape is also the first byte of every escape sequence,
        so a terminal that does not disambiguate leaves the parser
        holding it until a timeout says no more is coming: half a
        second before the command line closes, and the same half second
        before any alt key does anything. A terminal that
        disambiguates sends "CSI 27 u", which starts nothing, so the
        key arrives on the press.

        The two are one set of flags, because one terminal sends the
        keys of both. A pane reads what it asked for all the same: the
        key data reaches it as the terminal wrote it, and
        `Screen.encode_key` writes it again in the encoding of that
        pane. Lillecarl/pymux#164.

        `set-option extended-keys off` asks for nothing at all, and
        the client then writes "CSI = 0 ; 1 u" to put the terminal
        back in the legacy encoding. Lillecarl/pymux#173.
        """
        if self.extended_keys is ExtendedKeys.OFF:
            return 0
        return KeyboardFlag.DISAMBIGUATE | self.get_focused_kitty_flags()

    def sync_kitty_flags(self) -> None:
        """
        Send the kitty keyboard protocol flags to all clients, so that
        they can enable the protocol on their outer terminals. (Only
        sends when the value changed.)
        """
        flags = self.keyboard_flags_for_a_client()
        if flags == self._kitty_flags_sent:
            return
        self._kitty_flags_sent = flags
        try:
            for connection in self.connections:
                connection._send_packet(
                    {"cmd": "kitty-keyboard", "data": {"flags": flags}}
                )
        except Exception:
            logger.exception("Sending kitty keyboard flags failed.")

    def stop(self):
        # Kill all pane processes first. (The pending `waitpid` calls in
        # the event loop executor keep the server process alive as long
        # as the children run. Also, `kill-server` should not leave the
        # programs inside the panes running.)
        for pane in list(self.panes_by_id.values()):
            process = pane.process
            if not process.is_terminated:
                process.kill()

        for app in self.apps:
            try:
                app.exit()
            except Exception:
                # `Application.exit()` raises for applications that never
                # started running. (E.g. temporary CLIs that only handled a
                # command.)
                pass
        if not self.done_f.done():
            self.done_f.set_result(None)

    def create_window(
        self,
        command: str | None = None,
        start_directory: str | None = None,
        name=None,
        index: int | None = None,
    ):
        """
        Create a new :class:`pymux.arrangement.Window` in the arrangement.

        `index` says where it goes. Without one it takes the lowest
        free index. Lillecarl/pymux#191.
        """
        pane = self._create_pane(None, command, start_directory=start_directory)

        self.arrangement.create_window(pane, name=name, index=index)
        pane.focus()
        self.invalidate(Woke.WINDOW_OPENED)

    def add_process(
        self,
        command: str | None = None,
        vsplit: bool = False,
        start_directory: str | None = None,
        window: Window | None = None,
    ):
        """
        Add a new process to the given window (or the active window).
        (vsplit/hsplit).
        """
        if window is None:
            window = self.arrangement.get_active_window()

        pane = self._create_pane(window, command, start_directory=start_directory)
        window.add_pane(pane, vsplit=vsplit)
        pane.focus()
        self.invalidate(Woke.PANE_WAS_SPLIT_OFF)

    def kill_pane(self, pane: Pane) -> None:
        """
        Kill the given pane, and remove it from the arrangement.
        """
        # Send kill signal.
        if not pane.process.is_terminated:
            pane.process.kill()

        # Remove from layout.
        self.arrangement.remove_pane(pane)

    def leave_command_mode(self, append_to_history=False):
        """
        Leave the command/prompt mode.
        """
        client_state = self.get_client_state()

        client_state.command_buffer.reset(append_to_history=append_to_history)
        client_state.prompt_buffer.reset(append_to_history=True)

        client_state.prompt_command = ""
        client_state.confirm_command = ""
        # The completer belongs to the question, and the question is
        # over. It also says whether the prompt draws in a box, so a
        # completer left behind would put the next one in one.
        client_state.prompt_completer = None

        client_state.app.layout.focus_previous()

    def handle_command(self, command):
        """
        Handle command from the command line.
        """
        handle_command(self, command)

    def show_message(self, message):
        """
        Set a warning message. This will be shown at the bottom until a key has
        been pressed.

        :param message: String.
        """
        try:
            self.get_client_state().message = message
        except ValueError:
            pass  # No client. (E.g. a temporary CLI for a run-command.)

    def print_command_line(self, text: str) -> None:
        """
        Print the output of a command that was entered from the command line.
        (When `command_output` is set, this goes back to the client that sent
        the run-command packet.)
        """
        if self.command_output is not None:
            self.command_output.append(text)

    def add_command_error(self, message: str) -> None:
        """
        Record an error for a command that failed.

        A command that came over the command line answers the client
        that sent it, and `command_error` is that answer.

        A command that came from a configuration file has nobody to
        answer. `startup()` runs it while the first application is still
        being built, so `show_message` finds no client state and drops
        the message. So it is kept, logged, and shown to the first
        client that arrives. Lillecarl/pymux#38.
        """
        if self.sourcing is not None:
            message = "%s: %s" % (self.sourcing, message)

        if self.command_error is not None:
            self.command_error.append(message)
        elif not self._startup_done or self.sourcing is not None:
            logger.warning("%s", message)
            self.startup_errors.append(message)

    def report_startup_errors(self, client_state) -> None:
        """
        Show the lines of the configuration file that failed, once.

        The first client that draws gets them, and they are dropped
        afterwards: the second client did not make the mistake, and a
        message it cannot act on is noise.

        The message is set on the client state and not through
        `show_message`, because that one looks the state up by the
        application that is running now, and a client that has just been
        made is not running yet.
        """
        if not self.startup_errors:
            return
        messages, self.startup_errors = self.startup_errors, []
        client_state.message = "; ".join(messages)

    def detach_client(self, app):
        """
        Detach the client that belongs to this CLI.

        **Standalone quits instead.** It puts the user interface
        straight on the terminal, with no client and no protocol, so
        there is no connection to close and nothing for the session to
        live on after the person leaves. Detaching there wrote no
        teardown at all: the screen drew again and the panes kept
        running, and a key that does nothing teaches a person that the
        key does not exist.

        `ctrl+b d` is the gesture for leaving and standalone has no
        other one, so it means quit here. `run_integrated` already
        reads it that way, for the same reason, and one key that says
        one thing on all three routes is worth more than the
        difference between them. Lillecarl/pymux#160.
        """
        if self._runs_standalone:
            self.stop()
            return

        connection = self.get_connection()
        if connection:
            connection.detach_and_close()

        # Redraw all clients -> Maybe their size has to change.
        self.invalidate(Woke.CLIENT_DETACHED)

    def listen_on_socket(self, socket_name=None):
        """
        Listen for clients on a Unix socket.
        Returns the socket name.
        """

        def connection_cb(pipe_connection):
            # We have to create a new `context`, because this will be the scope for
            # a new prompt_toolkit.Application to become active.
            context = contextvars.copy_context()
            connection = context.run(lambda: ServerConnection(self, pipe_connection))

            self.connections.append(connection)

        self.socket_name = bind_and_listen_on_socket(
            socket_name, connection_cb, loop=self.loop
        )

        # Set session_name according to socket name.
        #        if '.' in self.socket_name:
        #            self.session_name = self.socket_name.rpartition('.')[-1]

        # `bind_and_listen_on_socket` already said it. Two lines were
        # half the log of a server that started well.
        return self.socket_name

    def run_server(self):
        # Ignore keyboard. (When people run "pymux server" and press Ctrl-C.)
        # Pymux has to be terminated by termining all the processes running in
        # its panes.
        def handle_sigint(*a):
            print("Ignoring keyboard interrupt.")

        signal.signal(signal.SIGINT, handle_sigint)

        self.server_starts()

        # Run eventloop.
        try:
            self.loop.run_until_complete(self.done_f)
        except:
            # When something bad happens, always dump the traceback.
            # (Otherwise, when running as a daemon, and stdout/stderr are not
            # available, it's hard to see what went wrong.)
            fd, path = tempfile.mkstemp(prefix="pymux.crash-")
            logger.fatal("Pymux has crashed, dumping traceback to {0}".format(path))
            os.write(fd, traceback.format_exc().encode("utf-8"))
            os.close(fd)
            raise

        finally:
            # Clean up socket.
            self._remove_socket()

    def _remove_socket(self) -> None:
        "Take away the socket file of this server, if it bound one."
        if not self.socket_name:
            return
        try:
            os.remove(self.socket_name)
        except OSError:
            pass

    def run_integrated(self, color_depth, detach_other_clients: bool = False):
        """
        Run the server and one client in this process.

        The client reaches the server through a pair of queues, not
        through a socket, so it reaches the server that this call
        started and nothing else. Both halves of the protocol run, so
        this route proves what the socket route proves.

        This is not `run_standalone`. Standalone has no client and no
        protocol at all: it puts the user interface straight on the
        terminal. This runs the real client against the real server.

        A socket is bound as well when one was asked for. Nothing of
        the user interface reads it. It is there so that
        `pymux -S <socket> <command>` and libpymux reach this server.

        The call returns when the server closes the connection: the
        last pane exited, somebody ran `kill-server`, or the client
        detached. The process ends with it, because both halves are in
        it.
        """
        # Here, not at the top of the file: the client reads a terminal
        # through `termios`, which a server on Windows does not have.
        from .client.memory import MemoryClient

        self.server_starts()

        async def run() -> None:
            server_end, client_end = connect_in_memory()

            # A context of its own, the same as for a client that
            # arrives over the socket. A `prompt_toolkit.Application`
            # becomes active in it.
            context = contextvars.copy_context()
            connection = context.run(lambda: ServerConnection(self, server_end))
            self.connections.append(connection)

            await MemoryClient(client_end).attach(
                detach_other_clients=detach_other_clients,
                color_depth=color_depth,
            )

        try:
            self.loop.run_until_complete(run())
        finally:
            # The client has gone, and the server is in this process,
            # so nothing is left for the session to live on. `ctrl+b d`
            # means quit here, and that is the one honest reading.
            #
            # It is also what lets the process exit at all. A pane that
            # is still running holds a `waitpid` in the executor of the
            # loop, and the interpreter waits for that thread. Without
            # this the client put the terminal back and the person was
            # left looking at their shell with no prompt.
            # Lillecarl/pymux#109.
            self.stop()
            self._remove_socket()

    def run_standalone(self, color_depth):
        """
        Run pymux standalone, rather than using a client/server architecture.
        This is mainly useful for debugging.
        """
        self._runs_standalone = True
        self.server_starts()

        client_state = self.add_client(
            input=create_input(),
            output=create_output(stdout=sys.stdout),
            color_depth=color_depth,
            connection=None,
        )

        # The same as for a client over a socket: an exception in the
        # event loop is logged, not turned into a prompt that nothing
        # can answer.
        client_state.app.run(set_exception_handler=False)

    def add_client(
        self, output, input, color_depth, connection, temporary: bool = False
    ) -> ClientState:
        client_state = ClientState(
            self,
            connection=connection,
            input=input,
            output=output,
            color_depth=color_depth,
        )
        client_state.temporary = temporary

        self._client_states[connection] = client_state

        # Attaching counts as using it, so `window-size latest` has an
        # answer before anybody has pressed a key. tmux stamps a client
        # on attach for the same reason. The fake CLI of a command does
        # not: nobody is using that terminal, and it is gone before the
        # answer comes back.
        if not temporary:
            self.client_was_used(client_state)

        # The configuration file was read while this client was being
        # built, so nothing could be told about a line that failed.
        # This is the first moment there is somebody to tell.
        self.report_startup_errors(client_state)

        # A client that just arrived may speak less than the ones that
        # are here, and the keyboard of every client has to serve what
        # a pane hears. (The detection of this client answers later,
        # and raises it again when the terminal can do more.)
        self.sync_keyboard_source_flags()

        return client_state

    def remove_client(self, connection):
        """
        Forget a client that has gone, and everything of it.

        **Both lists, not one.** `connections` used to keep every
        connection the server had ever accepted, so a person who
        attached and detached each morning left a `ServerConnection`
        behind each time, with its pipe input and its parser. Nothing
        drew for them -- `_send_packet` answers a closed connection
        with nothing -- so the cost was memory and a longer walk for
        every broadcast. Lillecarl/pymux#226.
        """
        if connection in self._client_states:
            del self._client_states[connection]
        if connection in self.connections:
            self.connections.remove(connection)
        # One client fewer can mean that the rest speak more.
        self.sync_keyboard_source_flags()
