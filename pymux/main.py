from __future__ import annotations

import asyncio
import base64
import contextvars
import datetime
import errno
import fnmatch
import os
import shlex
import signal
import socket
import time
import weakref
from collections import deque
from collections.abc import Awaitable, Callable, Coroutine, Iterable, Sequence
from contextlib import asynccontextmanager, suppress
from typing import TYPE_CHECKING, Any, ClassVar, NamedTuple, override

import anyio
import anyio.abc
from libpymux.agentic import CallerContext
from libpymux.protocol import Field, Message, Packet
from prompt_toolkit.application import Application
from prompt_toolkit.application.current import get_app, set_app
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.clipboard import ClipboardData, DynamicClipboard, InMemoryClipboard
from prompt_toolkit.completion import Completer, DynamicCompleter
from prompt_toolkit.cursor_shapes import CursorShape, CursorShapeConfig
from prompt_toolkit.data_structures import Size
from prompt_toolkit.document import Document
from prompt_toolkit.enums import EditingMode
from prompt_toolkit.filters import Condition
from prompt_toolkit.history import InMemoryHistory
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
from ptyhost.backends import Backend
from ptyhost.backends.posix import GRACE, PosixBackend
from ptyhost.ending import say_how_it_ends
from pyte.keep import Keep
from pyte.keys import KEY_PROFILES, KeyProfile, key_profile_of
from pyte.osc import CLEAR_SCROLLBACK, Osc
from pyte.page import RowMark

from . import hacks_and_lies, introspect, kernels, log
from .actions import ActionLog, Kind, Origin, awaited_as, note, note_keys, started_by
from .answer import Answer, current_answer
from .arrangement import Arrangement, Pane, Window
from .audio import AudioServer
from .browsers import Browsers
from .client.memory import MemoryClient
from .client_options import ClientOptions
from .colors import DefaultColors
from .commands import CommandException, call_command_handler, handle_command
from .commands.completer import create_command_completer
from .downloads import (
    FILE_KEYS,
    DownloadRefused,
    FinishedDownload,
    file_packets,
    route_of,
    sequences_of,
    terminal_saves_downloads,
)
from .enums import (
    CHOOSE,
    COMMAND,
    PROMPT,
    Chooser,
    Clipboard,
    EditingKeys,
    FileTransfer,
    ForwardMode,
    OpenUrlMode,
    OpenUrlTarget,
    RenderMode,
    WindowSize,
    Woke,
)
from .error_notice import ErrorNotice
from .forwarding import LOOPBACK_NAMES, loopback_port
from .graphics import OuterImageIds, PaneView
from .hooks import Hooks
from .ids import PaneId, SessionId, WindowIndex
from .inline_images import as_png, cells_of
from .jobs import JOB_SWEEP_EVERY, Job, JobFeed, JobTable, describe
from .key_bindings import PymuxKeyBindings
from .key_spelling import why_pane_cannot_read
from .keys import FOCUS_IN, FOCUS_OUT
from .layout import LayoutManager, change_pane_size
from .log import logger
from .nearest import NEAREST
from .notifications import (
    Notification,
    NotificationCenter,
    NotificationRoutes,
    NotificationSource,
    Urgency,
    close_of,
    is_question,
    osc99_of,
)
from .options import (
    ALL_CLIENT_OPTIONS,
    ALL_OPTIONS,
    ALL_SERVER_OPTIONS,
    ALL_WINDOW_OPTIONS,
    Scope,
)
from .osc import (
    build_osc,
    copy_data_of,
    current_dir_of,
    extension_key_of,
    file_url_dir_of,
    ftcs_of,
    message_of,
    open_url_of,
    progress_of,
    remote_host_of,
    request_attention_of,
    set_user_var_of,
    shell_integration_of,
    simple_notification_of,
)
from .pipes import bind_and_listen_on_socket, connect_in_memory
from .plane import RESIZE_SHAPES
from .portal import Portal
from .rc import STARTUP_COMMANDS
from .server import ServerConnection
from .server_options import ServerOptions
from .session import DEFAULT_SIZE, OwnOptions, Session
from .session_options import SessionOptions
from .shims import Shims
from .style import DEFAULT_THEME, theme
from .terminals import Terminals
from .wayland import WaylandServer
from .window_options import WindowOptions
from .window_pane import WindowProcess, WindowScreen, WindowWidget, is_window_pane

if TYPE_CHECKING:
    from prompt_toolkit.input import Input
    from prompt_toolkit.layout.screen import WritePosition
    from prompt_toolkit.output import ColorDepth, Output
    from pyte.keys import KeyEvent

    from .border_drag import BorderDrag
    from .client.memory import MemoryClient
    from .commands.wait_for import WaitChannel
    from .forwarding import Forward
    from .graphics import WindowView
    from .pipes import PipeConnection
    from .pipes.posix import PosixSocketListener
    from .url_mode import UrlMode

__all__ = [
    "Pymux",
]

kernels.install()


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


#: How many keys one pane remembers saying it could not read. A held
#: key repeats, and one line per repeat is a log nobody reads. A
#: keyboard has fewer keys than this.
MAX_KEYS_TO_REMEMBER = 512

#: How many user variables one pane keeps, from "OSC 1337 ;
#: SetUserVar". A shell sets the same handful on every prompt; a
#: program that mints names without end loses the oldest, the way the
#: notification hub does.
MAX_USER_VARS = 64

#: The rows "OSC 133" marks: where a prompt starts, and where the output
#: of a command starts.
_FTCS_ROW_MARKS = {"A": RowMark.PROMPT, "C": RowMark.OUTPUT}

#: How often a client whose terminal fell behind looks again whether it
#: caught up, in seconds: its next frame draws within this of it.
#: Lillecarl/pymux#585.
BEHIND_INTERVAL = 0.1

#: How often the windows are checked for output and for quiet, in
#: seconds: a mark goes up within this of what raised it.
WINDOW_OUTPUT_INTERVAL = 1.0

#: What a client refreshes in the session it attaches to, by default.
#: tmux's own list, which is every name that points at something on
#: the machine a person is sitting at: a display, an agent, a session
#: bus. ("update-environment", `options-table.c`.) Lillecarl/pymux#271.
DEFAULT_UPDATE_ENVIRONMENT = (
    "DISPLAY KRB5CCNAME MSYSTEM SSH_ASKPASS SSH_AUTH_SOCK SSH_AGENT_PID "
    "SSH_CONNECTION WAYLAND_DISPLAY WINDOWID XAUTHORITY XDG_CURRENT_DESKTOP "
    "XDG_SESSION_DESKTOP XDG_SESSION_TYPE"
)


def _say_key_did_not_fit():
    """
    A reporter for one pane, for a key it reads as something else.

    **Once per key, not once per repeat.** The same shape
    `KittyVt100Parser._say_it_has_no_name` uses for a key pymux itself
    cannot name, so the two failures read alike. Lillecarl/pymux#238.
    """
    already_said: set[KeyEvent] = set()

    def say(event: KeyEvent, lost: int, encoded: str) -> None:
        if event in already_said:
            return
        if len(already_said) < MAX_KEYS_TO_REMEMBER:
            already_said.add(event)
        logger.info("%s", why_pane_cannot_read(event, lost, encoded))

    return say


def _any_of_them_wants_chrome(watchers: Iterable[ClientState]) -> bool:
    """
    Whether any of those clients is not in full screen.

    **Nobody watching is yes**, which is the answer the plane was
    measured with before `full-screen` belonged to a client. It is not
    the obvious reading -- nothing draws a titlebar over a window
    nobody looks at, so the plan reserves a row that stays empty -- and
    changing it moves the size of every detached pane in the
    collection: measured, and `pymux-vterm`, `pymux-alacritty` and
    `pymux-pictures` all read a different screen. That is its own
    question, and it is Lillecarl/pymux#474.
    """
    return not watchers or any(not one.options.full_screen for one in watchers)


def _remember_the_size_made_at(window: Window, pane: Pane) -> None:
    """
    Keep the size a new window's one pane was given as the window's own
    while nobody watches it. `size_with_no_client` gave the pane the
    whole plane, so the two are the same number, and the window keeps it
    whatever the clients do after. Lillecarl/pymux#817.
    """
    terminal = pane.terminal
    assert isinstance(terminal, Terminal), "`_create_pane` builds a terminal pane"
    window.size_when_made = Size(rows=terminal.screen.lines, columns=terminal.screen.columns)


def _plane_of_clients(clients: Sequence[ClientState], rule: WindowSize) -> Size:
    """
    The plane those clients give a window under `window-size` `rule`,
    less the status rows. `manual` without a size is `smallest`.

    A client that attached with `-r` is left out of the size. **Unless
    they all are** -- then they decide it between them, because the
    alternative is a plane no terminal here can show. tmux does the
    same, and for the same reason (`resize.c:95-107`).
    Lillecarl/pymux#467.

    **Every client, and not the ones that size it**, gives status rows.
    A read-only client still draws its own status line, so the rows it
    needs come off the plane even while its columns do not. The tallest
    one decides: a phone whose window list wraps to three rows takes
    three off everybody's plane, and the others draw background there.
    Lillecarl/pymux#723.
    """
    sizing = [one for one in clients if not one.ignore_size] or list(clients)
    if rule is WindowSize.LATEST:
        size = max(sizing, key=lambda client: client.last_used).app.output.get_size()
    else:
        pick = max if rule is WindowSize.LARGEST else min
        sizes = [client.app.output.get_size() for client in sizing]
        size = Size(rows=pick(one.rows for one in sizes), columns=pick(one.columns for one in sizes))
    status_rows = max(one.layout_manager.status.rows() for one in clients)
    return Size(rows=size.rows - status_rows, columns=size.columns)


def _listens(sock: socket.socket) -> bool:
    """
    Whether a stream socket listens.

    Linux says so through `SO_ACCEPTCONN`; macOS has no such option for
    a unix socket. There a `listen` answers instead: it changes nothing
    on a socket that listens already, and fails on a connected one.
    """
    try:
        return bool(sock.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN))
    except OSError as error:
        if error.errno != errno.ENOPROTOOPT:
            raise
    try:
        sock.listen()
    except OSError:
        return False
    return True


class Asker(NamedTuple):
    """
    The client a forward is asked of, and whether a person asked.

    `in_person` is false when the request arrived over the socket,
    which is how a program in a pane runs a command. The forward still
    belongs to an attached client -- that is the machine with the SSH
    connection -- but nobody chose it, so `forward_needs_asking` says
    it has to be confirmed. Lillecarl/pymux#440.
    """

    client_state: ClientState
    in_person: bool


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
        if self.connection.waits_for_terminal:
            self.connection.frame_held = True
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
        drained = self.connection.frame_drained
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
        drained = self.connection.frame_drained
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


def _open_question(url: str, forward: dict[str, Any] | None) -> str:
    """
    What a person is asked before a URL opens on their machine.

    **The forward is named, because it is part of what a yes allows.**
    Opening a page is one thing; binding a port on the machine somebody
    is sitting at is another, and a question that hid the second would
    be collecting an answer to a smaller question than it asked.
    Lillecarl/pymux#437.
    """
    if forward is None:
        return "Open %s in the browser? (y/n)" % (url,)

    return "Open %s in the browser, and forward %s:%d here? (y/n)" % (
        url,
        forward["host"],
        forward["port"],
    )


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
    """

    #: What a hot upgrade does with each attribute; `pyte.keep` says.
    #: An option's value is saved, because a person may have set it
    #: since the configuration file was read; the tables that describe
    #: the options are rebuilt. The listening socket's fd passes to the
    #: new server under the same number (Lillecarl/pymux#553).
    #: Lillecarl/pymux#399.
    KEEP: ClassVar[dict[str, Keep]] = {
        # Sessions, and the counters that name them.
        "sessions": Keep.SAVED,
        "_session_counter": Keep.SAVED,
        "_uses": Keep.SAVED,
        "returning_clients": Keep.SAVED,
        "created": Keep.SAVED,
        "socket_name": Keep.SAVED,
        "listener": Keep.SAVED,
        "original_cwd": Keep.SAVED,
        "source_file": Keep.SAVED,
        "startup_command": Keep.SAVED,
        "_startup_done": Keep.SAVED,
        # Set while the first window opens; a loaded server opened it.
        "_first_window": Keep.DROPPED,
        # `resume-server` reads the configuration again before the
        # snapshot, and what it finds wrong now is what counts.
        "startup_errors": Keep.REBUILT,
        # An upgrade refuses a server that draws on its own terminal, so
        # the server that takes over is always on a socket.
        "_serves_one_terminal": Keep.REBUILT,
        # What the server holds for every session.
        "global_environment": Keep.SAVED,
        "clipboard": Keep.SAVED,
        "named_buffers": Keep.SAVED,
        "prompt_history": Keep.SAVED,
        "hooks": Keep.SAVED,
        "wait_channels": Keep.SAVED,
        "message_log": Keep.SAVED,
        "notifications": Keep.SAVED,
        "notification_center": Keep.SAVED,
        # As the panes' progress it follows.
        "progressing": Keep.DROPPED,
        "jobs": Keep.SAVED,
        # Bindings a person may have changed with `bind-key`.
        "key_bindings_manager": Keep.SAVED,
        # Option values.
        # Its writer starts again in `running`; what waited was written
        # as the old scope ended.
        "actions": Keep.REBUILT,
        # The compositor's task starts again in `running`.
        "wayland": Keep.REBUILT,
        "portal": Keep.REBUILT,
        "audio": Keep.REBUILT,
        "browsers": Keep.DROPPED,  # each one goes with its program, which the upgrade ends
        # Tables, indexes and what the server last pushed.
        "option_tables": Keep.REBUILT,
        "panes_by_id": Keep.REBUILT,
        "hanging_up": Keep.DROPPED,  # an upgrade kills them before it goes
        "_killed_in": Keep.DROPPED,  # the panes it names are hanging up, and go with them
        "refusing_commands": Keep.DROPPED,  # the new image takes them again
        "_client_states": Keep.REBUILT,  # from the client records, as clients reconnect
        # Told again at the first focus sync, which a program reads as one more focus-in.
        "_focused_panes": Keep.DROPPED,
        "shims": Keep.REBUILT,
        "options": Keep.SAVED,
        "session_defaults": Keep.SAVED,
        "terminals": Keep.REBUILT,
        # The running server.
        "connections": Keep.DROPPED,
        "tasks": Keep.DROPPED,
        "error_notice": Keep.REBUILT,
        "loop": Keep.DROPPED,
        "done": Keep.DROPPED,
        "counters": Keep.DROPPED,
        # A client that reconnects deletes every image it had, so the
        # next server may hand out other ids.
        "outer_image_ids": Keep.DROPPED,
        "sourcing": Keep.DROPPED,
        # `display-panes` showing, which a timer ends; the timer does
        # not survive, so neither does this.
        "display_pane_numbers": Keep.DROPPED,
    }

    def __init__(
        self,
        source_file: str | None = None,
        startup_command: str | None = None,
        session_name: str | None = None,
        size_with_no_client: Size | None = None,
    ):
        self._client_states = {}  # connection -> client_state
        #: The ids of the panes that have the focus. Lillecarl/pymux#795.
        self._focused_panes: set[PaneId] = set()

        #: How many times a client has been used, over the session.
        #: `client_was_used` bumps it and stamps the client, and
        #: `window-size latest` reads the stamps.
        self._uses = 0

        self.options = ServerOptions()
        self.options.owner = self
        #: What every session reads unless it set its own. Lillecarl/pymux#756.
        self.session_defaults = SessionOptions()
        self.session_defaults.owner = self
        # `--log-level` told the logger before this server existed.
        self.options.hold("log_level", log.level())

        self.shims = Shims()

        # The paste buffer of the session. Copy mode writes it, a pane
        # that writes the clipboard of the user writes it too, and
        # "paste-buffer" reads it. Every client shares this one.
        self.clipboard = InMemoryClipboard()

        # A Wayland compositor for the panes that have no display, when
        # pysmithay is installed and `wayland-server` is on. `wayland.py` says why.
        self.wayland = WaylandServer(self)
        # The light or dark scheme those panes' programs read. `portal.py`
        # says where. Lillecarl/pymux#812.
        self.portal = Portal(self)
        # Sound to and from the client used last. `audio.py` says what
        # each mode costs. Lillecarl/pymux#657.
        self.audio = AudioServer(self)
        #: Browsers programs here drive on a client's machine.
        #: Lillecarl/pymux#653.
        self.browsers = Browsers(self)

        self.actions = ActionLog()

        #: Which table a scope reads. `set-option`, `set-window-option`
        #: and `set-client-option` are one command over these, and
        #: `set-option` takes the server's names too.
        self.option_tables = {
            Scope.SERVER: ALL_SERVER_OPTIONS,
            Scope.SESSION: ALL_OPTIONS,
            Scope.WINDOW: ALL_WINDOW_OPTIONS,
            Scope.CLIENT: ALL_CLIENT_OPTIONS,
        }

        #: Which pane a desktop notification came from. The terminal of
        #: the user answers a notification by its identifier, and every
        #: pane names its own without knowing about the others.
        self.notifications = NotificationRoutes()

        #: The notifications the hub shows, across every source. Kept
        #: beside the routes and not in them: a route answers one
        #: question, the hub lists everything.
        self.notification_center = NotificationCenter()
        self.notification_center.on_added = self._notified

        #: The panes that say a progress (OSC 9 ; 4), so the window list
        #: walks no panes while none does. A pane that goes stays until
        #: it would have cleared it, which costs a walk and no wrong
        #: answer. Lillecarl/pymux#788.
        self.progressing: set[PaneId] = set()

        # When no panes are available.
        self.original_cwd = os.getcwd()

        self.display_pane_numbers = False

        #: True when this server draws on the one terminal it runs in.
        #: `pymux integrated` is that: a socket may still be bound, for
        #: commands, and a client that arrives over it cannot attach.
        #: Lillecarl/pymux#159.
        self._serves_one_terminal = False

        self.connections = []

        self.terminals = Terminals(self)

        #: The task group every route runs in. `running()` owns it, so
        #: it is there for exactly as long as this server serves, and
        #: `None` before and after. Lillecarl/pymux#87.
        self.tasks: anyio.abc.TaskGroup | None = None

        #: The corner that says an error was logged. Lillecarl/pymux#241.
        self.error_notice = ErrorNotice(self)

        #: The asyncio loop under anyio, for inspection only: `introspect`
        #: lists the tasks of a running server, and only the loop can
        #: list them. Nothing schedules work on it. `running()` sets it.
        self.loop: asyncio.AbstractEventLoop | None = None

        #: Set when this server is to stop. `run_server` waits for it.
        #: An `anyio.Event` may be made before a loop runs, which is
        #: what a server started by `daemonize` does.
        self.done = anyio.Event()

        # The lines of the configuration file that failed, until a
        # client is there to be told about them.
        self.startup_errors: list[str] = []

        # What show_message and add_command_error said, oldest last.
        # show-messages reads it back.
        self.message_log: deque[str] = deque(maxlen=100)

        #: The channels of `wait-for`, by name. A channel exists for
        #: as long as it remembers something: a signal nobody took, a
        #: lock somebody holds, or somebody waiting.
        #: Lillecarl/pymux#302.
        self.wait_channels: dict[str, WaitChannel] = {}

        #: The jobs of `run`, by id. A job is a command with pipes
        #: instead of a terminal, owned by the server rather than by
        #: any pane, so it outlives every view onto it.
        self.jobs = JobTable()

        self.hooks = Hooks()

        # The file and the line that `source-file` is reading now, so
        # that a failure can say which line it was.
        self.sourcing: str | None = None

        self._startup_done = False
        self._first_window: anyio.Event | None = None
        self.source_file = source_file
        self.startup_command = startup_command

        # Time when this server was started.
        self.created = time.time()

        #: What this server has done since it started. `pymux counters`
        #: reads it, and `pymux/introspect.py` says why the reasons
        #: matter more than the totals.
        self.counters = introspect.Counters()

        #: The id each pane image has on the terminals of the clients,
        #: one answer for all of them. Lillecarl/pymux#600.
        self.outer_image_ids = OuterImageIds()

        # Keep track of all the panes, by ID. (For quick lookup.)
        self.panes_by_id: weakref.WeakValueDictionary[PaneId, Pane] = weakref.WeakValueDictionary()
        #: The session each pane `kill_pane` took out was in, until its
        #: program ends: by then no session holds it, and the end has to
        #: know which one it may leave empty. Lillecarl/pymux#807.
        self._killed_in: dict[PaneId, Session] = {}
        #: Programs sent SIGHUP whose SIGKILL may still be due. The
        #: grace task keeps each backend alive until the program ends.
        self.hanging_up: weakref.WeakSet[PosixBackend] = weakref.WeakSet()
        #: Set from an upgrade's last snapshot write to its exec. A command
        #: then changes what the snapshot no longer describes: a pane
        #: killed in that window was neither killed nor reaped by either
        #: image. Lillecarl/pymux#740.
        self.refusing_commands = False

        # Socket information.
        self.socket_name: str | None = None
        #: The bound socket that waits for clients, once
        #: `listen_on_socket` made one. `running()` serves it.
        self.listener: PosixSocketListener | None = None

        # Key bindings manager.
        self.key_bindings_manager = PymuxKeyBindings(self)

        #: Every session of this server, oldest first, and the number
        #: the next one takes. tmux numbers a session for the life of
        #: the server, so a killed `$1` never comes back as `$1`.
        self.sessions: list[Session] = []
        self._session_counter = 0
        first = self.create_session(name=session_name)

        #: What a loaded snapshot says about each client, by the id it
        #: keeps across reattaches, until that client comes back.
        #: `snapshot.py` fills it. Lillecarl/pymux#399.
        self.returning_clients: dict[str, dict[str, Any]] = {}

        # `new-session -x -y` on the route that starts a server: the
        # session is made here and its first window comes later, in
        # `startup`, so this is early enough for the program in that
        # window to be told the right size from its first byte.
        # Lillecarl/pymux#459.
        if size_with_no_client is not None:
            first.default_size = size_with_no_client

        # What `set-environment -g` fills. The other scope is the
        # session's own, on `Session.environment`. A value of None is
        # an unset: the name leaves the environment of a new pane, so
        # a session unset falls through to the global value rather
        # than to nothing. Lillecarl/pymux#270.
        self.global_environment: dict[str, str | None] = {}

        # What the command line and the prompts of every client took.
        # The buffers append through `leave_command_mode`, the grey
        # suggestion reads it, and up and down walk it.
        # Lillecarl/pymux#305.
        self.prompt_history = InMemoryHistory()

        # The named buffers a `set-buffer` fills. The one buffer a
        # pane pastes from stays `clipboard`; a name here is tmux's
        # `-b`. Lillecarl/pymux#303.
        self.named_buffers: dict[str, str] = {}

    def create_session(self, name: str | None = None) -> Session:
        """
        Add a session to this server.

        A session with no name takes its number, which is how tmux names
        one that `new-session -s` did not.
        """
        session_id = SessionId(self._session_counter)
        self._session_counter += 1

        session = Session(
            session_id=session_id,
            name=name if name is not None else str(session_id),
            defaults=self.session_defaults,
        )
        self.sessions.append(session)

        # **A session counts as used the moment it is made.** Nothing
        # has looked at one yet, so every unused session tied at nought
        # and `max` answered with the first of them: a command with no
        # target, over the socket, landed on the session `__init__`
        # made rather than on the one somebody had just created. tmux
        # stamps a session's activity from its creation time
        # (`session.c:154`) and ranks by that (`cmd-find.c:146`).
        # Lillecarl/pymux#473.
        self._uses += 1
        session.last_used = self._uses
        return session

    def remove_session(self, session: Session) -> None:
        """
        Take a session off the server, and move its clients away.

        The last session to go stops the server. A server with nothing
        left to show is what a person means by closing the last pane,
        and it is where `kill-session` ends in tmux as well.

        **The last session stays in the list while the server stops.**
        A client draws one more frame on the way out -- prompt_toolkit
        renders once more as `run_async` returns -- and everything that
        draws reads a session. An empty list there is a crash in the
        last frame, where an empty arrangement is the state a server
        with no windows has always been in.
        """
        if session not in self.sessions:
            return

        if len(self.sessions) == 1:
            # `kill-session` hung its panes up a moment ago, and a
            # SIGKILL now would race their shells' history.
            if self.tasks is not None:
                self.tasks.start_soon(self.hang_up_and_stop)
            else:
                self.stop()
            return

        self.sessions.remove(session)

        somewhere = self.last_used_session
        for client_state in self._client_states.values():
            if client_state.session is session:
                client_state.session = somewhere
                client_state.sync_focus()

        self.invalidate(Woke.SESSION_CLOSED)

    def kill_session(self, session: Session) -> None:
        """
        Kill every pane of a session, and take the session off.

        The last pane already takes the session, so the call below is
        for a session that had no pane to start with.
        """
        for window in list(session.arrangement.windows):
            for pane in list(window.panes):
                self.kill_pane(pane)

        if session in self.sessions:
            self.remove_session(session)

    def take_environment_from(self, client_state: ClientState) -> None:
        """
        Refresh the named variables of a session from the client that
        just attached to it. ("update-environment".)

        **The machine a person attaches from is the machine their panes
        should talk to.** `DISPLAY`, `SSH_AUTH_SOCK` and `WAYLAND_DISPLAY`
        name things that live on that machine, and a server outlives the
        client that started it: over ssh (Lillecarl/pymux#90) an old
        `DISPLAY` from the server's own start is worse than none, because
        a pane then opens a window nobody is looking at.

        A name the client does not carry is taken out of the session
        rather than left behind, which is what tmux does
        (`environ_update` in its `environ.c`) and for the same reason:
        the point is that these follow the client.

        `set-environment` writes the same dictionary and is not touched
        here, except for the names this option lists -- those belong to
        whoever attached. Lillecarl/pymux#271, Lillecarl/pymux#270.
        """
        reported = client_state.connection.environment
        environment = client_state.session.environment

        for pattern in client_state.session.options.update_environment.split():
            # A pattern, because tmux matches one: `XDG_*` is how a
            # person takes a family of them without naming each.
            found = False
            for name, value in reported.items():
                if fnmatch.fnmatchcase(name, pattern):
                    environment[name] = value
                    found = True

            if not found:
                environment.pop(pattern, None)

    def welcome_back(self, client_state: ClientState, client_id: str) -> None:
        """
        Give a client that attaches again after `upgrade-server` the
        view and the settings it left with. A record names ids, and one
        whose session or window has gone since is skipped.
        Lillecarl/pymux#409.
        """
        record = self.returning_clients.pop(client_id, None) if client_id else None
        if record is None:
            return
        sessions = {session.session_id: session for session in self.sessions}

        app = client_state.app
        for session_id, (active_id, previous_id) in record["windows"].items():
            session = sessions.get(session_id)
            if session is None:
                continue
            arrangement = session.arrangement
            windows = {window.window_id: window for window in arrangement.windows}
            if active_id in windows:
                arrangement._active_window_for_cli[app] = windows[active_id]
            if previous_id in windows:
                arrangement._window_history_for_cli[app] = [windows[previous_id]]

        for name, value in record["settings"].items():
            setattr(client_state, name, value)
        for name, value in record["options"].items():
            setattr(client_state.options, name, value)
        self.terminals.sync_color_bases()
        if record.get("connected") is not None:
            client_state.connection.created = record["connected"]

        session = sessions.get(record["session_id"])
        if session is not None:
            client_state.session = session
        client_state.previous_session = sessions.get(record["previous_session_id"])
        client_state.sync_focus()
        if record.get("modes"):
            # The view is back already; a mode that cannot open again
            # must not cost the person the attach.
            try:
                client_state.take_modes(record["modes"])
            except Exception:
                logger.exception("Could not open again what a returning client was in the middle of.")
        self.invalidate(Woke.SESSION_CHANGED)

    def attach_client_to(self, client_state: ClientState, session: Session) -> None:
        "Put one client on a session, and draw what it shows."
        if client_state.session is session:
            return

        client_state.previous_session = client_state.session
        client_state.session = session
        # The session this client now watches takes what the client
        # reported when it attached. Lillecarl/pymux#271.
        self.take_environment_from(client_state)
        self.client_was_used(client_state)
        client_state.sync_focus()
        self.invalidate(Woke.SESSION_CHANGED)

    @property
    def current_session(self) -> Session:
        """
        The session of the client that asks.

        `get_app()` names that client, which is right for a key binding
        and for a command, and wrong during a render: the renderer of
        one client can run while another client is the current one. A
        caller that holds the client reads `client_state.session`
        instead. `Arrangement.get_active_window_for` says the same about
        the window, one level down. Lillecarl/pymux#323.

        Nothing asks -- a start-up command, a timer -- falls back to the
        session a person looked at last.
        """
        try:
            return self.get_client_state().session
        except ValueError:
            return self.last_used_session

    @property
    def last_used_session(self) -> Session:
        """
        The session a person looked at last, of the ones that are left.

        **A session with no window is not an answer.** A command with
        no target needs a window to act on, and that session has none,
        so answering with it turns a server full of windows into "no
        current window". tmux refuses one outright:
        `cmd_find_session_valid` calls a session whose `curw` is NULL
        invalid and `cmd_find_best_session` skips it
        (`cmd-find.c:150-158`).

        One is taken when no session holds a window, because then the
        answer is a message and there is no better session to give it
        about. Lillecarl/pymux#473.
        """
        holding = [one for one in self.sessions if one.arrangement.windows]
        return max(holding or self.sessions, key=lambda session: session.last_used)

    def client_of(self, app: Application[Any]) -> ClientState | None:
        "The client that this application draws for."
        for client_state in self._client_states.values():
            if client_state.app is app:
                return client_state
        return None

    def session_of(self, app: Application[Any]) -> Session:
        "The session of the client that this application draws for."
        client_state = self.client_of(app)
        return self.last_used_session if client_state is None else client_state.session

    def get_session(self, name: str) -> Session | None:
        """
        The session a target names, or None.

        A target is the name, tmux's exact-match `=name`, or the id
        `$<number>`.
        """
        name = name.removeprefix("=")

        if name.startswith("$") and name[1:].isdigit():
            session_id = SessionId(int(name[1:]))
            for session in self.sessions:
                if session.session_id == session_id:
                    return session
            return None

        for session in self.sessions:
            if session.name == name:
                return session
        return None

    @property
    def arrangement(self) -> Arrangement:
        "The windows of the session of the client that asks."
        return self.current_session.arrangement

    @property
    def session_name(self) -> str:
        return self.current_session.name

    @session_name.setter
    def session_name(self, name: str) -> None:
        self.current_session.name = name

    @property
    def session_environment(self) -> dict[str, str | None]:
        return self.current_session.environment

    @property
    def overlay_pane(self):
        "The overlay of the session of the client that asks."
        return self.current_session.overlay_pane

    @property
    def show_status(self) -> bool:
        """
        Whether the client that is asking draws the status line.

        **The client that is asking, because `full-screen` is one
        terminal's.** Every reader of this draws, and a frame is drawn
        under the application of the client it is for, so the client
        asking is the client the answer is about. The plane asks every
        watcher's `StatusLine.rows` instead, and
        `any_watcher_shows_pane_status`, which are not this one.
        Lillecarl/pymux#471.
        """
        options, full_screen = self._asking_for_chrome()
        return options.status and not full_screen

    @property
    def show_pane_status(self) -> bool:
        "Whether the client that is asking draws the titlebar of a pane."
        options, full_screen = self._asking_for_chrome()
        return options.pane_border_status and not full_screen

    def _asking_for_chrome(self) -> tuple[OwnOptions, bool]:
        """
        The options of the asking client's session, and whether it is
        full screen, from one lookup of the client: every frame asks
        both, several times. Lillecarl/pymux#770.
        """
        try:
            state = self.get_client_state()
        except ValueError:
            return self.last_used_session.options, False
        return state.session.options, state.options.full_screen

    def _asking_is_full_screen(self) -> bool:
        """
        Whether the client this frame is for wants no chrome.

        No client is no, not the server's own answer: a client is what
        holds the flag now, so with nobody there nobody asked for it.
        """
        try:
            return self.get_client_state().options.full_screen
        except ValueError:
            return False

    def _anyone_is_full_screen(self) -> bool:
        """
        Whether any client of this server wants no chrome.

        **The cheap half of the plane's question.** Until somebody asks
        for full screen the answer below is the server's option, and no
        window has to be walked for it. `clients_watching` sets the
        application of every client and asks which window it is on, and
        a plan asks the question four times per pane: measured at +128
        bytecode instructions each, which `checks.pymux-frame` read as
        10% of a one-pane window's neighbour cost.
        Lillecarl/pymux#471.
        """
        return any(one.options.full_screen for one in self._client_states.values())

    def any_watcher_shows_pane_status(self, window: Window | None = None) -> bool:
        """
        Whether any client watching that window draws the titlebar of a
        pane.

        **This is the plane's question, and `show_pane_status` is
        not.** The plane is one size for every client watching a
        window, because a pane has one pty, so it keeps the rows the
        chrome of any watcher needs and a client that dropped its own
        draws background in them. The other way round -- a plane that
        grew when the last client with chrome turned it off -- resizes
        everybody's programs because somebody else changed their mind.
        `plane_size` asks the same of the status line.

        **Nobody watching is yes**, which is what the plane was
        measured with before this question existed.
        `_any_of_them_wants_chrome` says why it is not the other
        answer. Lillecarl/pymux#471.
        """
        session = self.session_of_window(window) if window is not None else None
        wanted = (session or self.current_session).options.pane_border_status
        if not self._anyone_is_full_screen():
            return wanted
        return wanted and _any_of_them_wants_chrome(self.clients_watching(window))

    def refresh_what_time_moves(self, but_not: Application[Any] | None = None) -> None:
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
        that `client_asked_for_frame` can ask about the others and
        leave that one alone.
        """
        for client_state in self._client_states.values():
            # Every scheduled redraw records the tick, for its own
            # client and the skipped one alike: the renderer compares
            # it against the input since, and skips a frame that
            # input arrived after. Lillecarl/pymux#524.
            client_state.schedule_tick = client_state.input_tick
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

    def client_asked_for_frame(self, app: Application[Any]) -> None:
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

        self.terminals.sync_kitty_flags()
        self.terminals.sync_pointer_shape()
        self.terminals.sync_progress()
        self.terminals.sync_keyboard_source_flags()

    def server_starts(self) -> None:
        """
        What every route does before its loop turns.

        Both start a server -- the socket and `integrated` -- and each
        one takes the signal here. The work that needs a loop is in
        `running()`.
        """
        introspect.answer_signal()
        introspect.log_what_escapes()
        say_how_it_ends(logger)

    @asynccontextmanager
    async def running(self):
        """
        The scope this server serves in.

        It holds the task group that every background task of the
        server lives in, so a task cannot outlive the server that
        started it, and it cannot be collected while it runs.
        `_spawn` on a connection puts work here.

        Both routes enter it, and so does the test harness. A
        route that built its own would be a route the harness could
        not be. Lillecarl/pymux#87.
        """
        async with anyio.create_task_group() as tasks:
            self.tasks = tasks
            self.loop = asyncio.get_running_loop()
            self.error_notice.install()
            tasks.start_soon(self._auto_refresh)
            tasks.start_soon(self._watch_window_output)
            tasks.start_soon(self._sweep_jobs)
            tasks.start_soon(self.actions.keep, lambda: self.options.action_log_limit)
            tasks.start_soon(self.wayland.keep)
            tasks.start_soon(self.portal.keep)
            tasks.start_soon(self.audio.keep)
            if self.listener is not None:
                self.serve_listener()
            try:
                yield
            finally:
                # Every task of this server ends with the scope,
                # including the auto refresh above, which never ends
                # on its own.
                tasks.cancel_scope.cancel()
                self.tasks = None
                self.error_notice.uninstall()

    def serve_listener(self) -> None:
        "Take clients on `listener`, in the server's task group."
        if self.tasks is None or self.listener is None:
            raise RuntimeError("A listener is served inside `Pymux.running`, once there is one.")
        self.tasks.start_soon(self.listener.serve)

    def serve_connection(self, connection: ServerConnection) -> None:
        """
        Put a connection's own read loop in the server's task group.

        A `ServerConnection` is built in a sync callback -- the accept
        of a socket, or the harness -- and everything it then does is
        async. This is the one line between the two.
        """
        if self.tasks is None:
            raise RuntimeError("A connection was made outside `Pymux.running`, so nothing would read it.")
        self.tasks.start_soon(connection.serve)

    async def _watch_window_output(self) -> None:
        "Raise the activity and silence marks, once a second. Lillecarl/pymux#767."
        while True:
            await anyio.sleep(WINDOW_OUTPUT_INTERVAL)
            now = anyio.current_time()
            try:
                raised = False
                for session in self.sessions:
                    raised |= session.arrangement.check_output(self.apps, now)
                if raised:
                    self.invalidate(Woke.WINDOW_MARKED)
            except Exception:
                # Like the clock: this must not take the server with it.
                logger.exception("Checking the windows for output failed.")

    def _notified(self, record: Notification) -> None:
        "Mark the window a notification came from. Lillecarl/pymux#767."
        found = self.window_of_pane(record.pane_id)
        if found is None:
            return
        session, window, pane = found
        if session.arrangement.notify(window, self.apps):
            self.invalidate(Woke.WINDOW_MARKED)
        if record.source not in (NotificationSource.OSC_99, NotificationSource.UNKNOWN):
            self._send_clickable(record, pane)

    def _send_clickable(self, record: Notification, pane: Pane) -> None:
        """
        Send a notification pymux recorded, from OSC 777, `pymux notify`
        or an attention request, as OSC 99 to each terminal that reports
        a click, so the click shows `pane`. Lillecarl/pymux#779.
        """
        clickers = [
            connection
            for connection in self._client_states
            if connection is not None and "report" in connection.notification_actions
        ]
        if not clickers or pane.pane_id in self._panes_in_sight():
            return
        ours = self.notifications.own(pane.pane_id)
        sequences = [
            build_osc(Osc.NOTIFICATION, param) for param in osc99_of(ours, record.title, record.body, record.urgency)
        ]
        for connection in clickers:
            for sequence in sequences:
                if sequence is not None:
                    connection.forward_osc(sequence)
        self.invalidate(Woke.NOTIFICATION_SENT)

    def show_pane_on_click(self, client_state: ClientState, pane: Pane) -> None:
        "Take this client to the pane a clicked notification came from, unless its session says no."
        if not self.options_of_pane(pane).notification_click_shows_pane:
            return
        self.show_pane(client_state, pane)

    def show_pane(self, client_state: ClientState, pane: Pane) -> bool:
        "Move this client onto a pane, wherever it is. False when the pane is in no window."
        found = self.window_of_pane(pane.pane_id)
        if found is None:
            return False
        session, window, _pane = found
        self.attach_client_to(client_state, session)
        # The active window is kept per application, and a caller off
        # this client's input has another one current.
        with set_app(client_state.app):
            session.arrangement.set_active_window(window)
        window.active_pane = pane
        self.invalidate(Woke.CLICK_CHOSE_A_WINDOW)
        return True

    async def _auto_refresh(self) -> None:
        """
        Refresh the clients every `status_interval` seconds.

        On the loop, and not on a thread of its own. The refresh reads
        which window each client looks at, and `set_app` answers that
        by writing an `AppSession` that every client shares. A thread
        that writes it while a render reads it hands that render
        another client's application. Lillecarl/pymux#155.

        The interval is read each turn, so a `set-option
        status-interval` reaches the tick after this one. One timer
        serves every session, so it ticks at the shortest any of them
        asks for.
        """
        while True:
            await anyio.sleep(
                min((s.options.status_interval for s in self.sessions), default=self.session_defaults.status_interval)
            )
            try:
                self.refresh_what_time_moves()
            except Exception:
                # The clock must not take the server with it. In a task
                # group an exception cancels the siblings, and the
                # siblings here are every client of the server.
                logger.exception("The refresh on the clock failed.")

    async def _sweep_jobs(self) -> None:
        """
        Forget the jobs idle past `job_ttl`, on a slow clock.

        The time to live is read each turn, so a `set-option job-ttl`
        reaches the sweep after this one. A sweep that failed must not
        take the server with it, for the same reason the refresh
        above says the same.
        """
        while True:
            await anyio.sleep(JOB_SWEEP_EVERY)
            try:
                if self.options.job_ttl:
                    self.jobs.sweep(self.options.job_ttl)
            except Exception:
                logger.exception("The sweep of idle jobs failed.")
            try:
                self.notification_center.sweep(self._seen_of, self.options.notification_keep_seen, time.time())
            except Exception:
                logger.exception("The sweep of seen notifications failed.")

    def _seen_of(self, record: Notification) -> float | None:
        """
        When a client saw the window a notification came from, or None.
        One with no window to look at is seen now, so it goes in time.
        """
        found = self.window_of_pane(record.pane_id)
        if found is None:
            return time.time()
        session, window, _pane = found
        if window.seen_at >= record.at:
            return window.seen_at
        return time.time() if session.arrangement.watched(window, self.apps) else None

    @property
    def apps(self):
        return [c.app for c in self._client_states.values()]

    def get_client_state(self):
        "Return the active ClientState instance."
        app = get_app()
        client_state = self.client_of(app)
        if client_state is None:
            raise ValueError("Client state for app %r not found" % (app,))
        return client_state

    def the_client_to_tell(self):
        """
        The client a dialog belongs to, or `None` when nobody is there.

        A command that arrives over a socket runs under a temporary CLI
        that draws nothing and is taken away as soon as the command is
        answered. A dialog put on that one is a dialog nobody sees: a
        `display-message` typed in a pane said nothing at all, and a
        `confirm-before` asked a question no one could answer, so its
        command never ran. Lillecarl/pymux#272.

        So a real client answers for itself, and the fake one hands over
        to the client a person used last -- the rule
        `clients_to_open_on` already follows, for the same reason.

        A listing does not come through here: `show_listing` and
        `answer` write to stdout when a command arrived over the command
        line, which is where the person who typed it is looking.
        Lillecarl/pymux#288, #289.
        """
        try:
            asking = self.get_client_state()
        except ValueError:
            asking = None

        if asking is not None and not asking.temporary:
            return asking

        return self.latest_client()

    @property
    def clients(self) -> list[ClientState]:
        """
        Every client a person is sitting at.

        A command that arrives over a socket runs under a temporary
        client that draws nothing and goes as soon as the command is
        answered. Nobody is at one of those, so it is not a client to
        count, to list or to tell anything.
        """
        return [client for client in self._client_states.values() if not client.temporary]

    def clients_on(self, session: Session, except_for: ClientState | None = None) -> list[ClientState]:
        """
        Every client that holds this session, other than one.

        This is what `attach -d` means by "the other clients", and both
        ways of asking for it read it here. tmux's rule is one line --
        `if (c_loop->session != s || c == c_loop) continue;`
        (`cmd-attach-session.c:129`) -- and pymux had it written twice,
        once for the flag on the `start-gui` packet and once for the
        command. Lillecarl/pymux#345, Lillecarl/pymux#349.

        **A command connection is not a client**, and `clients` already
        says so: the temporary client a command runs under is nobody,
        and closing it killed somebody's `list-sessions` mid-answer.
        """
        return [
            client_state
            for client_state in self.clients
            if client_state.session is session and client_state is not except_for
        ]

    async def startup(self):
        # Handle start-up comands.
        # (Does initial key bindings.)
        if not self._startup_done:
            self._startup_done = True
            self._first_window = anyio.Event()
            try:
                self.spawn_command(self.configure())

                # Make sure that there is one window created.
                await self.create_window(command=self.startup_command)
            finally:
                self._first_window.set()
        elif self._first_window is not None:
            # The window waits for its process. A command that arrives
            # meanwhile waits too, where it answered "no current
            # window"; tmux opens it before an attach returns.
            # Lillecarl/pymux#571.
            await self._first_window.wait()

    def configure(self):
        """
        Run the default commands, and answer with the configuration file's
        reading to await, or None when there is no file.
        """
        with started_by(Origin.CONFIG):
            for cmd in STARTUP_COMMANDS.splitlines():
                self.handle_command(cmd)

            if self.source_file:
                answer = call_command_handler("source-file", self, [self.source_file])
                return None if answer is None else awaited_as(Origin.CONFIG, answer)
        return None

    def get_title(self):
        """
        The title to be displayed in the titlebar of the terminal.
        """
        w = self.arrangement.get_active_window()

        title = w.active_pane.screen.titles.window if w and w.active_pane else ""

        if title:
            return "%s - Pymux" % (title,)
        return "Pymux"

    def plane_size(self, window: Window | None = None) -> Size:
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
        `layout.room_for_panes` takes the rows the chrome
        around the panes wants off what is left. **A manual size is
        already the window's own**, so nothing comes off it.

        **Nobody watching is the session's `default_size`**, and no
        status row comes off it. A window exists before a client
        attaches to it, and the program in a pane needs a size from the
        first byte it writes. `new-session -x -y` is what names that
        size. Lillecarl/pymux#459.

        **A session nobody named a size for takes the clients' size**,
        by the same `window-size` rule, once: when the window is made.
        An agent's window is then the size the person's would be, and a
        program in it is never resized behind its back.
        Lillecarl/pymux#817.
        """
        if window is None:
            window = self.arrangement.get_active_window()
        if window is None:
            return self.size_with_no_client(None)

        if window.options.window_size is WindowSize.MANUAL and window.manual_size is not None:
            return window.manual_size

        watchers = self.clients_watching(window)

        if not watchers:
            return self.size_with_no_client(self.session_showing(window), window)

        return _plane_of_clients(watchers, window.options.window_size)

    def size_with_no_client(self, session: Session | None, window: Window | None = None) -> Size:
        """
        How big a window of that session is while nobody is watching it.

        The size `new-session -x -y` named, when it named one. Otherwise
        the size the window took when it was made, and for a window
        being made, what the clients give it now (`size_for_a_new_window`).
        """
        if session is not None and session.default_size is not None:
            return session.default_size
        if window is not None and window.size_when_made is not None:
            return window.size_when_made
        return self.size_for_a_new_window(session)

    def size_for_a_new_window(self, session: Session | None) -> Size:
        """
        What the clients on the server give a window made now, by the
        `window-size` a new window of that session starts with
        (`set-window-option -g`). With no client at all, the same two
        numbers tmux answers.
        """
        clients = self.clients
        if not clients:
            return DEFAULT_SIZE
        defaults = session.arrangement.window_defaults if session is not None else {}
        rule = defaults.get("window_size")
        if not isinstance(rule, WindowSize):
            rule = WindowOptions().window_size
        return _plane_of_clients(clients, rule)

    def session_showing(self, window: Window) -> Session | None:
        "The session whose windows hold this one, or None when it is gone."
        for session in self.sessions:
            if window in session.arrangement.windows:
                return session
        return None

    def client_was_used(self, client_state: ClientState) -> None:
        """
        Note that a person just used this client.

        `window-size latest` reads it: the plane belongs to whichever
        terminal somebody last typed in. A counter and not a clock,
        because two key presses in one millisecond still have an order
        and a clock may not say which came first.
        """
        self._uses += 1
        client_state.last_used = self._uses
        client_state.session.last_used = self._uses
        # The panes take this client's cell. Lillecarl/pymux#753.
        self.terminals.sync_cell_size()
        self.terminals.sync_color_scheme()

    def sync_pane_focus(self) -> None:
        """
        Send focus-in and focus-out to the panes that asked (DEC 1004).

        A pane has the focus while it is the active pane of a client
        whose terminal window has it. Several clients can give several
        panes the focus at once; tmux has one client and one focus.
        Lillecarl/pymux#795.
        """
        self._close_seen_notifications()
        focused = set()
        for client_state in self._client_states.values():
            if client_state.terminal_focused and not client_state.temporary:
                pane = client_state.session.arrangement.get_active_pane_for(client_state.app)
                if pane is not None:
                    focused.add(pane.pane_id)
        if focused == self._focused_panes:
            return
        for pane_id, report in [(one, FOCUS_IN) for one in focused - self._focused_panes] + [
            (one, FOCUS_OUT) for one in self._focused_panes - focused
        ]:
            pane = self.panes_by_id.get(pane_id)
            if pane is None:
                continue
            # A window pane has no program to read the report.
            screen, process = pane.screen, pane.process
            if isinstance(screen, WindowScreen) or isinstance(process, WindowProcess):
                continue
            if screen.reports_focus and not process.is_terminated:
                process.write_input(report)
        self._focused_panes = focused

    def _panes_in_sight(self) -> set[PaneId]:
        "The panes of every window a client shows on a terminal that has the focus."
        shown: set[PaneId] = set()
        for client_state in self._client_states.values():
            if client_state.terminal_focused and not client_state.temporary:
                window = client_state.session.arrangement.get_active_window_for(client_state.app)
                if window is not None:
                    shown.update(pane.pane_id for pane in window.panes)
        return shown

    def _close_seen_notifications(self) -> None:
        """
        Take the desktop notifications of a window off each terminal that
        can close one, once a client whose terminal has the focus shows
        that window. Lillecarl/pymux#799.
        """
        if not self.notifications.any_shown:
            return
        closed = self.notifications.seen(self._panes_in_sight())
        if not closed:
            return
        for connection in self._client_states:
            if connection is not None and connection.notification_closes:
                for ours in closed:
                    connection.forward_osc(build_osc(Osc.NOTIFICATION, close_of(ours)))

    def clients_watching(self, window: Window | None = None) -> list[ClientState]:
        """
        Every client that is looking at that window.

        The active window of the session by default. A client shows one
        window at a time, and a client on another window says nothing
        about how big this one should be.
        """

        def active_window_for_app(app: Application[Any]) -> Window | None:
            with set_app(app):
                return self.arrangement.get_active_window()

        if window is None:
            window = self.arrangement.get_active_window()

        return [
            client_state
            for client_state in self._client_states.values()
            if not client_state.temporary and active_window_for_app(client_state.app) == window
        ]

    async def _create_pane(
        self,
        window: Window | None = None,
        command: str | None = None,
        start_directory: str | None = None,
        on_done: Callable[[], None] | None = None,
        session: Session | None = None,
        job: Job | None = None,
        task_group: anyio.abc.TaskGroup | None = None,
    ):
        """
        Create a new :class:`pymux.arrangement.Pane` instance. (Don't put it in
        a window yet.)

        :param window: If a window is given, take the CWD of the current
            process of that window as the start path for this pane.
        :param command: If given, run this command instead of `default-shell`.
        :param start_directory: If given, use this as the CWD.
        :param session: Where this pane is going, which says how big it
            is until a client renders it. The pane is not in a window
            yet, so it cannot be asked.
        :param job: If given, show this job instead of running anything:
            a viewer pane, read-only, that replays the tail and follows
            the rest. Nothing forks and no program runs.
        :param task_group: Whose scope the program runs in. Without one
            it is the server's, and a pane made outside `Pymux.running`
            names its own: tests hold one for the test. Nothing is
            started without a group to watch it.
        """
        if task_group is None:
            task_group = self.tasks
        if task_group is None:
            raise RuntimeError(
                "A pane was made outside `Pymux.running` with no task group, so nothing would watch its program."
            )

        # Start directory.
        path: str | None

        path = start_directory or self._directory_to_start_in(window, session)
        options = (session or self.current_session).options

        def environment_of_the_pane(environment: dict[str, str]) -> None:
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
            environment["TERM"] = self.options.default_terminal

            if self.socket_name:
                environment["PYMUX"] = "%s,%i" % (self.socket_name, pane.pane_id)

            # The environment a `set-environment` filled. The global
            # scope lands first and the session's over it; a name
            # either unset takes itself out of the child's
            # environment, which is the one thing the child would
            # otherwise inherit from the server. Lillecarl/pymux#270.
            # Merged onto the copy and not onto os.environ, which still
            # holds what ptterm's edit above took out of the pane.
            # The sound names go first, so a `set-environment` of the
            # same name wins. Lillecarl/pymux#657.
            self.audio.pane_environment(environment)
            merged = self.pane_environment(environment)
            environment.clear()
            environment.update(merged)

            # The shims, when they are on, reach the opener and the
            # notifier of this session before anything else on the PATH
            # of the pane, and name the opener for what reads $BROWSER.
            self.shims.put_on_path(environment, options)

            # Our compositor only where the session names no display:
            # a program started beside a desktop belongs on it.
            if self.wayland.display and not environment.get("WAYLAND_DISPLAY"):
                environment["WAYLAND_DISPLAY"] = self.wayland.display

        # `shlex.split` and not `str.split`: a command reaches this as
        # one string, and the quoting inside it is what says where one
        # argument ends. A plain split on whitespace tears
        # `sh -c 'echo one two'` into seven words, and `sh` then reads
        # `'echo` as the whole of its script.
        #
        # `run_pymux.run` builds the string with `shlex.quote`, so this
        # undoes exactly what that did.
        command_list = shlex.split(command) if command else [options.default_shell]

        self.shims.make(options)

        if job is not None:
            pane = self._build_viewer(job)
        else:
            pane = self._build_pane(
                command=command_list,
                environment=environment_of_the_pane,
                directory=path or self.original_cwd,
                on_done=on_done,
            )
        assert isinstance(pane.terminal, Terminal), "`_build_pane` builds a terminal pane"
        terminal_control = pane.terminal.terminal_control

        if not terminal_control._running:
            # The size of the session this pane is going into, until a
            # client attaches. `new-session -x -y` is what names it, and
            # a pane no client ever renders keeps it for ever, which is
            # what an automated caller works on. Lillecarl/pymux#459.
            #
            # **The control and not the process.** `Process.set_size` tells
            # the pty and nothing else; `TerminalControl.set_size` tells the
            # pty and the screen. A screen starts at nought by nought and is
            # sized by the first render, which a detached pane never gets, so
            # sizing only the pty left the screen at zero columns: every
            # character a program wrote wrapped onto a row of its own.
            #
            # It showed as `capture-pane -p` printing one character per line,
            # while `-J` read correctly because it joins wrapped rows back
            # together. Lillecarl/pymux#321.
            size = self.size_with_no_client(session, window)
            terminal_control.set_size(size.columns, size.rows)
            # A program starts watched, so that panes in detached
            # sessions also run and produce output. (Like tmux does.) A
            # viewer's feed starts the same way and follows the job.
            await terminal_control.start(task_group)

        self._register_pane(pane)

        if job is not None:
            logger.info("Viewing job %d in a new pane.", job.job_id)
        else:
            logger.info("Created process %r.", command_list)

        return pane

    def _build_viewer(self, job: Job) -> Pane:
        """
        A pane that shows `job` where a program would run, with nothing
        started. The feed replays the tail and follows the rest once it
        starts. Nothing forks.
        """
        pane = self._build_pane(
            command=[self.session_defaults.default_shell],
            # A viewer never ends on its own: the job ending feeds an
            # exit line and the pane stays, so there is nothing to call
            # back. `JobFeed.kill` stops the follow; whoever kills
            # removes the pane.
            watch_end=False,
        )
        assert isinstance(pane.terminal, Terminal), "`_build_pane` builds a terminal pane"
        terminal_control = pane.terminal.terminal_control
        old_process = terminal_control.process
        terminal_control.process = JobFeed(job, terminal_control.feed_output)
        # No fork, so the pty the construction opened is dead weight:
        # give both ends back. The reader never connected, so `close`
        # only drops the master; the slave would otherwise stay open
        # until the server exits, which closes it only on a reap that
        # never comes.
        backend = old_process.backend
        if isinstance(backend, PosixBackend):
            backend.close()
            if backend.slave is not None:
                os.close(backend.slave)
                backend.slave = None
        return pane

    def _build_pane(
        self,
        backend: Backend | None = None,
        command: list[str] | None = None,
        environment: Callable[[dict[str, str]], None] | None = None,
        directory: str | None = None,
        on_done: Callable[[], None] | None = None,
        watch_end: bool = True,
    ) -> Pane:
        """
        A pane and its widget, wired to this server, with nothing started.

        `backend` is the program's pty: a new one from `command` when
        none is given, or one a snapshot's load adopted. Building is
        synchronous, so a load builds every pane before anything runs.
        Lillecarl/pymux#399.
        """

        def done_callback():
            "When the process finishes."
            if on_done is not None:
                on_done()

            self.finish_recordings([pane])

            if not self.options_of_pane(pane).remain_on_exit:
                self._take_ended_pane(pane)

            self.invalidate(Woke.PANE_ENDED)

        def bell():
            "Sound bell on all clients, and flag it where nobody saw it."
            place = self._place_of(pane)
            if (self.session_defaults if place is None else place[0].options).bell:
                for c in self.apps:
                    c.output.bell()
            if place is not None:
                session, window = place
                if session.arrangement.ring(window, pane, self.apps):
                    self.invalidate(Woke.PANE_RANG)

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
            return self.options_of_pane(pane).allow_program_resize

        def may_type() -> bool:
            """
            May the person whose key this is drive the pane?

            A pane is shared and a keyboard is not, so this asks about
            the client the key came from and never about the pane. The
            answer is no for a client that attached with
            `attach-session -r`. Lillecarl/pymux#467.

            **No client is yes.** A key with no client behind it comes
            from a test, which has nobody to refuse.
            """
            try:
                return not self.get_client_state().read_only
            except ValueError:
                return True

        def takes_key_first() -> bool:
            "A client that holds the prefix: the next key is pymux's."
            try:
                return self.get_client_state().has_prefix
            except ValueError:
                return False

        # Create new pane and terminal.
        terminal = Terminal(
            done_callback=done_callback if watch_end else None,
            backend=backend,
            bell_func=bell,
            osc_func=forward_osc,
            # What copy mode copied. The pane does not build the
            # sequence for it: the option that says whether it goes
            # out lives here, and so does the check every payload
            # passes. Lillecarl/pymux#378.
            copy_func=self._copied_in_copy_mode,
            resize_func=resize,
            may_resize=may_resize,
            may_type=may_type,
            takes_key_first=takes_key_first,
            environment=environment,
            directory=directory,
            command=command,
            # The `history-limit` option, which said how far copy mode
            # could scroll and never reached the screen that holds the
            # rows. A pane kept two thousand whatever the option said.
            get_history_limit=lambda: self.options_of_pane(pane).history_limit,
            unreadable_key_func=_say_key_did_not_fit(),
            key_profile_func=lambda: self.key_profile_of(self.client_of(get_app())),
            helix_keys=lambda: self.options_of_pane(pane).mode_keys is EditingKeys.HELIX,
            wheel_lines=lambda: self.options_of_pane(pane).copy_mode_wheel_lines,
        )
        terminal.on_copy_command = lambda keys: note_keys(self, "copy-mode", keys)
        pane = Pane(terminal)
        # The name the pane answers XTVERSION with, asked at each query:
        # the program in front may be one `hacks_and_lies` lies to.
        terminal.screen.version = lambda: hacks_and_lies.version(
            pane.process.get_name(), hacks_and_lies.outer_term(self.latest_client())
        )

        terminal_control = terminal.terminal_control
        terminal_control.keep_rows = self.options.render_mode is RenderMode.OPTIMIZED

        # What moves `#{pane_revision}`. It is wired here and not in
        # `Pane`, because a pane is about the arrangement and knows
        # nothing about a widget: a dozen tests build one around a stub
        # that has no terminal control at all. `ptyhost` fires this after
        # every read from the program, whatever the bytes were.
        # Lillecarl/pymux#387.
        def changed(_sender: object) -> None:
            pane.content_changed()

        terminal_control.on_content_changed += changed

        def focus_pane_on_click() -> None:
            """
            Select this pane: the mouse focused it.

            The widget moves the layout focus on a click in a pane
            that had none; the arrangement follows here, or the next
            keypress syncs the layout focus straight back to the pane
            that is active. A pane in no window -- an overlay, or one
            already gone -- stays as it is. Lillecarl/pymux#527.
            """
            found = self.window_of_pane(pane.pane_id)
            if found is None:
                return
            _, window, _ = found
            window.active_pane = pane
            with started_by(Origin.MOUSE):
                note(self, Kind.CLICK, "select-pane")

        # What a click selects. It is wired here and not in `Pane`,
        # for the reason `on_content_changed` gives above: a pane is
        # about the arrangement and knows nothing about a widget.
        terminal_control.on_mouse_focus = focus_pane_on_click
        terminal_control.placeholder_ids = lambda image_id: self.outer_image_ids.id_of(pane.pane_id, image_id)

        return pane

    def _take_ended_pane(self, pane: Pane) -> None:
        "Take a pane whose program ended out of its session, and the session if it empties."
        # The session the pane was in, and not the session of whoever
        # happens to be the current client when a program ends.
        killed_in = self._killed_in.pop(pane.pane_id, None)
        session = self.session_holding(pane) or killed_in
        if session is not None:
            session.arrangement.remove_pane(pane)
            self._end_if_empty(session)

        # Make sure the right pane is focused for each client.
        for client_state in self._client_states.values():
            client_state.sync_focus()

    def _end_if_empty(self, session: Session) -> None:
        "A session with no panes left is over. The last one to go stops the server."
        if session in self.sessions and not session.arrangement.has_panes:
            self.remove_session(session)

    def open_window_pane(self, widget: WindowWidget, beside: Pane | None = None) -> Pane | None:
        """
        A pane for a Wayland window that just opened, split off in the
        window that holds `beside`, or else beside the active pane of the
        session a person used last. None when there is no window to put
        it in. Lillecarl/pymux#620.
        """
        found = None if beside is None else self.window_of_pane(beside.pane_id)
        if found is not None:
            window = found[1]
        elif self.sessions:
            window = self.last_used_session.arrangement.get_active_window()
        else:
            return None
        if window is None:
            return None

        pane = self._window_pane_of(widget)
        if beside is not None:
            # One program, one set of tags: a caller finds all its windows by them.
            pane.tags.update(beside.tags)
        window.add_pane(pane, vsplit=True)
        self._window_pane_shown()
        return pane

    def adopt_window_pane(self, old: Pane, widget: WindowWidget) -> Pane | None:
        """
        A pane for a Wayland window, in the place of `old`, whose program
        is sent SIGHUP: `pymux wl` turns the pane it ran in into the
        window. None when `old` is gone. Lillecarl/pymux#630.

        SIGHUP and not SIGKILL, so the shell writes the `pymux wl` line
        to its history on the way out.
        """
        session = self.session_holding(old)
        if session is None:
            return None
        pane = self._window_pane_of(widget)
        pane.tags.update(old.tags)
        # The swap first: an ended program takes its pane out of the
        # session, and a session left with none stops the server.
        session.arrangement.replace_pane(old, pane)
        self.hang_up(old)
        self._window_pane_shown()
        return pane

    def hang_up(self, pane: Pane) -> None:
        """
        End the program of a pane a person closed: SIGHUP, and SIGKILL
        after a grace. `hanging_up` keeps it, because a pane closed
        within the grace is in no snapshot, and the server that resumes
        one never sends that SIGKILL.
        """
        if isinstance(backend := pane.process.backend, PosixBackend):
            self.hanging_up.add(backend)
        pane.process.hang_up()

    def _window_pane_of(self, widget: WindowWidget) -> Pane:
        pane = Pane(widget)

        def focus_pane_on_click() -> None:
            "Select this pane: the mouse went to it, as `_build_pane` does for a terminal."
            found = self.window_of_pane(pane.pane_id)
            if found is not None:
                found[1].active_pane = pane
                with started_by(Origin.MOUSE):
                    note(self, Kind.CLICK, "select-pane")

        widget.on_mouse_focus = focus_pane_on_click
        self._register_pane(pane)
        return pane

    def _window_pane_shown(self) -> None:
        for client_state in self._client_states.values():
            client_state.sync_focus()
        self.invalidate(Woke.WINDOW_PANE_OPENED)

    def window_pane_drew(self, pane: Pane) -> None:
        "A window pane's window drew a frame: send it to every client that shows it."
        for client_state in self._client_states.values():
            client_state.refresh_window_pane(pane)

    def window_pane_ended(self, pane: Pane) -> None:
        "The window of a window pane closed. A window leaves nothing to remain on exit."
        self._take_ended_pane(pane)
        self.invalidate(Woke.PANE_ENDED)

    def close_window_panes(self) -> None:
        "Take every window pane away, as an upgrade does: no window survives its compositor."
        for pane in list(self.panes_by_id.values()):
            if is_window_pane(pane):
                pane.process.kill()
                self._take_ended_pane(pane)

    def _register_pane(self, pane: Pane) -> None:
        "Record a pane, and tell it what the clients' terminals are."
        # Keep track of panes. This is a WeakKeyDictionary, we only add, but
        # don't remove.
        self.panes_by_id[pane.pane_id] = pane

        # A pane that starts now missed the last walk of the panes.
        self.terminals.tell_pane_about_keyboard(pane)
        self.terminals.tell_pane_about_colours(pane)
        self.terminals.tell_pane_about_cell_size(pane)

    async def display_overlay(
        self,
        command: str | None = None,
        width: str | None = None,
        height: str | None = None,
        title: str | None = None,
        session: Session | None = None,
    ):
        """
        Open an overlay pane in the middle of the screen.

        It runs `command`, or the default shell, and closes itself when
        that finishes. It takes the keyboard while it is open.

        The overlay belongs to the session, like a window does, so
        every client of that session sees the same one and a second
        call replaces the first. That also means a command from the
        command line reaches it: the temporary client that runs such a
        command is gone before the next render, and it is on the
        session of the pane the command was typed in.
        Lillecarl/pymux#324.
        """
        if session is None:
            session = self.current_session
        self.close_overlay(session)

        pane: Pane | None = None

        def done() -> None:
            self.overlay_ended(session, pane)

        try:
            window = session.arrangement.get_active_window()
        except Exception:
            window = None

        pane = await self._create_pane(window=window, command=command, on_done=done)

        session.overlay_pane = pane
        session.overlay_title = title or command or "overlay"
        session.overlay_width = width
        session.overlay_height = height
        self._sync_focus_everywhere()
        self.invalidate(Woke.OVERLAY_OPENED)

        return pane

    def overlay_ended(self, session: Session, pane: Pane | None) -> None:
        "The program of an overlay finished, so the overlay goes."
        if session.overlay_pane is pane:
            session.overlay_pane = None
            self._sync_focus_everywhere()

    def close_overlay(self, session: Session | None = None) -> None:
        """
        Close the overlay pane of one session, and kill what runs in
        it. Without a session, the one this client is on.
        """
        if session is None:
            session = self.current_session

        pane = session.overlay_pane
        if pane is None:
            return

        session.overlay_pane = None

        if not pane.process.is_terminated:
            self.hang_up(pane)

        self._sync_focus_everywhere()
        self.invalidate(Woke.OVERLAY_CLOSED)

    async def display_job_overlay(
        self,
        job: Job,
        session: Session | None = None,
        width: str | None = None,
        height: str | None = None,
    ) -> Pane:
        """
        Show a job in the overlay of a session.

        The tail replays and the rest follows, read-only, until
        Escape closes it: a viewer never ends on its own, so unlike a
        program's overlay this one stays past the end of the job,
        saying how it ended. Closing stops the follow and never
        touches the job, which runs on until it ends on its own. A
        second call replaces the first, the way overlays do.
        Lillecarl/pymux#528.
        """
        if session is None:
            session = self.current_session
        self.close_overlay(session)

        pane = await self._create_pane(session=session, job=job)

        session.overlay_pane = pane
        session.overlay_title = describe(job)
        session.overlay_width = width
        session.overlay_height = height
        self._sync_focus_everywhere()
        self.invalidate(Woke.OVERLAY_OPENED)

        return pane

    async def open_job_in_pane(self, job: Job, session: Session | None = None) -> Window:
        """
        Open a job as a pane in a new window of a session.

        A viewer, read-only, that replays the tail and follows the
        rest; nothing forks and no program runs. Closing the window
        never touches the job. Without a session it is the session of
        the client that asks. Lillecarl/pymux#528.
        """
        if session is None:
            session = self.current_session

        pane = await self._create_pane(session=session, job=job)
        _remember_the_size_made_at(session.arrangement.create_window(pane, name="job %d" % job.job_id), pane)
        if session is self.current_session:
            pane.focus()
        self.invalidate(Woke.WINDOW_OPENED)

        window = self._window_holding(pane)
        assert window is not None  # The arrangement just took the pane.
        return window

    async def take_over_job(self, job: Job, session: Session | None = None) -> None:
        """
        Run a job's command again, interactively, in a new window.

        A terminal the person can type in, from the job's directory:
        that is what taking over means for a job that holds no stdin
        (`run` starts one on `/dev/null`). The recorded job is
        untouched -- it runs on, and detaching never kills it -- and
        the new program answers to the session's environment, not the
        job's. Lillecarl/pymux#528.
        """
        if session is None:
            session = self.current_session
        await self.create_window(command=job.command, start_directory=job.directory, session=session)

    def _sync_focus_everywhere(self) -> None:
        "Give every client the focus that its state asks for."
        for client_state in list(self._client_states.values()):
            try:
                with set_app(client_state.app):
                    client_state.sync_focus()
            except Exception:
                # An application that never ran has no layout to focus.
                logger.exception("Could not sync the focus of a client.")

    def apply_render_mode(self) -> None:
        """
        Draw every client and every pane the way `render-mode` says, from
        the next frame on, and draw that frame whole.
        """
        reference = self.options.render_mode is RenderMode.REFERENCE
        for pane in self.panes_by_id.values():
            pane.terminal.terminal_control.keep_rows = not reference
        for client_state in self._client_states.values():
            client_state.app.renderer.reference = reference
            self.redraw(client_state)

    def redraw(self, client_state: ClientState) -> None:
        """
        Draw this client's terminal again from nothing.

        A frame normally writes what changed since the one before, so a
        cell the terminal lost -- or one the diff got wrong -- stays lost
        until something writes over it. This forgets the frame before:
        the next frame homes the cursor, erases the screen and writes
        every cell. Not `Renderer.clear`, which leaves the alternate
        screen and asks the terminal where its cursor is.

        It enters the alternate screen again, which a terminal can have
        left behind pymux's back: iTerm2 leaves it on a prompt start it
        reads, and the frames after went to its main screen. On a
        terminal that never left, it clears a screen this redraws
        anyway. Lillecarl/pymux#791.

        The erase takes kitty's placements with it, so the images go too,
        and each is sent and placed again. A window pane left as it was
        gets only its damage, on an image nothing shows, and its pane
        shows the background.
        """
        if client_state.connection is not None:
            client_state.connection.graphics.reset()
        renderer = client_state.app.renderer
        renderer.reset(leave_alternate_screen=False)
        if renderer.full_screen:
            # The switch clears in the colours set last; a frame's are not.
            renderer.output.reset_attributes()
            renderer.output.enter_alternate_screen()
            renderer.output.flush()
        client_state.app.invalidate()

    def invalidate(self, reason: str = Woke.APPLICATION):
        """
        Ask every client for a frame. `Woke` says why the reason is
        here, and holds every reason but the one that carries a name.
        """
        self.counters.invalidated(reason)
        self.hooks.fire(reason, self.handle_command)
        # DEBUG: a server draws eleven frames a second when a pane
        # animates, and a line each is what made one log 86 MB in four
        # days. `pymux counters` holds the same reasons with nothing
        # written down. Lillecarl/pymux#248.
        logger.debug("Drawing %s of the clients: %s", len(self.apps), reason)

        # **Before the invalidates, and here rather than on the path a
        # pane's write takes.** A window switch and a `set-option`
        # both come through here, and they are the only two things
        # that change the answer. A pane that writes does not.
        self.sync_frame_rate()

        for app in self.apps:
            app.invalidate()

        # The focused pane can have changed, or the process inside it
        # can have pushed/popped kitty keyboard protocol flags or asked
        # for a different pointer.
        self.terminals.sync_kitty_flags()
        self.terminals.sync_pointer_shape()
        self.terminals.sync_progress()
        # "set-option synthesize-key-events" takes effect here.
        self.terminals.sync_keyboard_source_flags()

    def sync_frame_rate(self) -> None:
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

            rate = window.options.frame_rate if window is not None else 0
            client_state.frame_interval = (1.0 / rate) if rate else None
            client_state.pace()

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

    def resize_pane_for_program(self, pane: Pane, lines: int | None, columns: int | None) -> None:
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
        if not self.options_of_pane(pane).allow_program_resize:
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

    def _place_of(self, pane: Pane) -> tuple[Session, Window] | None:
        "The session and window that hold this pane, or None when it is gone."
        for session in self.sessions:
            for window in session.arrangement.windows:
                if pane in window.panes:
                    return session, window
        return None

    def options_of_pane(self, pane: Pane) -> OwnOptions | SessionOptions:
        """
        The options of the session holding this pane, read when asked:
        `move-window` takes a pane to another session. A pane that is
        gone reads the defaults.
        """
        place = self._place_of(pane)
        return self.session_defaults if place is None else place[0].options

    def _window_holding(self, pane: Pane) -> Window | None:
        "The window that holds this pane, or None when it is gone."
        place = self._place_of(pane)
        return None if place is None else place[1]

    def session_of_window(self, window: Window) -> Session | None:
        "The session whose arrangement holds this window."
        for session in self.sessions:
            if window in session.arrangement.windows:
                return session
        return None

    def session_holding(self, pane: Pane) -> Session | None:
        """
        The session whose windows hold this pane, or None when it is
        gone.

        A pane belongs to one session, and the client whose turn it is
        when the program in it ends says nothing about which.
        """
        place = self._place_of(pane)
        return None if place is None else place[0]

    def window_of_pane(self, pane_id: PaneId | None):
        """
        The session, window and pane for a pane id, or None when the
        pane is gone. The hub jumps to a notification with this.
        """
        if pane_id is None:
            return None
        pane = self.panes_by_id.get(pane_id)
        if pane is None:
            return None
        place = self._place_of(pane)
        return None if place is None else (*place, pane)

    def displayed_now(self) -> datetime.datetime:
        """
        The time that a screen shows, as of now.

        In test-mode it is always 13:37 on the 14th of March, of the
        year it really is. Every clock a person reads goes through
        here, so a picture of a pane holds still under the reader.
        """
        now = datetime.datetime.now()
        if self.options.test_mode:
            now = now.replace(month=3, day=14, hour=13, minute=37, second=0, microsecond=0)
        return now

    def clients_to_open_on(self) -> list[ClientState]:
        """
        The clients that receive what "open-url" opens.

        `open-url-target last` is the client a person used last, by the
        same stamp that `window-size latest` reads. Nothing attached
        means nobody, and the person who asked hears so. The fake CLI
        of a command that arrived over a socket is never one of them:
        it is the connection that ran the command, and it closes while
        the answer is still being sent.
        """
        latest = self.latest_client()
        if self.current_session.options.open_url_target != OpenUrlTarget.BROADCAST and latest is not None:
            return [latest]
        return self.clients

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

        **A loopback URL travels with its port.** The packet then
        carries what to forward, and the client binds it before it
        opens anything. `url_forward` decides, and the question below
        says so, because binding a port here is part of what a yes
        allows. Lillecarl/pymux#437.
        """
        options = self.current_session.options
        if options.open_url_mode == OpenUrlMode.OFF:
            logger.info("Not opening %s: open-url-mode is off.", url)
            return

        clients = self.clients_to_open_on()
        if not clients:
            self.add_command_error("Nobody is attached to open %s." % (url,))
            return

        forwards = [self.url_forward(url, client_state) for client_state in clients]

        # **A `-c` counts only from a person.** It is the answer to a
        # question, and a pane that sends the flag itself answered
        # nothing -- without this, `open-url-mode ask` reads as a gate
        # and is not one. Lillecarl/pymux#443.
        confirmed = confirmed and self.a_person_asked()

        asking = options.open_url_mode == OpenUrlMode.ASK or (
            self.options.forward_mode == ForwardMode.ASK and any(forwards)
        )

        if asking and not confirmed:
            command = "open-url -c %s" % (shlex.quote(url),)
            for client_state, forward in zip(clients, forwards):
                client_state.ask(_open_question(url, forward), command)
            return

        for client_state, forward in zip(clients, forwards):
            packet: Message = {Field.CMD: Packet.OPEN, Field.DATA: url}
            if forward is None:
                client_state.message = "Opened %s in the browser of this machine." % (url,)
            else:
                # The client says what it opened, once it knows: a port
                # that was taken here moves the URL, and a message from
                # this side would name the address that did not work.
                packet[Field.FORWARD] = forward
            client_state.connection._send_packet(packet)

    def url_forward(self, url: str, client_state: ClientState) -> dict[str, Any] | None:
        """
        What to forward so that this URL works on that client, or
        `None`.

        **`None` for nearly every URL**, and for every client that is
        not on another machine. A forward is only the answer when the
        address means "the machine the server runs on" and the browser
        is somewhere else. Lillecarl/pymux#437.
        """
        if not client_state.connection.can_forward:
            return None
        options = self.current_session.options
        if not options.open_url_forward:
            return None
        if self.options.forward_mode == ForwardMode.OFF:
            # Somebody turned port forwarding off. A URL is not a way
            # around that.
            return None

        found = loopback_port(url)
        if found is None:
            return None

        host, port = found
        return {"host": host, "port": port, "idle": options.open_url_forward_idle}

    def a_person_asked(self) -> bool:
        """
        Whether the command running now was typed at a client.

        **This is what makes a confirmation mean anything.** A yes runs
        the same command again with a flag that says "do not ask" --
        `open-url -c`, `forward-port -c`. A program in a pane runs
        commands too, over the socket and under a temporary client, so
        it can send that flag itself and answer a question nobody
        asked. The flag counts only when this says so.
        Lillecarl/pymux#443, Lillecarl/pymux#440.
        """
        try:
            asking = self.get_client_state()
        except ValueError:
            return False

        return not asking.temporary

    def forwarding_client(self) -> Asker:
        """
        The client that a forward is asked of, and who asked.

        **The one that asked, when a client asked.** A forward binds a
        port on one machine, and the machine a person means is the one
        they typed the command on. That is different from `open-url`,
        which a program in a pane triggers and which therefore has to
        choose a client; here there is nothing to choose while the
        person is right there.

        A command that arrived over the socket has a temporary client
        of its own, which is no terminal and holds no SSH connection.
        Then the one attached client is the answer, and two of them is
        a question this cannot answer for the person.
        Lillecarl/pymux#436.
        """
        if self.a_person_asked():
            return self._can_it_forward(Asker(self.get_client_state(), in_person=True))

        attached = self.clients
        if not attached:
            raise CommandException("Nobody is attached, so there is nothing to forward through.")
        if len(attached) > 1:
            raise CommandException("Several clients are attached. Run this from the one that should forward.")
        # A temporary client is the fake CLI of a command that arrived
        # over the socket, which is how a program in a pane runs one.
        # Nobody typed this.
        return self._can_it_forward(Asker(attached[0], in_person=False))

    def _can_it_forward(self, asker: Asker) -> Asker:
        """
        Refuse a client with no SSH connection, before anything else.

        **Before the question**, because asking somebody whether to
        make a forward that cannot be made wastes the only answer they
        can give.
        """
        if not asker.client_state.connection.can_forward:
            raise CommandException(
                "%s did not reach this server over SSH, so it cannot forward a port."
                % (asker.client_state.connection.name,)
            )
        return asker

    def forward_needs_asking(self, forward: Forward, in_person: bool) -> bool:
        """
        Whether this forward has to be confirmed before it is made.

        **"on" is not "never ask".** Two cases ask whatever the option
        says, because in neither of them did the person at the keyboard
        choose this forward:

        - A program in a pane asked. Any command a pane runs reaches
          the server, so without this any program on the far machine
          could bind a port here.
        - It listens somewhere other than loopback, which publishes the
          far machine's service to the network this one is on. openssh
          makes the same split, through `GatewayPorts`.

        Lillecarl/pymux#440.
        """
        if self.options.forward_mode == ForwardMode.ASK:
            return True

        return not in_person or forward.listen_host not in LOOPBACK_NAMES

    def forward_through(self, client_state: ClientState, packet: Message) -> None:
        """
        Ask a client to open or close a forward.

        **The server never forwards anything itself.** It holds no SSH
        connection: the client does, and it is the machine the person
        sits at. So this is a request, the way `open-url` is, and the
        answer comes back as the client's whole table.

        `forwarding_client` has already refused a client that cannot
        forward at all. Lillecarl/pymux#436.
        """
        client_state.connection._send_packet(packet)

    def pane_environment(self, base: dict[str, str] | None = None) -> dict[str, str]:
        """
        The environment a new pane runs under, merged: `base`, or the
        server's own, with what `set-environment -g` set and then what
        a plain `set-environment` set over it. An unset takes the name out of
        its scope only: a session unset of a name the global set
        falls through to the global value, and a name no scope says
        anything about leaves entirely. Lillecarl/pymux#270.
        """
        merged = dict(os.environ if base is None else base)
        for name, value in self.global_environment.items():
            if value is None:
                merged.pop(name, None)
            else:
                merged[name] = value
        for name, value in self.session_environment.items():
            if value is None:
                # A session unset does not reach past the global
                # scope: what the global set stays, and a name the
                # global says nothing about leaves.
                if name not in self.global_environment:
                    merged.pop(name, None)
            else:
                merged[name] = value
        return merged

    def forward_osc(self, pane: Pane, code: str, param: str) -> None:
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
            # A notification the hub recorded goes to a terminal that
            # reports clicks as OSC 99 instead (`_notified`).
            recorded = False

            if code == Osc.POINTER_SHAPE:
                # The shape of the pointer is not a sequence to pass on
                # as it arrives: it belongs to the pane, and the client
                # has to see the shape of the pane it looks at. The
                # screen of the pane holds it; this only asks every
                # client to look again.
                self.terminals.sync_pointer_shape()
                return

            if code == Osc.TERMINAL_EXTENSION:
                # iTerm2's namespace, one subcommand at a time. A
                # subcommand this session parsed may reach the terminal
                # of the user; one it did not parse must not, because
                # an unchecked payload drives that terminal instead of
                # only naming a service.
                if not self._read_terminal_extension(pane, param):
                    return
                recorded = request_attention_of(param) not in (None, "no")

            if code == Osc.CURRENT_DIRECTORY:
                # Where the program is, as a file URL. The outer
                # terminal keeps its own copy, so this travels on; the
                # pane keeps one too.
                self._read_current_directory(pane, param)

            if code == Osc.SHELL_INTEGRATION:
                # The prompt, the command and the exit status it
                # finished with, which the pane keeps. **They stay
                # inside**, as tmux keeps them: they name rows of one
                # pane, not of the client's screen, and iTerm2 reads a
                # prompt start as the end of the alternate screen
                # (`softAlternateScreenMode = NO`), so every frame after
                # one drew on its main screen, a row off.
                self._read_shell_integration(pane, param)
                return

            if code == Osc.CLIPBOARD:
                # **Only "on" lets a program in a pane near the
                # clipboard.** tmux asks the same question in
                # `input_osc_52`, and its default answer is no. A copy
                # that the person makes asks `write_user_clipboard`
                # instead, which is the other half of the option.
                if self.options.set_clipboard is not Clipboard.ON:
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
                # And keep it for the hub, which lists what the
                # terminal was asked, not only what it showed.
                self.notification_center.add_osc99(pane.pane_id, param)

            if code == Osc.SIMPLE_NOTIFICATION:
                # No identifier and no answer, so nothing to route.
                notice = simple_notification_of(param)
                if notice is None:
                    return
                title, body = notice
                self.notification_center.add(title, body, pane_id=pane.pane_id, source=NotificationSource.OSC_777)
                recorded = True

            if code == Osc.MESSAGE:
                text = message_of(param)
                # ConEmu's numbered commands drive ConEmu itself, but for
                # the progress, which each client's terminal is given
                # for the pane it looks at.
                if text is None:
                    progress = progress_of(param)
                    if progress is not None and progress != pane.progress:
                        pane.progress = progress
                        if progress.state:
                            self.progressing.add(pane.pane_id)
                        else:
                            self.progressing.discard(pane.pane_id)
                        self.terminals.sync_progress()
                        self.invalidate(Woke.PANE_PROGRESSED)
                    return
                self.notification_center.add(text, pane_id=pane.pane_id, source=NotificationSource.OSC_9)
                recorded = True

            # A person looking at the window already sees what the
            # notification says: it stays in the hub, seen. A terminal
            # without the focus is no look. Lillecarl/pymux#799.
            notice = recorded or (code == Osc.NOTIFICATION and not is_question(param))
            if notice and pane.pane_id in self._panes_in_sight():
                logger.debug("Kept a notification of pane %%%d: a terminal with the focus shows it.", pane.pane_id)
                return

            sequence = build_osc(code, param)
            if sequence is None:
                logger.warning("Dropped an unsafe OSC %s of a pane.", code)
                return

            # The clipboard and a notification come from any pane: a
            # build that ends in a pane out of sight is exactly what a
            # notification is for.
            for connection in self._client_states:
                # No connection is a socket command's temporary
                # client: nothing to send to. Lillecarl/pymux#493.
                if connection is None or (recorded and "report" in connection.notification_actions):
                    continue
                connection.forward_osc(sequence)

            # **The escape travels with the next frame, so there has to
            # be one.** A pane that writes wakes the clients watching
            # it, and those would draw anyway; a pane in a window
            # nobody looks at draws no frame at all, and a notification
            # from one is what the line above is for. Without this, its
            # escape would sit in a client's buffer until something
            # else happened to draw. Lillecarl/pymux#478.
            self.invalidate(Woke.PANE_WROTE_AN_OSC)
        except Exception:
            logger.exception("Forwarding an OSC sequence failed.")

    def _directory_to_start_in(self, window: Window | None, session: Session | None) -> str | None:
        """
        Where a new pane starts when `-c` named nothing.

        What a pane reported about itself wins over what the operating
        system says about it: the shell knows its logical directory on
        every system, and the process table only on some. What the OS
        says covers a pane that never reported, and nothing covers the
        first pane of a session. An explicit `-c` already won before
        this was asked.
        """
        pane = None
        if window is not None:
            pane = window.active_pane
        if pane is None and session is not None:
            active = session.arrangement.get_active_window()
            if active is not None:
                pane = active.active_pane
        if pane is not None and pane.current_directory:
            return pane.current_directory
        if window is not None and window.active_process:
            # When the path of the active process is known, start the
            # new process at the same location.
            return window.active_process.get_cwd()
        return None

    def _read_current_directory(self, pane: Pane, param: str) -> None:
        """
        Keep where the program says it is.

        "OSC 7" carries a file URL with a host and a path. A URL that
        does not parse stores nothing; the sequence still travels on,
        because the outer terminal reads it for itself.
        """
        parsed = file_url_dir_of(param)
        if parsed is None:
            return
        host, directory = parsed
        pane.current_host = host
        pane.current_directory = directory

    def _read_shell_integration(self, pane: Pane, param: str) -> None:
        """
        Keep the shell integration zone and the exit status.

        "OSC 133" names the prompt (A), the command (B), its output
        (C) and its end (D, with the status or bare for an abort). An
        abort clears the zone and the status; anything else that does
        not parse stores nothing. The sequence travels on either way,
        for the zones of the outer terminal.
        """
        parsed = ftcs_of(param)
        if parsed is None:
            return
        letter, status = parsed
        if letter == "D":
            pane.command_zone = None
            pane.last_exit_status = status
        else:
            pane.command_zone = letter
        # The rows copy mode jumps between, as tmux's `input_osc_133`
        # marks them. Lillecarl/pymux#500.
        mark = _FTCS_ROW_MARKS.get(letter)
        screen = pane.screen
        if mark is not None and not isinstance(screen, WindowScreen):
            screen.mark_row(mark)

    def _read_terminal_extension(self, pane: Pane, param: str) -> bool:
        """
        Read one subcommand of "OSC 1337". True when the sequence may
        travel on to the terminal of the user.

        The browser keeps its question (`open-url-mode` asks it). The
        working directory, a user variable, a mark, an attention ask
        and a clipboard write travel on once parsed, the way the
        sequences they synonymise do. A download goes to one client,
        through `send_download`. Anything else stays inside: images
        wait for their slice, and an unchecked payload never reaches
        the terminal of the user.
        """
        url = open_url_of(param)
        if url is not None:
            self.open_url(url)
            return False

        keyed = extension_key_of(param)
        if keyed is None:
            return False
        key, rest = keyed

        if key == "CurrentDir":
            directory = current_dir_of(rest)
            if directory is None:
                return False
            pane.current_directory = directory
            return True

        if key == "StealFocus":
            if rest or not self.options_of_pane(pane).steal_focus:
                return False
            session = self.session_holding(pane)
            for client_state in list(self._client_states.values()):
                if client_state.session is session and not client_state.temporary:
                    self.show_pane(client_state, pane)
            # And the terminal of the user raises its own window.
            return True

        if key == "RemoteHost":
            remote = remote_host_of(rest)
            if remote is None:
                return False
            pane.current_user, pane.current_host = remote
            return True

        if key == "ShellIntegrationVersion":
            integration = shell_integration_of(rest)
            if integration is None:
                return False
            pane.shell_integration_version, pane.shell = integration
            return True

        if key == "SetMark":
            if rest:
                return False
            # iTerm2's mark is where a person wants to come back to,
            # which is what a prompt mark is for.
            screen = pane.screen
            if not isinstance(screen, WindowScreen):
                screen.mark_row(RowMark.PROMPT)
            return True

        if key == "SetUserVar":
            parsed = set_user_var_of(rest)
            if parsed is None:
                return False
            name, value = parsed
            if name not in pane.user_vars and len(pane.user_vars) >= MAX_USER_VARS:
                del pane.user_vars[next(iter(pane.user_vars))]
            pane.user_vars[name] = value
            return True

        if key == "RequestAttention":
            value = request_attention_of(param)
            if value is None:
                return False
            # A withdrawal cancels a request the hub never tracked as
            # one: there is nothing to take back, so only the asks
            # record anything.
            if value != "no":
                self.notification_center.add(
                    "Attention requested",
                    "With fireworks." if value == "fireworks" else "",
                    Urgency.CRITICAL if value == "yes" else Urgency.NORMAL,
                    pane.pane_id,
                    NotificationSource.ATTENTION,
                )
            return True

        if key == "Copy":
            data = copy_data_of(param)
            if data is None:
                return False
            # The same gate as OSC 52: only "on" lets a program in a
            # pane near the clipboard.
            if self.options.set_clipboard is not Clipboard.ON:
                return False
            self._mirror_clipboard("c;" + data)
            return True

        if key == CLEAR_SCROLLBACK:
            # The pane's screen has cleared its own history already.
            return not rest

        if key in FILE_KEYS:
            # A download goes to one client, which `send_download`
            # chooses; it is never this broadcast.
            self._read_download(pane, key, rest)
            return False

        return False

    def _read_download(self, pane: Pane, key: str, rest: str) -> None:
        "Read one sequence of a pane's download, and send a whole file on."
        try:
            finished = pane.downloads.read(key, rest)
        except DownloadRefused as refused:
            client = self.download_client(pane)
            logger.warning("Refused a download of pane %s: %s", pane.pane_id, refused)
            if client is not None:
                client.message = str(refused)
            return
        if finished is not None and finished.inline:
            self._draw_inline_image(pane, finished)
        elif finished is not None:
            self.send_download(pane, finished)

    def _draw_inline_image(self, pane: Pane, finished: FinishedDownload) -> None:
        """
        Place an iTerm2 inline image at the pane's cursor, as pyte places
        a kitty image. Something that is no image draws nothing. Lillecarl/pymux#789.
        """
        screen = pane.screen
        if isinstance(screen, WindowScreen):
            return
        found = as_png(finished.data)
        if found is None:
            logger.warning("Dropped an inline image of pane %s that does not decode.", pane.pane_id)
            return
        png, pixels = found
        columns, rows = cells_of(
            finished.arguments, pixels, (screen.cell_width, screen.cell_height), (screen.columns, screen.lines)
        )
        screen.graphics.add_png(
            png, columns, rows, screen, move_cursor=finished.arguments.get("doNotMoveCursor") != "1"
        )

    def download_client(self, pane: Pane) -> ClientState | None:
        """
        The client a download from this pane goes to: of the clients on
        the pane's session, the one a person used last. `None` when
        nobody is attached to that session.

        **One client and not every one.** A file written twice is not
        what anybody asked for, and the clipboard's broadcast is not a
        rule for it. A person in another session did not ask for it
        either.
        """
        session = self.session_holding(pane)
        if session is None:
            return None
        clients = self.clients_on(session)
        if not clients:
            return None
        return max(clients, key=lambda client: client.last_used)

    def send_download(self, pane: Pane, download: FinishedDownload) -> None:
        """
        Give a whole file from a pane to the person, the way the
        client's `file-transfer` says. Lillecarl/pymux#743.
        """
        client = self.download_client(pane)
        if client is None:
            logger.warning("Dropped the download %s of pane %s: nobody is attached.", download.name, pane.pane_id)
            return
        # Only `SshClient` says it can forward, so this is the fact that
        # the client came over SSH.
        terminal_saves = terminal_saves_downloads(client.connection.graphics.terminal_version)
        route = route_of(client.options.file_transfer, client.connection.can_forward, terminal_saves)
        refused = None
        if route is FileTransfer.PASSTHROUGH and not terminal_saves:
            refused = "file-transfer is passthrough, and this terminal saves no iTerm2 download"
        elif route is FileTransfer.PYMUX and not client.connection.saves_files:
            refused = "this pymux client is too old to save one; start a new one"
        if refused is not None:
            message = "Did not send %s: %s." % (download.name, refused)
            logger.warning(message)
            client.message = message
            self.invalidate(Woke.PANE_WROTE_AN_OSC)
            return
        if route is FileTransfer.PASSTHROUGH:
            for sequence in sequences_of(download):
                client.connection.forward_osc(sequence)
            # The sequences go behind a frame, so there has to be one.
            self.invalidate(Woke.PANE_WROTE_AN_OSC)
            return
        for packet in file_packets(download):
            client.connection._send_packet(packet)

    def _copied_in_copy_mode(self, text: str) -> None:
        """
        Copy mode is a client's, so what it copied goes to that client's
        terminal and to nobody else's. Another person on the session
        keeps their own clipboard. Lillecarl/pymux#470.
        """
        try:
            client = self.get_client_state()
        except ValueError:
            client = None
        self.write_user_clipboard(text, client=client)

    def write_user_clipboard(self, text: str, selection: str = "c", client: ClientState | None = None) -> None:
        """
        Put what a person copied on the clipboard of their terminal, or
        on the selection OSC 52's letter names: "p" is the primary. With
        `client`, on that client's terminal alone.

        Copy mode calls this, and so does a pane's wl-copy, which
        `wayland.py` lets through only under "on". **It is not `forward_osc`**, although the
        sequence is the same one: the two have different answers under
        `set-clipboard`, because a person asking for their own
        clipboard is not a program in a pane asking for it. tmux keeps
        them apart the same way -- a copy asks only that the option is
        not "off". Lillecarl/pymux#378.

        The paste buffer of the session is not written here. Copy mode
        writes it through the clipboard of the application, which is
        that buffer, and the selection type goes with it.

        Never raises: this runs under a key press.
        """
        if self.options.set_clipboard is Clipboard.OFF or not text:
            return
        try:
            payload = base64.b64encode(text.encode("utf-8")).decode("ascii")
            sequence = build_osc(Osc.CLIPBOARD, selection + ";" + payload)
            if sequence is None:
                # The payload is base64 and the length is the only
                # thing that can fail it. A person who copies half a
                # megabyte gets nothing, and has to know why.
                logger.warning("The copy is too long for the clipboard.")
                return
            connections = list(self._client_states) if client is None else [client.connection]
            for connection in connections:
                # No connection is a socket command's temporary
                # client: nothing to send to. Lillecarl/pymux#493.
                if connection is None:
                    continue
                connection.forward_osc(sequence)
            # The copy travels with the next frame, so there has to be
            # one. Copy mode's key press draws anyway; a pane's wl-copy
            # draws nothing, and its copy would wait for some other
            # frame.
            self.invalidate(Woke.CLIPBOARD_WRITTEN)
        except Exception:
            logger.exception("Writing the clipboard of the user failed.")

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

    def _has_focus(self, client_state: ClientState, pane: Pane) -> bool:
        "True when this client looks at this pane."
        return self.focused_pane_of(client_state) is pane

    def focused_pane_of(self, client_state: ClientState) -> Pane | None:
        """
        The pane that this client looks at, or `None`.

        Never raises: this runs on the invalidate path, also for
        headless servers without a running prompt_toolkit application.
        """
        if client_state.session.overlay_pane is not None:
            return client_state.session.overlay_pane

        try:
            with set_app(client_state.app):
                window = client_state.session.arrangement.get_active_window()
        except Exception:
            # `get_active_window` needs a running application.
            return None
        return window.active_pane if window is not None else None

    def latest_client(self):
        """
        The client a person used last, or `None` when nobody is
        attached. The fake CLI of a socket command is never it, for the
        reason `clients_to_open_on` names.
        """
        clients = self.clients
        if not clients:
            return None
        return max(clients, key=lambda client: client.last_used)

    def key_profile_of(self, client: ClientState | None) -> KeyProfile:
        """
        Whose answer a legacy pane gets for a key from `client`: the
        terminal it sits at, by its XTVERSION answer or its TERM, or
        else "key-profile". Lillecarl/pymux#706.
        """
        version = client.connection.graphics.terminal_version if client is not None else ""
        term = hacks_and_lies.outer_term(client) if client is not None else ""
        options = client.session.options if client is not None else self.session_defaults
        return KEY_PROFILES[key_profile_of(version, term) or options.key_profile]

    def finish_recordings(self, holders: Iterable[Pane | ServerConnection] | None = None) -> None:
        """
        Write out what `record-pane` and `record-client` were recording
        of `holders`: panes and client connections, every one by default.
        """
        if holders is None:
            holders = [*self.panes_by_id.values(), *self.connections]
        for holder in holders:
            recording, holder.recording = holder.recording, None
            if recording is None:
                continue
            try:
                logger.info("Recorded %s.", recording.finish())
            except OSError:
                logger.exception("Writing the recording %s failed.", recording.name)

    async def hang_up_and_stop(self) -> None:
        """
        End every pane's program the way closing a terminal does, then
        `stop`.

        SIGHUP first, and a shell writes its history on the way out.
        Whatever has not ended `GRACE` seconds later gets `stop`'s
        SIGKILL. `stop` alone SIGKILLs at once, and every shell lost the
        lines typed before `kill-server`. tmux hangs its panes up too.
        Lillecarl/pymux#679.
        """
        running = [pane.process for pane in self.panes_by_id.values() if not pane.process.is_terminated]
        for process in running:
            process.hang_up()
        # A window pane's program has no reap to wait for: closing it
        # asks the window to go.
        reaps = [getattr(process.backend, "ready_f", None) for process in running]
        with anyio.move_on_after(GRACE):
            for reaped in reaps:
                if isinstance(reaped, anyio.Event):
                    await reaped.wait()
        self.stop()

    def stop(self):
        self.finish_recordings()
        # Kill all pane processes first. (The pending `waitpid` calls in
        # the event loop executor keep the server process alive as long
        # as the children run. Also, `kill-server` should not leave the
        # programs inside the panes running.)
        for pane in list(self.panes_by_id.values()):
            process = pane.process
            if not process.is_terminated:
                process.kill()

        for app in self.apps:
            # `Application.exit()` raises for applications that never
            # started running. (E.g. temporary CLIs that only handled a
            # command.)
            with suppress(Exception):
                app.exit()
        self.done.set()

    async def create_window(
        self,
        command: str | None = None,
        start_directory: str | None = None,
        name: str | None = None,
        index: WindowIndex | None = None,
        session: Session | None = None,
        task_group: anyio.abc.TaskGroup | None = None,
    ) -> Window:
        """
        Create a new :class:`pymux.arrangement.Window` in the arrangement,
        and answer it.

        `index` says where it goes. Without one it takes the lowest
        free index. Lillecarl/pymux#191.

        `session` says where the window goes. Without one it is the
        session of the client that asks. A window made for another
        session is not focused: its pane is in no layout the asking
        client draws.

        `task_group` is whose scope the pane's program runs in, and
        without one it is the server's. A window made outside
        `Pymux.running` names its own.
        """
        asked_for = self.current_session
        if session is None:
            session = asked_for

        pane = await self._create_pane(
            None, command, start_directory=start_directory, session=session, task_group=task_group
        )

        window = session.arrangement.create_window(pane, name=name, index=index)
        _remember_the_size_made_at(window, pane)
        if session is asked_for:
            pane.focus()
        self.invalidate(Woke.WINDOW_OPENED)
        return window

    async def add_process(
        self,
        command: str | None = None,
        vsplit: bool = False,
        start_directory: str | None = None,
        window: Window | None = None,
        task_group: anyio.abc.TaskGroup | None = None,
        select: bool = True,
    ) -> Pane:
        """
        Add a new process to the given window (or the active window).
        (vsplit/hsplit). Answers the new pane. Without `select` the
        focus stays where it was: `split-window -d`.
        """
        if window is None:
            window = self.arrangement.get_active_window()
        if window is None:
            raise CommandException("no current window")

        pane = await self._create_pane(
            window,
            command,
            start_directory=start_directory,
            session=self.session_showing(window),
            task_group=task_group,
        )
        window.add_pane(pane, vsplit=vsplit, select=select)
        if select:
            pane.focus()
        self.invalidate(Woke.PANE_WAS_SPLIT_OFF)
        return pane

    def kill_pane(self, pane: Pane) -> None:
        """
        Kill the given pane, and remove it from the arrangement.

        **The session it empties is not taken here.** The callback that
        runs when the process ends does that, and it has to: a pane
        ends on its own as often as a person kills one. Doing it in
        both places would end a session while the program in its last
        pane is still answering the signal.

        **SIGHUP, not SIGKILL**, the way closing a terminal ends what
        runs in it: a shell writes its history on the way out, and a
        program that ignores it gets SIGKILL after a grace.
        """
        # Remove from layout. The session the pane is in, and not the
        # session of whoever asked: `kill-pane -t` reaches across.
        session = self.session_holding(pane)
        if session is None:
            if not pane.process.is_terminated:
                self.hang_up(pane)
            return
        session.arrangement.remove_pane(pane)

        if pane.process.is_terminated:
            # Its end was heard already, under `remain-on-exit`, and
            # nothing calls back again.
            self._end_if_empty(session)
        else:
            self._killed_in[pane.pane_id] = session
            self.hang_up(pane)

    def leave_command_mode(self, append_to_history: bool = False) -> None:
        """
        Leave the command/prompt mode.
        """
        client_state = self.get_client_state()

        client_state.command_buffer.reset(append_to_history=append_to_history)
        client_state.prompt_buffer.reset(append_to_history=True)
        # The next line starts in insert mode, with no undo of this one.
        helix = self.key_bindings_manager.helix
        helix.reset(client_state.command_buffer)
        helix.reset(client_state.prompt_buffer)

        client_state.prompt_command = ""
        client_state.forget_questions()
        # The completer belongs to the question, and the question is
        # over. It also says whether the prompt draws in a box, so a
        # completer left behind would put the next one in one.
        client_state.prompt_completer = None

        client_state.app.layout.focus_previous()

    def handle_command(self, command: str) -> None:
        """
        Handle a command from a key binding, a hook or a config file.

        **The caller is not waiting.** A key press has to return at
        once, so a command that answers with work to finish is put in
        the server's task group and the key is done. The socket route
        does the other thing: `ServerConnection._run_command` awaits,
        so `pymux wait-for done` holds the client that typed it.
        Lillecarl/pymux#87.
        """
        self.spawn_command(handle_command(self, command))

    def spawn_command(self, answer: Coroutine[Any, Any, object] | None) -> None:
        """
        Finish a command that answered with work, in the background.

        Outside `running()` there is nothing to finish it in, and
        dropping the work would be a command that quietly did half of
        what it says. That is a mistake in the caller, so it says so.
        """
        if answer is None:
            return

        if self.tasks is None:
            answer.close()
            raise RuntimeError("A command that waits was run outside `Pymux.running`, so nothing would finish it.")

        self.tasks.start_soon(self._finish_command, answer)

    async def _finish_command(self, answer: Coroutine[Any, Any, object]) -> None:
        """
        A command nobody waits for still says what went wrong.

        In a task group an exception cancels the siblings, and the
        siblings of a command are every client of this server.
        """
        try:
            await answer
        except anyio.get_cancelled_exc_class():
            raise
        except Exception:
            logger.exception("A command that ran in the background failed.")

    def show_message(self, message: str) -> None:
        """
        Set a warning message. This will be shown at the bottom until a key has
        been pressed.

        :param message: String.
        """
        self.message_log.append(message)

        # Not `get_client_state`. A command from a pane runs under a fake
        # CLI that draws nothing, and a message left there is a message
        # nobody sees. Lillecarl/pymux#272.
        client_state = self.the_client_to_tell()
        if client_state is not None:
            client_state.message = message

    @property
    def answer(self) -> Answer | None:
        "The answer of the command this task runs; `pymux.answer` says."
        return current_answer.get()

    @answer.setter
    def answer(self, answer: Answer | None) -> None:
        current_answer.set(answer)

    @property
    def caller(self) -> CallerContext | None:
        "Who sent the command this task runs; None when nobody waits for it."
        answer = self.answer
        return answer.caller if answer is not None else None

    def warn_caller(self, message: str) -> None:
        "Say `message` once to whoever sent this command over the socket."
        if self.answer is not None:
            self.answer.warn(message)

    def print_command_line(self, text: str) -> None:
        """
        Print the output of a command that was entered from the command line.
        (When there is an answer, this goes back to the client that sent
        the run-command packet.)
        """
        if self.answer is not None:
            self.answer.output.append(text)

    def add_command_error(self, message: str) -> None:
        """
        Record an error for a command that failed.

        A command that came over the command line answers the client
        that sent it, and the errors of its answer are that answer.

        A command that came from a configuration file has nobody to
        answer. `startup()` runs it while the first application is still
        being built, so `show_message` finds no client state and drops
        the message. So it is kept, logged, and shown to the first
        client that arrives. Lillecarl/pymux#38.
        """
        if self.sourcing is not None:
            message = "%s: %s" % (self.sourcing, message)

        self.message_log.append(message)
        if self.answer is not None:
            self.answer.errors.append(message)
        elif not self._startup_done or self.sourcing is not None:
            logger.warning("%s", message)
            self.startup_errors.append(message)

    def report_startup_errors(self, client_state: ClientState) -> None:
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
        messages = self.startup_errors
        self.startup_errors = []
        client_state.message = "; ".join(messages)

    def detach_client(self, app: Application[Any], hang_up: bool = False) -> None:
        """
        Detach the client that belongs to this CLI.

        **The client of this application, and not the current one.**
        This took an app and then read `get_app()`, so
        `attach-session -d` detached the person who typed it, once for
        each client it meant to take the session from, and left every
        one of them attached. Lillecarl/pymux#347.
        """
        client_state = self.client_of(app)
        if client_state is not None and client_state.connection is not None:
            client_state.connection.detach_and_close(hang_up=hang_up)

        # Redraw all clients -> Maybe their size has to change.
        self.invalidate(Woke.CLIENT_DETACHED)

    def listen_on_socket(self, socket_name: str | None = None) -> str:
        """
        Bind the socket that clients connect to, and say its name.

        **The clients are taken later.** The bind has to happen before
        `daemonize` forks, because the name is what the client that
        started this server connects to, and the accepting needs the
        loop that the child starts. `running()` runs the listener.
        """
        self.listener = bind_and_listen_on_socket(socket_name, self._accept_connection)
        self.socket_name = self.listener.socket_name

        # Set session_name according to socket name.
        #        if '.' in self.socket_name:
        #            self.session_name = self.socket_name.rpartition('.')[-1]

        # `bind_and_listen_on_socket` already said it. Two lines were
        # half the log of a server that started well.
        return self.socket_name

    def adopt_listener(self, fd: int) -> None:
        """
        Serve the listening socket the server before an upgrade bound.

        `socket_name` is the snapshot's, and the fd has to be listening
        on that name: a number is only what the old server wrote down.
        Binding again is no answer, because this process still listens
        there. Lillecarl/pymux#408.
        """
        from .pipes.posix import BACKLOG, PosixSocketListener

        sock = socket.socket(fileno=fd)
        if sock.family != socket.AF_UNIX or not _listens(sock):
            sock.detach()
            raise OSError(errno.ENOTSOCK, "fd %d is not a listening unix socket" % fd)
        socket_name = self.socket_name
        if socket_name is None or sock.getsockname() != socket_name:
            sock.detach()
            raise OSError(errno.EADDRNOTAVAIL, "fd %d does not listen on %r" % (fd, socket_name))
        # The backlog is the socket's, and a server bound by an older
        # build carries that build's number across every exec.
        sock.listen(BACKLOG)
        self.listener = PosixSocketListener(socket_name, sock, self._accept_connection)

    def _accept_connection(self, pipe_connection: PipeConnection) -> None:
        # We have to create a new `context`, because this will be the scope for
        # a new prompt_toolkit.Application to become active.
        #
        # **A client that arrives here may not always attach.** An
        # integrated server draws on the terminal it runs in, and
        # this socket is for commands. The question is asked at the
        # moment a client arrives, because the route is chosen after
        # the bind. Lillecarl/pymux#159.
        may_attach = not self._serves_one_terminal
        context = contextvars.copy_context()
        connection = context.run(lambda: ServerConnection(self, pipe_connection, may_attach=may_attach))

        self.connections.append(connection)

    def run_server(self, resumed: Callable[[], Awaitable[None]] | None = None):
        """
        Serve on the socket until the server stops.

        `resumed` runs first inside `running()`: an upgrade's new build
        loads its snapshot there (`pymux.upgrade.resume`). An exception
        from it ends the server with a crash report, as any other does.
        """

        # Ignore keyboard. (When people run "pymux server" and press Ctrl-C.)
        # Pymux has to be terminated by termining all the processes running in
        # its panes.
        def handle_sigint(*a: object) -> None:
            print("Ignoring keyboard interrupt.")

        signal.signal(signal.SIGINT, handle_sigint)

        self.server_starts()

        async def serve() -> None:
            try:
                # This server forks its programs, and an upgrade execs in
                # place to keep them. Lillecarl/pymux#736.
                async with self.running():
                    if resumed is not None:
                        await resumed()
                    await self.done.wait()
            finally:
                # `stop()` is what set `done`, so this is usually a
                # second call that finds nothing. It is here for the
                # way out that nobody asked for: a pane that still runs
                # holds a `waitpid` in the executor, and the loop waits
                # for that thread when it closes.
                self.stop()

        # Run eventloop.
        started = time.monotonic()
        try:
            anyio.run(serve)
        except BaseException as error:
            # A daemon's stderr is /dev/null, so a crash that is not
            # written down is a crash nobody can read. The report goes
            # beside the log, and the log's last line names it.
            # Lillecarl/pymux#736.
            report = introspect.write_crash_report(error, started)
            logger.critical("Pymux crashed: %r. The report is %s.", error, report, exc_info=error)
            raise

        finally:
            # Clean up socket.
            self._remove_socket()

    def _remove_socket(self) -> None:
        "Take away the socket file of this server, if it bound one."
        if self.listener is not None:
            self.listener.close()
        if not self.socket_name:
            return
        with suppress(OSError):
            os.remove(self.socket_name)

    def run_integrated(
        self,
        color_depth: ColorDepth | None,
        detach_other_clients: bool = False,
        chosen_name: str | None = None,
        hang_up_others: bool = False,
        read_only: bool = False,
    ):
        """
        Run the server and one client in this process.

        The client reaches the server through a pair of queues, not
        through a socket, so it reaches the server that this call
        started and nothing else. Both halves of the protocol run, so
        this route proves what the socket route proves.

        A socket is bound as well when one was asked for. Nothing of
        the user interface reads it. It is there so that
        `pymux -S <socket> <command>` and libpymux reach this server.

        **A client cannot attach over that socket.** This server draws
        on the terminal it runs in, and a second terminal would hold
        half a session that the first one ends: `ctrl+b d` here takes
        the process with it. So `start-gui` on that socket is answered
        with `server.CANNOT_ATTACH` and the connection closes, and
        commands are what the socket serves. Lillecarl/pymux#159.

        The call returns when the server closes the connection: the
        last pane exited, somebody ran `kill-server`, or the client
        detached. The process ends with it, because both halves are in
        it.
        """
        self._serves_one_terminal = True
        self.server_starts()

        #: The client, kept so that the hangup of `attach -x` can be
        #: sent after the loop has gone. Lillecarl/pymux#347.
        attached: list[MemoryClient] = []

        async def run() -> None:
            try:
                async with self.running():
                    server_end, client_end = connect_in_memory()

                    # A context of its own, the same as for a client
                    # that arrives over the socket. A
                    # `prompt_toolkit.Application` becomes active in it.
                    context = contextvars.copy_context()
                    connection = context.run(lambda: ServerConnection(self, server_end))
                    self.connections.append(connection)

                    client = MemoryClient(client_end)
                    # One process reads the file twice: as the server,
                    # for everything, and as the client, for the lines
                    # that are about this terminal. Lillecarl/pymux#223.
                    client.config_file = self.source_file
                    client.chosen_name = chosen_name
                    client.hang_up_others = hang_up_others
                    # `-r` here is a watcher of a session nobody else
                    # can reach, which is a thin thing to want; it is
                    # passed on all the same, because the flag means
                    # the same wherever it is written and `ctrl+b d`
                    # is still the way out. Lillecarl/pymux#467.
                    client.read_only = read_only
                    attached.append(client)
                    await client.attach(
                        detach_other_clients=detach_other_clients,
                        color_depth=color_depth,
                    )
            finally:
                # The client has gone, and the server is in this
                # process, so nothing is left for the session to live
                # on. `ctrl+b d` means quit here, and that is the one
                # honest reading.
                #
                # It is also what lets the process exit at all, and
                # **it has to happen inside the loop**: a pane that is
                # still running holds a `waitpid` in the executor of
                # the loop, and `anyio.run` waits for the executor
                # threads on its way out. Without this the client put
                # the terminal back and the person was left looking at
                # their shell with no prompt. Lillecarl/pymux#109, and
                # the place it is written is Lillecarl/pymux#87.
                self.stop()

        try:
            anyio.run(run)
        finally:
            self._remove_socket()
            # After the loop, so the terminal is back before the
            # process that started this one hears about it.
            for client in attached:
                client.hang_up_the_parent()

    def add_client(
        self,
        output: Output,
        input: Input,
        color_depth: ColorDepth,
        connection: ServerConnection,
        temporary: bool = False,
        session: Session | None = None,
    ) -> ClientState:
        # A client that names no session lands on the one a person
        # looked at last, which is what `attach-session` with no `-t`
        # means in tmux. The fake client of a socket command names the
        # session of the pane the command was typed in, which the
        # run-command packet carries. Lillecarl/pymux#323.
        if session is None:
            session = self.last_used_session

        client_state = ClientState(
            self,
            connection=connection,
            input=input,
            output=output,
            color_depth=color_depth,
            session=session,
        )
        client_state.temporary = temporary
        client_state.app.renderer.reference = self.options.render_mode is RenderMode.REFERENCE

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
        self.terminals.sync_keyboard_source_flags()

        # This client speaks for the panes only if no earlier one does,
        # and only once its terminal has answered the cell size query.
        # Both are why the reply calls this again.
        self.terminals.sync_cell_size()
        self.terminals.sync_color_scheme()

        return client_state

    def remove_client(self, connection: ServerConnection) -> None:
        """
        Forget a client that has gone, and everything of it.

        **Both lists, not one.** A connection left in `connections`
        stays for the life of the server, so a person who attaches and
        detaches each morning leaves a `ServerConnection` behind each
        time, with its pipe input and its parser. Nothing draws for
        them -- `_send_packet` answers a closed connection with nothing
        -- so the cost is memory and a longer walk for every broadcast.
        Lillecarl/pymux#226.
        """
        gone = self._client_states.pop(connection, None)
        if connection in self.connections:
            self.connections.remove(connection)
            self.counters.client_left(connection.pipe_connection)
        if gone is not None:
            self.browsers.client_gone(gone)
            self.sync_pane_focus()
        # One client fewer can mean that the rest speak more.
        self.terminals.sync_keyboard_source_flags()
        # And the client that spoke for the panes may be the one gone.
        self.terminals.sync_cell_size()
        self.terminals.sync_color_scheme()
