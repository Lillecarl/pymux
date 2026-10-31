"""
The replies of the outer terminal that arrive as input.

The parser hands one on as `TERMINAL_REPLY` in place of a key, and the
server reads it with `pyte.sequences.replies.parse_reply`. The SGR
pixel mouse is the other thing here: a report pymux turns into the
cell report the rest of it reads.
"""

from __future__ import annotations

from pyte.sequences.replies import parse_reply
from pyte.sequences.reports import CursorPositionReport, SgrMouseReport

__all__ = ["TERMINAL_REPLY", "pixels_to_cells", "reply_of"]


#: The key a terminal reply arrives as. None of them is a key event,
#: and every one of them must be consumed all the same, or it reaches a
#: pane as key presses.
TERMINAL_REPLY = object()


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
    pixels = SgrMouseReport.parse(report)
    if pixels is None or pixels.button & _LEAVE_BIT:
        return None
    column, across = divmod(max(pixels.column - origin, 0), width)
    row, down = divmod(max(pixels.row - origin, 0), height)
    cells = SgrMouseReport(pixels.button, column + 1, row + 1, pixels.released)
    return str(cells), (across + 0.5) / width, (down + 0.5) / height


def reply_of(prefix: str) -> object | None:
    """
    `TERMINAL_REPLY` when this sequence is a whole terminal reply, or None.

    Two replies are left to prompt_toolkit, which reads them as its own
    keys: a cursor position report, which is also F3 with a modifier,
    and an SGR mouse report.
    """
    reply = parse_reply(prefix)
    if reply is None or isinstance(reply, (CursorPositionReport, SgrMouseReport)):
        return None
    return TERMINAL_REPLY
