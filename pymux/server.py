from __future__ import annotations

import base64
import json
import re
import time
from collections.abc import Callable, Coroutine
from contextlib import AbstractContextManager, suppress
from typing import (
    TYPE_CHECKING,
    Any,
    ClassVar,
    TextIO,
    cast,
    override,
)

import anyio
from libpymux.agentic import caller_context
from libpymux.protocol import Field, Message, Mode, Packet
from libpymux.wire import MSGPACK_ZSTD, choose, encode_json, encode_msgpack
from prompt_toolkit.application.current import create_app_session, set_app
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import Input, PipeInput
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.input.vt100 import Vt100Input
from prompt_toolkit.output import ColorDepth
from prompt_toolkit.output.vt100 import Vt100_Output
from ptterm import Terminal
from pyte.cells import VS16, without_widening_selectors
from pyte.keep import Keep
from pyte.placeholders import PLACEHOLDER, blanked

from pymux.commands import CommandException
from pymux.commands.common import find_pane, terminal_of

from .actions import Origin, started_by
from .answer import Answer
from .audio import AudioLink
from .colors import ColorDetection, DefaultColors
from .commands import handle_command
from .enums import ExtendedKeys, Woke
from .graphics import CELL_SIZE_QUERY, ClientGraphics, draws_placeholders, draws_progress
from .ids import PaneId
from .keys import KittyVt100Parser
from .log import logger
from .nearest import NEAREST, nearest_theme, wanted_from
from .notifications import QUERY as NOTIFICATION_QUERY
from .notifications import QUERY_IDENTIFIER, is_click, read_actions, read_identifier, split_payload
from .options import Scope, SetOptionError
from .osc import NO_PROGRESS, Progress
from .packet_table import PacketTable
from .pipes import BrokenPipeError
from .rate import Rate
from .terminal_answers import SURVEY, TerminalAnswers
from .uploads import LIST_SECONDS, Listing, Upload

if TYPE_CHECKING:
    from pymux.introspect import Counters
    from pymux.main import ClientState, Pymux
    from pymux.pipes import PipeConnection
    from pymux.recording import Recording
    from pymux.session import Session
    from pymux.web.stream import PaneStream

__all__ = ["ServerConnection"]

# An OSC reply of the outer terminal: the code, and everything between
# the code and the terminator.
#: The answer to "CSI ? 2026 $ p" (DECRQM): "CSI ? 2026 ; <state> $ y".
#: A state of 0 means the terminal does not know the mode. 1 and 2 are
#: set and reset, and 4 means it is on and cannot be turned off; all
#: three say the terminal understands it.
_SYNCHRONIZED_REPLY_RE = re.compile(r"^\x1b\[\?2026;(\d+)\$y$")

#: Whether the terminal reports the mouse in pixels (SGR pixel mode,
#: 1016): asked with DECRQM, answered like 2026. Lillecarl/pymux#648.
#: Where a terminal draws a text emoji with U+FE0F after it. The probe
#: writes one at the start of row 2 and asks where the cursor is:
#: column 2 says one cell, column 3 says the terminal widened it, as
#: kitty alone of seven does. Row 2, because "CSI 1;2R" is shift+F3 as
#: well. The two cells are erased again, and the answer repaints.
#: Lillecarl/pymux#539.
EMOJI_WIDTH_PROBE = "\x1b7\x1b[2;1H✳️\x1b[6n\x1b[2;1H\x1b[2X\x1b8"
_EMOJI_WIDTH_REPLY_RE = re.compile(r"^\x1b\[2;(\d+)R$")
PIXEL_MOUSE_QUERY = "\x1b[?1016$p"
PIXEL_MOUSE_ON = "\x1b[?1016h"
PIXEL_MOUSE_OFF = "\x1b[?1016l"
_PIXEL_MOUSE_REPLY_RE = re.compile(r"^\x1b\[\?1016;(\d+)\$y$")

#: A status report and its answer: 0 is ready, 3 a malfunction, and
#: either says the terminal read everything sent before the report.
#: Lillecarl/pymux#259.
STATUS_REPORT = "\x1b[5n"
_STATUS_REPLY_RE = re.compile(r"^\x1b\[[03]n$")

_OSC_REPLY_RE = re.compile(r"^(?:\x1b\]|\x9d)(\d+);(.*?)(?:\x1b\\|\x9c|\x07)$", re.DOTALL)

#: The start of a terminal's answer to a clipboard query.
_CLIPBOARD_REPLY = "\x1b]52;"

#: How long a terminal has to answer a clipboard query, in seconds. A
#: paste waits this long once; a terminal that lets it pass is not asked
#: again on this connection. Most refuse reads by default, silently.
CLIPBOARD_READ_SECONDS = 1.0

#: The longest clipboard answer taken, in characters of base64. Past it
#: the answer is dropped, and nothing of it reaches a pane as keys.
CLIPBOARD_REPLY_LIMIT = 24 << 20

#: How many reads in a row may fail before a connection is closed.
#: A packet that cannot be read is answered and the next one is taken,
#: which is what makes one bad packet harmless; a read that fails again
#: and again is a connection nothing can use. Lillecarl/pymux#329.
FAILURES_THAT_END_A_CONNECTION = 5

#: How often to ask an attached client whether it is still there, in
#: seconds, and how many unanswered pings end it.
#:
#: Thirty seconds of a ghost, and one small packet per fifteen seconds
#: per attached client. It is a backstop and not the first line: over
#: SSH the client notices its own machine woke (Lillecarl/pymux#445)
#: and a server's sshd may reap the session too. This is what covers
#: the cases neither does -- a client process that was stopped, or a
#: transport with no liveness of its own. Lillecarl/pymux#446.
PING_INTERVAL = 15.0
PING_MISSES = 2

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


#: How long a window pane's frame may go unanswered before the next one
#: goes all the same. Lillecarl/pymux#643.
FRAME_ANSWER_SECONDS = 2.0


def _without_left(forwards: list[dict[str, Any]]) -> list[dict[str, Any]]:
    "A forwarding table without the time each forward has left."
    return [{key: value for key, value in one.items() if key != "left"} for one in forwards]


#: The method of `ServerConnection` that handles each kind of packet.
_PACKETS: PacketTable[ServerConnection] = PacketTable()


class ServerConnection:
    """
    For each client that connects, we have one instance of this class.
    """

    #: What a hot upgrade does with each attribute; `pyte.keep` says.
    #: The socket ends with the old server and the client reconnects
    #: (Lillecarl/pymux#409), so the attach handshake rebuilds most of
    #: this. Saved: the client's own record and when it came.
    KEEP: ClassVar[dict[str, Keep]] = {
        "client_state": Keep.SAVED,
        "created": Keep.SAVED,
        # A restarting client takes the old server's images off its
        # terminal, and the next frame places them again.
        "graphics": Keep.DROPPED,
        "pymux": Keep.REBUILT,
        "size": Keep.REBUILT,
        "hostname": Keep.REBUILT,
        "ttyname": Keep.REBUILT,
        "pid": Keep.REBUILT,
        "client_id": Keep.REBUILT,
        "environment": Keep.REBUILT,
        "colors": Keep.REBUILT,
        "default_colors": Keep.REBUILT,
        # Asked again of the terminal the client reattaches from.
        "clipboard_answers": Keep.REBUILT,
        "_clipboard_waiters": Keep.REBUILT,
        "_clipboard_asked": Keep.REBUILT,
        "_clipboard_partial": Keep.REBUILT,
        "kitty_source_flags": Keep.REBUILT,
        "_kitty_supported": Keep.REBUILT,
        "notification_actions": Keep.REBUILT,
        "_pointer_shape_sent": Keep.REBUILT,
        "_progress_sent": Keep.REBUILT,
        "answers_ping": Keep.REBUILT,
        "paced": Keep.REBUILT,
        "can_forward": Keep.REBUILT,
        "answers_forwards": Keep.REBUILT,
        "_next_forwards": Keep.DROPPED,
        "starts_browsers": Keep.REBUILT,
        "saves_files": Keep.REBUILT,
        "sends_files": Keep.REBUILT,
        # A `pymux receive` that waits ends when the client goes.
        "uploads": Keep.DROPPED,
        "listings": Keep.DROPPED,
        "may_attach": Keep.REBUILT,
        "forwards": Keep.DROPPED,
        "pipe_connection": Keep.DROPPED,
        # The client offers msgpack again on the connection it opens to
        # the next server.
        "_encode": Keep.DROPPED,
        "_pipeinput": Keep.DROPPED,
        "_stream": Keep.DROPPED,
        # The next server measures the client's round trip again.
        "_audio": Keep.DROPPED,
        "_tasks": Keep.DROPPED,
        "_closed": Keep.DROPPED,
        "_kitty_detection_pending": Keep.DROPPED,
        "_unanswered": Keep.DROPPED,
        # A frame in flight belongs to the connection it went out on.
        "_frames_out": Keep.DROPPED,
        # A new server asks its own reports, and learns again whether
        # the terminal answers them.
        "reports_out": Keep.DROPPED,
        # The client asks again when it reattaches to the new server.
        "terminal_said": Keep.DROPPED,
        "answers_reports": Keep.DROPPED,
        # The next server asks the terminal again.
        "pixel_mouse": Keep.DROPPED,
        "widens_text_emoji": Keep.DROPPED,
        "_frames_shown": Keep.DROPPED,
        # A recording holds Python objects of this build, so it ends
        # with it. `upgrade.py` writes it out first.
        "recording": Keep.DROPPED,
        # The pipe's counts start again on the next server's connection.
        "rate_down": Keep.DROPPED,
        "rate_up": Keep.DROPPED,
        "frames_sent": Keep.DROPPED,
        "rate_frames": Keep.DROPPED,
        "window_frames_sent": Keep.DROPPED,
        "rate_window_frames": Keep.DROPPED,
    }

    def __init__(self, pymux: Pymux, pipe_connection: PipeConnection, may_attach: bool = True) -> None:
        self.pymux = pymux

        self.pipe_connection = pipe_connection
        #: How a packet to this client is encoded: JSON until the client
        #: offers msgpack in `start-gui`. Lillecarl/pymux#633.
        self._encode = encode_json

        #: Whether a client on this connection may ask for the user
        #: interface. False for a connection that arrived over the
        #: socket of an integrated server: that server draws on the one
        #: terminal it runs in, and commands are all it serves to
        #: anybody else. Lillecarl/pymux#159.
        self.may_attach = may_attach

        self.size = Size(rows=20, columns=80)
        self._closed = False

        #: Whether this client's terminal reports the mouse in pixels.
        #: Lillecarl/pymux#648.
        self.pixel_mouse = False

        #: Whether this client's terminal draws a text emoji with U+FE0F
        #: two columns wide, where a pane holds one. Lillecarl/pymux#539.
        self.widens_text_emoji = False

        #: Window frames sent and not yet answered with `frame-shown`,
        #: for a client that answers; set while there are none.
        #: Lillecarl/pymux#641.
        self._frames_out = 0
        self._frames_shown = anyio.Event()
        self._frames_shown.set()

        #: Whether this client said its terminal answers a status
        #: report, the status reports asked and not yet answered, and
        #: whether one ever was. Lillecarl/pymux#259.
        self.paced = False
        self.reports_out = 0
        self.answers_reports = False

        #: Every reply this client's terminal gave. Lillecarl/pymux#725.
        self.terminal_said = TerminalAnswers()

        #: What `record-client` is recording of this client, or None.
        #: Lillecarl/pymux#612.
        self.recording: Recording | None = None

        #: What `#{client_rate_down}` and `#{client_rate_up}` read, off
        #: the pipe's counts. Lillecarl/pymux#678.
        self.rate_down = Rate()
        self.rate_up = Rate()
        #: The frames this client was sent, and what `#{client_fps}`
        #: reads off them. Lillecarl/pymux#596.
        self.frames_sent = 0
        self.rate_frames = Rate()
        #: The window panes' frames, which go out apart from the frames
        #: above, and what `#{client_window_fps}` reads off them.
        self.window_frames_sent = 0
        self.rate_window_frames = Rate()

        #: The scope the background work of this connection runs in.
        #: `serve()` opens it, so every task of a client ends when that
        #: client goes, and none of them can outlive the server: the
        #: group is a child of `Pymux.running`'s. Lillecarl/pymux#87.
        self._tasks: anyio.abc.TaskGroup | None = None

        self.client_state: ClientState | None = None

        #: The pane this connection is streaming, when it asked for one.
        #: A connection streams at most one pane: a caller that watches
        #: two opens two, so one slow viewer never holds up another.
        #: Lillecarl/pymux#461.
        self._stream: PaneStream | None = None

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

        #: The click actions of desktop notifications (OSC 99) that the
        #: outer terminal says it takes, from the detection's `p=?`.
        #: With `report`, a notification pymux makes itself goes out as
        #: OSC 99, so a click on it shows its pane. Lillecarl/pymux#779.
        self.notification_actions: frozenset[str] = frozenset()

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
            spawn=self._spawn,
            image_ids=pymux.outer_image_ids,
            reads_placeholders=lambda: draws_placeholders(self.graphics.terminal_version, self.colors.term),
        )
        # The client sent the shared memory probe with its detection,
        # and only the server reads the terminal's replies.
        # Lillecarl/pymux#634.
        self.graphics.on_shm = self._terminal_reads_shm

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
        self.environment: dict[str, str] = {}

        #: The terminal this client draws on, as the client named it,
        #: and the process it runs as. Both are of the client's own
        #: machine, so neither means anything here on its own: `name`
        #: puts the hostname in front. Lillecarl/pymux#335.
        self.ttyname = ""
        self.pid = 0

        #: Who the client says it is, the same on every attach of one
        #: client process, or "" from a client that did not say. A
        #: snapshot keys what belongs to a client by this, not by the
        #: connection, which an upgrade replaces. Lillecarl/pymux#399.
        self.client_id = ""

        #: Whether this client can forward a port, and what it is
        #: forwarding. Both come from the client: it holds the SSH
        #: connection, so the list here is a copy to draw and never the
        #: truth. Lillecarl/pymux#436.
        self.can_forward = False
        self.forwards: list[dict[str, Any]] = []
        #: Whether it sends its table again when asked, and what the
        #: next table that arrives sets. Lillecarl/pymux#487.
        self.answers_forwards = False
        self._next_forwards: anyio.Event | None = None
        #: Whether this client starts browsers. Lillecarl/pymux#653.
        self.starts_browsers = False
        #: Whether this client saves a download. Lillecarl/pymux#745.
        self.saves_files = False
        #: Whether this client sends files for `pymux receive`, and the
        #: requests that wait for its answer, by id. Lillecarl/pymux#743.
        self.sends_files = False
        self.uploads: dict[str, Upload] = {}
        self.listings: dict[str, Listing] = {}
        #: This client's sound, when it plays it. Lillecarl/pymux#657.
        self._audio: AudioLink | None = None

        #: Whether this client answers a ping, and how many have gone
        #: unanswered. Only a client that said it answers is ever
        #: dropped for not answering. Lillecarl/pymux#446.
        self.answers_ping = False
        self._unanswered = 0

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

        #: Whether this terminal answers a clipboard query: None until
        #: one is asked, False after one went unanswered, True once an
        #: answer came, even a late one. Lillecarl/pymux#383.
        self.clipboard_answers: bool | None = None
        #: The pastes waiting for an answer, oldest first: each one's
        #: event, and the list its text goes into.
        self._clipboard_waiters: list[tuple[anyio.Event, list[str]]] = []
        #: Set once a query went out, so a late answer is still caught.
        self._clipboard_asked = False
        #: The start of an answer whose end has not arrived yet.
        self._clipboard_partial = ""

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
            pixel_cell=self._pixel_cell,
        )

        # The shape of the pointer that this client was told about. A
        # client that never saw one is not told to reset it.
        self._pointer_shape_sent = ""

        # The progress this client's terminal was told, the same way.
        self._progress_sent = NO_PROGRESS

        self.pymux.serve_connection(self)

    def _spawn(self, coro: Coroutine[Any, Any, object]) -> None:
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

    async def _run_task(self, coro: Coroutine[Any, Any, object]) -> None:
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
            try:
                async with anyio.create_task_group() as tasks:
                    self._tasks = tasks
                    try:
                        await self._read_until_it_ends()
                    finally:
                        # What the client still had running goes with it.
                        self._tasks = None
                        tasks.cancel_scope.cancel()
            finally:
                await self.pipe_connection.linger()

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

        **It goes after the next frame, and not the moment it
        arrives.** `_FrameOutput` holds it until the renderer's flush
        and puts it behind the frame. A flush here made the clipboard
        change before the screen that showed the copy, and anything
        that reads the clipboard as a marker for what is drawn saw the
        two the wrong way round -- which is how `checks.pymux-pictures`
        photographed a pane that was still catching up. Behind the
        frame, its arrival says the frame has arrived too.

        tmux orders it the same way and for the same reason: a pane's
        "OSC 52" goes through `screen_write_setselection`
        (`input.c:3414-3440`), so it is dispatched as
        `tty_cmd_setselection` among the drawing commands
        (`tty.c:2140`). Only its popup case, which has no pane, writes
        straight to the terminal. Lillecarl/pymux#478.

        An OSC sequence moves no cursor and paints no cell, so it is
        safe anywhere between two frames. `pymux.osc.build_osc` has
        already checked the payload of the pane.

        **The temporary client of a socket command has no terminal.**
        It holds a client state like a real one, and its focused pane
        is the pane a person is looking at, so a sync that walks every
        client would hand it the shape of that pointer and the OSC of
        that pane -- read back as garbage after the answer of the
        command. Lillecarl/pymux#420.
        """
        if self.client_state is None or self.client_state.temporary:
            return
        output = self.client_state.output
        assert isinstance(output, _FrameOutput)
        output.write_after_frame(sequence)

    async def read_clipboard(self, selection: str = "c") -> str | None:
        """
        Ask this client's terminal for its clipboard, or None.

        `selection` is OSC 52's letter: "c" for the clipboard, "p" for
        the primary selection. The query goes behind the next frame, like
        a copy, so it is answered after any copy queued before it.

        None when the terminal does not answer in time, has been seen not
        to, or answered with nothing. Lillecarl/pymux#383.
        """
        if self.clipboard_answers is False or self.client_state is None or self.client_state.temporary:
            return None
        done = anyio.Event()
        answer: list[str] = []
        self._clipboard_waiters.append((done, answer))
        self._clipboard_asked = True
        self.forward_osc("\x1b]52;%s;?\x1b\\" % (selection,))
        self.pymux.invalidate(Woke.CLIPBOARD_ASKED)
        with anyio.move_on_after(CLIPBOARD_READ_SECONDS):
            await done.wait()
        if not done.is_set():
            with suppress(ValueError):
                self._clipboard_waiters.remove((done, answer))
            if self.clipboard_answers is None:
                self.clipboard_answers = False
            return None
        return answer[0] or None

    def _take_clipboard_answer(self, data: str) -> str:
        """
        Cut a clipboard answer out of the terminal's input, and return
        the rest for the key parser.

        **Not the key parser's job.** It caps a string reply at
        `MAX_STRING_LENGTH` and reads the whole prefix again on every
        character, so a clipboard of a few kilobytes would reach the pane
        as keys, after quadratic work. This sees the bytes first, and
        only once a query went out: nothing typed begins "ESC ] 52 ;".
        """
        if not self._clipboard_asked and not self._clipboard_partial:
            return data
        data = self._clipboard_partial + data
        self._clipboard_partial = ""
        start = data.find(_CLIPBOARD_REPLY)
        if start < 0:
            return data
        body = start + len(_CLIPBOARD_REPLY)
        ends = [(at, len(end)) for end in ("\x07", "\x1b\\") if (at := data.find(end, body)) >= 0]
        if not ends:
            if len(data) - start <= CLIPBOARD_REPLY_LIMIT:
                self._clipboard_partial = data[start:]
            return data[:start]
        end, length = min(ends)
        self._clipboard_answered(data[body:end])
        return data[:start] + self._take_clipboard_answer(data[end + length :])

    def _clipboard_answered(self, payload: str) -> None:
        "One answer: 'c;<base64>'. It goes to the oldest waiting paste."
        self.clipboard_answers = True
        _selection, _semicolon, encoded = payload.partition(";")
        try:
            text = base64.b64decode(encoded, validate=True).decode("utf-8", "replace")
        except ValueError:
            text = ""
        if self._clipboard_waiters:
            done, answer = self._clipboard_waiters.pop(0)
            answer.append(text)
            done.set()

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

    def set_progress(self, progress: Progress) -> None:
        """
        Show this client's terminal the progress of the pane it looks
        at, when it draws one: some terminals take any OSC 9 for a
        notification. Only a change goes out. Lillecarl/pymux#788.
        """
        if progress == self._progress_sent or not draws_progress(self.graphics.terminal_version):
            return
        self._progress_sent = progress
        self.forward_osc("\x1b]9;%s\x1b\\" % progress.sequence)

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
        # Every reply is kept, whatever is done with it below.
        # Lillecarl/pymux#725.
        self.terminal_said.heard(data)

        # The terminal reports the mouse in pixels if asked: from here on
        # it is asked, with the rest of the mouse. Lillecarl/pymux#648.
        pixel_mouse = _PIXEL_MOUSE_REPLY_RE.match(data)
        if pixel_mouse is not None:
            # Only with a cell to divide by. xterm reports pixels and, by
            # default, keeps its cell size to itself; its answer to that
            # query came before this one, or never. Lillecarl/pymux#651.
            # DECRPM: 1 set, 2 reset, 3 always set; 4 is always reset,
            # which iTerm2 answers, and 0 a mode it does not know.
            if pixel_mouse.group(1) in ("1", "2", "3") and self.graphics.cell_size_known:
                self._turn_on_pixel_mouse()
            return

        emoji_width = _EMOJI_WIDTH_REPLY_RE.match(data)
        if emoji_width is not None:
            widens = int(emoji_width.group(1)) > 2
            if widens != self.widens_text_emoji:
                self.widens_text_emoji = widens
                logger.info("The terminal of client %s draws a text emoji with U+FE0F wide.", self.client_id or "?")
                # The emoji already on its screen were written for the
                # other reading.
                self._request_repaint()
            return

        # The terminal read a frame. Lillecarl/pymux#259.
        if _STATUS_REPLY_RE.match(data):
            self._status_answered()
            return

        # The terminal processed a window pane's frame. Lillecarl/pymux#643.
        if self.graphics.frame_room is not None and self.graphics.is_frame_answer(data):
            self._frame_answered()
            return

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
                    self._match_the_theme()
                    self.pymux.terminals.sync_color_bases()
            return

        # The cell size, whenever it comes: asked again at each resize.
        # The client that attached before its terminal answered can
        # first speak for the panes now, and one whose cell changed
        # speaks again. Lillecarl/pymux#369, Lillecarl/pymux#774.
        new_cell = self.graphics.take_cell_size(data)
        if new_cell is not None:
            if new_cell:
                self.pymux.terminals.sync_cell_size()
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
            self.pymux.terminals.sync_keyboard_source_flags()
            return

        # The answer to the synchronised output query. A terminal that
        # holds a frame back does not need the cursor hidden while the
        # frame is painted, and hiding it restarts the blink: at a frame
        # rate that is a cursor which never blinks.
        synchronized = _SYNCHRONIZED_REPLY_RE.match(data)
        if synchronized is not None:
            if self.client_state is not None:
                self.client_state.output.synchronized_output = synchronized.group(1) != "0"
            return

        # The graphics query reply and the device attributes say
        # something about the images. The reply of the colour probe says
        # how many colours the terminal takes.
        self.graphics.handle_reply(data)
        self.colors.handle_reply(data)

        # The secondary attributes end in `c` too, and are the survey's,
        # never the fence. Lillecarl/pymux#725.
        if not data.endswith("c") or data.startswith("\x1b[>"):
            return

        # Device attributes reply: the fence of the detection. What did
        # not answer by now is not supported.
        self._kitty_detection_pending = False
        self._apply_color_depth()
        self._send_packet(
            {
                Field.CMD: Packet.KITTY_KEYBOARD,
                Field.DATA: {"supported": self.keyboard_is_supported()},
            }
        )
        # Enable the flags on this client's terminal as well. (Other
        # clients are kept in sync through `Terminals.sync_kitty_flags`,
        # which only sends on change, so a client that attaches after
        # the value settled would otherwise never hear it.)
        self._send_packet(
            {
                Field.CMD: Packet.KITTY_KEYBOARD,
                Field.DATA: {"flags": self.pymux.terminals.keyboard_flags_for_client()},
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
        if self.pymux.options.extended_keys is ExtendedKeys.ALWAYS:
            return True
        return self._kitty_supported

    def _route_notification(self, param: str) -> None:
        """
        Give the answer to a desktop notification to the pane that
        asked for it, under the identifier that the pane chose.

        An answer that names no notification of a pane is dropped. It
        must not reach a pane as key presses.

        A click shows this client the pane, and reaches the program only
        when it asked for clicks itself. Lillecarl/pymux#779.
        """
        metadata, _semicolon, text = split_payload(param)
        if read_identifier(metadata) == QUERY_IDENTIFIER:
            self.notification_actions = frozenset(read_actions(text))
            return

        routed = self.pymux.notifications.incoming(param)
        if routed is None:
            return

        route, param = routed
        pane = self.pymux.panes_by_id.get(route.pane_id)
        if pane is None:
            return
        if is_click(param):
            if route.focuses and self.client_state is not None:
                self.pymux.show_pane_on_click(self.client_state, pane)
            if not route.reports:
                return
        # A window pane sends no notification to answer.
        if not isinstance(pane.terminal, Terminal):
            return

        try:
            pane.terminal.process.write_input("\x1b]99;%s\x1b\\" % param)
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

    def _process(self, packet: Message) -> None:
        "Hand a packet from the client to the method that handles its kind."
        _PACKETS.handle(self, packet)

    @_PACKETS.handles(Packet.WIRE)
    def _on_wire(self, packet: Message) -> None:
        "The client's writer turned to msgpack. The pipe's reader has turned with it already."

    @_PACKETS.handles(Packet.RUN_COMMAND)
    def _on_run_command(self, packet: Message) -> None:
        # Handle this in a task. The command handler can produce output
        # that has to be sent back to the client.
        self._spawn(self._run_command(packet))

    @_PACKETS.handles(Packet.STREAM_PANE)
    def _on_stream_pane(self, packet: Message) -> None:
        # "Send me this pane as frames." A stream holds the connection
        # and writes many packets, so it is not a command that prints.
        self._spawn(self._stream_pane(packet))

    @_PACKETS.handles(Packet.SOUND, Packet.ECHO, Packet.MICROPHONE, Packet.BEHIND)
    def _on_sound(self, packet: Message) -> None:
        # Sound from a client that plays it. Lillecarl/pymux#657.
        if self._audio is not None:
            self._audio.heard(packet)

    @_PACKETS.handles(Packet.STREAM_IN)
    def _on_stream_in(self, packet: Message) -> None:
        # One input message of a stream, which the stream itself reads:
        # a packet that arrives with no stream running has nowhere to go.
        if self._stream is None:
            logger.warning("Input for a stream that is not running. Ignoring.")
        else:
            self._stream.take(packet.get(Field.DATA, ""))

    @_PACKETS.handles(Packet.IN)
    def _on_input(self, packet: Message) -> None:
        if self.recording is not None:
            self.recording.hear("in", packet[Field.DATA])
        self._pipeinput.send_text(self._take_clipboard_answer(packet[Field.DATA]))
        # Input newer than a scheduled redraw makes that frame
        # stale: the renderer skips it and asks for another one
        # instead, so this is handled first. Lillecarl/pymux#524.
        if self.client_state is not None:
            self.client_state.input_tick += 1

    @_PACKETS.handles(Packet.KITTY_DETECT)
    def _on_kitty_detect(self, packet: Message) -> None:
        # The client queried its terminal for kitty keyboard protocol
        # support. (The replies come back as input on this connection.)
        self._kitty_detection_pending = True
        # The server asks this one itself, so a client of any age
        # gets a pointer to the pixel. Lillecarl/pymux#648.
        # And the survey, whose answers are only kept.
        # Lillecarl/pymux#725. And what a desktop notification's click
        # can do (OSC 99 `p=?`), which a client of an older build never
        # asked. Lillecarl/pymux#779.
        self._write_output_raw(
            PIXEL_MOUSE_QUERY + EMOJI_WIDTH_PROBE + SURVEY + "\x1b]99;" + NOTIFICATION_QUERY + "\x1b\\"
        )
        self._flush_output()

    @_PACKETS.handles(Packet.SIZE)
    def _on_size(self, packet: Message) -> None:
        rows, columns = packet[Field.DATA]
        if self.recording is not None and (rows, columns) != tuple(self.size):
            self.recording.hear("size", "%dx%d" % (columns, rows))
        self.size = Size(rows=rows, columns=columns)
        self.pymux.invalidate(Woke.CLIENT_RESIZED)
        # A SIGWINCH comes for pixels alone too: the terminal moved
        # to a display of another density. Lillecarl/pymux#774.
        if self.graphics.cell_size_known:
            self._write_output_raw(CELL_SIZE_QUERY)
            self._flush_output()

    @_PACKETS.handles(Packet.START_GUI)
    def _on_start_gui(self, packet: Message) -> None:
        if not self.may_attach:
            self._spawn(self._refuse_the_attach())
            return

        # The client reads msgpack, perhaps inside zstd: say which
        # in JSON, and write it from the next packet on.
        # `libpymux.wire`.
        wire = choose(packet.get(Field.WIRE), self.pipe_connection.can_compress)
        if wire is not None and self._encode is encode_json:
            self._send_packet(
                {Field.CMD: Packet.WIRE, Field.WIRE: wire},
                compress_after=wire == MSGPACK_ZSTD,
            )
            self._encode = encode_msgpack

        # Window frames as raw pixels, which the client encodes for
        # its own terminal. Only in msgpack, which carries bytes.
        # Lillecarl/pymux#634.
        if packet.get(Field.FRAMES) and self._encode is encode_msgpack:
            self.graphics.send_frame = self._send_frame
            # And the codecs it decodes, for `window-codec`.
            # Lillecarl/pymux#639.
            self.graphics.codecs = frozenset(packet.get(Field.CODECS) or ())
            # A client that says when it wrote a frame gets the next
            # one after that: the socket draining says only that the
            # kernel took it, and over SSH megabytes of frames wait
            # in the buffers past it, with every key's echo behind
            # them. Lillecarl/pymux#641.
            if packet.get(Field.SHOWS):
                self.graphics.frame_room = self._frames_all_shown

        detach_other_clients = bool(packet[Field.DETACH_OTHERS])
        # `attach -x`: the others leave, and the terminals they were
        # in close. Lillecarl/pymux#347.
        hang_up_others = bool(packet.get(Field.HANG_UP_OTHERS))
        forced = packet[Field.COLOR_DEPTH]
        term = packet[Field.TERM]

        # The colour depth of the outer terminal. A flag of the
        # client forces one; otherwise the environment gives the
        # first answer and the probe can raise it later.
        self.colors.forced = ColorDepth(forced) if forced else None
        self.colors.term = term
        self.colors.colorterm = packet.get(Field.COLORTERM, "")
        self.hostname = packet.get(Field.HOSTNAME, "")
        # Whether `forward-port` can reach this client at all. Only
        # the SSH client says yes. Lillecarl/pymux#436.
        self.can_forward = bool(packet.get(Field.FORWARDS))
        self.answers_forwards = bool(packet.get(Field.REPORTS))
        self.starts_browsers = bool(packet.get(Field.BROWSERS))
        self.saves_files = bool(packet.get(Field.SAVES_FILES))
        self.sends_files = bool(packet.get(Field.SENDS_FILES))
        self.answers_ping = bool(packet.get(Field.PINGS))
        self.paced = bool(packet.get(Field.PACED))
        self.environment = packet.get(Field.ENVIRONMENT) or {}
        self.ttyname = packet.get(Field.TTYNAME, "")
        self.pid = packet.get(Field.PID) or 0
        self.client_id = packet.get(Field.CLIENT_ID) or ""

        self._create_app(color_depth=self.colors.depth, term=term)

        # `pymux attach -r`. The client state exists from
        # `_create_app` on, and the flag has to be on it before the
        # first key arrives. `attach-session -r` is the same two
        # lines for a client that is already here.
        # Lillecarl/pymux#467.
        if packet.get(Field.READ_ONLY) and self.client_state is not None:
            self.client_state.read_only = True
            self.client_state.ignore_size = True

        if detach_other_clients or hang_up_others:
            self._detach_the_others(hang_up=hang_up_others)

        # What this client's own configuration file said about it.
        # Applied before the first frame, so nothing draws with a
        # theme the person did not choose and then change.
        # Lillecarl/pymux#223.
        self._take_client_options(packet.get(Field.CLIENT_OPTIONS) or [])

        # After the options: what the client had set while it ran
        # wins over what its file says. Lillecarl/pymux#409.
        if self.client_state is not None:
            self.pymux.welcome_back(self.client_state, self.client_id)

        # The session this client landed on takes the names that
        # follow a client: a display, an agent, a session bus.
        # `attach_client_to` does it for every later move between
        # sessions; this is the first attach, which does not go
        # through it. Lillecarl/pymux#271.
        if self.client_state is not None:
            self.pymux.take_environment_from(self.client_state)

        # Sound, on this connection, for a client that plays it.
        # Lillecarl/pymux#657.
        if packet.get(Field.AUDIO) and self._encode is encode_msgpack and self._audio is None:
            self._spawn(self._carry_audio())

        if self.answers_ping:
            self._spawn(self._watch_the_client())

    @_PACKETS.handles(Packet.BROWSER_STARTED)
    def _on_browser_started(self, packet: Message) -> None:
        # A URL that the client of this connection could not open. The
        # request went out as a status line here, so the answer goes
        # there too, and it names the URL to copy.
        self.pymux.browsers.started(packet)

    @_PACKETS.handles(Packet.BROWSER_ENDED)
    def _on_browser_ended(self, packet: Message) -> None:
        self.pymux.browsers.ended(packet[Field.BROWSER])

    @_PACKETS.handles(Packet.FILE_SAVED)
    def _on_file_saved(self, packet: Message) -> None:
        # Where the client saved a pane's download, or why it did not.
        # Only the client knows the path. Lillecarl/pymux#743.
        if self.client_state is not None:
            self.client_state.message = str(packet.get(Field.MESSAGE) or "")
            self.client_state.app.invalidate()

    @_PACKETS.handles(Packet.UPLOAD, Packet.UPLOAD_DONE)
    def _on_upload(self, packet: Message) -> None:
        # A file `pymux receive` asked this client for, a part at a
        # time. Only the connection that was asked holds the request,
        # so another client cannot answer it. Lillecarl/pymux#743.
        upload = self.uploads.get(str(packet.get(Field.REQUEST)))
        if upload is not None:
            upload.take(packet)

    @_PACKETS.handles(Packet.FILES_LISTED)
    def _on_files_listed(self, packet: Message) -> None:
        listed = self.listings.pop(str(packet.get(Field.REQUEST)), None)
        if listed is not None:
            listed.take(packet)

    @_PACKETS.handles(Packet.REDRAW)
    def _on_redraw(self, packet: Message) -> None:
        # The client drew a question over the frame, and it is gone.
        if self.client_state is not None:
            self.pymux.redraw(self.client_state)

    @_PACKETS.handles(Packet.OPEN_FAILED)
    def _on_open_failed(self, packet: Message) -> None:
        if self.client_state is not None:
            self.client_state.message = "Could not open %s in a browser on this machine." % (packet[Field.DATA],)

    @_PACKETS.handles(Packet.PONG)
    def _on_pong(self, packet: Message) -> None:
        # The client is still there. Any answer clears the count, so a
        # client that is merely slow keeps its place.
        self._unanswered = 0

    @_PACKETS.handles(Packet.FRAME_SHOWN)
    def _on_frame_shown(self, packet: Message) -> None:
        self._frame_answered()

    @_PACKETS.handles(Packet.FORWARDS)
    def _on_forwards(self, packet: Message) -> None:
        # What this client is forwarding, after it changed or after the
        # link came back. The server keeps a copy and never the truth:
        # it holds no SSH connection, so only the client can say whether
        # a listener is really open. Lillecarl/pymux#436.
        reported = packet.get(Field.DATA) or []
        said = packet.get(Field.MESSAGE)
        arrived, self._next_forwards = self._next_forwards, None

        if said and self.client_state is not None:
            self.client_state.message = said

        # Every SSH client reports once as it attaches, nearly
        # always with nothing forwarded. A frame that draws the
        # same screen again is a fault of its own
        # (Lillecarl/pymux#117), so the unchanged case asks for
        # none. The time left moves on every report and only a
        # listing reads it, so it is no change.
        changed = _without_left(reported) != _without_left(self.forwards)
        self.forwards = reported
        if changed or said:
            self.pymux.invalidate(Woke.FORWARDS_CHANGED)
        if arrived is not None:
            arrived.set()

    async def fresh_forwards(self) -> None:
        """
        Ask the client for its table now, and wait until it arrives.

        **Every call asks.** A request the client never answered leaves
        its event unset, and a caller that only waited on it would wait
        out every listing after it.
        """
        if self._next_forwards is None:
            self._next_forwards = anyio.Event()
        arrived = self._next_forwards
        self._send_packet({Field.CMD: Packet.REPORT_FORWARDS})
        await arrived.wait()

    def _detach_the_others(self, hang_up: bool = False) -> None:
        """
        What `attach -d` means: take this session from whoever else
        holds it. `attach -x` is the same set of clients, told to hang
        up as well as to leave. Lillecarl/pymux#347.

        **The session, and not the server.** `Pymux.clients_on` holds
        that rule, for this and for `attach-session -d` alike.
        Lillecarl/pymux#345, Lillecarl/pymux#349.

        **This runs after `_create_app`**, because until then this
        connection has no session to compare the others against.
        """
        if self.client_state is None:
            return

        others = self.pymux.clients_on(self.client_state.session, except_for=self.client_state)
        for other in others:
            connection = getattr(other, "connection", None)
            if connection is not None:
                connection.detach_and_close(hang_up=hang_up)

    def _match_the_theme(self) -> None:
        """
        Choose again which known theme this terminal is nearest to.

        **After every reply, not once.** The answers arrive one at a
        time -- the background, then the foreground, then sixteen
        palette entries -- so a match made on the first of them is a
        match made on a third of what the terminal says. Each reply
        can move it, and the last one settles it.

        A client that named a theme is left alone. A terminal that
        reports a theme change later comes through here as well, and
        the match follows it. Lillecarl/pymux#346.
        """
        if self.client_state is None or self.client_state.options.theme != NEAREST:
            return

        found = nearest_theme(wanted_from(self.default_colors))
        if found is None or found == self.client_state.matched:
            return

        self.client_state.matched = found
        self.pymux.invalidate(Woke.THEME_WAS_CHOSEN)

    def _take_client_options(self, announced: list[Any]) -> None:
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
                option = self.pymux.option_tables[Scope.CLIENT][name]
            except KeyError, TypeError, ValueError:
                self.client_state.message = "Invalid client option: %s" % (one,)
                continue

            try:
                option.set_value(self.pymux, value, self.client_state)
            except SetOptionError as e:
                # The line as they wrote it, then the reason. A person
                # reading "Expecting one of" needs to see what they
                # typed to know which line to go and fix.
                self.client_state.message = "%s %s: %s" % (name, value, e.message)

        self.pymux.terminals.sync_color_bases()

    def _send_packet(self, data: object, compress_after: bool = False) -> None:
        """
        Send packet to client. `compress_after` turns the stream to zstd
        after this one (`PipeConnection.write`).
        """
        if self._closed:
            return
        # Every byte for the client's terminal passes here, the frames
        # and what follows them alike.
        if self.recording is not None and isinstance(data, dict) and data.get(Field.CMD) == Packet.OUT:
            self.recording.hear("out", data[Field.DATA])
        # Serialised now: the write runs later, and the packet is what
        # was true when it was sent, in the format of the moment.
        self._spawn(self._write(self._encode(data), compress_after))

    async def say_restarting(self, wait: float, why: str) -> None:
        """
        Tell this client that the next server comes on the same socket,
        how many seconds to wait for it, and why, for the screen it
        draws meanwhile. Awaited, so the packet is written before the
        server stops. Lillecarl/pymux#409.
        """
        await self._write_packet({Field.CMD: Packet.RESTARTING, Field.WAIT: wait, Field.MESSAGE: why})
        if self._closed:
            # `_write` lets a client go on a broken pipe or a full
            # buffer, and that client leaves for good instead of
            # waiting. Lillecarl/pymux#766.
            logger.warning("Client %s went before the restart notice reached it.", self.client_id or "?")

    async def _refuse_the_attach(self) -> None:
        """
        Tell this client that it cannot have the user interface, and
        close the connection. Lillecarl/pymux#159.

        **The write is awaited, and `_send_packet` does not wait.** That
        one spawns the write into the scope of this connection, and
        closing cancels that scope: the reason the client came would
        reach it only if the cancel lost the race.
        """
        await self._write_packet({Field.CMD: Packet.OUT, Field.DATA: CANNOT_ATTACH})
        # And a code to leave with. A client that attached reads this
        # too now, so a script hears the refusal rather than reading a
        # zero and calling it attached. Lillecarl/pymux#332.
        await self._write_packet({Field.CMD: Packet.EXIT, Field.CODE: 1})

        logger.info("A client asked to attach to a server that serves one terminal.")
        self.detach_and_close()

    async def _stream_pane(self, packet: dict[str, Any]) -> None:
        """
        Send a pane to this client as frames, until it goes away.

        **The frames are the protocol and this envelope is not.** A relay
        passes `data` on without reading it, so the packet names here stay
        ours to change. Lillecarl/pymux#461.
        """
        from pymux.web.stream import PaneStream

        target = packet.get(Field.PANE)
        pane = find_pane(self.pymux, target) if target else None
        try:
            if pane is None:
                raise CommandException("can't find pane: %s" % (target,))
            terminal = terminal_of(pane)
        except CommandException as refused:
            await self._write_packet({Field.CMD: Packet.ERR, Field.DATA: "%s\n" % (refused.message,)})
            await self._write_packet({Field.CMD: Packet.EXIT, Field.CODE: 1})
            self._close_connection()
            return

        async def send(frame: dict[str, Any]) -> None:
            await self._write_packet({Field.CMD: Packet.STREAM_OUT, Field.DATA: json.dumps(frame)})

        self._stream = PaneStream(self.pymux, pane, terminal, send, writable=bool(packet.get(Field.WRITABLE)))
        try:
            await self._stream.run()
        except BrokenPipeError:
            pass  # The viewer went away, which is how a stream usually ends.
        finally:
            self._stream = None
            self._close_connection()

    async def _carry_audio(self) -> None:
        """
        Carry this client's sound, until the connection goes. Only in
        msgpack, which carries bytes. `AudioLink` says why on this
        connection and not one of its own. Lillecarl/pymux#657.
        """
        link = AudioLink(self)
        self._audio = link
        self.pymux.audio.link_opened(link)
        try:
            await link.run()
        except BrokenPipeError:
            pass
        finally:
            self._audio = None
            self.pymux.audio.link_closed(link)

    async def _run_command(self, packet: dict[str, Any]) -> None:
        """
        Execute a run command from the client.
        """
        create_temp_cli = self.client_state is None

        if create_temp_cli:
            # If this client doesn't have a CLI. Create a Fake CLI where the
            # window containing this pane, is the active one. (The CLI instance
            # will be removed right after the command handler ran, so it
            # doesn't hurt too much and makes the code easier.)
            pane_id = packet.get(Field.PANE_ID)
            pane = self.pymux.panes_by_id.get(PaneId(int(pane_id))) if pane_id is not None else None

            # The session of the pane the command was typed in. Without
            # it the fake client lands on the session a person looked at
            # last, so `pymux new-window` typed in a pane of one session
            # would open the window in another. Lillecarl/pymux#323.
            session = self.pymux.session_holding(pane) if pane is not None else None

            self._create_app(start=False, session=session)
            client_state = self.client_state
            if client_state is not None and pane is not None and session is not None:
                client_state.caller_pane_id = pane.pane_id
                with set_app(client_state.app):
                    session.arrangement.set_active_window_from_pane_id(pane.pane_id)

            # A command can be the first touch of a server nobody
            # attached to yet: the bindings and the first window it
            # would have gotten then arrive now. Later ones noop.
            # Under the temporary application, so the new pane focuses.
            if client_state is not None:
                with set_app(client_state.app):
                    await self.pymux.startup()

        pymux = self.pymux
        # Who sent this, and from where: the client resolves both fresh
        # on every invocation and says them in the packet, because the
        # server's own environment names nothing about the caller.
        # `libpymux/agentic.py` holds the names.
        pymux.answer = reply = Answer(caller=caller_context(packet.get(Field.ENVIRONMENT), packet.get(Field.CWD)))

        client_state = self.client_state
        if client_state is None:
            self._close_connection()
            return

        with set_app(client_state.app):
            try:
                # **The client waits and the server does not.** A
                # command that has to wait answers with a coroutine,
                # and this route is the one that awaits it: the person
                # who typed `pymux wait-for done` waits for it, while
                # every other client of this server keeps running.
                # Lillecarl/pymux#87.
                with started_by(Origin.SOCKET):
                    answer = handle_command(pymux, packet[Field.DATA])
                    if answer is not None:
                        await answer
            finally:
                # Send the output of the command back to the client, and
                # close the connection.
                pymux.answer = None

                try:
                    if reply.output:
                        await self._write_packet({Field.CMD: Packet.OUT, Field.DATA: "\n".join(reply.output) + "\n"})
                    if reply.errors or reply.warnings:
                        said = [*reply.warnings, *reply.errors]
                        await self._write_packet({Field.CMD: Packet.ERR, Field.DATA: "\n".join(said) + "\n"})
                    exit_code = reply.exit_code
                    if exit_code is None:
                        exit_code = 1 if reply.errors else 0
                    await self._write_packet({Field.CMD: Packet.EXIT, Field.CODE: exit_code})
                except BrokenPipeError:
                    pass
                self._close_connection()

    def ask_for_files(self, paths: list[str], pane: str) -> Upload:
        """
        Ask this client for files on the person's machine, for
        `pymux receive` in `pane`. The caller removes the request from
        `uploads` when it stops waiting. Lillecarl/pymux#743.
        """
        upload = Upload(paths)
        if self._closed:
            upload.end("the client went before it answered")
            return upload
        self.uploads[upload.request] = upload
        self._send_packet(upload.packet(pane))
        return upload

    def stop_waiting_for_files(self, upload: Upload) -> None:
        """
        Forget a request whose `pymux receive` stopped waiting. One the
        client has not finished is withdrawn, or its question stays up
        and a later "y" sends a file nobody takes.
        """
        self.uploads.pop(upload.request, None)
        if upload.ended or self._closed:
            return
        with suppress(BrokenPipeError):
            self._send_packet({Field.CMD: Packet.FILES_WITHDRAWN, Field.REQUEST: upload.request})

    async def list_files(self, path: str) -> list[str]:
        """
        The names the client lets show in a directory on the person's
        machine, for completing `pymux receive`. Empty when it answers
        nothing within `LIST_SECONDS`, or goes.
        """
        if self._closed:
            return []
        listed = Listing(path)
        self.listings[listed.request] = listed
        self._send_packet(listed.packet())
        with anyio.move_on_after(LIST_SECONDS):
            await listed.answered.wait()
        self.listings.pop(listed.request, None)
        return listed.names

    async def answer(self, exit_code: int) -> None:
        "Send the exit code of a command, for one that ends the server before it returns."
        with suppress(BrokenPipeError):
            await self._write_packet({Field.CMD: Packet.EXIT, Field.CODE: exit_code})

    async def _write_urgent(self, data_obj: object) -> None:
        "Write a packet ahead of every write that waits for the connection: sound."
        if self._closed:
            return
        try:
            await self.pipe_connection.write(self._encode(data_obj), urgent=True)
        except BrokenPipeError:
            self.detach_and_close()

    async def _write_packet(self, data_obj: object) -> None:
        """
        Write a packet to the client. (This waits for the write to complete.)
        """
        if self._closed:
            return
        await self._write(self._encode(data_obj))

    def _terminal_reads_shm(self) -> None:
        logger.info("The terminal of client %s reads window frames from shared memory.", self.client_id or "?")
        self._send_packet({Field.CMD: Packet.SHM})

    async def _send_frame(self, parts: list[Message]) -> None:
        """
        Write a window pane's frame, one part at a time.

        **Each part is awaited before the next is encoded**, so a packet
        sent meanwhile -- a key's echo, a line of text -- takes the write
        lock between two parts, and never waits behind the whole frame.
        What the terminal was sent before goes first. Lillecarl/pymux#634.
        """
        self._flush_output()
        self.window_frames_sent += 1
        if self.graphics.frame_room is not None:
            self._frames_out += 1
            if self._frames_shown.is_set():
                self._frames_shown = anyio.Event()
        for part in parts:
            if self._closed:
                return
            await self._write(self._encode(part))

    def _pixel_cell(self) -> tuple[int, int, int] | None:
        "The cell size mouse reports are divided by, while they come in pixels, and their first pixel's number."
        if not self.pixel_mouse or not self.graphics.cell_size_known:
            return None
        origin = 0 if (self.graphics.terminal_version or "").startswith("kitty") else 1
        return self.graphics.cell_width, self.graphics.cell_height, origin

    def _turn_on_pixel_mouse(self) -> None:
        if self.pixel_mouse:
            return
        self.pixel_mouse = True
        logger.info("The terminal of client %s reports the mouse in pixels.", self.client_id or "?")
        app = self.client_state.app if self.client_state is not None else None
        if app is not None and app.renderer._mouse_support_enabled:
            self._write_output_raw(PIXEL_MOUSE_ON)
            self._flush_output()

    @property
    def pointer_fraction(self) -> tuple[float, float]:
        "Where in its cell the client's last mouse report was. Lillecarl/pymux#648."
        return self._pipeinput.pointer_fraction

    def _frame_answered(self) -> None:
        self._frames_out = max(0, self._frames_out - 1)
        if not self._frames_out:
            self._frames_shown.set()

    async def _frames_all_shown(self) -> None:
        """
        Wait until the client answered every window frame it was sent, or
        went. A terminal that never answers is let go after
        `FRAME_ANSWER_SECONDS`, so it costs frames and never the window.
        """
        with anyio.move_on_after(FRAME_ANSWER_SECONDS) as waited:
            while self._frames_out and not self._closed:
                await self._frames_shown.wait()
        if waited.cancelled_caught:
            logger.warning("Client %s did not answer %d window frames.", self.client_id or "?", self._frames_out)
            self._frames_out = 0
            self._frames_shown.set()

    async def frame_drained(self) -> None:
        """
        Wait until this client's terminal took what it was sent: every
        window frame answered, for a client that answers, and the
        socket empty. Lillecarl/pymux#641.
        """
        await self.pipe_connection.drained()
        if self.graphics.frame_room is not None:
            await self._frames_all_shown()

    async def _write(self, frame: bytes, compress_after: bool = False) -> None:
        "Write one encoded packet; a client that is gone is let go."
        try:
            await self.pipe_connection.write(frame, compress_after)
        except BrokenPipeError:
            self.detach_and_close()

    def _frame_went_out(self) -> None:
        self.frames_sent += 1

    def _report_after_frame(self) -> str:
        """
        What goes behind a frame: a status report, for a client whose
        terminal answers one. The answer arrives once the terminal read
        everything before it, which over ssh is the only thing that
        says a frame reached the person. Lillecarl/pymux#259.
        """
        if not self.paced:
            return ""
        self.reports_out += 1
        return STATUS_REPORT

    def _status_answered(self) -> None:
        """
        The terminal read a frame. A frame held back for it can draw.
        """
        self.reports_out = max(0, self.reports_out - 1)
        self.answers_reports = True
        if self.client_state is not None and not self.reports_out:
            self.client_state.app.invalidate()

    @property
    def waits_for_terminal(self) -> bool:
        """
        Whether the next frame waits: a frame is in flight to a terminal
        that has answered before. One that never answered is never
        waited for, so a terminal that ignores the report costs nothing;
        one that answered and went quiet holds one frame and no more,
        which is the dead link of a phone that lost its signal.
        """
        return self.answers_reports and self.reports_out > 0

    def _create_app(
        self,
        color_depth: ColorDepth = ColorDepth.DEPTH_8_BIT,
        term: str = "xterm",
        start: bool = True,
        session: Session | None = None,
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
        stdout = (
            _SocketStdout(self._send_packet, self.pymux.counters, self._frame_went_out, self._report_after_frame)
            if start
            else _NoStdout()
        )
        output = _FrameOutput(
            cast(TextIO, stdout),
            lambda: self.size,
            term=term,
        )
        output.keeps_placeholders = lambda: self.graphics.forwards_placeholders
        output.pixel_mouse = lambda: self.pixel_mouse
        output.narrows_text_emoji = lambda: self.widens_text_emoji

        client_state = self.pymux.add_client(
            # A proxy: what it lacks of an `Input` it forwards to its pipe.
            input=cast("Input", self._pipeinput),
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

        def frame_begins(_app: object) -> None:
            output.frame_begins()

        def frame_ends(_app: object) -> None:
            output.frame_ends()

        client_state.app.before_render += frame_begins
        client_state.app.after_render += frame_ends

        if start:

            async def run() -> None:
                try:
                    # The first window, before the first frame: a client
                    # that arrives finds a session with a window, the
                    # way attaching always did. Under its application,
                    # so the new pane is focused where it is looked at.
                    with set_app(client_state.app):
                        await self.pymux.startup()
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
        if self.recording is not None:
            self.pymux.finish_recordings([self])
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
        self._frames_shown.set()
        # A `pymux receive` waiting for this client would wait for ever.
        for upload in self.uploads.values():
            upload.end("the client went before it answered")
        self.uploads.clear()
        for listed in self.listings.values():
            listed.answered.set()
        self.listings.clear()

        # The background work of this connection ends with the read
        # loop that `serve` runs: leaving that scope cancels the group.
        # This is called from inside those tasks, so cancelling here
        # would cancel the caller before it finished closing.

        # Close input pipe and remove connection from eventloop.
        self._pipeinput.close()
        with suppress(Exception):
            self.pipe_connection.close()

    def suspend_client_to_background(self) -> None:
        """
        Ask the client to suspend itself. (Like, when Ctrl-Z is pressed.)
        """
        self._send_packet({Field.CMD: Packet.SUSPEND})

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

    async def _watch_the_client(self) -> None:
        """
        Drop a client that stops answering.

        **A closed socket is the only thing that dropped a client
        before this**, and a socket can stay open long after the
        person is gone: a laptop that sleeps leaves a dead TCP
        connection that the far sshd holds until it gives up, and the
        forwarded unix socket here stays open behind it. Nothing else
        asks, because a client that is attached and quiet sends
        nothing -- it reports its size only when the size changes.

        What the ghost costs is not the socket. `window-size smallest`
        is the default, so a client holding a stale terminal size
        keeps the window that size for whoever is really looking at
        it. Lillecarl/pymux#446.

        **Only a client that said it answers is ever dropped.**
        `start-gui` carries that, the way it carries whether the
        client can forward a port: a client that does not know the
        packet would otherwise be reaped for staying quiet, which is
        the one mistake this must not make.

        A pong clears the count, so a slow link keeps its place and
        only silence ends it.
        """
        while True:
            await anyio.sleep(PING_INTERVAL)

            if self._unanswered >= PING_MISSES:
                logger.info("Dropping %s: %d pings unanswered.", self.name, self._unanswered)
                self.detach_and_close()
                return

            self._unanswered += 1
            await self._write_packet({Field.CMD: Packet.PING})

    def detach_and_close(self, hang_up: bool = False) -> None:
        """
        Take this client off its session and close the connection.

        `hang_up` is `attach -x`: the client leaves and then hangs up
        the process that started it, so the terminal it was in closes.
        The client hears that in the `exit` packet, which is the one
        packet that already means "the server let you go" -- so the
        SSH client reads it as an ending and not as a link that went,
        and does not come back. Lillecarl/pymux#332,
        Lillecarl/pymux#347.

        **The packet has to be written before the close.**
        `_send_packet` spawns the write into the scope of this
        connection, and closing cancels that scope, so the two in a row
        would race and the client would usually hear nothing. This
        spawns one task that writes and then closes.
        """
        # A connection with no scope to run the write in cannot be told
        # anything, and a hangup that waited for one would never close
        # it. Leaving is the part that still works.
        if not hang_up or self._tasks is None:
            self._close_connection()
            return

        async def hang_up_and_close() -> None:
            await self._write_packet({Field.CMD: Packet.EXIT, Field.CODE: 0, Field.HANG_UP: True})
            self._close_connection()

        self._spawn(hang_up_and_close())


class _SocketStdout:
    """
    Stdout-like object that writes everything through the unix socket to the
    client.
    """

    def __init__(
        self,
        send_packet: Callable[[Message], None],
        counters: Counters | None = None,
        frame_went_out: Callable[[], None] | None = None,
        after_frame: Callable[[], str] | None = None,
    ) -> None:
        self.send_packet = send_packet
        self.counters = counters
        #: Told of each frame, for the count of the one client it goes to.
        self.frame_went_out = frame_went_out
        #: What goes behind each frame that holds anything.
        self.after_frame = after_frame
        self._buffer: list[str] = []

    def write(self, data: str) -> int:
        self._buffer.append(data)
        return len(data)

    def flush(self) -> None:
        # One flush is one frame: the renderer writes a whole frame and
        # then flushes it. So this is where a server can count what it
        # actually sent, which the invalidates cannot say -- several of
        # those become one frame.
        written = "".join(self._buffer)
        if written and self.after_frame is not None:
            written += self.after_frame()
        if self.counters is not None:
            self.counters.frame_went_out(len(written))
        if self.frame_went_out is not None:
            self.frame_went_out()

        self.send_packet({Field.CMD: Packet.OUT, Field.DATA: written})
        self._buffer = []

    def isatty(self) -> bool:
        return True


class _FrameOutput(Vt100_Output):
    """
    A client's output that sends what a pane passed on after the frame.

    `forward_osc` queues a sequence here and `flush` puts it at the end
    of what goes out, behind the frame the renderer wrote. A sequence
    written when it arrived stood in front of that frame, so a
    clipboard changed before the screen showing the copy, and a harness
    that waits for a pane's notification as a fence still had to wait
    for silence after it. Lillecarl/pymux#579.

    **Only a frame's own flush carries it**: one made between the
    application's `before_render` and `after_render`, which
    `frame_begins` and `frame_ends` mark. Not every flush ends a frame:
    a bell, a reset of the renderer and graphics flush on their own, and
    a frame that the stale-frame check skips flushes nothing at all.
    Carried by any flush, the vterm middleman's fence went out ahead of
    the frame that drew the payload, and libvterm read an empty screen.
    Lillecarl/pymux#611.

    A pane draws an image cell as kitty's unicode placeholder, and
    `flush` turns each one into a space unless `keeps_placeholders`
    says this terminal reads them. Once per frame and not once per
    cell: the renderer writes cell by cell. Lillecarl/pymux#600.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._after_frame: list[str] = []
        self._in_frame = False
        self.keeps_placeholders: Callable[[], bool] = lambda: False
        #: Whether the terminal reports the mouse in pixels when the mouse
        #: is on. Lillecarl/pymux#648.
        self.pixel_mouse: Callable[[], bool] = lambda: False
        #: Whether U+FE0F goes after a one-column character, which this
        #: terminal would widen. Lillecarl/pymux#539.
        self.narrows_text_emoji: Callable[[], bool] = lambda: False

    @override
    def enable_mouse_support(self) -> None:
        super().enable_mouse_support()
        if self.pixel_mouse():
            self.write_raw(PIXEL_MOUSE_ON)

    @override
    def disable_mouse_support(self) -> None:
        super().disable_mouse_support()
        # Always: a terminal left in pixel mode gives the next program
        # pixels where it expects cells.
        self.write_raw(PIXEL_MOUSE_OFF)

    def write_after_frame(self, data: str) -> None:
        self._after_frame.append(data)

    def frame_begins(self) -> None:
        self._in_frame = True

    def frame_ends(self) -> None:
        self._in_frame = False

    @override
    def flush(self) -> None:
        if self._buffer:
            frame = "".join(self._buffer)
            if PLACEHOLDER in frame and not self.keeps_placeholders():
                frame = blanked(frame)
            if VS16 in frame and self.narrows_text_emoji():
                frame = without_widening_selectors(frame)
            self._buffer = [frame]
        if self._in_frame and self._after_frame:
            self.write_raw("".join(self._after_frame))
            self._after_frame = []
        super().flush()


class _NoStdout:
    """
    Stdout-like object that keeps nothing: writes go nowhere.

    The temporary CLI of a socket command has no terminal behind it,
    and nothing it holds is ever drawn. But building the application
    builds a renderer, and prompt_toolkit's renderer answers its
    construction with a reset -- `show_cursor`, the cursor shape --
    written to the output and flushed there. On a connection that
    carries a command, the person reading the answer got those bytes
    after it, as garbage that came and went with the timing of the
    writes around it. Lillecarl/pymux#420.
    """

    def write(self, data: str) -> int:
        return len(data)

    def flush(self) -> None:
        pass

    def isatty(self) -> bool:
        return True


class _ClientInput:
    """
    Input class that can be given to the CommandLineInterface.
    We only need this for turning the client into raw_mode/cooked_mode.
    """

    def __init__(
        self,
        send_packet: Callable[[Message], None],
        kitty_reply_callback: Callable[[str], None] | None = None,
        speaks_protocol: Callable[[], bool | None] | None = None,
        pixel_cell: Callable[[], tuple[int, int, int] | None] | None = None,
    ) -> None:
        self.send_packet = send_packet
        # Keep a reference to the context manager for the whole lifetime of
        # this object. `create_pipe_input()` returns a generator context
        # manager; when it's garbage collected, the pipe is closed.
        self._input_cm = create_pipe_input()
        self._input: PipeInput | None = self._input_cm.__enter__()

        # Replace the parser of the pipe input with one that also
        # understands the key encoding of the kitty keyboard protocol,
        # and routes terminal replies (keyboard flags query, device
        # attributes) to the given callback.
        vt100_input = cast(Vt100Input, self._input)
        self._parser = KittyVt100Parser(
            lambda key_press: vt100_input._buffer.append(key_press),
            reply_callback=kitty_reply_callback,
            speaks_protocol=speaks_protocol,
            pixel_cell=pixel_cell,
        )
        vt100_input.vt100_parser = self._parser

    def send_text(self, data: str) -> None:
        "Feed a text string to the input pipe."
        if self._input is not None:
            self._input.send_text(data)

    @property
    def pointer_fraction(self) -> tuple[float, float]:
        "Where in its cell the last mouse report was."
        return self._parser.pointer_fraction

    def close(self) -> None:
        "Close the input pipe. (Idempotent.)"
        if self._input is not None:
            with suppress(Exception):
                self._input_cm.__exit__(None, None, None)
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

    def raw_mode(self) -> AbstractContextManager[None]:
        return self._create_context_manager(Mode.RAW)

    def cooked_mode(self) -> AbstractContextManager[None]:
        return self._create_context_manager(Mode.COOKED)

    def _create_context_manager(self, mode: Mode) -> AbstractContextManager[None]:
        "Create a context manager that sends 'mode' commands to the client."

        class mode_context_manager:
            def __enter__(*a: object) -> None:
                self.send_packet({Field.CMD: Packet.MODE, Field.DATA: mode})

            def __exit__(*a: object) -> None:
                self.send_packet({Field.CMD: Packet.MODE, Field.DATA: Mode.RESTORE})

        return mode_context_manager()

    def __getattr__(self, name: str) -> object:
        return getattr(self._input, name)
