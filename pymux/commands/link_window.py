from __future__ import annotations

import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import CommandException, CommandParser, add_command
from pymux.commands.common import find_window, session_part
from pymux.ids import WindowId, WindowIndex


def _find_anywhere(pymux: Pymux, target: str | None):
    """
    A window of the order, or one that unlink_window took out: the
    pen is the window's own place while it waits, and link-window is
    the command that reaches into it.
    """
    window = find_window(pymux, target)
    if window is None and target and target.startswith("@") and target[1:].isdigit():
        window_id = WindowId(int(target[1:]))
        for unlinked in pymux.arrangement._unlinked_windows:
            if unlinked.window_id == window_id:
                return unlinked
    return window


def link_window(pymux: Pymux, args: argparse.Namespace) -> None:
    """
    Put a window in the order at the index -t names.

    The window is one unlink_window took out, or one that is in an
    order already, which moves it: tmux's link-window and move-window
    answer the same ask here, because a window belongs to one order
    and linking it elsewhere takes it out of its own.
    Lillecarl/pymux#297.

    `-t` takes an optional session in front of the index, and the
    window moves across when it names another one:
    `link-window -s 2 -t work:` parks window 2 after the last window
    of `work`. Without a session part the window lands in the
    session of the client that asks. Lillecarl/pymux#533.
    """
    window = _find_anywhere(pymux, args.s)
    if window is None:
        raise CommandException("can't find window: %s" % (args.s,))

    if args.t is None:
        dst = pymux.current_session
        index = None
    else:
        dst, rest = session_part(pymux, args.t)
        if dst is None:
            raise CommandException("can't find session: %s" % (args.t,))
        if rest == "":
            # A session and no index: after the last window, which is
            # what no `-t` at all does.
            index = None
        else:
            try:
                index = WindowIndex(int(rest))
            except ValueError:
                raise CommandException("Can't link window: bad index.") from None

    src = pymux.session_of_window(window)
    if src is None:
        # Parked in the pen of the session the caller is on:
        # `_find_anywhere` reaches into no other one. Take it out of
        # the pen, and the link below puts it in the order.
        pymux.arrangement._unlinked_windows.remove(window)
        dst.arrangement.link_window(window, index)
    elif src is dst:
        dst.arrangement.link_window(window, index)
    else:
        src.arrangement.move_window_to(window, dst.arrangement, index)


def register(subparsers: argparse._SubParsersAction[CommandParser]):
    parser = add_command(subparsers, link_window)
    parser.add_argument(
        "-s", dest="s", metavar="<src-window>", help="The window to link; the active one is the default."
    )
    parser.add_argument("-t", dest="t", metavar="[<dst-session>:]<dst-index>", help="The index to put it at.")
