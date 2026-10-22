"""
What every client does with the terminal of the user.

A client has two sides. One side is the terminal: read the keys, write
the frames, ask what the terminal supports, put it back as it was. The
other side is the transport that carries the packets to the server.

This holds the first side. The transport is what a subclass adds: a
unix socket in `posix.py`, a pair of queues in `memory.py`. Both sides
speak the same packets, so a difference between the two routes is a
difference of the transport and nothing else.
"""

from __future__ import annotations

import contextlib
import os
import secrets
import select
import signal
import socket
import sys
import threading
import time
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import anyio
from libpymux.protocol import Field, Mode, Packet
from libpymux.wire import MSGPACK, encode_json, encode_msgpack
from prompt_toolkit.input.posix_utils import PosixStdinReader
from prompt_toolkit.input.vt100 import cooked_mode, raw_mode
from prompt_toolkit.output.vt100 import Vt100_Output, _get_size

from pymux.colors import COLOR_QUERIES, TRUECOLOR_PROBE
from pymux.config import client_options_in, find_config
from pymux.files import ArrivingFiles, Whole
from pymux.graphics import CELL_SIZE_QUERY, RESTORE_CURSOR, SAVE_CURSOR, VERSION_QUERY
from pymux.graphics import QUERY_SEQUENCE as GRAPHICS_QUERY
from pymux.kitty_frames import Base64Carrier, Pictures, ShmCarrier, decoders, frame_commands, shm_probe
from pymux.osc import PASSTHROUGH_VARIABLE, set_user_var
from pymux.utils import nonblocking

from . import browser
from .base import Client
from .downloads import save_whole
from .uploads import (
    ESCAPE,
    LOCAL_CLIENT_OPTIONS,
    Answer,
    UploadSettings,
    Wanted,
    allowed,
    answer_of,
    done_packet,
    inspect,
    outcome_packets,
    question,
    upload_settings,
)

if TYPE_CHECKING:
    from .audio import AudioClient

__all__ = [
    "TerminalClient",
]

#: What the client asks the outer terminal at attach time.
#:
#: The replies arrive as input and the server reads them: keyboard
#: flags, the kitty graphics protocol, the cell size, the terminal's
#: name and version, the colour depth,
#: the two colours the terminal draws with, and last the device
#: attributes, which also say whether sixel works. Every terminal
#: answers the device attributes query, so a feature that did not
#: answer before it is not supported.
#:
#: The keyboard query asks for every flag first, so that the reply says
#: which ones the terminal took. A terminal that speaks a part of the
#: protocol only says so that way. The pop puts the terminal back as it
#: was.
#: Does the terminal hold a frame back until it is complete?
#: ("CSI ? 2026 $ p", DECRQM.) A terminal that does answers
#: "CSI ? 2026 ; <state> $ y" with a state of 1, 2 or 4; one that does
#: not answers 0, or does not answer at all.
SYNCHRONIZED_OUTPUT_QUERY = b"\x1b[?2026$p"

#: How often a client reads the size of its terminal, in seconds.
#: `Application.terminal_size_polling_interval` is the same number, and
#: for the same reason: it is short enough that a person who drags a
#: window does not wait for it, and one `ioctl` twice a second costs
#: nothing.
SIZE_INTERVAL = 0.5

#: What starts a kitty graphics command in a server's output.
KITTY_GRAPHICS = "\x1b_G"

#: Take every image placement off the screen, quietly.
DELETE_EVERY_IMAGE = b"\x1b_Ga=d,d=A,q=2\x1b\\"

#: A server ends every command with its exit code. A peer that closes
#: without one ran no command, or is not a pymux server. Lillecarl/pymux#606.
NO_ANSWER = "pymux: %s closed the connection with no answer. Is a pymux server listening there?\n"

#: The device attributes query, which closes the detection. The shared
#: memory probe goes in front of it, so its answer comes first.
DEVICE_ATTRIBUTES_QUERY = b"\x1b[c"

DETECTION_QUERIES = (
    b"\x1b[>31u\x1b[?u\x1b[<u"
    + SYNCHRONIZED_OUTPUT_QUERY
    + GRAPHICS_QUERY.encode("ascii")
    + CELL_SIZE_QUERY.encode("ascii")
    + VERSION_QUERY.encode("ascii")
    + TRUECOLOR_PROBE.encode("ascii")
    + COLOR_QUERIES.encode("ascii")
    + DEVICE_ATTRIBUTES_QUERY
)


def detection_queries(probe: str) -> bytes:
    "`DETECTION_QUERIES` with `probe` asked before the device attributes."
    return DETECTION_QUERIES[: -len(DEVICE_ATTRIBUTES_QUERY)] + probe.encode("ascii") + DEVICE_ATTRIBUTES_QUERY


def _write_all(fd: int, data: bytes) -> None:
    """
    Write every byte: a frame is megabytes, and a write may take part of
    it. stdin is non-blocking while attached, and on a terminal stdout
    is the same open file, so a full one waits here for room.
    """
    view = memoryview(data)
    while view:
        try:
            view = view[os.write(fd, view) :]
        except BlockingIOError:
            select.select([], [fd], [])


#: Go to the start of a row, and erase it.
_ROW = "\x1b[%d;1H\x1b[2K"
REVERSE = "\x1b[7m"
NO_ATTRIBUTES = "\x1b[m"

#: Why a request is refused without a question.
BUSY = "the client is asking the person about another request"
SAID_NO = "the person said no"


@dataclass(slots=True)
class _Asking:
    """
    A request the person has not answered yet. `flags_after` holds the
    keyboard flags the server asked for meanwhile.
    """

    request: str
    wanted: list[Wanted]
    text: str
    grant_seconds: float
    flags_before: int | None
    flags_after: int | None = None


def _ttyname() -> str:
    """
    The terminal this client draws on, or "" when it has none.

    A client with no terminal is not an error. A pymux command in a
    script or a cron job has its output on a pipe, and the tests run
    one with stdout replaced; the server names those by the process
    instead. Lillecarl/pymux#335.
    """
    try:
        return os.ttyname(sys.stdout.fileno())
    except OSError, ValueError, AttributeError:
        return ""


class TerminalClient(Client):
    """
    The terminal side of a client. A subclass adds the transport.

    A subclass has to give `_send_packet`, and an `attach` that reads
    the transport and the keyboard until one of them ends.
    """

    #: Whether this client can forward a port. Only `SshClient` can:
    #: forwarding needs an SSH connection, and a client on a unix
    #: socket is already on the machine the server runs on.
    #: Lillecarl/pymux#436.
    can_forward = False

    #: How a packet to the server is encoded. `_start_gui` sets it back
    #: to JSON for each connection, and a `wire` packet from the server
    #: turns it to msgpack. Lillecarl/pymux#633.
    _encode = staticmethod(encode_json)

    #: The formats this client reads, best first. Compression pays only
    #: on a link that crosses a network, so only `SshClient` asks for it.
    wire_formats: tuple[str, ...] = (MSGPACK,)

    #: The sound of the server, where this machine can play it. A
    #: subclass that carries it sets it. Lillecarl/pymux#657.
    audio: AudioClient | None = None

    #: Every real client answers the server's ping. The server drops a
    #: client that stops answering, and only one that said it answers,
    #: so this is what makes that safe. Lillecarl/pymux#446.
    answers_ping = True

    def __init__(self) -> None:
        self._mode_context_managers = []

        # Kitty keyboard protocol state of the outer terminal. Whether
        # the terminal supports the protocol is detected at attach time
        # ("CSI ? u" query + device attributes). The currently enabled
        # flags follow the focused pane.
        self._kitty_supported = False
        self._kitty_flags: int | None = None

        self.__stdin_reader: PosixStdinReader | None = None

        #: What this client leaves with when it is attached. The server
        #: names it in an `exit` packet, which it sends before it closes
        #: a connection it will not serve: a refused attach is not the
        #: same as a person who detached, and a script has only the
        #: code to tell them apart. Lillecarl/pymux#332.
        self.exit_code = 0

        #: How many seconds to wait for the next server when this one
        #: goes, or `None`. Only a `restarting` packet sets it: a server
        #: that ends without one is gone, and the client leaves.
        #: Lillecarl/pymux#409.
        self.restart_wait: float | None = None

        #: Whether a server put a kitty image on this terminal. The
        #: next server knows none of them, so a restart takes them off,
        #: and a terminal that never had one is never sent the APC.
        self.placed_images = False

        #: Who this client is, for as long as its process runs. It goes
        #: out with every attach, so a server that comes back after an
        #: upgrade knows a client it served before and can give it its
        #: windows back. Lillecarl/pymux#399.
        self.client_id = secrets.token_hex(8)

        #: Whether this client has attached once already. **Only the
        #: first attach detaches or hangs up the others.** `attach -d`
        #: and `-x` name the clients that were there when the person
        #: typed it; the ones that reattach beside this one after an
        #: upgrade or a dropped link did not.
        self._attached_before = False

        #: How a window pane's frame reaches the terminal: base64 until
        #: the terminal answers the shared memory probe, which `_shm`
        #: makes. Lillecarl/pymux#634.
        self._carrier: Base64Carrier | ShmCarrier = Base64Carrier()
        self._shm: ShmCarrier | None = None
        #: The bands of the frames that have not had their last part, by
        #: outer image id.
        self._frame_parts: dict[int, list] = {}
        #: What makes a decoder of each codec this client decodes, and
        #: the decoder of each window pane's stream, by outer image id.
        #: Lillecarl/pymux#639.
        self._decoder_kinds = decoders()
        self._decoders: dict[int, Any] = {}
        #: Each window pane's picture, so a frame goes as one edit.
        #: Lillecarl/pymux#644.
        self._pictures = Pictures()
        #: The browsers a program on the server drives, by launch.
        #: Lillecarl/pymux#653.
        self._browsers: dict[str, Any] = {}
        #: The files a pane sends the person, until each is whole.
        #: Lillecarl/pymux#743.
        self.downloads = ArrivingFiles()
        #: The attachment's task group, for a client that runs on a loop.
        #: A download is written from it, and from a thread without it.
        self._tasks: anyio.abc.TaskGroup | None = None
        #: The request for files the person is being asked about, and
        #: until when "a" lets the server read without asking. A client
        #: serves one server, so the grant is that server's.
        #: Lillecarl/pymux#743.
        self._asking: _Asking | None = None
        self._grant_until: float | None = None
        #: Requests whose paths are being looked at, off the loop. One the
        #: server withdraws meanwhile leaves, and is never asked about.
        self._looking: set[str] = set()
        #: One write to the terminal at a time: the socket client draws
        #: a question from a thread.
        self._terminal_lock = threading.Lock()

    #: The clock a grant runs on.
    _clock = staticmethod(time.monotonic)

    def server_host(self) -> str:
        "The machine the server runs on, as this client knows it, for the person to read."
        return socket.gethostname()

    @property
    def _stdin_reader(self) -> PosixStdinReader:
        """
        The keyboard, opened when something first reads it.

        **Not in `__init__`.** A client that only runs a command never
        reads the keyboard, and asking for `sys.stdin.fileno()` is not
        free: a pymux command in a script, in a cron job, or under a
        test runner that replaced stdin has no such file, and building
        the client raised before it had sent anything.

        Some terminals, like lxterminal, send non UTF-8 input sequences
        even when the input encoding is supposed to be UTF-8. This
        happens for mouse clicks in the right area of a wide terminal:
        binary blobs in between the UTF-8. They must not be replaced,
        because that breaks the decoding of what follows, and
        `errors="ignore"` does not work either, because a mouse
        sequence is a fixed number of bytes.
        """
        if self.__stdin_reader is None:
            self.__stdin_reader = PosixStdinReader(sys.stdin.fileno(), errors="replace")
        return self.__stdin_reader

    def _send_packet(self, data):
        "Send one packet to the server. (The transport gives this.)"
        raise NotImplementedError

    def _start_gui(self, detach_other_clients: bool, color_depth) -> None:
        """
        Tell the server that this client wants the user interface, and
        ask the outer terminal what it supports.
        """
        first = not self._attached_before
        self._attached_before = True
        # A new connection, to this server or the next: JSON until it
        # answers the offer below.
        self._encode = encode_json
        # And the rest of a file the last connection was sending never
        # arrives.
        self.downloads.clear()
        # A question about a request of the last connection has nobody
        # to answer to.
        self._looking.clear()
        if self._asking is not None:
            self._stop_asking()
        self._send_size()
        self._send_packet(
            {
                Field.CMD: Packet.START_GUI,
                Field.DETACH_OTHERS: detach_other_clients and first,
                # `-x`: the other clients of the session leave, and the
                # terminals they were in close. Lillecarl/pymux#347.
                Field.HANG_UP_OTHERS: self.hang_up_others and first,
                # `-r`: this client only watches. Lillecarl/pymux#467.
                Field.READ_ONLY: self.read_only,
                Field.COLOR_DEPTH: color_depth,
                Field.TERM: os.environ.get("TERM", ""),
                Field.COLORTERM: os.environ.get("COLORTERM", ""),
                # The machine this client runs on. Only the client can
                # say it: over ssh the server answers `gethostname`
                # with another machine's name. Lillecarl/pymux#287.
                Field.HOSTNAME: socket.gethostname(),
                # Whether this client can forward a port. Only the one
                # that reached the server over SSH holds a connection
                # that carries one, and the server cannot tell from its
                # end: a unix socket is a unix socket either way.
                # Lillecarl/pymux#436.
                Field.FORWARDS: self.can_forward,
                Field.REPORTS: self.can_forward,
                Field.BROWSERS: True,
                Field.SAVES_FILES: True,
                Field.SENDS_FILES: True,
                # Whether this client answers a ping. A server drops a
                # client that stops answering, and never one that did
                # not say it would. Lillecarl/pymux#446.
                Field.PINGS: self.answers_ping,
                # A real terminal answers a status report, and its
                # answer is what says a frame arrived: over ssh the
                # bytes wait in buffers this client cannot see.
                # Lillecarl/pymux#259.
                Field.PACED: True,
                # The whole environment of this client, of which the
                # server keeps the names "update-environment" lists and
                # drops the rest. The client cannot do the filtering: it
                # is the server that holds the option, and asking for it
                # first would be a round trip at every attach. tmux
                # sends the whole environment too, one message per
                # variable. Lillecarl/pymux#271.
                Field.ENVIRONMENT: dict(os.environ),
                # The terminal this client draws on, and the process it
                # runs as. Both belong to this machine, which is why
                # the server keeps the hostname in front of them: two
                # people can each be on their own `/dev/pts/3`. The
                # process is the fallback for a client with no terminal
                # at all, the way tmux falls back for a control client.
                # Lillecarl/pymux#335.
                Field.TTYNAME: _ttyname(),
                Field.PID: os.getpid(),
                Field.CLIENT_ID: self.client_id,
                # What this client's own configuration file says about
                # this client. A theme belongs to the terminal a
                # person is sitting at, and only this side can read
                # the file that names it: over SSH the server's
                # configuration is another machine's.
                # Lillecarl/pymux#223.
                Field.CLIENT_OPTIONS: self._client_options(),
                # What this client reads besides JSON. A server that
                # takes one says which with a `wire` packet.
                # Lillecarl/pymux#633.
                Field.WIRE: list(self.wire_formats),
                # This client plays the server's sound, on this
                # connection. Lillecarl/pymux#657.
                Field.AUDIO: self.audio is not None,
                # This client encodes a window pane's frames for its
                # own terminal: the server sends the raw pixels.
                # Lillecarl/pymux#634.
                Field.FRAMES: True,
                # And can take them in these codecs. Lillecarl/pymux#639.
                Field.CODECS: [str(codec) for codec in self._decoder_kinds],
                # And says when it wrote each. Lillecarl/pymux#641.
                Field.SHOWS: True,
                Field.DATA: "",
            }
        )

        # Frames go in base64 until this terminal reads the probe; a new
        # connection asks again, and drops the parts of a frame it had.
        self._carrier = Base64Carrier()
        self._frame_parts.clear()
        self._decoders.clear()
        self._pictures.clear()
        self._write_terminal(detection_queries(self._shm_probe()))
        self._send_packet({Field.CMD: Packet.KITTY_DETECT})

    def _shm_probe(self) -> str:
        "The shared memory probe, or nothing where no shared memory can be made."
        if self._shm is None:
            self._shm = ShmCarrier()
        try:
            return shm_probe(self._shm)
        except OSError:
            return ""

    def _restore_modes(self) -> None:
        """
        Put back every raw or cooked mode the server asked for.

        The server asks for cooked mode while a pane reads a password,
        and asks for it back afterwards. An attachment that ends in
        between leaves the terminal as the pane wanted it, which means
        the keys a person presses next are echoed and held for a line.
        Lillecarl/pymux#256.
        """
        while self._mode_context_managers:
            with contextlib.suppress(Exception):
                self._mode_context_managers.pop().__exit__()

    def _client_options(self) -> list:
        """
        What this client announces about itself, in the order the
        server applies it.

        The configuration file first and the command line after, so a
        flag wins: a file belongs to one machine and a flag to the one
        terminal it was typed in. Lillecarl/pymux#223,
        Lillecarl/pymux#340.
        """
        # The upload options stay here: they are this client's to read,
        # and the server is not to learn the allow-list.
        announced = [
            one for one in client_options_in(self.config_file or find_config()) if one[0] not in LOCAL_CLIENT_OPTIONS
        ]
        if self.chosen_name:
            announced.append(("name", self.chosen_name))
        return announced

    def _reset_terminal(self) -> None:
        """
        Put the terminal of the user back as it was. (The server is
        gone, or the client is leaving.)
        """
        output = Vt100_Output.from_pty(sys.stdout)
        self._pop_kitty_flags()
        # DECTCEM is not part of what the alternate screen puts back,
        # so a cursor that pymux hid stays hidden in the shell the
        # person returns to.
        output.show_cursor()
        output.quit_alternate_screen()
        output.disable_mouse_support()
        # SGR pixel mouse, which the server may have turned on: the next
        # program would read pixels as cells. Lillecarl/pymux#648.
        output.write_raw("\x1b[?1016l")
        # The user variable passthrough sets, which a terminal may hold
        # its own shortcuts back on. Cleared whatever mode the client
        # was in, because a detach in passthrough leaves no leave-mode.
        # Lillecarl/pymux#530.
        output.write_raw(set_user_var(PASSTHROUGH_VARIABLE, None))
        output.disable_bracketed_paste()
        output.reset_attributes()
        output.flush()

    def _write_terminal(self, data: bytes) -> None:
        "Write a frame of the server, or a sequence of this client's, to the terminal."
        with self._terminal_lock:
            _write_all(sys.stdout.fileno(), data)

    def _leave_the_terminal(self) -> None:
        """
        Put back everything an attachment changed, however it ended.

        Raw or cooked mode the server pushed over ours
        (Lillecarl/pymux#411), then what the server's bytes set: the
        alternate screen, the mouse, the cursor, the attributes and
        our kitty push. stdout may be the thing that failed, so the
        error that ended the attachment outranks anything this raises.
        Lillecarl/pymux#404.
        """
        self._restore_modes()
        with contextlib.suppress(Exception):
            self._reset_terminal()
        # The objects of frames the terminal never got to read.
        if self._shm is not None:
            with contextlib.suppress(Exception):
                self._shm.close()

    def _window_frame(self, packet: dict) -> bool:
        """
        Gather a window pane's frame, and at its last part write it to
        the terminal, encoded the way the terminal reads it.

        A whole frame goes as one image, so its bands are put back
        together; the bands of an edit are edits each. Lillecarl/pymux#634.
        A frame in a codec is decoded first, and what changed is taken
        from it. Lillecarl/pymux#639.

        True when the terminal was asked to answer the frame, which then
        stands for `frame-shown`. Lillecarl/pymux#643.
        """
        image = packet[Field.IMAGE]
        codec = packet.get(Field.CODEC)
        parts = self._frame_parts.setdefault(image, [])
        if codec:
            parts.append(packet.get(Field.VIDEO) or b"")
        else:
            parts.extend(packet.get(Field.RECTS) or [])
        if not packet.get(Field.LAST):
            return False
        del self._frame_parts[image]
        whole, width, height = packet[Field.WHOLE], packet[Field.WIDTH], packet[Field.HEIGHT]
        if codec:
            rects = self._decoded(image, codec, b"".join(parts), packet.get(Field.RECTS) or [])
            if rects is None:
                return False
        elif whole and parts:
            rects = [(0, 0, width, height, b"".join(band[4] for band in parts))]
        else:
            rects = [tuple(band) for band in parts]
        rects = self._pictures.merged(image, whole, width, height, rects)
        answer = bool(packet.get(Field.ANSWER)) and bool(rects)
        try:
            commands = frame_commands(image, whole, width, height, rects, self._carrier, answer)
        except OSError:
            # Shared memory ran out, or went: base64 from here on.
            self._carrier = Base64Carrier()
            commands = frame_commands(image, whole, width, height, rects, self._carrier, answer)
        text = commands + packet.get(Field.AFTER, "")
        if text:
            self._write_terminal((SAVE_CURSOR + text + RESTORE_CURSOR).encode("ascii"))
            self.placed_images = True
        return answer

    def _decoded(self, image: int, codec: str, data: bytes, rects: list) -> list | None:
        "What changed in a window pane's encoded frame, as RGB rectangles; None when it would not decode."
        if not data:
            return []
        decoder = self._decoders.get(image)
        if decoder is None:
            make = self._decoder_kinds.get(codec)
            if make is None:
                return None
            decoder = self._decoders[image] = make()
        try:
            _width, _height, decoded = decoder.decode(data, [tuple(rect) for rect in rects])
        except ValueError:
            # A stream this decoder cannot follow. Each connection and
            # each switch to the codec starts with a keyframe, which a
            # new decoder takes.
            del self._decoders[image]
            return None
        return decoded

    def _process(self, packet: dict):
        """
        Handle incoming packet from server.
        """
        if packet[Field.CMD] == Packet.WIRE:
            # The server writes msgpack from here on, and the reader has
            # turned already. This side turns too, saying so first. Its
            # packets are keystrokes, too small for zstd to win on.
            self._send_packet({Field.CMD: Packet.WIRE, Field.WIRE: MSGPACK})
            self._encode = encode_msgpack

        elif packet[Field.CMD] == Packet.OUT:
            data = packet[Field.DATA]
            self._write_terminal(data.encode("utf-8"))
            if KITTY_GRAPHICS in data:
                self.placed_images = True
            # The frame drew over the question.
            self._draw_question()

        elif packet[Field.CMD] == Packet.WINDOW_FRAME:
            answered = False
            try:
                answered = self._window_frame(packet)
            finally:
                # Whatever came of it, the server waits for an answer:
                # the terminal's, or this one.
                if packet.get(Field.LAST) and not answered:
                    self._send_packet({Field.CMD: Packet.FRAME_SHOWN})
            if packet.get(Field.LAST):
                self._draw_question()

        elif packet[Field.CMD] == Packet.SHM and self._shm is not None:
            self._carrier = self._shm

        elif packet[Field.CMD] == Packet.EXIT:
            # The server is about to close this connection, and says
            # what this client leaves with. The read loop ends on the
            # close itself.
            self.exit_code = packet[Field.CODE]
            # And whether to hang up the process that started this
            # client on the way out, which is `attach -x`. The signal
            # goes after the terminal is back, so it is remembered
            # here and sent there. Lillecarl/pymux#347.
            if packet.get(Field.HANG_UP):
                self.hang_up_asked = True

        elif packet[Field.CMD] == Packet.RESTARTING:
            self.restart_wait = float(packet[Field.WAIT])

        elif packet[Field.CMD] == Packet.ECHO:
            # The server measures the round trip that sizes the sound
            # buffers: the answer goes at once, before anything else.
            # Lillecarl/pymux#657.
            self._send_packet({Field.CMD: Packet.ECHO, Field.TIME: packet.get(Field.TIME)})

        elif packet[Field.CMD] in (Packet.SOUND, Packet.AUDIO, Packet.LATENCY, Packet.MICROPHONE):
            if self.audio is not None:
                self.audio.heard(packet)

        elif packet[Field.CMD] == Packet.PING:
            # The server is asking whether anybody is still here. An
            # answer costs one packet and keeps this client's place;
            # silence is what tells the server the person is gone.
            # Lillecarl/pymux#446.
            self._send_packet({Field.CMD: Packet.PONG})

        elif packet[Field.CMD] == Packet.SUSPEND:
            # Suspend client process to background.
            os.kill(os.getpid(), signal.SIGTSTP)

        elif packet[Field.CMD] == Packet.OPEN:
            # The server asks this machine, not the machine of the
            # server, to open the URL. A machine with no browser says
            # so back, where the request was made visible.
            url = packet[Field.DATA]
            if not self._open_url(url):
                self._send_packet({Field.CMD: Packet.OPEN_FAILED, Field.DATA: url})

        elif packet[Field.CMD] == Packet.BROWSER:
            self._start_browser(packet)

        elif packet[Field.CMD] == Packet.FILE:
            # A pane's download, saved on this machine. The server
            # shows where it went: only this side knows the path.
            arrived = self.downloads.receive(packet)
            if isinstance(arrived, Whole):
                self._save(arrived)
            elif arrived is not None:
                self._send_packet({Field.CMD: Packet.FILE_SAVED, Field.MESSAGE: arrived})

        elif packet[Field.CMD] == Packet.SEND_FILES:
            self._files_asked(packet)

        elif packet[Field.CMD] == Packet.FILES_WITHDRAWN:
            self._files_withdrawn(str(packet.get(Field.REQUEST) or ""))

        elif packet[Field.CMD] == Packet.BROWSER_STOP:
            self.stop_browser(packet[Field.BROWSER])

        elif packet[Field.CMD] == Packet.KITTY_KEYBOARD:
            # Kitty keyboard protocol instructions for the outer
            # terminal.
            data = packet[Field.DATA]
            if "supported" in data:
                self._kitty_supported = data["supported"]
            if "flags" in data:
                # A question takes plain keys; the flags wait for it.
                if self._asking is not None:
                    self._asking.flags_after = data["flags"]
                else:
                    self._set_kitty_flags(data["flags"])

        elif packet[Field.CMD] == Packet.MODE:
            # Set terminal to raw/cooked.
            action = packet[Field.DATA]

            if action == Mode.RAW:
                cm = raw_mode(sys.stdin.fileno())
                cm.__enter__()
                self._mode_context_managers.append(cm)

            elif action == Mode.COOKED:
                cm = cooked_mode(sys.stdin.fileno())
                cm.__enter__()
                self._mode_context_managers.append(cm)

            elif action == Mode.RESTORE and self._mode_context_managers:
                cm = self._mode_context_managers.pop()
                cm.__exit__()

    def _set_kitty_flags(self, flags: int) -> None:
        """
        Put the outer terminal in the keyboard encoding the focused
        pane wants. (Ignored when the terminal does not support the
        protocol.)

        The first enable is a push, the protocol's own way to hold the
        state that was there: a shell's flags, most of all a nested
        pymux's. A later pane asks for different flags and gets a set
        -- our push still holds the restore point, and the value over
        it is ours to change. The way out pops what we pushed
        (`_pop_kitty_flags`), and that is how the nested case gives
        the outer session its encoding back. Lillecarl/pymux#403.
        """
        if not self._kitty_supported and flags != 0:
            return
        if flags == self._kitty_flags:
            return
        if self._kitty_flags is None:
            if flags == 0:
                return  # Nothing of ours is on the stack; 0 changes nothing.
            self._write_terminal(("\x1b[>%du" % flags).encode())
        else:
            self._write_terminal(("\x1b[=%d;1u" % flags).encode())
        self._kitty_flags = flags

    def _pop_kitty_flags(self) -> None:
        """
        Take our push off the outer terminal's stack. The state
        beneath it comes back: the shell's, or a nested pymux's.
        """
        if self._kitty_flags is None:
            return  # Never enabled anything.
        os.write(sys.stdout.fileno(), b"\x1b[<1u")
        self._kitty_flags = None

    def _save(self, whole: Whole) -> None:
        """
        Write a download off the loop, and tell the server where it went.
        A write of 64 MiB here stopped drawing and typing. Lillecarl/pymux#747.

        A client on a loop answers from it: asyncssh's writer and the
        memory stream are not safe from a thread. The socket client has
        no loop, and its send takes a lock.
        """
        if self._tasks is not None:
            self._tasks.start_soon(self._save_and_say, whole)
            return

        def save_and_say() -> None:
            self._send_packet({Field.CMD: Packet.FILE_SAVED, Field.MESSAGE: save_whole(whole)})

        threading.Thread(target=save_and_say, name="pymux-download", daemon=True).start()

    async def _save_and_say(self, whole: Whole) -> None:
        said = await anyio.to_thread.run_sync(save_whole, whole)
        with contextlib.suppress(BrokenPipeError):
            self._send_packet({Field.CMD: Packet.FILE_SAVED, Field.MESSAGE: said})

    # ------------------------------------------------------------------
    # Files a pane asks for: `pymux receive`. Lillecarl/pymux#743.

    def _off_the_loop[T](self, work: Callable[[], T], then: Callable[[T], None]) -> None:
        """
        Run `work`, which blocks, away from the loop that draws, and
        `then` with its result. A client on a loop runs `then` on it.
        The socket client has no loop, so `then` runs on the thread.
        """
        if self._tasks is not None:

            async def on_the_loop() -> None:
                result = await anyio.to_thread.run_sync(work)
                with contextlib.suppress(BrokenPipeError):
                    then(result)

            self._tasks.start_soon(on_the_loop)
            return

        def on_a_thread() -> None:
            then(work())

        threading.Thread(target=on_a_thread, name="pymux-upload", daemon=True).start()

    def _upload_settings(self) -> UploadSettings:
        "What this client's own configuration file says. **It blocks.**"
        return upload_settings(client_options_in(self.config_file or find_config()), Path.home())

    def _granted(self) -> bool:
        return self._grant_until is not None and self._clock() < self._grant_until

    def _files_asked(self, packet: dict) -> None:
        """
        A pane asks for files on this machine. Look at the paths and
        the settings off the loop, then send or ask.
        """
        request = str(packet.get(Field.REQUEST) or "")
        paths = [str(path) for path in packet.get(Field.PATHS) or []]
        pane = str(packet.get(Field.PANE) or "?")
        if self._asking is not None:
            self._send_packet(done_packet(request, BUSY))
            return

        def look() -> tuple[UploadSettings, list[Wanted]]:
            return self._upload_settings(), inspect(paths, Path.home())

        def then(found: tuple[UploadSettings, list[Wanted]]) -> None:
            if request in self._looking:
                self._looking.discard(request)
                self._decide(request, pane, *found)

        self._looking.add(request)
        self._off_the_loop(look, then)

    def _files_withdrawn(self, request: str) -> None:
        "The `pymux receive` that asked stopped waiting: ask nothing more about it."
        self._looking.discard(request)
        if self._asking is not None and self._asking.request == request:
            self._stop_asking()

    def _decide(self, request: str, pane: str, settings: UploadSettings, wanted: list[Wanted]) -> None:
        "Send what a grant or `upload-paths` lets pass, and ask the person about the rest."
        if self._granted() or allowed(wanted, settings.roots):
            self._send_files(request, wanted)
            return
        if self._asking is not None:
            self._send_packet(done_packet(request, BUSY))
            return
        _rows, columns = self.size()
        text = question(pane, self.server_host(), wanted, settings.grant_seconds, Path.home(), columns)
        self._asking = _Asking(request, wanted, text, settings.grant_seconds, self._kitty_flags)
        # The answer has to arrive as a plain key.
        if self._kitty_flags:
            self._set_kitty_flags(0)
        self._draw_question()

    def _draw_question(self) -> None:
        "Draw the question on the last row, and put the cursor back."
        asking = self._asking
        if asking is None:
            return
        rows, _columns = self.size()
        drawn = SAVE_CURSOR + _ROW % (rows,) + REVERSE + asking.text + NO_ATTRIBUTES + RESTORE_CURSOR
        self._write_terminal(drawn.encode("utf-8"))

    def _stop_asking(self) -> _Asking | None:
        "Take the question off the terminal, and ask the server to draw over where it was."
        asking, self._asking = self._asking, None
        if asking is None:
            return None
        rows, _columns = self.size()
        self._write_terminal((SAVE_CURSOR + _ROW % (rows,) + RESTORE_CURSOR).encode("ascii"))
        flags = asking.flags_after if asking.flags_after is not None else asking.flags_before
        if flags:
            self._set_kitty_flags(flags)
        with contextlib.suppress(BrokenPipeError):
            self._send_packet({Field.CMD: Packet.REDRAW})
        return asking

    def _answer(self, keys: str) -> bool:
        """
        Take one read of the keyboard as the answer to the question.
        False when it is for the server instead.

        **A sequence goes on.** The server waits for the answers of the
        terminal, a status report among them, and they arrive here as
        input. A key that the question takes never starts with an
        escape: the keyboard flags are off while it shows.
        """
        if keys.startswith(ESCAPE) and keys != ESCAPE:
            return False
        answer = answer_of(keys)
        if answer is None:
            return True
        asking = self._stop_asking()
        if asking is None:
            return True
        if answer is Answer.NO:
            self._send_packet(done_packet(asking.request, SAID_NO))
            return True
        if answer is Answer.GRANT:
            self._grant_until = self._clock() + asking.grant_seconds
        self._send_files(asking.request, asking.wanted)
        return True

    def _send_files(self, request: str, wanted: list[Wanted]) -> None:
        "Read each file off the loop, one at a time, and send it."
        if self._tasks is not None:
            self._tasks.start_soon(self._send_files_from_the_loop, request, wanted)
            return
        threading.Thread(target=self._send_files_here, args=(request, wanted), name="pymux-upload", daemon=True).start()

    async def _send_files_from_the_loop(self, request: str, wanted: list[Wanted]) -> None:
        with contextlib.suppress(BrokenPipeError):
            for one in wanted:
                packets = await anyio.to_thread.run_sync(lambda one=one: list(outcome_packets(request, one)))
                for packet in packets:
                    self._send_packet(packet)
            self._send_packet(done_packet(request))

    def _send_files_here(self, request: str, wanted: list[Wanted]) -> None:
        with contextlib.suppress(BrokenPipeError):
            for one in wanted:
                for packet in outcome_packets(request, one):
                    self._send_packet(packet)
            self._send_packet(done_packet(request))

    def _start_browser(self, packet: dict) -> None:
        """
        Start the browser a program on the server asked for, and say
        where it listens, or why it does not. This machine is the
        server's too, so the port needs no forward. Lillecarl/pymux#653.
        """
        launch = packet[Field.BROWSER]
        try:
            process, path, kind, profile = browser.start(
                packet.get(Field.PROGRAM) or "",
                packet.get(Field.PROFILE) or "",
                packet[Field.PORT],
                packet.get(Field.ARGUMENTS) or [],
            )
        except (browser.BrowserError, OSError) as error:
            self._send_packet({Field.CMD: Packet.BROWSER_STARTED, Field.BROWSER: launch, Field.ERROR: str(error)})
            return
        self._browsers[launch] = (process, profile)
        self._send_packet(
            {Field.CMD: Packet.BROWSER_STARTED, Field.BROWSER: launch, Field.PATH: path, Field.REMOTE: kind}
        )

    async def run_browser(self, packet: dict, forward=None) -> None:
        """
        `_start_browser` for a client with an event loop, which holds
        the browser until it ends and then says so. `forward(port)`,
        when given, opens the way to it from the server's machine and
        answers why not, or None. Lillecarl/pymux#653.
        """
        launch, port = packet[Field.BROWSER], packet[Field.PORT]

        def failed(why: str) -> None:
            self._send_packet({Field.CMD: Packet.BROWSER_STARTED, Field.BROWSER: launch, Field.ERROR: why})

        try:
            process, path, kind, profile = await browser.start_async(
                packet.get(Field.PROGRAM) or "",
                packet.get(Field.PROFILE) or "",
                port,
                packet.get(Field.ARGUMENTS) or [],
            )
        except (browser.BrowserError, OSError) as error:
            failed(str(error))
            return
        if forward is not None:
            error = await forward(port)
            if error is not None:
                browser.end(process, profile)
                failed(error)
                return
        self._browsers[launch] = (process, profile)
        self._send_packet(
            {Field.CMD: Packet.BROWSER_STARTED, Field.BROWSER: launch, Field.PATH: path, Field.REMOTE: kind}
        )
        try:
            await process.wait()
        finally:
            if self._browsers.pop(launch, None) is not None:
                self._send_packet({Field.CMD: Packet.BROWSER_ENDED, Field.BROWSER: launch})

    def stop_browser(self, launch: str) -> None:
        "End a browser this client started, if it still runs."
        held = self._browsers.pop(launch, None)
        if held is not None:
            browser.end(*held)

    def stop_browsers(self) -> None:
        "End every browser this client started: the program that drove it cannot reach it any more."
        for launch in list(self._browsers):
            self.stop_browser(launch)

    def _reap_browsers(self) -> None:
        "Tell the server about each browser that ended by itself."
        for launch, (process, _profile) in list(self._browsers.items()):
            if process.poll() is not None:
                del self._browsers[launch]
                self._send_packet({Field.CMD: Packet.BROWSER_ENDED, Field.BROWSER: launch})

    def _open_url(self, url: str) -> bool:
        """
        Ask this platform to open the URL in a browser, the way
        `webbrowser` picks: "open" on macOS, "xdg-open" or what $BROWSER
        names on Linux.

        True is a best effort: an opener can still fail after it
        started. A Linux machine without a display says False without
        trying, because the openers it does have say yes and fail
        anyway: `xdg-open` starts, finds no display and exits, and
        `webbrowser.open` counts the start as success. Knowing sooner
        is worth more than the chance of a `BROWSER` that names a
        browser which runs without a display.
        """
        if sys.platform != "darwin" and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
            return False
        try:
            return bool(webbrowser.open(url))
        except Exception:
            return False

    def _process_stdin(self) -> bool:
        """
        Received data on stdin. Read and send to server.

        Returns False when stdin is over: the person's input side is
        gone, and there is nothing left this client can be given.

        An empty read is not the thing to judge by -- the reader
        returns "" also when nothing was ready to read, and when a
        paste of junk decoded to nothing at all. Its docstring says
        only the `closed` attribute is the end of the file, and that
        is what is reported here.
        """
        with nonblocking(sys.stdin.fileno()):
            data = self._stdin_reader.read()
        self._take_input(data)
        return not self._stdin_reader.closed

    def _take_input(self, data: str) -> None:
        """
        What the person typed: the answer to this client's question
        while one shows, and the server's otherwise.
        """
        if data and self._asking is not None and self._answer(data):
            return

        # Send the input in chunks, so that a long paste reaches the
        # server as keystrokes rather than as one packet the server
        # only dispatches when the whole of it has arrived.
        #
        # Nothing enforces the size. `_send_packet` sets the socket
        # blocking and hands `send` whatever it is given, so this is a
        # choice and not a limit. Four thousand and ninety six is a
        # page, and the forty bytes under it are room for the JSON
        # around the data: `{"cmd": "in", "data": ""}` is twenty five
        # of them.
        #
        # The forty do not bound the packet. JSON escapes a control
        # character as six bytes, and a paste is mostly control
        # characters in no case at all, so a full chunk can still pass
        # a page. It bounds how much arrives at once, which is what it
        # is for.
        step = 4096 - 40
        for i in range(0, len(data), step):
            self._send_packet(
                {
                    Field.CMD: Packet.IN,
                    Field.DATA: data[i : i + step],
                }
            )

    def size(self):
        "The rows and columns of the terminal this client draws on."
        return _get_size(sys.stdout.fileno())

    def _send_size(self):
        "Report terminal size to server."
        rows, cols = self.size()
        self._send_packet({Field.CMD: Packet.SIZE, Field.DATA: [rows, cols]})

    async def _watch_signal(self) -> None:
        """
        Report the size when the terminal says it changed.

        A signal that this platform does not have is one this client
        does without: `_watch_size` reads the size on a timer as well.
        """
        try:
            with anyio.open_signal_receiver(signal.SIGWINCH) as signals:
                async for _signum in signals:
                    self._send_size()
        except NotImplementedError, ValueError, RuntimeError:
            pass  # No signals here. The size stays as it was.

    async def _watch_size(self) -> None:
        """
        Tell the server whenever the terminal has a new size.

        The signal does this at once. The timer covers a terminal that
        sends no signal, and the memory client, whose server's
        application takes the signal away in the same process.

        Only a change is sent. A packet on every turn would ask the
        server to lay every window out twice a second.
        """
        last = self.size()

        while True:
            await anyio.sleep(SIZE_INTERVAL)

            size = self.size()
            if size != last:
                last = size
                self._send_size()
