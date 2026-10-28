from __future__ import annotations

from enum import StrEnum

__all__ = [
    "CHOOSE",
    "COMMAND",
    "PROMPT",
    "Chooser",
    "EditingKeys",
    "FileTransfer",
    "PaneBrowser",
    "WindowCodec",
    "WindowScroll",
    "WindowSize",
    "Woke",
]


class EditingKeys(StrEnum):
    """
    Which keys a line a person types on takes: `status-keys` for the
    command line and prompts, `mode-keys` for copy mode.
    """

    EMACS = "emacs"
    VI = "vi"
    #: Helix's modal keys, through ptkhx. The command line and prompts
    #: only, so far. Lillecarl/pymux#749.
    HELIX = "helix"


#: Name of the command buffer.
COMMAND = "COMMAND"

#: Name of the input for a "command-prompt" command.
PROMPT = "PROMPT"

#: Name of the buffer that narrows the window chooser's list.
CHOOSE = "CHOOSE"


class Chooser(StrEnum):
    """
    Which chooser a client shows. One value, because one shows at a
    time. The values are the names a snapshot stores.

    The windows flow across a bar; every other kind is a box.
    """

    WINDOW = "choose_window"
    BUFFER = "choose_buffer"
    OPTIONS = "choose_options"
    NOTIFICATIONS = "choose_notifications"
    JOB = "choose_job"


class WindowCodec(StrEnum):
    """
    How the Wayland windows in a window reach a client: as the pixels
    that changed, or as a VP9 stream, which costs some sharpness and a
    few milliseconds of encoding for far fewer bytes on a slow link. A
    client that cannot decode the codec gets the pixels.
    Lillecarl/pymux#639.
    """

    LOSSLESS = "lossless"
    VP9 = "vp9"


class PaneBrowser(StrEnum):
    """
    Which programs in a pane find `pymux-browser` as their browser, and
    so drive the browser on the machine of the person instead of one on
    the server. Each launch is still asked. Lillecarl/pymux#653.
    """

    #: None of them.
    OFF = "off"
    #: Playwright MCP and `playwright-cli`, through
    #: `PLAYWRIGHT_MCP_EXECUTABLE_PATH`: an agent's browser, which a
    #: person wants to see.
    AGENTS = "agents"
    #: Puppeteer too, through `PUPPETEER_EXECUTABLE_PATH`, which a test
    #: suite reads as well: every one of its launches would ask.
    ALL = "all"


class AudioServerMode(StrEnum):
    """
    Whose PipeWire holds the speaker and the microphone that panes play
    into and record from, which pymux carries to the client used last.
    `pymux/audio.py` says what each costs. Lillecarl/pymux#657.
    """

    #: Private when the build carries PipeWire, shared otherwise.
    AUTO = "auto"
    #: A PipeWire and a WirePlumber of this server's own.
    PRIVATE = "private"
    #: The PipeWire the server finds, with the speaker named to panes.
    SHARED = "shared"
    OFF = "off"


class ClientAudio(StrEnum):
    "What a client does with the sound of the server. Lillecarl/pymux#657."

    #: Play it, and lend the microphone while a program records.
    ON = "on"
    #: Play it, and never open the microphone.
    PLAYBACK = "playback"
    OFF = "off"


class AudioFrame(StrEnum):
    """
    How much sound one packet carries, in milliseconds: every size Opus
    encodes. Shorter is less delay, because each end buffers two frames
    and its device one; longer is fewer packets. Lillecarl/pymux#657.

    `auto` starts at the shortest and goes up a size each time the link
    cannot keep up (`AudioLink` in `pymux/audio.py`).
    """

    AUTO = "auto"
    MS_2_5 = "2.5"
    MS_5 = "5"
    MS_10 = "10"
    MS_20 = "20"
    MS_40 = "40"
    MS_60 = "60"

    @property
    def ms(self) -> float:
        "Its length, and for `auto` the length it starts at."
        return 2.5 if self is AudioFrame.AUTO else float(self.value)


class FileTransfer(StrEnum):
    """
    Who saves a file that a program in a pane sends to the person: the
    terminal of the client, or the pymux client itself.
    Lillecarl/pymux#743.
    """

    #: `passthrough` for a client on the machine of the server, and
    #: `pymux` for a client that came over SSH.
    AUTO = "auto"
    #: The pymux client writes the file on the machine it runs on.
    PYMUX = "pymux"
    #: The sequence of the pane goes on to the terminal of the client.
    PASSTHROUGH = "passthrough"


class WindowScroll(StrEnum):
    """
    What one scroll event from a client's terminal does to a Wayland
    window. kitty sends a trackpad as one event per cell of finger
    travel, and a mouse wheel as one per notch: the terminal says which
    it was nowhere, so the person at the client does.
    Lillecarl/pymux#652.
    """

    #: One cell of pixels, which the program scrolls exactly: a trackpad.
    SMOOTH = "smooth"
    #: One wheel notch, which the program scrolls by its own step: a wheel.
    NOTCH = "notch"


class WindowSize(StrEnum):
    """
    Who decides how big a window's plane is.

    A plane is shared and a view is not: a pane has one pty, so it has
    one size however many clients look at it. This says which client's
    terminal that one size comes from. Decision 11 of
    `docs/layout-engine-plan.md`, and tmux spells the four the same
    way.

    `SMALLEST` is the default and what pymux always did. Every client
    sees the whole window, and the biggest of them draws background
    around it.

    `LARGEST` is the one views make worth having. tmux leaves a client
    too small to see the whole window stuck at the top left of it;
    here that client moves its view over the plane instead, so it
    reaches every pane.

    `LATEST` follows whoever last used a client, which is what a person
    with a laptop and a desktop on one session wants: the terminal
    they are typing in gets the window.

    `MANUAL` is a size a person set, and no client changes it.
    `resize-window` sets one and turns this on. **A manual size is the
    window's own**, so no status row comes off it: `resize-window -x
    100 -y 40` means a window of a hundred by forty.
    """

    SMALLEST = "smallest"
    LARGEST = "largest"
    LATEST = "latest"
    MANUAL = "manual"


class Woke(StrEnum):
    """
    What asked the clients for a frame.

    `Pymux.invalidate` writes it into the log. A frame that goes out
    when the screen did not change is a fault (Lillecarl/pymux#117),
    and the check that catches one could only say which bytes it saw.
    The reason is what makes such a run readable: it names what woke
    the client, at the moment it did. Lillecarl/pymux#180.

    `APPLICATION` is the one reason pymux cannot name, and it is the
    default for that reason. It is what `client_asked_for_frame`
    counts: one client's application invalidated itself, which is what
    a pane that writes does. So a run of frames with no named reason
    before them is a pane writing.
    """

    APPLICATION = "an application asked for a frame"

    #: This one takes the name of the command, so it is a format
    #: string: `Woke.COMMAND_RAN % "kill-pane"`. The name is worth
    #: carrying, and the words around it are still written once.
    COMMAND_RAN = "the command %s ran"

    CLIENT_ATTACHED = "a client attached"
    CLIENT_DETACHED = "a client detached"
    SESSION_OPENED = "a session opened"
    SESSION_CLOSED = "a session closed"
    SESSION_CHANGED = "a client moved to another session"
    CLIENT_RESIZED = "a client reported its size"
    PANE_ENDED = "a pane ended"
    PANE_WAS_RESPAWNED = "a pane was respawned"
    PANE_RESIZED = "a program in a pane asked for a size"
    PANE_RANG = "a pane rang its bell where nobody saw it"
    WINDOW_MARKED = "a window nobody saw wrote, went quiet or notified"
    WINDOW_OPENED = "a window opened"
    PANE_WAS_SPLIT_OFF = "a pane was split off"
    PANE_BROKE_OUT = "a pane broke out into a window"
    OVERLAY_OPENED = "an overlay opened"
    OVERLAY_CLOSED = "an overlay closed"
    CLICK_CHOSE_A_WINDOW = "a click chose a window"
    CLICK_CHOSE_A_BUFFER = "a chooser chose a buffer"
    CLICK_LEFT_THE_CLOCK = "a click left the clock"
    THEME_WAS_CHOSEN = "a theme was chosen"
    COLUMN_CHANGED_WIDTH = "a column of the strip took another width"
    COLUMN_MOVED = "a column of the strip moved along the row"
    PANE_CHANGED_COLUMN = "a pane joined another column of the strip, or left one"
    FORWARDS_CHANGED = "a client reported the ports it forwards"
    #: A pane's clipboard write or notification is held until the next
    #: frame, so that it lands with the screen that shows what it is
    #: about. A pane in a window nobody looks at draws no frame of its
    #: own, and its notification has to go out anyway, so this asks for
    #: one. Lillecarl/pymux#478.
    PANE_WROTE_AN_OSC = "a pane wrote an escape for the terminal of a client"
    NOTIFICATION_SENT = "pymux sent a notification to the terminal of a client"
    #: A clipboard query goes behind a frame, after any copy queued
    #: before it, so a paste reads what was just copied.
    #: Lillecarl/pymux#383.
    CLIPBOARD_ASKED = "a pane pasted, so a client's terminal is asked for its clipboard"
    #: A copy for the person's clipboard goes behind a frame too.
    CLIPBOARD_WRITTEN = "a copy goes to the clipboard of the clients"
    #: Window panes. Lillecarl/pymux#620.
    WINDOW_PANE_OPENED = "a Wayland window opened in a pane"
    WINDOW_PANE_CHANGED = "a Wayland window took a title or an app id"


class ExtendedKeys(StrEnum):
    """
    How much of the keyboard a session uses.

    `OFF` is the escape hatch. A program that misbehaves under the
    extended encodings has somewhere to go, and so does a person who
    attaches with a terminal that claims more than it does: the whole
    session steps down for as long as they choose, and steps back up
    without restarting anything.

    `ALWAYS` is for a terminal that speaks the protocol and does not
    answer the query that asks. There are some.
    """

    OFF = "off"
    ON = "on"
    ALWAYS = "always"


class Clipboard(StrEnum):
    """
    Who may write the clipboard of the person at the keyboard.

    `EXTERNAL` is the careful one, and tmux's default: a copy the
    person makes in copy mode goes out to their terminal, and a
    program in a pane cannot reach the clipboard at all.

    `ON` lets a program reach it, which is what a terminal does when
    nothing stands between the two. It is a real choice: a program on
    the other side of an ssh connection can then write what a person
    pastes next.
    """

    OFF = "off"
    EXTERNAL = "external"
    ON = "on"


class OpenUrlTarget(StrEnum):
    """
    Which client's browser "open-url" opens in.

    `LAST` is the client somebody used last, by the stamp that
    `window-size latest` reads. `BROADCAST` is every attached client.
    """

    LAST = "last"
    BROADCAST = "broadcast"


class OpenUrlMode(StrEnum):
    """
    What happens to a request to open a URL.

    `ASK` shows "(y/n)" in the command bar of the clients it would
    land on, and a yes runs `open-url -c`, so the question is asked
    once.
    """

    OPEN = "open"
    ASK = "ask"
    OFF = "off"


class ForwardMode(StrEnum):
    """
    What happens to a request to forward a port.

    **`ON` is not "never ask".** A request that binds somewhere other
    than loopback, and one that a program in a pane made rather than
    the person, are confirmed whatever this says: `Pymux.forward_needs_asking`
    holds the two. The port belongs to the machine somebody is sitting
    at. Lillecarl/pymux#440.
    """

    ON = "on"
    ASK = "ask"
    OFF = "off"


class RenderMode(StrEnum):
    """
    How a client's frames reach its terminal.

    `OPTIMIZED` writes what changed since the frame before, scrolls
    rows on the wire instead of repainting them, and builds a pane's
    rows only when they changed. `REFERENCE` writes every cell of
    every frame at its own position and keeps nothing between frames.
    It is slow on purpose: it is the answer the optimized frames are
    held against, and the way round a fault in them until it is fixed.
    """

    OPTIMIZED = "optimized"
    REFERENCE = "reference"


class Justify(StrEnum):
    "Where the window list sits in the status line: `status-justify`."

    LEFT = "left"
    CENTER = "center"
    RIGHT = "right"
