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
    "CAPABILITY_SINCE",
    "END",
    "OLDEST_PEER_VERSION",
    "PEER_VERSION",
    "READ_SIZE",
    "Capability",
    "Field",
    "Framer",
    "Message",
    "Mode",
    "Packet",
    "capabilities_in_common",
    "capabilities_named",
    "peer_upgrade",
    "peer_version",
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

#: The version of what a peer link carries, and the oldest one this
#: build still speaks. A build that changes the link so that an older
#: one misreads it raises `PEER_VERSION`; one that stops speaking the
#: older form raises `OLDEST_PEER_VERSION` too. Every field is named
#: and has a default, so most changes need neither, and a change that
#: only adds is a `Capability`. Lillecarl/pymux#759, Lillecarl/pymux#959.
PEER_VERSION = 13
OLDEST_PEER_VERSION = 1


class Capability(StrEnum):
    """
    One thing a peer link carries that an older build does not.

    **A feature that adds to the link is a capability, not a version.**
    `PEER_VERSION` moves only when a change would make an older side
    misread the link. Each side names its capabilities in `peer` and
    `peer-accepted`, and the link uses those both named. Two workspaces
    that each add one no longer claim the same number, and one can be
    dropped alone. Lillecarl/pymux#959.
    """

    #: A frame may name an image the peer stored by its hash instead of
    #: sending it again (`pyte.grid`).
    IMAGES_BY_HASH = "images-by-hash"
    #: A peer answers a ping, so an owner may drop a link that stops
    #: answering. Lillecarl/pymux#877.
    PINGS = "pings"
    #: An owner lends a Wayland window pane: its pixels as
    #: `mirror-window-frame`s, and the person's input back as
    #: `mirror-window-input`. Lillecarl/pymux#888.
    WINDOWS = "windows"
    #: A `mirror-osc` that opens a URL or saves a download says under
    #: `acts` whether this peer is the one server that does it.
    #: Lillecarl/pymux#854.
    ACTS = "acts"
    #: A pane's kitty clipboard write reaches a peer whole, as
    #: `mirror-clipboard-write`, and not only its text as OSC 52; and a
    #: pane's read may go to a peer's terminal (`mirror-clipboard-read`).
    #: Lillecarl/pymux#902.
    RICH_CLIPBOARD = "rich-clipboard"
    #: A frame carries what a pane's program registered for kitty's drag
    #: and drop, under `dnd`, and its OSC 72 packets cross as
    #: `mirror-dnd`. Lillecarl/pymux#903.
    DND = "dnd"
    #: A mirrored pane's kitty file transfer (OSC 5113) crosses as
    #: `mirror-kitty-transfer`, and its pacing as
    #: `mirror-kitty-transfer-taken`. Lillecarl/pymux#912.
    FILE_TRANSFER = "file-transfer"
    #: A pane's OSC 7501 records cross whole, as `mirror-program-status`,
    #: whenever they change. Lillecarl/pymux#928.
    PROGRAM_STATUS = "program-status"
    #: Either side answers `report-log`, and `peer` names the peer's host
    #: under `server`. Lillecarl/pymux#934.
    LOGS = "logs"
    #: A peer sends the owner the files of a client recording made on
    #: one of the owner's sessions, as `recording`. Lillecarl/pymux#955.
    RECORDINGS = "recordings"
    #: A peer passes on what a client's `clipboard-image` says, as
    #: `mirror-clipboard-image`, for the owner's compositor to offer.
    #: Lillecarl/pymux#956.
    IMAGE_PASTE = "image-paste"
    #: The owner asks a peer for the files of `pymux receive`, as
    #: `mirror-send-files`, and for the names that complete one, as
    #: `mirror-list-files`. The peer's client answers, and the peer
    #: passes each answer back. Lillecarl/pymux#947.
    FILES = "files"


#: The version each capability came with, while each was a version. A
#: side that names no capabilities is older than they are, and has every
#: one its version reaches. A new capability is not added here.
CAPABILITY_SINCE: dict[Capability, int] = {
    Capability.IMAGES_BY_HASH: 2,
    Capability.PINGS: 3,
    Capability.WINDOWS: 4,
    Capability.ACTS: 5,
    Capability.RICH_CLIPBOARD: 6,
    Capability.DND: 7,
    Capability.FILE_TRANSFER: 8,
    Capability.PROGRAM_STATUS: 9,
    Capability.LOGS: 10,
    Capability.RECORDINGS: 11,
    Capability.IMAGE_PASTE: 12,
    Capability.FILES: 13,
}


def capabilities_named() -> list[str]:
    "What this side names under `capabilities`."
    return [capability.value for capability in Capability]


def capabilities_in_common(version: int, named: object) -> frozenset[Capability]:
    """
    What a link carries, with a side that speaks `version` and named
    `named` (a list, or nothing from a side older than names).
    """
    if isinstance(named, list):
        return frozenset(capability for capability in Capability if capability.value in named)
    return frozenset(capability for capability, since in CAPABILITY_SINCE.items() if version >= since)


def peer_upgrade(version: object, oldest: object) -> str | None:
    """
    Which side to upgrade before a link to a side of `version` and
    `oldest` works: "there", "here", or `None` when the two speak a
    version in common. A side that names no version is older than any.
    """
    theirs = version if isinstance(version, int) else 0
    their_oldest = oldest if isinstance(oldest, int) else theirs
    if theirs < OLDEST_PEER_VERSION:
        return "there"
    if PEER_VERSION < their_oldest:
        return "here"
    return None


def peer_version(version: object) -> int:
    "The version a link speaks with a side that names `version`, once `peer_upgrade` let it through."
    return min(PEER_VERSION, version if isinstance(version, int) else 0)


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
    # Every client option a person set on this client, as `[name, value]`
    # under `client-options`, each time one changes: to a client that
    # said `keeps-options`, so it can name them in its next `start-gui`.
    # Lillecarl/pymux#957.
    CLIENT_OPTIONS = "client-options"
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
    # Either side: one command of kitty's file transfer, OSC 5113, as it
    # stands after `5113;`, under `data`. The server sends what the
    # program in the pane `pane` said, and the client answers as the
    # terminal. Lillecarl/pymux#912.
    KITTY_TRANSFER = "kitty-transfer"
    # Either side, with a session's id under `data`. The client sends it
    # after a part of a file the program reads, and the server sends it
    # back once the pane's program has taken that part. The client sends
    # no more of the file before the answer. Lillecarl/pymux#914.
    KITTY_TRANSFER_TAKEN = "kitty-transfer-taken"
    # Server: stop reading the terminal, and start again. A file that the
    # client's terminal sends a pane through kitty's file transfer
    # waits, in the terminal, for the pane's program to read what came
    # before. Lillecarl/pymux#915.
    HOLD_INPUT = "hold-input"
    RELEASE_INPUT = "release-input"

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
    # peer reads, and the `version` and `oldest` of the link it speaks;
    # the connection is a peer link from then on.
    PEER = "peer"
    # The owner answers `peer`: its own `version` and `oldest`, first on
    # the link, or `peer-refused` with an `error` that names the side to
    # upgrade, and the connection closes.
    PEER_ACCEPTED = "peer-accepted"
    PEER_REFUSED = "peer-refused"
    # "Mirror this pane": its page as `mirror-frame`s, the first whole.
    MIRROR = "mirror"
    # "Nobody here watches it": no more frames until `mirror-resume`.
    MIRROR_PAUSE = "mirror-pause"
    # "Watched again": a frame of what changed since the last one.
    MIRROR_RESUME = "mirror-resume"
    MIRROR_STOP = "mirror-stop"
    # A person at the peer pressed a key: the input under `data` as
    # their terminal sent it, and `key-profile`. The owner encodes it
    # for the pane's keyboard mode, which only the owner holds.
    MIRROR_KEY = "mirror-key"
    # A person at the peer pasted the text under `data`.
    MIRROR_PASTE = "mirror-paste"
    # The mouse of a person at the peer, under `mouse`: `action`
    # (`pyte.mouse.MouseAction`), `column` and `row` on the page.
    MIRROR_MOUSE = "mirror-mouse"
    # Whether a terminal window with the focus shows the mirror, under
    # `focused`. The owner sends the program one focus report for all
    # its viewers (DEC 1004), so a peer never types one.
    MIRROR_FOCUS = "mirror-focus"
    # "Open copy mode on this pane for me." The owner runs it and its
    # frames show it, until the person leaves it with the keys they
    # send meanwhile, which go to copy mode and not to the program.
    # Optional `copying` puts the caret and selection back.
    MIRROR_COPY = "mirror-copy"
    # Bytes for the program under `data`, such as a terminal's reply,
    # which the owner writes as they are: unlike `mirror-key`, nothing
    # encodes them. An owner too old to know it drops it.
    MIRROR_WRITE = "mirror-write"
    # A person at the peer used a mirrored window pane, under `data`:
    # `{"kind": "key", "modifiers": [...], "code": n}` in evdev codes,
    # `{"kind": "text", "text": ...}`, or `{"kind": "pointer" | "scroll",
    # "x", "y"}` in cells with the fraction of the cell, plus `button`
    # and `pressed`, or `steps`. Lillecarl/pymux#888.
    MIRROR_WINDOW_INPUT = "mirror-window-input"
    # "I took the window frame you sent": the owner reads the next one.
    MIRROR_WINDOW_SHOWN = "mirror-window-shown"
    # The terminal of the person at the peer, as the peer worked it out
    # from its own clients: `cell-size`, `color-base` and `keyboard`.
    # The owner weighs it with its own clients' terminals and answers a
    # program's questions about the terminal with what it settles on.
    PEER_TERMINAL = "peer-terminal"
    # "Show me your sessions": the owner sends `tree` now and on every
    # change after.
    WATCH = "watch"
    # The windows of the owner a client at the peer looks at, and the
    # room the peer has for each, under `sizes` as
    # `[[window_id, rows, columns], ...]`. The owner counts each in
    # `window-size` as it counts a client, and lays the window out.
    PEER_SIZES = "peer-sizes"
    # On a stream of its own beside the link, in msgpack: "connect to
    # `dest_host:dest_port` on the machine pane `pane_id` runs on". A
    # server that only mirrors the pane asks its own owner the same.
    # Then `tunnel-data` both ways, the bytes under `data`, until either
    # end closes the stream. Lillecarl/pymux#853.
    TUNNEL = "tunnel"
    TUNNEL_DATA = "tunnel-data"
    # This server tells.
    # Every session with its windows, splits and panes, under `rows` as
    # the live tables name them, each row as `{column: value}` so two
    # versions of pymux still read each other; `server` names the
    # owner for the peer's session names.
    TREE = "tree"
    # The rows of the pane's page that changed and its cursor, under
    # `frame` (`pyte.grid`). One is in flight per pane at a time, so a
    # peer that falls behind gets the newest page and not each one.
    MIRROR_FRAME = "mirror-frame"
    # A window pane's frame, as `window-frame` carries one, under
    # `pane_id` and not `image`. One is in flight per pane until
    # `mirror-window-shown`. Lillecarl/pymux#888.
    MIRROR_WINDOW_FRAME = "mirror-window-frame"
    # An OSC sequence a pane wrote for the terminal of a person, under
    # `code` and `data`: the clipboard, a notification, the directory.
    # It goes for every pane of a session the peer watches, seen or
    # not, and the peer serves it as one of its own panes said it.
    MIRROR_OSC = "mirror-osc"
    # The person at the peer copied the text under `data` in copy mode:
    # it goes to their own clipboard, as a person's copy and not as a
    # program's OSC 52.
    MIRROR_COPIED = "mirror-copied"
    # A pane's kitty clipboard write: `data` maps each MIME type to its
    # base64, `aliases` each alias to the type it shares, with
    # `primary`, `name` and `password` as the pane gave them.
    MIRROR_CLIPBOARD_WRITE = "mirror-clipboard-write"
    # A pane's kitty clipboard read, as the OSC 5522 the peer's terminal
    # is to get, under `data`; the peer sends each packet of the answer
    # back as `mirror-clipboard-answer`, under the owner's id.
    MIRROR_CLIPBOARD_READ = "mirror-clipboard-read"
    MIRROR_CLIPBOARD_ANSWER = "mirror-clipboard-answer"
    # A paste event at the peer gave a mirrored pane the password under
    # `data`: the owner sends a read under it to this peer.
    MIRROR_CLIPBOARD_PASSWORD = "mirror-clipboard-password"
    # One OSC 72 packet of a drop or a drag over a mirrored pane, after
    # its code, under `data`: from the peer's terminal to the pane's
    # program, and back.
    MIRROR_DND = "mirror-dnd"
    # One OSC 5113 command of a mirrored pane, after its code, under
    # `data`: from the pane's program to the peer, and the answers of the
    # peer's terminal back.
    MIRROR_KITTY_TRANSFER = "mirror-kitty-transfer"
    # A session's id under `data`: the peer asks when the pane's program
    # has taken what it was sent, and the owner answers with the same.
    MIRROR_KITTY_TRANSFER_TAKEN = "mirror-kitty-transfer-taken"
    # Every OSC 7501 record of a pane, as a list of report payloads
    # under `data`, oldest update first. It replaces what the peer held.
    MIRROR_PROGRAM_STATUS = "mirror-program-status"
    # "That pane is not mirrored": there is none, under `error`.
    MIRROR_REFUSED = "mirror-refused"

    # Either side: "every packet I write after this one is in `wire`".
    # `wire.py` holds the format. Lillecarl/pymux#633.
    WIRE = "wire"
    # Either side, on an attached client's connection and on a peer link:
    # "send the last `lines` lines of your machine's pymux log", under
    # the id `request`. The answer is `log`, with the text under `data`,
    # or `error`. Lillecarl/pymux#934.
    REPORT_LOG = "report-log"
    LOG = "log"
    # Peer to owner: a piece of one file of a client recording made on
    # one of the owner's sessions, under `name` and `data` (base64),
    # at byte `offset`, with `last` on the final piece of that file.
    # Lillecarl/pymux#955.
    RECORDING = "recording"
    # Client to server, ahead of the Ctrl-V that pastes it: the image
    # the person's machine holds on its clipboard, as base64 PNG under
    # `data`, or "" for none. A peer passes it to the owner of the pane
    # the key goes to as `mirror-clipboard-image`. Lillecarl/pymux#956.
    CLIPBOARD_IMAGE = "clipboard-image"
    MIRROR_CLIPBOARD_IMAGE = "mirror-clipboard-image"
    # Owner to peer: `send-files` and `list-files` for a person who
    # attaches through the peer, with the owner's `request` and
    # `pane_id`; and `files-withdrawn` when `pymux receive` stops
    # waiting. Peer to owner: each `upload` and `upload-done` of the
    # peer's client, whole under `data`, and the names it listed.
    # Lillecarl/pymux#947.
    MIRROR_SEND_FILES = "mirror-send-files"
    MIRROR_FILES_WITHDRAWN = "mirror-files-withdrawn"
    MIRROR_LIST_FILES = "mirror-list-files"
    PEER_UPLOAD = "peer-upload"
    PEER_FILES_LISTED = "peer-files-listed"


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
    #: In a `mirror-osc`, whether the peer opens the URL or saves the
    #: download. A packet without it is from an owner that lets every
    #: server act.
    ACTS = "acts"
    #: In a `mirror-clipboard-write` (`Packet`).
    ALIASES = "aliases"
    PRIMARY = "primary"
    PASSWORD = "password"
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
    KEEPS_OPTIONS = "keeps-options"
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
    #: In `start-gui`: this client answers `kitty-transfer` as the
    #: terminal. An older one drops it unread. Lillecarl/pymux#912.
    KITTY_TRANSFERS = "kitty-transfers"
    #: In `send-files` and its answers: the id of the request, and the
    #: paths it asks for.
    REQUEST = "request"
    PATHS = "paths"
    #: In `report-log`: how many lines of the log to send.
    LINES = "lines"
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
    #: In `recording`: where in its file a piece starts.
    OFFSET = "offset"
    TRANSFER = "transfer"
    #: In `peer` and `peer-accepted`: the newest version of a peer link
    #: a side speaks, and the oldest (`PEER_VERSION`).
    VERSION = "version"
    OLDEST = "oldest"
    #: In `peer` and `peer-accepted`: what a side's link can carry,
    #: as `Capability` names. Lillecarl/pymux#959.
    CAPABILITIES = "capabilities"
    #: In `mirror-key`: what the person's terminal does where terminals
    #: split (`pyte.keys.events.KeyProfile`), by field name. In
    #: `mirror-mouse`: what the mouse did. Lillecarl/pymux#759.
    KEY_PROFILE = "key-profile"
    MOUSE = "mouse"
    #: In `peer-terminal`: the cell in pixels as `[width, height]` or
    #: null when no terminal said, the colours (`pyte.osc.color_base_data`) or null,
    #: the keyboard protocol flags every client there can report, and
    #: the theme of the client used last there.
    CELL_SIZE = "cell-size"
    COLOR_BASE = "color-base"
    KEYBOARD = "keyboard"
    THEME = "theme"
    #: In `mirror-focus`: true while the mirror has the focus.
    FOCUSED = "focused"
    #: In `mirror-frame`: `[caret, start, kind]` of the copy mode the
    #: frame shows, or null outside it; start and kind are null with no
    #: selection. In `mirror-copy`: where to open it, so a peer that
    #: links again comes back to the same place.
    COPYING = "copying"
    #: In `tree`: `{table: [row, ...]}`, and the owner's name and id.
    ROWS = "rows"
    SERVER = "server"
    SERVER_ID = "server-id"
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
