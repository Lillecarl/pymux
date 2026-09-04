import asyncio
import base64
import contextvars
import os
import signal
import sys
import tempfile
import threading
import time
import traceback
import weakref
from typing import Callable, Optional

from prompt_toolkit.application import Application
from prompt_toolkit.application.current import get_app, set_app
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.clipboard import ClipboardData, InMemoryClipboard
from prompt_toolkit.data_structures import Size
from prompt_toolkit.enums import EditingMode
from prompt_toolkit.filters import Condition
from prompt_toolkit.input.defaults import create_input
from prompt_toolkit.key_binding.vi_state import InputMode
from prompt_toolkit.layout.layout import Layout
from prompt_toolkit.output.defaults import create_output
from prompt_toolkit.styles import (
    ConditionalStyleTransformation,
    SwapLightAndDarkStyleTransformation,
)
from ptterm import Terminal

from .arrangement import Arrangement, Pane, Window
from .commands.commands import call_command_handler, handle_command
from .commands.completer import create_command_completer
from .enums import COMMAND, PROMPT
from .environment import scrub_terminal_identity
from .graphics import PaneView
from .key_bindings import PymuxKeyBindings
from .layout import Justify, LayoutManager
from .log import logger
from .terminfo import add_to_environment, terminal_name
from .notifications import NotificationRoutes
from .options import ALL_OPTIONS, ALL_WINDOW_OPTIONS
from .osc import build_osc
from .pipes import bind_and_listen_on_socket
from .prompt_toolkit_compat import apply_prompt_toolkit_compat_fixes
from .ptterm_compat import apply_ptterm_compat_fixes
from .rc import STARTUP_COMMANDS
from .server import ServerConnection
from .style import ui_style
from .utils import get_default_shell

__all__ = [
    "Pymux",
]

apply_ptterm_compat_fixes()
apply_prompt_toolkit_compat_fixes()


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

        # Popup.
        self.display_popup = False


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
        )

        # Layout.
        self.layout_manager = LayoutManager(self.pymux, self)

        self.app = self._create_app()

        # Clear write positions right before rendering. (They are populated
        # during rendering).
        def before_render(_):
            self.layout_manager.reset_write_positions()

        self.app.before_render += before_render

        # Draw the images of the panes right after rendering. (The text
        # is on the screen by then; kitty draws images over it.)
        def after_render(_):
            graphics = getattr(self.connection, "graphics", None)
            if graphics is None or not graphics.supported:
                return
            try:
                graphics.render(self._graphics_views())
            except Exception:
                logger.exception("Drawing the pane images failed.")

        self.app.after_render += after_render

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
            graphics = getattr(pane.process.screen, "graphics", None)
            if graphics is None:
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
                    graphics=graphics,
                    screen=pane.process.screen,
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
            layout=Layout(container=self.layout_manager.layout),
            key_bindings=pymux.key_bindings_manager.key_bindings,
            mouse_support=Condition(lambda: pymux.enable_mouse_support),
            full_screen=True,
            style=self.pymux.style,
            style_transformation=ConditionalStyleTransformation(
                SwapLightAndDarkStyleTransformation(),
                Condition(lambda: self.pymux.swap_dark_and_light),
            ),
            on_invalidate=(lambda _: pymux.invalidate()),
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

        # Hide message when a key has been pressed.
        def key_pressed(_):
            self.message = None

        app.key_processor.before_key_press += key_pressed

        # The following code needs to run with the application active.
        # Especially, `create_window` needs to know what the current
        # application is, in order to focus the new pane.
        with set_app(app):
            # Redraw all CLIs. (Adding a new client could mean that the others
            # change size, so everything has to be redrawn.)
            pymux.invalidate()

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

    Or:

        p = Pymux()
        p.run_standalone()
    """

    def __init__(
        self,
        source_file=None,
        startup_command=None,
        session_name: Optional[str] = None,
    ):
        self._client_states = {}  # connection -> client_state

        # Options
        self.enable_mouse_support = True
        self.enable_status = True
        self.enable_pane_status = True  # False
        self.enable_bell = True
        self.enable_clipboard = True

        # The paste buffer of the session. Copy mode writes it, a pane
        # that writes the clipboard of the user writes it too, and
        # "paste-buffer" reads it. Every client shares this one.
        self.clipboard = InMemoryClipboard()
        self.remain_on_exit = False
        self.status_keys_vi_mode = False
        self.mode_keys_vi_mode = False
        self.history_limit = 2000
        self.status_interval = 4
        # What a pane is told it is. The entry of pymux describes what
        # a pane really does; a build without one falls back to xterm.
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
        self.command_output: Optional[list] = None
        self.command_error: Optional[list] = None

        self._startup_done = False
        self.source_file = source_file
        self.startup_command = startup_command

        # Time when this server was started.
        self.created = time.time()

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

        self.style = ui_style

    def _start_auto_refresh_thread(self):
        """
        Start the background thread that auto refreshes all clients according to
        `self.status_interval`.
        """

        def run():
            while True:
                time.sleep(self.status_interval)
                self.invalidate()

        t = threading.Thread(target=run)
        t.daemon = True
        t.start()

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

        if w and w.active_process:
            title = w.active_process.screen.title
        else:
            title = ""

        if title:
            return "%s - Pymux" % (title,)
        else:
            return "Pymux"

    def get_window_size(self):
        """
        Get the size to be used for the DynamicBody.
        This will be the smallest size of all clients.
        """

        def active_window_for_app(app):
            with set_app(app):
                return self.arrangement.get_active_window()

        active_window = self.arrangement.get_active_window()

        # Get sizes for connections watching the same window.
        apps = [
            client_state.app
            for client_state in self._client_states.values()
            if active_window_for_app(client_state.app) == active_window
        ]
        sizes = [app.output.get_size() for app in apps]

        rows = [s.rows for s in sizes]
        columns = [s.columns for s in sizes]

        if rows and columns:
            return Size(
                rows=min(rows) - (1 if self.enable_status else 0), columns=min(columns)
            )
        else:
            return Size(rows=20, columns=80)

    def _create_pane(
        self,
        window: Optional[Window] = None,
        command: Optional[str] = None,
        start_directory: Optional[str] = None,
        on_done: Optional[Callable[[], None]] = None,
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

            self.invalidate()

        def bell():
            "Sound bell on all clients."
            if self.enable_bell:
                for c in self.apps:
                    c.output.bell()

        def forward_osc(code: str, param: str) -> None:
            "Pass an OSC sequence of this pane to the terminals of the clients."
            self.forward_osc(pane, code, param)

        # Start directory.
        path: Optional[str]

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
            # from: it answers the protocol queries for itself. Drop
            # the variables that name the outer terminal, so that a
            # program asks the pane instead of believing them.
            scrub_terminal_identity(os.environ)

            # Set terminal variable. A program built on ncurses reads
            # the database instead of asking, so the entry that
            # describes a pane goes on the path it searches.
            os.environ["TERM"] = self.default_terminal
            add_to_environment(os.environ)

            # A pane takes 24 bit colour, whatever the terminal of the
            # client takes. ptterm keeps the colour that a program
            # writes, and every client renders it as deeply as its own
            # terminal allows.
            #
            # Saying so matters. Without it a program falls back to the
            # 256 colours of TERM and picks the nearest index itself,
            # which it then writes as that index: a colour of a theme
            # ends up as the palette entry beside it, and pymux can no
            # longer tell what the program meant. (A dark background
            # becomes plain black that way.) The value that a client
            # happened to start the server with says nothing about the
            # pane, so it is not inherited either.
            os.environ["COLORTERM"] = "truecolor"

            # Make sure to set the PYMUX environment variable.
            if self.socket_name:
                os.environ["PYMUX"] = "%s,%i" % (self.socket_name, pane.pane_id)

        if command:
            command_list = command.split()
        else:
            command_list = [self.default_shell]

        # Create new pane and terminal.
        terminal = Terminal(
            done_callback=done_callback,
            bell_func=bell,
            osc_func=forward_osc,
            before_exec_func=before_exec,
            command=command_list,
        )
        pane = Pane(terminal)

        # ptterm starts the process only when the terminal is rendered for
        # the first time. Start it right away, so that panes in detached
        # sessions also run and produce output. (Like tmux does.)
        terminal_control = terminal.terminal_control
        if not getattr(terminal_control, "_running", False):
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

        logger.info("Created process %r.", command_list)

        return pane

    def display_overlay(
        self,
        command: Optional[str] = None,
        width: Optional[str] = None,
        height: Optional[str] = None,
        title: Optional[str] = None,
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

        pane: Optional["arrangement.Pane"] = None

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
        self.invalidate()

        return pane

    def close_overlay(self) -> None:
        """
        Close the overlay pane, and kill what runs in it.
        """
        pane = self.overlay_pane
        if pane is None:
            return

        self.overlay_pane = None

        process = getattr(pane, "process", None)
        if process is not None and not process.is_terminated:
            process.kill()

        self._sync_focus_everywhere()
        self.invalidate()

    def _sync_focus_everywhere(self) -> None:
        "Give every client the focus that its state asks for."
        for client_state in list(self._client_states.values()):
            try:
                with set_app(client_state.app):
                    client_state.sync_focus()
            except Exception:
                # An application that never ran has no layout to focus.
                logger.exception("Could not sync the focus of a client.")

    def invalidate(self):
        "Invalidate the UI for all clients."
        logger.info("Invalidating %s applications", len(self.apps))

        for app in self.apps:
            app.invalidate()

        # The focused pane can have changed, or the process inside it
        # can have pushed/popped kitty keyboard protocol flags or asked
        # for a different pointer.
        self.sync_kitty_flags()
        self.sync_pointer_shape()

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
        screen = getattr(pane.process, "screen", None)
        return getattr(screen, "kitty_keyboard_flags", 0) or 0

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
            if code == "22":
                # The shape of the pointer is not a sequence to pass on
                # as it arrives: it belongs to the pane, and the client
                # has to see the shape of the pane it looks at. The
                # screen of the pane holds it; this only asks every
                # client to look again.
                self.sync_pointer_shape()
                return

            if code == "52":
                if not self.enable_clipboard:
                    return
                # What a pane copies goes into the paste buffer of the
                # session as well, so that "paste-buffer" can put it in
                # another pane. This stays one way: a pane may write the
                # clipboard, and may not read what the user copied
                # somewhere else.
                self._mirror_clipboard(param)

            if code == "99":
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
            text = base64.b64decode(data.encode("ascii")).decode(
                "utf-8", "replace"
            )
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
        screen = getattr(pane.process, "screen", None)
        return getattr(screen, "pointer_shape", "") or ""

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

    def sync_kitty_flags(self) -> None:
        """
        Send the kitty keyboard protocol flags of the focused pane to
        all clients, so that they can enable the protocol on their outer
        terminals. (Only sends when the value changed.)
        """
        flags = self.get_focused_kitty_flags()
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
            process = getattr(pane, "process", None)
            if process is not None and not process.is_terminated:
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
        command: Optional[str] = None,
        start_directory: Optional[str] = None,
        name=None,
    ):
        """
        Create a new :class:`pymux.arrangement.Window` in the arrangement.
        """
        pane = self._create_pane(None, command, start_directory=start_directory)

        self.arrangement.create_window(pane, name=name)
        pane.focus()
        self.invalidate()

    def add_process(
        self,
        command: Optional[str] = None,
        vsplit: bool = False,
        start_directory: Optional[str] = None,
        window: Optional[Window] = None,
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
        self.invalidate()

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
        Record an error for a command that was entered from the command line.
        """
        if self.command_error is not None:
            self.command_error.append(message)

    def detach_client(self, app):
        """
        Detach the client that belongs to this CLI.
        """
        connection = self.get_connection()
        if connection:
            connection.detach_and_close()

        # Redraw all clients -> Maybe their size has to change.
        self.invalidate()

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

        logger.info("Listening on %r." % self.socket_name)
        return self.socket_name

    def run_server(self):
        # Ignore keyboard. (When people run "pymux server" and press Ctrl-C.)
        # Pymux has to be terminated by termining all the processes running in
        # its panes.
        def handle_sigint(*a):
            print("Ignoring keyboard interrupt.")

        signal.signal(signal.SIGINT, handle_sigint)

        # Start background threads.
        self._start_auto_refresh_thread()

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
            os.remove(self.socket_name)

    def run_standalone(self, color_depth):
        """
        Run pymux standalone, rather than using a client/server architecture.
        This is mainly useful for debugging.
        """
        self._runs_standalone = True
        self._start_auto_refresh_thread()

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

    def add_client(self, output, input, color_depth, connection) -> ClientState:
        client_state = ClientState(
            self, connection=connection, input=input, output=output, color_depth=color_depth
        )

        self._client_states[connection] = client_state

        return client_state

    def remove_client(self, connection):
        if connection in self._client_states:
            del self._client_states[connection]
