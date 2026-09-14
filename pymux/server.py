import json
import re
import time
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    ContextManager,
    Dict,
    List,
    TextIO,
    cast,
)

import anyio

from prompt_toolkit.application.current import create_app_session, set_app
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.output import ColorDepth
from prompt_toolkit.output.vt100 import Vt100_Output

from .colors import ColorDetection, DefaultColors
from .commands import handle_command
from .enums import Woke
from .graphics import ClientGraphics
from .keys import KittyVt100Parser
from .log import logger
from .options import ExtendedKeys, SetOptionError
from .pipes import BrokenPipeError

if TYPE_CHECKING:
    from pymux.main import ClientState, Pymux

__all__ = ["ServerConnection"]

# An OSC reply of the outer terminal: the code, and everything between
# the code and the terminator.
#: The answer to "CSI ? 2026 $ p" (DECRQM): "CSI ? 2026 ; <state> $ y".
#: A state of 0 means the terminal does not know the mode. 1 and 2 are
#: set and reset, and 4 means it is on and cannot be turned off; all
#: three say the terminal understands it.
_SYNCHRONIZED_REPLY_RE = re.compile(r"^\x1b\[\?2026;(\d+)\$y$")

_OSC_REPLY_RE = re.compile(
    r"^(?:\x1b\]|\x9d)(\d+);(.*?)(?:\x1b\\|\x9c|\x07)$", re.DOTALL
)

#: How many reads in a row may fail before a connection is closed.
#: A packet that cannot be read is answered and the next one is taken,
#: which is what makes one bad packet harmless; a read that fails again
#: and again is a connection nothing can use. Lillecarl/pymux#329.
FAILURES_THAT_END_A_CONNECTION = 5

#: What a client is told when it may not attach here.
#:
#: `pymux integrated` holds the server and its one terminal in one
#: process. The socket it binds is for commands, and a client that
#: attached over it would own half a session that the other half can
#: end: `ctrl+b d` in the first terminal takes the process, and the
#: second terminal loses the session whatever the first person meant.
#: Lillecarl/pymux#159.
CANNOT_ATTACH = (
    "This pymux runs in one terminal, and that terminal is taken.\r\n"
    "The socket is for commands: pymux -S <socket> <command>.\r\n"
)


class ServerConnection:
    """
    For each client that connects, we have one instance of this class.
    """

    def __init__(
        self, pymux: "Pymux", pipe_connection, may_attach: bool = True
    ) -> None:
        self.pymux = pymux

        self.pipe_connection = pipe_connection

        #: Whether a client on this connection may ask for the user
        #: interface. False for a connection that arrived over the
        #: socket of an integrated server: that server draws on the one
        #: terminal it runs in, and commands are all it serves to
        #: anybody else. Lillecarl/pymux#159.
        self.may_attach = may_attach

        self.size = Size(rows=20, columns=80)
        self._closed = False

        #: The scope the background work of this connection runs in.
        #: `serve()` opens it, so every task of a client ends when that
        #: client goes, and none of them can outlive the server: the
        #: group is a child of `Pymux.running`'s. Lillecarl/pymux#87.
        self._tasks: "anyio.abc.TaskGroup" | None = None

        self._recv_buffer = b""
        self.client_state: "ClientState" | None = None

        # Kitty keyboard protocol support of the outer terminal. The
        # client sends "kitty-detect" right after querying its terminal;
        # the reply of that query passes through this connection.
        self._kitty_detection_pending = False
        self._kitty_supported = False

        #: What the outer terminal can report of the keyboard protocol,
        #: in the flags of that protocol. The query of the detection
        #: asks for every flag, so the reply says which ones the
        #: terminal took. Zero means the legacy encoding only.
        self.kitty_source_flags = 0

        # Kitty graphics protocol state of the outer terminal. The same
        # query answers this one: the reply of the graphics query
        # arrives before the device attributes reply that closes the
        # detection.
        self.graphics = ClientGraphics(
            self._write_output_raw,
            self._flush_output,
            self._request_repaint,
            # Read when it is needed, not now: the colour detection
            # below has not run yet, and the half blocks only ask once
            # a frame carries an image.
            color_depth=lambda: self.colors.depth,
        )

        # Colour depth of the outer terminal. The same handshake
        # answers it: the probe reply arrives before the device
        # attributes reply that closes the detection.
        self.colors = ColorDetection()

        #: The machine the client of this connection runs on, as the
        #: client reported it. Empty until it attaches. **Only a client
        #: can say it**: `socket.gethostname()` here answers the
        #: server's own machine, and over ssh that is not the one the
        #: person sits at. Lillecarl/pymux#287.
        self.hostname = ""

        #: The environment of the machine this client runs on, as the
        #: client reported it when it attached. The session keeps the
        #: names "update-environment" lists, and nothing keeps the rest.
        #: Lillecarl/pymux#271.
        self.environment: Dict[str, str] = {}

        #: The terminal this client draws on, as the client named it,
        #: and the process it runs as. Both are of the client's own
        #: machine, so neither means anything here on its own: `name`
        #: puts the hostname in front. Lillecarl/pymux#335.
        self.ttyname = ""
        self.pid = 0

        #: When this connection attached. A client that reattaches from
        #: the same terminal takes the same name and a later time, so
        #: this is what tells two runs of one terminal apart, and what
        #: `#{client_created}` reads.
        self.created = time.time()

        #: The two colours the outer terminal draws with by itself.
        #: The same handshake asks for them, and either one stays
        #: `None` when the terminal does not say. **Only a client can
        #: find this out**, which is why it is here and not on the
        #: session. Lillecarl/pymux#223.
        self.default_colors = DefaultColors()

        # The client input is parsed by the application that reads from
        # the pipe input (see `_ClientInput`). Give that input a parser
        # that also understands the kitty keyboard protocol, and route
        # terminal replies to `_handle_kitty_reply`.
        self._pipeinput = _ClientInput(
            self._send_packet,
            kitty_reply_callback=self._handle_kitty_reply,
            # Whether this client's terminal counts the modifiers the
            # way the protocol does. It is asked when a key arrives and
            # not now, because the detection has not answered yet.
            # Lillecarl/pymux#182.
            speaks_protocol=self.keyboard_is_supported,
        )

        # The shape of the pointer that this client was told about. A
        # client that never saw one is not told to reset it.
        self._pointer_shape_sent = ""

        self.pymux.serve_connection(self)

    def _spawn(self, coro) -> None:
        """
        Run a coroutine in the background of this connection.

        It belongs to the connection's scope, so it ends when the
        client goes and it cannot be collected while it runs. A task
        that nobody holds used to be collectable mid-flight: the loop
        then reported that, and prompt_toolkit answers an exception in
        the loop by leaving the alternate screen and asking the user to
        press a key. There is no user at that end, so the answer fails
        and reports again -- a lost task turned into an endless storm
        of repaints on the terminal of every client.
        """
        if self._tasks is None:
            logger.warning("A task was spawned on a connection that does not serve.")
            coro.close()
            return
        self._tasks.start_soon(self._run_task, coro)

    async def _run_task(self, coro) -> None:
        """
        Run one spawned coroutine, and say what it raised.

        **The failure stays inside the connection.** In a task group an
        exception cancels the siblings and goes up, and up from here is
        every other client of this server. The work a connection spawns
        is the work that answers one client, so the honest blast radius
        is that client -- and even that is more than the old shape did,
        which only logged. Lillecarl/pymux#87.
        """
        try:
            await coro
        except anyio.get_cancelled_exc_class():
            raise
        except Exception as error:
            logger.error("A task of this connection failed.", exc_info=error)

    async def serve(self) -> None:
        """
        Read this client until the connection ends.

        This is the scope of the connection: the task group it spawns
        into, and the `AppSession` its application lives in.

        **The session is why this is a coroutine and not a callback.**
        `set_app` saves and restores one attribute of one session
        object, so two clients sharing a session save and restore each
        other's application: the last one to exit puts back a value
        that is two attachments stale, and the application it names is
        then held by a module-level `ContextVar` for as long as the
        server runs, with its layout, its panes and their scrollbacks
        behind it. Lillecarl/pymux#230.

        A session of its own is what prompt_toolkit does for the same
        job -- `contrib/ssh/server.py` wraps each client the same way.
        """
        with create_app_session():
            async with anyio.create_task_group() as tasks:
                self._tasks = tasks
                try:
                    await self._read_until_it_ends()
                finally:
                    # What the client still had running goes with it.
                    self._tasks = None
                    tasks.cancel_scope.cancel()

    def _write_output_raw(self, data: str) -> None:
        "Write to the outer terminal, without escaping. (For graphics.)"
        if self.client_state is not None:
            self.client_state.output.write_raw(data)

    def _flush_output(self) -> None:
        if self.client_state is not None:
            self.client_state.output.flush()

    def forward_osc(self, sequence: str) -> None:
        """
        Write an OSC sequence of a pane to the outer terminal of this
        client. (The clipboard, a notification, the shape of the
        pointer: pymux cannot serve those, the terminal of the user
        can.)

        An OSC sequence moves no cursor and paints no cell, so it is
        safe between two frames. `pymux.osc.build_osc` has already
        checked the payload of the pane.
        """
        if self.client_state is None:
            return
        self._write_output_raw(sequence)
        self._flush_output()

    def set_pointer_shape(self, shape: str) -> None:
        """
        Put the shape that the focused pane asks for on the pointer of
        this client. An empty shape gives the pointer back to the
        terminal of the user.

        Only a change goes out. The shape belongs to the pane that the
        client looks at, so it has to be written again when the focus
        moves to a pane that wants a different one, or none.
        """
        if shape == self._pointer_shape_sent:
            return
        self._pointer_shape_sent = shape
        self.forward_osc("\x1b]22;%s\x1b\\" % shape)

    def _apply_color_depth(self) -> None:
        """
        Render with the colour depth that the detection settled on.

        The first frames use the fallback from TERM and COLORTERM; the
        probe can raise it. A change needs a full repaint, because the
        colours already on the screen were written with the old depth.
        """
        if self.client_state is None:
            return
        depth = self.colors.depth
        if depth == self.client_state.color_depth:
            return
        self.client_state.color_depth = depth
        self._request_repaint()

    def _request_repaint(self) -> None:
        """
        Ask the renderer to paint the whole screen again.

        The sixel output needs this: those pixels sit in the cells, so
        the text under an image that moved has to be written once more.
        Dropping the last screen of the renderer makes the next render
        a full one. (There is no public way to ask for that.)
        """
        if self.client_state is None:
            return
        app = self.client_state.app
        try:
            app.renderer._last_screen = None
        except Exception:
            logger.exception("Could not ask for a full repaint.")
            return
        # Named here and not through `Pymux.invalidate`, because this
        # wakes the one client that asked and not all of them.
        logger.debug("Drawing 1 of the clients: it asked for a full repaint")
        app.invalidate()

    def _handle_kitty_reply(self, data: str) -> None:
        """
        A terminal reply arrived from the outer terminal. The replies of
        the keyboard flags query and of the graphics query say which
        protocols the terminal speaks. The device attributes reply comes
        last and closes the detection.

        An answer to a desktop notification is not part of that. It can
        arrive at any time, long after the detection, and it belongs to
        one pane.
        """
        osc = _OSC_REPLY_RE.match(data)
        if osc is not None:
            if osc.group(1) == "99":
                self._route_notification(osc.group(2))
            else:
                # The colours the terminal draws with, defaults and
                # palette alike. It answers each on the code that
                # asked, so this reads them whenever they arrive: a
                # terminal that reports a theme change later says it
                # the same way. Anything learned is what the panes
                # answer their programs with, so they are told again.
                if self.default_colors.handle_osc_reply(osc.group(1), osc.group(2)):
                    self.pymux.sync_color_bases()
            return

        if not self._kitty_detection_pending:
            return

        if data.startswith("\x1b[?") and data.endswith("u"):
            # Reply of the "CSI ? u" query: the terminal supports the
            # keyboard protocol. The detection asks for every flag
            # first, so the number says which ones the terminal took,
            # and that is what it can report.
            self._kitty_supported = True
            try:
                self.kitty_source_flags = int(data[3:-1] or 0)
            except ValueError:
                self.kitty_source_flags = 0
            self.pymux.sync_keyboard_source_flags()
            return

        # The answer to the synchronised output query. A terminal that
        # holds a frame back does not need the cursor hidden while the
        # frame is painted, and hiding it restarts the blink: at a frame
        # rate that is a cursor which never blinks.
        synchronized = _SYNCHRONIZED_REPLY_RE.match(data)
        if synchronized is not None:
            if self.client_state is not None:
                self.client_state.output.synchronized_output = (
                    synchronized.group(1) != "0"
                )
            return

        # The graphics query reply, the cell size report and the device
        # attributes all say something about the images. The reply of
        # the colour probe says how many colours the terminal takes.
        self.graphics.handle_reply(data)
        self.colors.handle_reply(data)

        if not data.endswith("c"):
            return

        # Device attributes reply: the fence of the detection. What did
        # not answer by now is not supported.
        self._kitty_detection_pending = False
        self._apply_color_depth()
        self._send_packet(
            {
                "cmd": "kitty-keyboard",
                "data": {"supported": self.keyboard_is_supported()},
            }
        )
        # Enable the flags on this client's terminal as well. (Other
        # clients are kept in sync through `Pymux.sync_kitty_flags`,
        # which only sends on change, so a client that attaches after
        # the value settled would otherwise never hear it.)
        self._send_packet(
            {
                "cmd": "kitty-keyboard",
                "data": {"flags": self.pymux.keyboard_flags_for_client()},
            }
        )

    def keyboard_is_supported(self) -> bool:
        """
        Whether this client's terminal may be put into the protocol.

        The detection decides it, and one option overrides the
        detection. `set-option extended-keys always` is for a terminal
        that speaks the protocol and does not answer the query that
        asks; there are some, and a person who knows theirs is one can
        say so. Lillecarl/pymux#173.
        """
        if self.pymux.extended_keys is ExtendedKeys.ALWAYS:
            return True
        return self._kitty_supported

    def _route_notification(self, param: str) -> None:
        """
        Give the answer to a desktop notification to the pane that
        asked for it, under the identifier that the pane chose.

        An answer that names no notification of a pane is dropped. It
        must not reach a pane as key presses.
        """
        routed = self.pymux.notifications.incoming(param)
        if routed is None:
            return

        pane_id, param = routed
        pane = self.pymux.panes_by_id.get(pane_id)
        if pane is None:
            return  # The pane is gone.

        try:
            pane.process.write_input("\x1b]99;%s\x1b\\" % param)
        except Exception:
            logger.exception("Giving a notification answer to a pane failed.")

    async def _read_until_it_ends(self) -> None:
        failures = 0

        while True:
            try:
                data = await self.pipe_connection.read()
                self._process(data)
            except BrokenPipeError:
                self.detach_and_close()
                break

            except anyio.get_cancelled_exc_class():
                raise

            except Exception:
                # The read loop must never die silently: log the
                # exception and keep the connection alive.
                logger.exception("Exception while processing client packet.")

                failures += 1
                if failures >= FAILURES_THAT_END_A_CONNECTION:
                    # **A read that keeps failing is a connection that
                    # ended in a way the transport did not name.** With
                    # no count here the loop retries at full speed: one
                    # whole processor and a traceback in the log on
                    # every turn, for as long as the server runs.
                    # Measured at millions of tracebacks in eighteen
                    # minutes. Lillecarl/pymux#329.
                    logger.error(
                        "This client failed %d reads in a row. Closing it.",
                        failures,
                    )
                    self.detach_and_close()
                    break
            else:
                failures = 0

    def _process(self, data) -> None:
        """
        Process packet received from client.
        """
        # Posix pipe returns bytes, win32 returns str. Normalize to str.
        if isinstance(data, (bytes, bytearray)):
            try:
                data = data.decode("utf-8")
            except Exception:
                logger.warning("Received invalid UTF-8 from client. Ignoring.")
                return
        try:
            packet = json.loads(data)
        except ValueError:
            # So far, this never happened. But it would be good to have some
            # protection.
            logger.warning("Received invalid JSON from client. Ignoring.")
            return

        # Handle commands.
        if packet["cmd"] == "run-command":
            # Handle this in a task. The command handler can produce output
            # that has to be sent back to the client.
            self._spawn(self._run_command(packet))
            return

        # Handle stdin.
        elif packet["cmd"] == "in":
            self._pipeinput.send_text(packet["data"])

        # The client queried its terminal for kitty keyboard protocol
        # support. (The replies come back as input on this connection.)
        elif packet["cmd"] == "kitty-detect":
            self._kitty_detection_pending = True

        # Set size. (The client reports the size.)
        elif packet["cmd"] == "size":
            rows, columns = packet["data"]
            self.size = Size(rows=rows, columns=columns)
            self.pymux.invalidate(Woke.CLIENT_RESIZED)

        # Start GUI. (Create CommandLineInterface front-end for pymux.)
        elif packet["cmd"] == "start-gui":
            if not self.may_attach:
                self._spawn(self._refuse_the_attach())
                return

            detach_other_clients = bool(packet["detach-others"])
            forced = packet["color-depth"]
            term = packet["term"]

            # The colour depth of the outer terminal. A flag of the
            # client forces one; otherwise the environment gives the
            # first answer and the probe can raise it later.
            self.colors.forced = ColorDepth(forced) if forced else None
            self.colors.term = term
            self.colors.colorterm = packet.get("colorterm", "")
            self.hostname = packet.get("hostname", "")
            self.environment = packet.get("environment") or {}
            self.ttyname = packet.get("ttyname", "")
            self.pid = packet.get("pid") or 0

            self._create_app(color_depth=self.colors.depth, term=term)

            if detach_other_clients:
                self._detach_the_others()

            # What this client's own configuration file said about it.
            # Applied before the first frame, so nothing draws with a
            # theme the person did not choose and then change.
            # Lillecarl/pymux#223.
            self._take_client_options(packet.get("client-options") or [])

            # The session this client landed on takes the names that
            # follow a client: a display, an agent, a session bus.
            # `attach_client_to` does it for every later move between
            # sessions; this is the first attach, which does not go
            # through it. Lillecarl/pymux#271.
            if self.client_state is not None:
                self.pymux.take_environment_from(self.client_state)

        # A URL that the client of this connection could not open. The
        # request went out as a status line here, so the answer goes
        # there too, and it names the URL to copy.
        elif packet["cmd"] == "open-failed":
            if self.client_state is not None:
                self.client_state.message = (
                    "Could not open %s in a browser on this machine." % (packet["data"],)
                )

    def _detach_the_others(self) -> None:
        """
        What `attach -d` means: take this session from whoever else
        holds it.

        **The session, and not the server.** tmux's rule is one line --
        `if (c_loop->session != s || c == c_loop) continue;`
        (`cmd-attach-session.c:127`) -- and pymux had neither half of
        it. A person attaching to one session threw everybody off every
        other session too. `detach-client -a` is the gesture for the
        whole server, in pymux as in tmux. Lillecarl/pymux#345.

        That one rule also answers a question pymux had answered
        wrongly. `pymux.connections` holds every connection and not
        every client: a connection that arrived to run a command is in
        it from the moment the server accepted it, and closing that one
        killed somebody's `list-sessions` mid-answer. A command has no
        session, so it is skipped here without a case of its own.

        **This runs after `_create_app`**, because until then this
        connection has no session to compare the others against.
        """
        if self.client_state is None:
            return

        for connection in list(self.pymux.connections):
            if connection is self:
                continue
            other = connection.client_state
            if other is None or other.session is not self.client_state.session:
                continue
            connection.detach_and_close()

    def _take_client_options(self, announced) -> None:
        """
        Set on this client what its own configuration file named.

        **A bad line never fails the attach.** A person with a typo in
        their configuration gets their panes and a message saying what
        was wrong; refusing to attach would leave them with a terminal
        they cannot use and a file they may not be able to reach.
        Lillecarl/pymux#223.
        """
        if self.client_state is None:
            return

        for one in announced:
            try:
                name, value = one
                option = self.pymux.client_options[name]
            except (KeyError, TypeError, ValueError):
                self.client_state.message = "Invalid client option: %s" % (one,)
                continue

            try:
                option.set_value(self.pymux, value, self.client_state)
            except SetOptionError as e:
                # The line as they wrote it, then the reason. A person
                # reading "Expecting one of" needs to see what they
                # typed to know which line to go and fix.
                self.client_state.message = "%s %s: %s" % (name, value, e.message)

        self.pymux.sync_color_bases()

    def _send_packet(self, data: object) -> None:
        """
        Send packet to client.
        """
        if self._closed:
            return

        data = json.dumps(data)

        async def send() -> None:
            try:
                await self.pipe_connection.write(data)
            except BrokenPipeError:
                self.detach_and_close()

        self._spawn(send())

    async def _refuse_the_attach(self) -> None:
        """
        Tell this client that it cannot have the user interface, and
        close the connection. Lillecarl/pymux#159.

        **The write is awaited, and `_send_packet` does not wait.** That
        one spawns the write into the scope of this connection, and
        closing cancels that scope: the reason the client came would
        reach it only if the cancel lost the race.
        """
        await self._write_packet({"cmd": "out", "data": CANNOT_ATTACH})
        # And a code to leave with. A client that attached reads this
        # too now, so a script hears the refusal rather than reading a
        # zero and calling it attached. Lillecarl/pymux#332.
        await self._write_packet({"cmd": "exit", "code": 1})

        logger.info("A client asked to attach to a server that serves one terminal.")
        self.detach_and_close()

    async def _run_command(self, packet: Dict[str, Any]) -> None:
        """
        Execute a run command from the client.
        """
        create_temp_cli = self.client_state is None

        if create_temp_cli:
            # If this client doesn't have a CLI. Create a Fake CLI where the
            # window containing this pane, is the active one. (The CLI instance
            # will be removed right after the command handler ran, so it
            # doesn't hurt too much and makes the code easier.)
            pane_id = packet.get("pane_id")
            pane = (
                self.pymux.panes_by_id.get(int(pane_id))
                if pane_id is not None
                else None
            )

            # The session of the pane the command was typed in. Without
            # it the fake client lands on the session a person looked at
            # last, so `pymux new-window` typed in a pane of one session
            # would open the window in another. Lillecarl/pymux#323.
            session = self.pymux.session_holding(pane) if pane is not None else None

            self._create_app(start=False, session=session)
            if pane is not None and session is not None:
                with set_app(self.client_state.app):
                    session.arrangement.set_active_window_from_pane_id(pane.pane_id)

        pymux = self.pymux
        pymux.command_output = []
        pymux.command_error = []

        with set_app(self.client_state.app):
            try:
                # **The client waits and the server does not.** A
                # command that has to wait answers with a coroutine,
                # and this route is the one that awaits it: the person
                # who typed `pymux wait-for done` waits for it, while
                # every other client of this server keeps running.
                # Lillecarl/pymux#87.
                answer = handle_command(pymux, packet["data"])
                if answer is not None:
                    await answer
            finally:
                # Send the output of the command back to the client, and
                # close the connection.
                output = pymux.command_output
                errors = pymux.command_error
                pymux.command_output = None
                pymux.command_error = None

                try:
                    if output:
                        await self._write_packet(
                            {"cmd": "out", "data": "\n".join(output) + "\n"}
                        )
                    if errors:
                        await self._write_packet(
                            {"cmd": "err", "data": "\n".join(errors) + "\n"}
                        )
                    await self._write_packet(
                        {"cmd": "exit", "code": 1 if errors else 0}
                    )
                except BrokenPipeError:
                    pass
                self._close_connection()

    async def _write_packet(self, data_obj: object) -> None:
        """
        Write a packet to the client. (This waits for the write to complete.)
        """
        if self._closed:
            return

        data = json.dumps(data_obj)

        try:
            await self.pipe_connection.write(data)
        except BrokenPipeError:
            self.detach_and_close()

    def _create_app(
        self,
        color_depth: ColorDepth = ColorDepth.DEPTH_8_BIT,
        term: str = "xterm",
        start: bool = True,
        session=None,
    ) -> None:
        """
        Create CommandLineInterface for this client.
        Called when the client wants to attach the UI to the server.

        :param start: Start the application. (`False` for the temporary CLI
            that handles `run-command` packets. That one is removed right
            after the command handler ran.)
        :param session: The session this client is on. Without one it is
            the session a person looked at last.
        """
        output = Vt100_Output(
            cast(TextIO, _SocketStdout(self._send_packet, self.pymux.counters)),
            lambda: self.size,
            term=term,
        )

        client_state = self.pymux.add_client(
            input=self._pipeinput,
            output=output,
            connection=self,
            color_depth=color_depth,
            # The fake CLI of a command that arrived over a socket is
            # not a client a person uses: it must not win "used last",
            # and what a command shows must go to a real one.
            temporary=not start,
            session=session,
        )
        self.client_state = client_state

        if start:

            async def run() -> None:
                try:
                    # A server has no terminal of its own to print a
                    # traceback on, and nobody to press ENTER, which is
                    # what prompt_toolkit does with an exception in the
                    # event loop. Let asyncio log it instead.
                    await client_state.app.run_async(
                        set_exception_handler=False,
                        # **A server's application has no terminal of
                        # its own to be resized.** Each client reports
                        # its size in a packet, and SIGWINCH is one
                        # process-wide handler that only one
                        # application can hold: two of them save and
                        # restore it in the wrong order, and the last
                        # restore puts back an application that has
                        # already gone, with its whole layout behind
                        # it. Lillecarl/pymux#231.
                        handle_sigwinch=False,
                    )
                except anyio.get_cancelled_exc_class():
                    raise
                except Exception:
                    logger.exception("Application crashed.")
                finally:
                    self._close_connection()

            self._spawn(run())

    def _close_connection(self) -> None:
        # This is important. If we would forget this, the server will
        # render CLI output for clients that aren't connected anymore.
        if self._closed:
            return
        # Remove the images that this client put on its terminal.
        try:
            self.graphics.reset()
        except Exception:
            logger.exception("Removing the graphics of the client failed.")

        # Try to exit the application if it's still running.
        if self.client_state is not None:
            try:
                if not self.client_state.app.is_done:
                    self.client_state.app.exit()
            except Exception:
                pass
        self.pymux.remove_client(self)
        self.client_state = None
        self._closed = True

        # The background work of this connection ends with the read
        # loop that `serve` runs: leaving that scope cancels the group.
        # This is called from inside those tasks, so cancelling here
        # would cancel the caller before it finished closing.

        # Close input pipe and remove connection from eventloop.
        self._pipeinput.close()
        try:
            self.pipe_connection.close()
        except Exception:
            pass

    def suspend_client_to_background(self) -> None:
        """
        Ask the client to suspend itself. (Like, when Ctrl-Z is pressed.)
        """
        self._send_packet({"cmd": "suspend"})

    @property
    def name(self) -> str:
        """
        What `detach-client -t` selects this client by.

        tmux names a client by its tty and falls back to its process
        when it has none (`server-client.c:2989`), and pymux does the
        same with the machine in front. **The machine is always
        there**, because a pymux client can be on another one: two
        people can each be on their own `/dev/pts/3`, and a tty path
        alone would name them both. Lillecarl/pymux#335.

        A terminal keeps its tty for as long as it is open, so a person
        who detaches and attaches again from the same window types the
        same name. That is the property this is chosen for: a name that
        moves to another client is worse than no name.
        """
        if self.ttyname:
            what = self.ttyname
        elif self.pid:
            what = "client-%d" % (self.pid,)
        else:
            # Nothing said. A connection that never sent `start-gui`
            # is the fake CLI of a command, which nobody selects.
            return ""
        return "%s:%s" % (self.hostname or "?", what)

    def detach_and_close(self) -> None:
        # Remove from Pymux.
        self._close_connection()


class _SocketStdout:
    """
    Stdout-like object that writes everything through the unix socket to the
    client.
    """

    def __init__(self, send_packet: Callable, counters=None) -> None:
        self.send_packet = send_packet
        self.counters = counters
        self._buffer: List[str] = []

    def write(self, data: str) -> int:
        self._buffer.append(data)
        return len(data)

    def flush(self) -> None:
        # One flush is one frame: the renderer writes a whole frame and
        # then flushes it. So this is where a server can count what it
        # actually sent, which the invalidates cannot say -- several of
        # those become one frame.
        written = "".join(self._buffer)
        if self.counters is not None:
            self.counters.frame_went_out(len(written))

        self.send_packet({"cmd": "out", "data": written})
        self._buffer = []

    def isatty(self) -> bool:
        return True


class _ClientInput:
    """
    Input class that can be given to the CommandLineInterface.
    We only need this for turning the client into raw_mode/cooked_mode.
    """

    def __init__(
        self,
        send_packet: Callable,
        kitty_reply_callback=None,
        speaks_protocol=None,
    ) -> None:
        self.send_packet = send_packet
        # Keep a reference to the context manager for the whole lifetime of
        # this object. `create_pipe_input()` returns a generator context
        # manager; when it's garbage collected, the pipe is closed.
        self._input_cm = create_pipe_input()
        self._input = self._input_cm.__enter__()

        # Replace the parser of the pipe input with one that also
        # understands the key encoding of the kitty keyboard protocol,
        # and routes terminal replies (keyboard flags query, device
        # attributes) to the given callback.
        self._input.vt100_parser = KittyVt100Parser(
            lambda key_press: self._input._buffer.append(key_press),
            reply_callback=kitty_reply_callback,
            speaks_protocol=speaks_protocol,
        )

    def close(self) -> None:
        "Close the input pipe. (Idempotent.)"
        if self._input is not None:
            try:
                self._input_cm.__exit__(None, None, None)
            except Exception:
                pass
            self._input = None

    @property
    def closed(self) -> bool:
        """
        Whether this input is closed. A closed pipe is closed.

        prompt_toolkit reads this before it takes keys, and it reads it
        after the pipe has gone: the key press that detached the client
        is still on the loop, and the application is still exiting.
        `__getattr__` forwarded that read to `None` and raised
        `AttributeError` on the terminal of the person who detached.
        `typeahead_hash` below carries the same guard for the same
        reason. Lillecarl/pymux#109.
        """
        if self._input is None:
            return True
        return self._input.closed

    def typeahead_hash(self) -> str:
        """
        The typeahead hash must keep working after the pipe is closed:
        prompt_toolkit stores unprocessed input as typeahead when the
        application exits, which can happen after the connection was
        closed.
        """
        if self._input is not None:
            return self._input.typeahead_hash()
        return "closed-pipe-input-%i" % id(self)

    # Implement raw/cooked mode by sending this to the attached client.

    def raw_mode(self) -> ContextManager[None]:
        return self._create_context_manager("raw")

    def cooked_mode(self) -> ContextManager[None]:
        return self._create_context_manager("cooked")

    def _create_context_manager(self, mode: str) -> ContextManager[None]:
        "Create a context manager that sends 'mode' commands to the client."

        class mode_context_manager:
            def __enter__(*a: object) -> None:
                self.send_packet({"cmd": "mode", "data": mode})

            def __exit__(*a: object) -> None:
                self.send_packet({"cmd": "mode", "data": "restore"})

        return mode_context_manager()

    def __getattr__(self, name: str) -> object:
        return getattr(self._input, name)
