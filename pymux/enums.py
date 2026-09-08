from enum import StrEnum

__all__ = [
    "COMMAND",
    "PROMPT",
    "Woke",
]


#: Name of the command buffer.
COMMAND = "COMMAND"

#: Name of the input for a "command-prompt" command.
PROMPT = "PROMPT"


class Woke(StrEnum):
    """
    What asked the clients for a frame.

    `Pymux.invalidate` writes it into the log. A frame that goes out
    when the screen did not change is a fault (Lillecarl/pymux#117),
    and the check that catches one could only say which bytes it saw.
    The reason is what makes such a run readable: it names what woke
    the client, at the moment it did. Lillecarl/pymux#180.

    `AN_APPLICATION` is the one reason pymux cannot name, and it is the
    default for that reason. Every invalidate runs through
    `on_invalidate` and comes back here, and so does a pane that
    writes, because prompt_toolkit invalidates an application from the
    events of the window that has the focus. So a run of frames with
    no named reason before them is a pane writing.
    """

    AN_APPLICATION = "an application asked for a frame"

    #: This one takes the name of the command, so it is a format
    #: string: `Woke.A_COMMAND_RAN % "kill-pane"`. The name is worth
    #: carrying, and the words around it are still written once.
    A_COMMAND_RAN = "the command %s ran"

    A_CLIENT_ATTACHED = "a client attached"
    A_CLIENT_DETACHED = "a client detached"
    A_CLIENT_RESIZED = "a client reported its size"
    A_PANE_ENDED = "a pane ended"
    A_PANE_RESIZED = "a program in a pane asked for a size"
    A_WINDOW_OPENED = "a window opened"
    A_PANE_WAS_SPLIT_OFF = "a pane was split off"
    A_PANE_BROKE_OUT = "a pane broke out into a window"
    AN_OVERLAY_OPENED = "an overlay opened"
    AN_OVERLAY_CLOSED = "an overlay closed"
    A_CLICK_CHOSE_A_WINDOW = "a click chose a window"
    A_CLICK_LEFT_THE_CLOCK = "a click left the clock"
    A_THEME_WAS_CHOSEN = "a theme was chosen"
    A_COLUMN_CHANGED_WIDTH = "a column of the strip took another width"
    A_COLUMN_MOVED = "a column of the strip moved along the row"
