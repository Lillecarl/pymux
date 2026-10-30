from __future__ import annotations

import asyncio
import datetime
import fnmatch
import os
import time
import weakref
from collections import deque
from collections.abc import Coroutine, Iterable
from contextlib import asynccontextmanager, suppress
from typing import TYPE_CHECKING, Any, ClassVar

import anyio
import anyio.abc
from libpymux.agentic import CallerContext
from prompt_toolkit.application import Application
from prompt_toolkit.application.current import get_app, set_app
from prompt_toolkit.clipboard import InMemoryClipboard
from prompt_toolkit.data_structures import Size
from prompt_toolkit.history import InMemoryHistory
from ptyhost.backends.posix import GRACE
from ptyhost.ending import say_how_it_ends
from pyte.keep import Keep
from pyte.keys import KEY_PROFILES, KeyProfile

from .. import hacks_and_lies, introspect, log
from ..answer import Answer, current_answer
from ..audio import AudioServer
from ..commands import call_command_handler, handle_command
from ..connection.client_state import ClientState
from ..connection.known_terminals import identify
from ..connection.server import ServerConnection
from ..enums import OpenUrlTarget, RenderMode, Woke
from ..graphics import kernels
from ..graphics.graphics import OuterImageIds
from ..ids import PaneId, SessionId
from ..jobs import JOB_SWEEP_EVERY, JobTable
from ..keys.actions import ActionLog, Origin, awaited_as, started_by
from ..keys.key_bindings import PymuxKeyBindings
from ..keys.keys import FOCUS_IN, FOCUS_OUT
from ..layout.error_notice import ErrorNotice
from ..log import logger
from ..notify.notifications import Notification, NotificationCenter, NotificationRoutes
from ..notify.notifier import Notifier
from ..options.options import ALL_CLIENT_OPTIONS, ALL_OPTIONS, ALL_SERVER_OPTIONS, ALL_WINDOW_OPTIONS, Scope
from ..options.server_options import ServerOptions
from ..options.session_options import SessionOptions
from ..rc import STARTUP_COMMANDS
from ..shims import Shims
from ..terminals import Terminals
from ..wayland.browsers import Browsers
from ..wayland.portal import Portal
from ..wayland.wayland import WaylandServer
from ..wayland.window_pane import WindowProcess, WindowScreen
from .arrangement import Arrangement
from .forward_requests import ForwardRequests
from .hooks import Hooks
from .outward import Outward
from .overlays import Overlays
from .panes import PaneMaker
from .planes import Planes
from .serving import Serving
from .session import OwnOptions, Session
from .waking import Waking
from .window_tree import Pane, Window

if TYPE_CHECKING:
    from prompt_toolkit.input import Input
    from prompt_toolkit.output import ColorDepth, Output

    from ..commands.wait_for import WaitChannel
    from ..pipes.posix import PosixSocketListener

__all__ = [
    "Pymux",
]

kernels.install()


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
        "refusing_commands": Keep.DROPPED,  # the new image takes them again
        "_client_states": Keep.REBUILT,  # from the client records, as clients reconnect
        # Told again at the first focus sync, which a program reads as one more focus-in.
        "_focused_panes": Keep.DROPPED,
        "shims": Keep.REBUILT,
        "options": Keep.SAVED,
        "session_defaults": Keep.SAVED,
        "terminals": Keep.REBUILT,
        "outward": Keep.REBUILT,
        "notifier": Keep.REBUILT,
        "panes": Keep.REBUILT,
        "serving": Keep.REBUILT,
        "planes": Keep.REBUILT,
        "overlays": Keep.REBUILT,
        "forward_requests": Keep.REBUILT,
        "waking": Keep.REBUILT,
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
        self.notifier = Notifier(self)
        self.notification_center.on_added = self.notifier.notified

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
        self.planes = Planes(self)
        self.overlays = Overlays(self)
        self.forward_requests = ForwardRequests(self)
        self.waking = Waking(self)
        self.serving = Serving(self)
        self.panes = PaneMaker(self)
        self.outward = Outward(self)

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
            tasks.start_soon(self.waking.auto_refresh)
            tasks.start_soon(self._watch_window_output)
            tasks.start_soon(self._sweep_jobs)
            tasks.start_soon(self.actions.keep, lambda: self.options.action_log_limit)
            tasks.start_soon(self.wayland.keep)
            tasks.start_soon(self.portal.keep)
            tasks.start_soon(self.audio.keep)
            if self.listener is not None:
                self.serving.serve_listener()
            try:
                yield
            finally:
                # Every task of this server ends with the scope,
                # including the auto refresh above, which never ends
                # on its own.
                tasks.cancel_scope.cancel()
                self.tasks = None
                self.error_notice.uninstall()

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

    def show_pane_on_click(self, client_state: ClientState, pane: Pane) -> None:
        "Take this client to the pane a clicked notification came from, unless its session says no."
        if not self.options_of_pane(pane).notification_click_shows_pane:
            return
        self.show_pane(client_state, pane)

    def show_pane(self, client_state: ClientState, pane: Pane) -> bool:
        "Move this client onto a pane, wherever it is. False when the pane is in no window."
        found = self.place_of_pane_id(pane.pane_id)
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
        found = self.place_of_pane_id(record.pane_id)
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

        A command that arrives over a socket runs under a temporary client
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
                await self.panes.create_window(command=self.startup_command)
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
        self.notifier.close_seen_notifications()
        self.notifier.drop_seen_popups()
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

    def sync_focus_everywhere(self) -> None:
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
        self.waking.sync_frame_rate()

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

    def place_of_pane_id(self, pane_id: PaneId | None) -> tuple[Session, Window, Pane] | None:
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
        means nobody, and the person who asked hears so. The temporary client
        of a command that arrived over a socket is never one of them:
        it is the connection that ran the command, and it closes while
        the answer is still being sent.
        """
        latest = self.latest_client()
        if self.current_session.options.open_url_target != OpenUrlTarget.BROADCAST and latest is not None:
            return [latest]
        return self.clients

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
        attached. The temporary client of a socket command is never it, for the
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
        return KEY_PROFILES[identify(version, term).key_profile or options.key_profile]

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
            # started running. (The temporary clients that only handled
            # a command.)
            with suppress(Exception):
                app.exit()
        self.done.set()

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
        # client that draws nothing, and a message left there is a message
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
        Detach the client that belongs to this application.

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
        # on attach for the same reason. The temporary client of a command does
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
