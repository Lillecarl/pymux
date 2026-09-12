from enum import StrEnum

__all__ = [
    "COMMAND",
    "PROMPT",
    "CHOOSE",
    "WindowSize",
    "Woke",
]


#: Name of the command buffer.
COMMAND = "COMMAND"

#: Name of the input for a "command-prompt" command.
PROMPT = "PROMPT"

#: Name of the buffer that narrows the window chooser's list.
CHOOSE = "CHOOSE"


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
    default for that reason. It is what `client_asked_for_a_frame`
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
