"""
The names that cross the wire between a client and the server.

**A packet name spelled by hand is a silent failure.** The writer and
the reader are almost never in the same file -- a client writes
`start-gui` in `client/terminal.py` and the server reads it in
`server.py` -- and nothing checks the two agree. A typo on either side
matches nothing, raises nothing, and drops the packet: the feature
simply does not happen. Three packet kinds were added in one sitting
for Lillecarl/pymux#436 and #446, each spelled by hand in two files.

So the name lives here once, and both sides import it.
Lillecarl/pymux#447.

**They are `StrEnum`, so nothing else has to change.** A member is a
`str` with that value: `json.dumps` writes the plain name, and a
member compares equal to the plain name that comes back off the wire.
The other side may be an older pymux spelling strings by hand and the
two still meet.

This module imports nothing of pymux's. Both halves of the protocol
depend on it, so anything it reached for would be reachable from both.
"""

from __future__ import annotations

from enum import StrEnum

__all__ = [
    "Mode",
    "Packet",
]


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

    # The client answers.
    PONG = "pong"
    OPEN_FAILED = "open-failed"
    FORWARDS = "forwards"

    # The server tells.
    OUT = "out"
    ERR = "err"
    EXIT = "exit"
    SUSPEND = "suspend"
    MODE = "mode"
    KITTY_KEYBOARD = "kitty-keyboard"

    # The server asks, and the machine the person sits at acts.
    OPEN = "open"
    FORWARD = "forward"
    PING = "ping"


class Mode(StrEnum):
    """
    What the `mode` packet asks the client's terminal to be.

    `RESTORE` puts back whatever the last one replaced, so the three
    are a stack and not a setting. `client/terminal.py` keeps it.
    """

    RAW = "raw"
    COOKED = "cooked"
    RESTORE = "restore"
