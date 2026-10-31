"""
The replies of the outer terminal that arrive as input: keyboard
flags, device attributes, a string sequence, the cell size, a mode, a
status report, focus, and the SGR pixel mouse. Each is a sentinel the
parser hands on in place of a key.
"""

from __future__ import annotations

import re

__all__ = [
    "CELL_SIZE_REPLY",
    "DA1_REPLY",
    "FLAGS_REPLY",
    "FOCUS_IN",
    "FOCUS_OUT",
    "FOCUS_REPORT",
    "MODE_REPLY",
    "SGR_MOUSE_RE",
    "STATUS_REPLY",
    "STRING_REPLY",
    "pixels_to_cells",
    "reply_of",
]


# An APC, DCS or OSC string sequence and any prefix of one. The payload
# never contains the escape character, so the terminator is
# unambiguous. (Both the two-character ST and the 8-bit ST are
# accepted.) An OSC ends at a bell as well, which is how most terminals
# close their colour answers; APC and DCS do not, so the two shapes
# stay apart.
_STRING_RE = re.compile(
    r"^(?:\x1b[_P][^\x1b\x9c]*(?:\x1b\\|\x9c)"
    r"|\x1b\][^\x1b\x9c\x07]*(?:\x1b\\|\x9c|\x07))\Z"
)


# Sentinels for terminal replies that are not key events: the reply of
# the "CSI ? u" keyboard flags query, and a Primary/Secondary device
# attributes reply ("CSI ? ... c"). Both are used to detect whether the
# outer terminal supports the keyboard protocol.
FLAGS_REPLY = object()


DA1_REPLY = object()


# Sentinel for a complete APC, DCS or OSC string sequence. The kitty
# graphics protocol and the colour depth probe answer with one, and so
# does a terminal that answers a colour or clipboard query. They are
# reported like the other replies.
STRING_REPLY = object()


# Sentinel for the "CSI 6 ; height ; width t" reply of the cell size
# query.
CELL_SIZE_REPLY = object()


# Sentinel for the "CSI ? <mode> ; <state> $ y" reply of a DECRQM
# request. pymux asks whether the terminal holds a frame back while it
# is painted ("CSI ? 2026 $ p").
MODE_REPLY = object()


# Sentinel for the "CSI 0 n" reply of a status report, which the
# server asks after a frame to learn that the frame arrived.
# Lillecarl/pymux#259.
STATUS_REPLY = object()


#: What a terminal in DEC mode 1004 sends when its window gains and
#: loses the focus. Lillecarl/pymux#794.
FOCUS_IN = "\x1b[I"


FOCUS_OUT = "\x1b[O"


# Sentinel for either of them.
FOCUS_REPORT = object()


#: An SGR mouse report: button, x, y, and press or release.
SGR_MOUSE_RE = re.compile(r"^\x1b\[<(\d+);(\d+);(\d+)([Mm])$")


#: The button bit of a report SGR pixel mode adds: the pointer left the
#: window. kitty's `mouse.c`, `LEAVE_INDICATOR`.
_LEAVE_BIT = 1 << 8


def pixels_to_cells(report: str, width: int, height: int, origin: int) -> tuple[str, float, float] | None:
    """
    An SGR pixel mouse report (mode 1016) as the cell report the rest of
    pymux reads, and where in that cell the pointer is, as fractions.
    None for a report to drop: one that is not SGR, or a leave.

    `origin` is the number the terminal gives the first pixel of its
    cells. xterm, which defines the mode, and foot say 1; kitty alone
    says 0 (`mouse.c`, `round(global_x)`). Lillecarl/pymux#648,
    Lillecarl/pymux#651.
    """
    match = SGR_MOUSE_RE.match(report)
    if match is None:
        return None
    button, x, y = int(match.group(1)), int(match.group(2)), int(match.group(3))
    if button & _LEAVE_BIT:
        return None
    column, across = divmod(max(x - origin, 0), width)
    row, down = divmod(max(y - origin, 0), height)
    cells = "\x1b[<%d;%d;%d%s" % (button, column + 1, row + 1, match.group(4))
    return cells, (across + 0.5) / width, (down + 0.5) / height


# Reply of the "CSI ? u" flags query.
_FLAGS_REPLY_RE = re.compile(r"^\x1b\[\?(\d+)u$")


# Primary ("CSI ? ... c") or secondary ("CSI > ... c") device
# attributes reply. The secondary one answers the survey of
# Lillecarl/pymux#725, and left out it reached a pane as keys.
_DA1_REPLY_RE = re.compile(r"^\x1b\[[?>][\d;]*c$")


# Reply of the "CSI 16 t" cell size query, and of the "CSI 14 t" text
# area and "CSI 14 ; 2 t" window size queries of the survey. Or the
# in-band size report of DEC 2048, "CSI 48 ; rows ; cols ; height ;
# width t".
_CELL_SIZE_REPLY_RE = re.compile(r"^\x1b\[(?:[46];\d+;\d+|48;\d+;\d+;\d+;\d+)t$")


# Reply of a DECRQM request: "CSI ? <mode> ; <state> $ y".
_MODE_REPLY_RE = re.compile(r"^\x1b\[\?\d+;\d+\$y$")


# Reply of a status report, "CSI 5 n": 0 is ready, 3 a malfunction. Or
# a theme report of DEC 2031, "CSI ? 997 ; 1 n" dark and "; 2 n" light.
_STATUS_REPLY_RE = re.compile(r"^\x1b\[(?:[03]|\?997;[12])n$")


def reply_of(prefix: str) -> object | None:
    """
    The kind of terminal reply this sequence is, or None.

    None of these is a key event, and every one of them must be
    consumed all the same, or it reaches a pane as key presses.
    """
    if _FLAGS_REPLY_RE.match(prefix):
        return FLAGS_REPLY
    if _DA1_REPLY_RE.match(prefix):
        return DA1_REPLY
    if _CELL_SIZE_REPLY_RE.match(prefix):
        return CELL_SIZE_REPLY
    if _MODE_REPLY_RE.match(prefix):
        return MODE_REPLY
    if _STATUS_REPLY_RE.match(prefix):
        return STATUS_REPLY
    if _STRING_RE.match(prefix):
        return STRING_REPLY
    if prefix in (FOCUS_IN, FOCUS_OUT):
        return FOCUS_REPORT
    return None
