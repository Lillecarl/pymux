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
from typing import Any

__all__ = [
    "END",
    "READ_SIZE",
    "Field",
    "Framer",
    "Message",
    "Mode",
    "Packet",
]

#: One packet as it crosses the wire: each `Field` to its value.
type Message = dict[str, Any]

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
    # The client queried its terminal; the server asks the rest and
    # reads every reply. The wire keeps the old word: a client of an
    # older build reconnects to a newer server and still says it.
    DETECT = "kitty-detect"
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
    # "I saved the file of `file`", or why not, under `message`.
    # Lillecarl/pymux#743.
    FILE_SAVED = "file-saved"
    # One part of a file that `send-files` asked for, in the fields of
    # `file` plus `request` and the `path` it answers; or, with `error`
    # and no `data`, why that path is not sent.
    UPLOAD = "upload"
    # "Nothing more comes for `request`", with `error` when the person
    # refused all of it.
    UPLOAD_DONE = "upload-done"
    # "Draw my terminal again from nothing": the client drew over the
    # frame, for a question of its own.
    REDRAW = "redraw"
    # The names `list-files` asked for, under `paths`.
    FILES_LISTED = "files-listed"

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
    # Save a file that a pane sent, on the machine the person sits at:
    # one part of it as base64 under `data`, with its `name`, the
    # `transfer` the parts share, and `last` on the final part. Base64,
    # because a JSON connection carries no bytes. Lillecarl/pymux#743.
    FILE = "file"
    # "Send these files from the person's machine": `pymux receive` in
    # the pane `pane` asked, under the id `request`, for `paths`. The
    # client asks the person first. Lillecarl/pymux#743.
    SEND_FILES = "send-files"
    # "Nobody waits for `request` any more": the `pymux receive` that
    # asked was stopped. The client takes its question down.
    FILES_WITHDRAWN = "files-withdrawn"
    # "Which names are in the directory `path`?", under the id
    # `request`, to complete `pymux receive`. The client answers only
    # inside a grant or `upload-paths`.
    LIST_FILES = "list-files"

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

    # A peer link: another server shows panes of this one, and may be
    # shown panes of a third the same way. Each mirror names its pane
    # by `pane_id`. Lillecarl/pymux#759.
    #
    # The peer asks. `peer` opens the link, with the `wire` formats the
    # peer reads; the connection is a peer link from then on.
    PEER = "peer"
    # "Mirror this pane": its screen frozen, then every read it takes.
    MIRROR = "mirror"
    # "Nobody here watches it": no more reads until `mirror-resume`.
    MIRROR_PAUSE = "mirror-pause"
    # "Watched again": the rows written since the pause, then reads.
    MIRROR_RESUME = "mirror-resume"
    MIRROR_STOP = "mirror-stop"
    # A person at the peer typed: bytes for the program, under `data`,
    # already encoded for the pane's keyboard mode.
    MIRROR_INPUT = "mirror-input"
    # Whether a terminal window with the focus shows the mirror, under
    # `focused`. The owner sends the program one focus report for all
    # its viewers (DEC 1004), so a peer never types one.
    MIRROR_FOCUS = "mirror-focus"
    # The terminal of the person at the peer, as the peer worked it out
    # from its own clients: `cell-size`, `color-base` and `keyboard`.
    # The owner weighs it with its own clients' terminals, and the
    # screens it settles on reach every mirror as `mirror-surrounded`.
    PEER_TERMINAL = "peer-terminal"
    # "Show me your sessions": the owner sends `tree` now and on every
    # change after.
    WATCH = "watch"
    # The windows of the owner a client at the peer looks at, and the
    # room the peer has for each, under `sizes` as
    # `[[window_id, rows, columns], ...]`. The owner counts each in
    # `window-size` as it counts a client, and lays the window out.
    PEER_SIZES = "peer-sizes"
    # This server tells.
    # Every session with its windows, splits and panes, under `rows` as
    # the live tables name them, each row as `{column: value}` so two
    # versions of pymux still read each other; `server` names the
    # owner for the peer's session names.
    TREE = "tree"
    # The pane's screen, its parser and its surroundings, frozen: the first whole, a later one only what
    # changed since the pause or the lag that stopped the reads.
    MIRROR_SCREEN = "mirror-screen"
    # One read, under `data`, with `expired` when the ground timer
    # dropped an open sequence first.
    MIRROR_READ = "mirror-read"
    MIRROR_RESIZED = "mirror-resized"
    MIRROR_SURROUNDED = "mirror-surrounded"
    # The pane's history limit changed, under `history-limit`. The
    # mirror trims its history at the same reads the pane does.
    MIRROR_HISTORY_LIMIT = "mirror-history-limit"
    # "That pane is not mirrored": there is none, under `error`.
    MIRROR_REFUSED = "mirror-refused"

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
    #: In `start-gui`: this client's terminal answers a status report,
    #: so the server may ask one after a frame and draw the next only
    #: once the terminal answered. Lillecarl/pymux#259.
    PACED = "paced"
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
    #: In `start-gui`: this client saves the files `file` packets carry.
    #: An older one drops them unread. Lillecarl/pymux#745.
    SAVES_FILES = "saves-files"
    #: In `start-gui`: this client answers `send-files`. An older one
    #: drops it unread, and `pymux receive` would wait for ever.
    SENDS_FILES = "sends-files"
    #: In `send-files` and its answers: the id of the request, and the
    #: paths it asks for.
    REQUEST = "request"
    PATHS = "paths"
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
    #: In `file`: the name the pane gave the file, and the id its parts
    #: share. Lillecarl/pymux#743.
    NAME = "name"
    TRANSFER = "transfer"
    #: In the mirror packets: a frozen screen, its parser and its
    #: surroundings (`pyte.freeze`), and the pane's history limit, size
    #: and ground timer decision. Lillecarl/pymux#759.
    SCREEN = "screen"
    STREAM = "stream"
    SURROUNDINGS = "surroundings"
    HISTORY_LIMIT = "history-limit"
    LINES = "lines"
    COLUMNS = "columns"
    EXPIRED = "expired"
    #: In `peer-terminal`: the cell in pixels as `[width, height]` or
    #: null when no terminal said, the colours (`pyte.freeze`) or null,
    #: the keyboard protocol flags every client there can report, and
    #: the theme of the client used last there.
    CELL_SIZE = "cell-size"
    COLOR_BASE = "color-base"
    KEYBOARD = "keyboard"
    THEME = "theme"
    #: In `mirror-focus`: true while the mirror has the focus.
    FOCUSED = "focused"
    #: In `tree`: `{table: [row, ...]}`, and the owner's name.
    ROWS = "rows"
    SERVER = "server"
    #: In `peer-sizes`.
    SIZES = "sizes"
    #: In `run-command` from a peer: `{dest: target}`, the targets the
    #: peer resolved, which stand in for what its words name. In its
    #: `exit`, `pane_id` is the pane the command left its caller on, and
    #: `message` the messages the command showed.
    TARGETS = "targets"
    #: In `run-command`: a peer forwarded it, for a person at the peer.
    FORWARDED = "forwarded"


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
