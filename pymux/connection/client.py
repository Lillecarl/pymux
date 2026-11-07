"""
The attached client of a connection: what it said of itself and of its
terminal, what was sent to that terminal, and the packets only a client
sends. A connection that is a peer link or a tunnel has none.
Lillecarl/pymux#886.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, ClassVar

import anyio
from libpymux.protocol import Field, Message, Packet
from pyte.keep import Keep
from pyte.osc import Osc
from pyte.program_status import ProgramState, beneath
from pyte.sequences.strings import osc

from ..audio import AudioLink
from ..enums import Woke
from ..graphics.graphics import ClientGraphics
from ..log import logger
from ..notify.osc import NO_PROGRESS, Progress
from ..pipes.base import BrokenPipeError
from ..style.colors import ColorDetection, DefaultColors
from .client_io import ClientInput, FrameOutput
from .clipboard_reads import ClipboardReader
from .extra_cursors import ExtraCursorsSent
from .files import Files
from .log_asks import LogAsks
from .pacing import FramePacing
from .packet_table import PacketTable
from .replies import TerminalReplies

if TYPE_CHECKING:
    from pymux.connection.client_state import ClientState
    from pymux.connection.recording import Recording
    from pymux.core.main import Pymux

    from .server import ServerConnection

__all__ = ["ClientRole"]


def _without_left(forwards: list[dict[str, Any]]) -> list[dict[str, Any]]:
    "A forwarding table without the time each forward has left."
    return [{key: value for key, value in one.items() if key != "left"} for one in forwards]


#: The method of `ClientRole` that handles each kind of packet.
_PACKETS: PacketTable[ClientRole] = PacketTable()


class ClientRole:
    """
    The client attached on one connection, made with its client state
    in `create_app`. The temporary client of a socket command is one
    too: it reads the pacing, the replies and the pipe input.
    """

    #: What a hot upgrade does with each attribute; `pyte.keep` says.
    #: The client reconnects to the next server (Lillecarl/pymux#409),
    #: so the attach handshake rebuilds most of this. Saved: the
    #: client's own record and when it came.
    KEEP: ClassVar[dict[str, Keep]] = {
        "state": Keep.SAVED,
        "created": Keep.SAVED,
        "connection": Keep.REBUILT,
        "pymux": Keep.REBUILT,
        # A restarting client takes the old server's images off its
        # terminal, and the next frame places them again.
        "graphics": Keep.DROPPED,
        "replies": Keep.REBUILT,
        "pacing": Keep.REBUILT,
        "clipboard": Keep.REBUILT,
        "files": Keep.REBUILT,
        "log_asks": Keep.REBUILT,
        "hostname": Keep.REBUILT,
        "ttyname": Keep.REBUILT,
        "pid": Keep.REBUILT,
        "client_id": Keep.REBUILT,
        "environment": Keep.REBUILT,
        "colors": Keep.REBUILT,
        "default_colors": Keep.REBUILT,
        # Asked again of the terminal the client reattaches from.
        "_pointer_shape_sent": Keep.REBUILT,
        # A new server clears them with its first frame.
        "extra_cursors_sent": Keep.REBUILT,
        "_progress_sent": Keep.REBUILT,
        "_program_status_sent": Keep.REBUILT,
        "can_forward": Keep.REBUILT,
        "answers_forwards": Keep.REBUILT,
        "_next_forwards": Keep.DROPPED,
        "starts_browsers": Keep.REBUILT,
        "saves_files": Keep.REBUILT,
        "sends_files": Keep.REBUILT,
        "answers_transfers": Keep.REBUILT,
        "keeps_options": Keep.REBUILT,
        "forwards": Keep.DROPPED,
        "pipeinput": Keep.DROPPED,
        # The next server measures the client's round trip again.
        "audio": Keep.DROPPED,
        # A recording holds Python objects of this build, so it ends
        # with it. `upgrade.py` writes it out first.
        "recording": Keep.DROPPED,
    }

    #: The client's record, which `create_app` sets right after it
    #: makes this role: the client state takes the role.
    state: ClientState

    def __init__(self, connection: ServerConnection) -> None:
        self.connection = connection
        self.pymux: Pymux = connection.pymux

        self.replies = TerminalReplies(self)

        self.pacing = FramePacing(self)

        #: What `record-client` is recording of this client, or None.
        #: Lillecarl/pymux#612.
        self.recording: Recording | None = None

        # Kitty graphics protocol state of the outer terminal. The same
        # query answers this one: the reply of the graphics query
        # arrives before the device attributes reply that closes the
        # detection.
        self.graphics = ClientGraphics(
            self.write_output_raw,
            self.flush_output,
            self.request_repaint,
            # Read when it is needed, not now: the colour detection
            # below has not run yet, and the half blocks only ask once
            # a frame carries an image.
            color_depth=lambda: self.colors.depth,
            spawn=connection.spawn,
            image_ids=self.pymux.outer_image_ids,
            reads_placeholders=lambda: self.replies.identity.draws_placeholders(),
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
        #: machine, so neither means anything here on its own:
        #: `ServerConnection.name` puts the hostname in front.
        #: Lillecarl/pymux#335.
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
        #: requests that wait for its answer. Lillecarl/pymux#743.
        self.sends_files = False
        self.files = Files(self)
        #: The `show-log` requests that wait for this client. Lillecarl/pymux#934.
        self.log_asks = LogAsks(self.connection.send_packet)
        #: Whether this client answers kitty's file transfer as the
        #: terminal. Lillecarl/pymux#912.
        self.answers_transfers = False
        #: Whether this client keeps its own options between attaches,
        #: and so hears them each time one changes. Lillecarl/pymux#957.
        self.keeps_options = False
        #: This client's sound, when it plays it. Lillecarl/pymux#657.
        self.audio: AudioLink | None = None

        #: When this client attached. A client that reattaches from the
        #: same terminal takes the same name and a later time, so this
        #: is what tells two runs of one terminal apart, and what
        #: `#{client_created}` reads.
        self.created = time.time()

        #: The two colours the outer terminal draws with by itself.
        #: The same handshake asks for them, and either one stays
        #: `None` when the terminal does not say. **Only a client can
        #: find this out**, which is why it is here and not on the
        #: session. Lillecarl/pymux#223.
        self.default_colors = DefaultColors()

        self.clipboard = ClipboardReader(self)

        # The client input is parsed by the application that reads from
        # the pipe input (see `ClientInput`). Give that input a parser
        # that also understands the kitty keyboard protocol, and route
        # terminal replies to `TerminalReplies.arrived`.
        self.pipeinput = ClientInput(
            connection.send_packet,
            reply_callback=self.replies.arrived,
            # Whether this client's terminal counts the modifiers the
            # way the protocol does. It is asked when a key arrives and
            # not now, because the detection has not answered yet.
            # Lillecarl/pymux#182.
            speaks_protocol=self.replies.keyboard_is_supported,
            pixel_cell=self.replies.pixel_cell,
            pointer_moved=lambda: self.pymux.terminals.pointer_moved(self),
        )

        # The shape of the pointer that this client was told about. A
        # client that never saw one is not told to reset it.
        self._pointer_shape_sent = ""

        # The progress this client's terminal was told, the same way.
        self._progress_sent = NO_PROGRESS

        # The records this client's terminal holds of the panes, as
        # report payloads by id; None before the first. Lillecarl/pymux#927.
        self._program_status_sent: dict[str, str] | None = None

        # The extra cursors this client's terminal draws itself.
        # Lillecarl/pymux#891.
        self.extra_cursors_sent = ExtraCursorsSent()

    def handle(self, packet: Message) -> None:
        "Hand a packet to the method of this client for its kind, or else to the connection's."
        if not _PACKETS.handle(self, packet):
            self.connection.handle_own(packet)

    def write_output_raw(self, data: str) -> None:
        "Write to the outer terminal, without escaping. (For graphics.)"
        self.state.output.write_raw(data)

    def flush_output(self) -> None:
        self.state.output.flush()

    def forward_osc(self, sequence: str) -> None:
        """
        Write an OSC sequence of a pane to the outer terminal of this
        client. (The clipboard, a notification, the shape of the
        pointer: pymux cannot serve those, the terminal of the user
        can.)

        **It goes after the next frame, and not the moment it
        arrives.** `FrameOutput` holds it until the renderer's flush
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
        safe anywhere between two frames. `pymux.notify.osc.build_osc` has
        already checked the payload of the pane.

        **The temporary client of a socket command has no terminal.**
        It holds a client state like a real one, and its focused pane
        is the pane a person is looking at, so a sync that walks every
        client would hand it the shape of that pointer and the OSC of
        that pane -- read back as garbage after the answer of the
        command. Lillecarl/pymux#420.
        """
        if self.state.temporary or self.connection.closed:
            return
        output = self.state.output
        assert isinstance(output, FrameOutput)
        output.write_after_frame(sequence)

    def set_pointer_shape(self, shape: str) -> bool:
        """
        Put the shape that the focused pane asks for on the pointer of
        this client. An empty shape gives the pointer back to the
        terminal of the user.

        Only a change goes out. The shape belongs to the pane that the
        client looks at, so it has to be written again when the focus
        moves to a pane that wants a different one, or none.

        Says whether the shape changed: it goes out behind the next frame.
        """
        if shape == self._pointer_shape_sent:
            return False
        self._pointer_shape_sent = shape
        self.forward_osc(osc(Osc.POINTER_SHAPE, shape))
        return True

    def set_progress(self, progress: Progress) -> None:
        """
        Show this client's terminal the progress of the pane it looks
        at, when it draws one: some terminals take any OSC 9 for a
        notification. Only a change goes out. Lillecarl/pymux#788.
        """
        if progress == self._progress_sent or not self.replies.identity.draws_progress():
            return
        self._progress_sent = progress
        self.forward_osc(osc(Osc.MESSAGE, progress.sequence))

    def set_program_status(self, records: dict[str, str]) -> None:
        """
        Make this client's terminal hold `records`, report payloads by
        id, when it keeps OSC 7501 records. Only the difference goes out.

        A clear takes the records beneath its id too, so what is still
        there under one goes again. The first time clears everything,
        which a server before this one may have left. Lillecarl/pymux#927.
        """
        if not self.replies.program_status:
            return
        sent = self._program_status_sent
        cleared: list[str] = []
        if sent is None:
            sent = dict[str, str]()
            cleared.append("")
        for id in sorted(set(sent) - set(records), key=len):
            if not any(beneath(id, parent) for parent in cleared):
                cleared.append(id)
        out = ["state=%s:id=%s" % (ProgramState.CLEAR, id) if id else "state=%s" % ProgramState.CLEAR for id in cleared]
        out += [
            payload
            for id, payload in records.items()
            if sent.get(id) != payload or any(beneath(id, parent) for parent in cleared)
        ]
        self._program_status_sent = dict(records)
        for payload in out:
            self.forward_osc(osc(Osc.PROGRAM_STATUS, payload))

    def tell_options(self) -> None:
        "Send the client options set on this client, when it keeps them."
        if self.keeps_options:
            self.connection.send_packet(
                {Field.CMD: Packet.CLIENT_OPTIONS, Field.CLIENT_OPTIONS: self.state.options.set_ones()}
            )

    def request_repaint(self) -> None:
        """
        Ask the renderer to paint the whole screen again.

        The sixel output needs this: those pixels sit in the cells, so
        the text under an image that moved has to be written once more.
        Not `Renderer.reset`, which switches the mouse and bracketed
        paste off and on again around every such frame.
        """
        app = self.state.app
        app.renderer.request_full_redraw()
        # Named here and not through `Pymux.invalidate`, because this
        # wakes the one client that asked and not all of them.
        logger.debug("Drawing 1 of the clients: it asked for a full repaint")
        app.invalidate()

    @_PACKETS.handles(Packet.SOUND, Packet.ECHO, Packet.MICROPHONE, Packet.BEHIND)
    def _on_sound(self, packet: Message) -> None:
        # Sound from a client that plays it. Lillecarl/pymux#657.
        if self.audio is not None:
            self.audio.heard(packet)

    @_PACKETS.handles(Packet.IN)
    def _on_input(self, packet: Message) -> None:
        if self.recording is not None:
            self.recording.hear("in", packet[Field.DATA])
        self.pipeinput.send_text(self.clipboard.take_answer(packet[Field.DATA]))
        # Input newer than a scheduled redraw makes that frame
        # stale: the renderer skips it and asks for another one
        # instead, so this is handled first. Lillecarl/pymux#524.
        self.state.input_tick += 1

    @_PACKETS.handles(Packet.CLIPBOARD_IMAGE)
    def _on_clipboard_image(self, packet: Message) -> None:
        "The image on the person's clipboard, sent ahead of their Ctrl-V. Lillecarl/pymux#956."
        self.pymux.wayland.take_client_image(self.state, str(packet.get(Field.DATA) or ""))

    @_PACKETS.handles(Packet.DETECT)
    def _on_detect(self, packet: Message) -> None:
        self.replies.start_detection()

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
        self.files.saved(packet)

    @_PACKETS.handles(Packet.UPLOAD, Packet.UPLOAD_DONE)
    def _on_upload(self, packet: Message) -> None:
        self.files.took_part(packet)

    @_PACKETS.handles(Packet.FILES_LISTED)
    def _on_files_listed(self, packet: Message) -> None:
        self.files.listed(packet)

    @_PACKETS.handles(Packet.LOG)
    def _on_log(self, packet: Message) -> None:
        self.log_asks.answered(packet)

    @_PACKETS.handles(Packet.KITTY_TRANSFER)
    def _on_kitty_transfer(self, packet: Message) -> None:
        self.pymux.kitty_transfers.terminal_said(self.connection, str(packet.get(Field.DATA) or ""))

    @_PACKETS.handles(Packet.KITTY_TRANSFER_TAKEN)
    def _on_kitty_transfer_taken(self, packet: Message) -> None:
        self.connection.spawn(self.pymux.kitty_transfers.taken(self.connection, str(packet.get(Field.DATA) or "")))

    @_PACKETS.handles(Packet.REDRAW)
    def _on_redraw(self, packet: Message) -> None:
        # The client drew a question over the frame, and it is gone.
        self.pymux.waking.redraw(self.state)

    @_PACKETS.handles(Packet.OPEN_FAILED)
    def _on_open_failed(self, packet: Message) -> None:
        self.state.message = "Could not open %s in a browser on this machine." % (packet[Field.DATA],)

    @_PACKETS.handles(Packet.FRAME_SHOWN)
    def _on_frame_shown(self, packet: Message) -> None:
        self.pacing.frame_answered()

    @_PACKETS.handles(Packet.FORWARDS)
    def _on_forwards(self, packet: Message) -> None:
        # What this client is forwarding, after it changed or after the
        # link came back. The server keeps a copy and never the truth:
        # it holds no SSH connection, so only the client can say whether
        # a listener is really open. Lillecarl/pymux#436.
        reported = packet.get(Field.DATA) or []
        said = packet.get(Field.MESSAGE)
        arrived, self._next_forwards = self._next_forwards, None

        if said:
            self.state.message = said

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
        self.connection.send_packet({Field.CMD: Packet.REPORT_FORWARDS})
        await arrived.wait()

    async def carry_audio(self) -> None:
        """
        Carry this client's sound, until the connection goes. Only in
        msgpack, which carries bytes. `AudioLink` says why on this
        connection and not one of its own. Lillecarl/pymux#657.
        """
        link = AudioLink(self)
        self.audio = link
        self.pymux.audio.link_opened(link)
        try:
            await link.run()
        except BrokenPipeError:
            pass
        finally:
            self.audio = None
            self.pymux.audio.link_closed(link)

    def _terminal_reads_shm(self) -> None:
        logger.info("The terminal of client %s reads window frames from shared memory.", self.client_id or "?")
        self.connection.send_packet({Field.CMD: Packet.SHM})

    async def send_frame(self, parts: list[Message]) -> None:
        """
        Write a window pane's frame, one part at a time.

        **Each part is awaited before the next is encoded**, so a packet
        sent meanwhile -- a key's echo, a line of text -- takes the write
        lock between two parts, and never waits behind the whole frame.
        What the terminal was sent before goes first. Lillecarl/pymux#634.

        **The checkpoint is what makes that true.** A write to a socket
        with room never suspends, so without it the parts of a frame go
        out in one turn of the loop: on a zstd stream, about 7 ms of
        compression for a 1600x880 frame, and every other task waits.
        Lillecarl/pymux#632.
        """
        self.flush_output()
        self.pacing.window_frame_went_out()
        connection = self.connection
        for part in parts:
            if connection.closed:
                return
            await connection.write_encoded(connection.encode(part))
            await anyio.lowlevel.checkpoint()

    @property
    def pointer_fraction(self) -> tuple[float, float]:
        "Where in its cell the client's last mouse report was. Lillecarl/pymux#648."
        return self.pipeinput.pointer_fraction

    @property
    def pointer_cell(self) -> tuple[int, int] | None:
        "The cell of the client's last mouse report, column and row from 0."
        return self.pipeinput.pointer_cell

    @property
    def activity(self) -> float:
        "When a person last pressed a key or used the mouse at this client."
        return self.pipeinput.activity

    @property
    def pointer_shape_sent(self) -> str:
        return self._pointer_shape_sent

    def suspend_client_to_background(self) -> None:
        """
        Ask the client to suspend itself. (Like, when Ctrl-Z is pressed.)
        """
        self.connection.send_packet({Field.CMD: Packet.SUSPEND})

    def close(self) -> None:
        "End what this client holds, as its connection closes."
        if self.recording is not None:
            self.pymux.finish_recordings([self])
        # Remove the images that this client put on its terminal.
        try:
            self.graphics.reset()
        except Exception:
            logger.exception("Removing the graphics of the client failed.")

        # Try to exit the application if it's still running.
        try:
            if not self.state.app.is_done:
                self.state.app.exit()
        except Exception:
            pass

    def went(self) -> None:
        "What waits on this client lets go, after its connection closed."
        self.pacing.frames_shown.set()
        self.files.client_went()
        self.log_asks.gone()
        self.pipeinput.close()
