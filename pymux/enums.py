from __future__ import annotations

from enum import StrEnum

__all__ = [
    "CHOOSE",
    "COMMAND",
    "PROMPT",
    "Chooser",
    "PaneBrowser",
    "WindowCodec",
    "WindowScroll",
    "WindowSize",
    "Woke",
]


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
    """

    MS_2_5 = "2.5"
    MS_5 = "5"
    MS_10 = "10"
    MS_20 = "20"
    MS_40 = "40"
    MS_60 = "60"

    @property
    def ms(self) -> float:
        return float(self.value)


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
    #: A clipboard query goes behind a frame, after any copy queued
    #: before it, so a paste reads what was just copied.
    #: Lillecarl/pymux#383.
    CLIPBOARD_ASKED = "a pane pasted, so a client's terminal is asked for its clipboard"
    #: A copy for the person's clipboard goes behind a frame too.
    CLIPBOARD_WRITTEN = "a copy goes to the clipboard of the clients"
    #: Window panes. Lillecarl/pymux#620.
    WINDOW_PANE_OPENED = "a Wayland window opened in a pane"
    WINDOW_PANE_CHANGED = "a Wayland window took a title or an app id"
