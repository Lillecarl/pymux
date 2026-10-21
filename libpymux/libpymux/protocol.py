"""
The names that cross the wire between a client and the server.

**A packet name spelled by hand is a silent failure.** The writer and
the reader are almost never in the same file -- a client writes
`start-gui` in `client/terminal.py` and the server reads it in
`server.py` -- and nothing checks the two agree. A typo on either side
matches nothing, raises nothing, and drops the packet: the feature
simply does not happen. Three packet kinds were added in one sitting
for Lillecarl/pymux#436 and #446, each spelled by hand in two files.

So the name lives here once, and every side imports it.
Lillecarl/pymux#447.

**It lives in the client library and not in pymux.** pymux takes
libpymux, and nothing here takes pymux, so a name in pymux could not
be reached from here without a cycle -- and this library spells
packets too (`streams.py`, `connection.py`).

**They are `StrEnum`, so nothing else has to change.** A member is a
`str` with that value: `json.dumps` writes the plain name, and a
member compares equal to the plain name that comes back off the wire.
The other side may be an older pymux spelling strings by hand and the
two still meet.

This module imports nothing else.
"""

from __future__ import annotations

from enum import StrEnum

__all__ = [
    "END",
    "Field",
    "Framer",
    "Mode",
    "Packet",
    "READ_SIZE",
]

#: The byte that ends one JSON message on the wire.
END = b"\0"

#: The most one read of a pymux connection takes: more than a unix
#: socket holds (208 KB here), so a read takes whatever is there. Measured:
#: a 1 MB frame takes 977 reads at 1 KiB and 18 at this size, and only
#: past 4 MiB does the allocation slow a small read. Lillecarl/pymux#633.
READ_SIZE = 1 << 20


class Framer:
    "The messages in a byte stream that `END` cuts up, each one once it is whole."

    def __init__(self) -> None:
        self._held = bytearray()

    def feed(self, data: bytes) -> list[bytes]:
        """
        What `data` completed. The part of a message it began waits for
        the next feed.

        **Appended, and split only when `data` ends a message.** Joining
        what is held with each read copied the whole held part every
        time: a 1 MB window pane frame read 1 KB at a time cost 1.3 s of
        copying for 5 s of video. Lillecarl/pymux#633.
        """
        self._held += data
        if END not in data:
            return []
        *whole, rest = self._held.split(END)
        self._held = rest
        return [bytes(message) for message in whole]


class Packet(StrEnum):
    """
    What a packet is, under the key `cmd`.

    The direction is in the comment because it is not in the name, and
    a reader of one side wants to know whether a packet is theirs to
    send or to expect.
    """

    # The client asks.
    START_GUI = "start-gui"
    RUN_COMMAND = "run-command"
    IN = "in"
    SIZE = "size"
    KITTY_DETECT = "kitty-detect"
    # "Send me this pane as frames, and take what I send as input." A
    # stream is not a command that prints: it holds the connection and
    # writes many packets. Lillecarl/pymux#461.
    STREAM_PANE = "stream-pane"
    STREAM_IN = "stream-in"
    # The client answers the server's `echo` with it, unchanged and at
    # once: the server measures the round trip that sizes the sound
    # buffers. Lillecarl/pymux#657.
    ECHO = "echo"

    # The client answers.
    PONG = "pong"
    # "I wrote the last `window-frame` to my terminal." The server sends
    # the next frame of any window pane only after this, so frames never
    # pile up in the buffers of an SSH link between. Lillecarl/pymux#641.
    FRAME_SHOWN = "frame-shown"
    OPEN_FAILED = "open-failed"
    FORWARDS = "forwards"
    # "The browser of `browser` listens", with the path of its endpoint,
    # or `error` when it did not start; and later "it ended".
    # Lillecarl/pymux#653.
    BROWSER_STARTED = "browser-started"
    BROWSER_ENDED = "browser-ended"

    # The server tells.
    OUT = "out"
    ERR = "err"
    EXIT = "exit"
    SUSPEND = "suspend"
    MODE = "mode"
    KITTY_KEYBOARD = "kitty-keyboard"
    # "This server is going, and another will listen on the same
    # socket": wait `wait` seconds for it instead of leaving.
    # Lillecarl/pymux#409.
    RESTARTING = "restarting"
    # One frame of a pane a client is streaming, under `data`. A relay
    # passes `data` on without reading it: the frames are the protocol
    # and this envelope is ours to change. Lillecarl/pymux#461.
    STREAM_OUT = "stream-out"
    # Part of a window pane's frame as raw pixels, which the client
    # encodes for its terminal itself (`pymux/kitty_frames.py`). A frame
    # goes in parts, so a key or a line of text is never queued behind
    # all of it; the part with `last` carries the placement, `after`.
    # Lillecarl/pymux#634.
    WINDOW_FRAME = "window-frame"
    # "Your terminal read the shared memory probe": carry frames in POSIX
    # shared memory from here on. Lillecarl/pymux#634.
    SHM = "shm"

    # The server asks, and the machine the person sits at acts.
    OPEN = "open"
    FORWARD = "forward"
    # "Send `forwards` again, now." What a forward pymux made has left
    # before it is reaped moves between the reports a change sends, so
    # `list-forwards` asks. Lillecarl/pymux#487.
    REPORT_FORWARDS = "report-forwards"
    PING = "ping"
    # Start a browser a program on the server drives, or stop it.
    # Lillecarl/pymux#653.
    BROWSER = "browser"
    BROWSER_STOP = "browser-stop"

    # Sound, on the attached connection of a client that said `audio` in
    # `start-gui`. Lillecarl/pymux#657.
    #
    # Either side: 20 ms of Opus under `data`. The server sends what
    # panes play; the client sends what its microphone hears.
    SOUND = "sound"
    # The server tells: what this client's `audio` option is, under
    # `data`, and its `audio-frame` under `frame`, in milliseconds, when
    # sound starts and when either changes.
    AUDIO = "audio"
    # The server tells: hold `buffer` milliseconds in the speaker.
    LATENCY = "latency"
    # The client tells: its speaker fell behind -- ran out of sound in
    # the middle of it, or could not take frames as fast as they came.
    # The server raises an `auto` frame on these.
    BEHIND = "behind"
    # The server tells: open the microphone, or close it, under `data`.
    # The client answers with `error` "silent" when it hears only zeros,
    # which is how macOS refuses a terminal the microphone.
    MICROPHONE = "microphone"

    # Either side: "every packet I write after this one is in `wire`".
    # `wire.py` holds the format. Lillecarl/pymux#633.
    WIRE = "wire"


class Field(StrEnum):
    """
    The keys of a packet, beside `cmd`.

    A packet is a JSON object. `cmd` says what it is and the rest of
    its keys carry the payload, and both sides read and write them. A
    key one side spells wrong matches nothing and raises nothing, the
    same silent drop a command name has, so they live here once.
    Lillecarl/pymux#447.
    """

    CMD = "cmd"
    DATA = "data"
    #: In `start-gui`, the formats a client reads; in a `wire` packet,
    #: the one its writer turns to.
    WIRE = "wire"
    CODE = "code"
    PANE = "pane"
    PANE_ID = "pane_id"
    WRITABLE = "writable"
    DETACH_OTHERS = "detach-others"
    HANG_UP = "hang-up"
    HANG_UP_OTHERS = "hang-up-others"
    READ_ONLY = "read-only"
    COLOR_DEPTH = "color-depth"
    TERM = "term"
    COLORTERM = "colorterm"
    HOSTNAME = "hostname"
    FORWARDS = "forwards"
    #: In `start-gui`: this client answers `report-forwards`.
    REPORTS = "reports"
    PINGS = "pings"
    ENVIRONMENT = "environment"
    #: The directory a detached command is sent from. A job without
    #: `-d` runs there, the way it would had the caller run it itself.
    CWD = "cwd"
    TTYNAME = "ttyname"
    PID = "pid"
    #: Who a client is across reattaches to a restarted server.
    CLIENT_ID = "client-id"
    CLIENT_OPTIONS = "client-options"
    DIRECTION = "direction"
    LISTEN_HOST = "listen_host"
    LISTEN_PORT = "listen_port"
    DEST_HOST = "dest_host"
    DEST_PORT = "dest_port"
    REMOVE = "remove"
    MESSAGE = "message"
    FORWARD = "forward"
    #: How many seconds a client waits for the next server after a
    #: `restarting` packet.
    WAIT = "wait"
    #: In `start-gui`: this client takes `window-frame` packets.
    FRAMES = "frames"
    #: In `start-gui`: this client answers each frame with `frame-shown`.
    SHOWS = "shows"
    #: In the last part of a `window-frame`: let the terminal answer the
    #: frame's last command, which the server counts as `frame-shown`.
    #: kitty answers once it processed the command, and the client's
    #: write says only that kitty read it. Lillecarl/pymux#643.
    ANSWER = "answer"
    #: In `window-frame`: the outer image id, whether the parts make the
    #: whole frame, its size, the bands of this part as `[x, y, w, h,
    #: pixels]` with RGB pixels, whether it is the last part, and what
    #: goes to the terminal after the frame.
    IMAGE = "image"
    WHOLE = "whole"
    WIDTH = "width"
    HEIGHT = "height"
    RECTS = "rects"
    LAST = "last"
    AFTER = "after"
    #: In the browser packets: which launch, the port its browser
    #: listens on, on both machines, the program and profile the person
    #: chose ("" for the default), the arguments the program on the
    #: server passed, the endpoint's path and the protocol it speaks
    #: ("cdp" or "bidi"), and why it did not start. Lillecarl/pymux#653.
    BROWSER = "browser"
    #: In `start-gui`: this client starts browsers.
    BROWSERS = "browsers"
    PORT = "port"
    PROGRAM = "program"
    PROFILE = "profile"
    ARGUMENTS = "arguments"
    PATH = "path"
    REMOTE = "remote"
    ERROR = "error"
    #: In `start-gui`, the `Codec`s a client decodes. In `window-frame`,
    #: the one the frame is in, its encoded bytes in parts under `video`,
    #: and `rects` as `[x, y, w, h]` with no pixels: what changed, which
    #: the client takes from the decoded frame. Lillecarl/pymux#639.
    CODECS = "codecs"
    CODEC = "codec"
    VIDEO = "video"
    #: In `start-gui`: this client plays sound. In `echo`, the server's
    #: clock when it sent it, which comes back unchanged. In `latency`,
    #: milliseconds of buffer. In `sound`, the channels of the frame.
    #: Lillecarl/pymux#657.
    AUDIO = "audio"
    TIME = "time"
    BUFFER = "buffer"
    CHANNELS = "channels"
    FRAME = "frame"
    #: In `audio`, the bits a second the client's microphone encodes
    #: at. Lillecarl/pymux#674.
    BITRATE = "bitrate"


class Codec(StrEnum):
    "How a window pane's frame is encoded, when it is not raw pixels."

    VP9 = "vp9"


class Mode(StrEnum):
    """
    What the `mode` packet asks the client's terminal to be.

    `RESTORE` puts back whatever the last one replaced, so the three
    are a stack and not a setting. `client/terminal.py` keeps it.
    """

    RAW = "raw"
    COOKED = "cooked"
    RESTORE = "restore"
