from __future__ import annotations

import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.core.main import Pymux


from pymux.commands import CommandException, CommandParser, add_command
from pymux.commands.common import session_part, the_window
from pymux.ids import WindowIndex


def move_window(pymux: Pymux, args: argparse.Namespace) -> None:
    """
    Move a window to another index, in this session or another one.

    `-s` names the window; the one the caller is looking at is the
    default. `-t` names the index, with an optional session in front
    of it: `move-window -s 2 -t work:5` moves window 2 of this
    session to index 5 of `work`. Without a session part the window
    lands in the session of the client that asks, which is tmux's
    answer as well (`cmd-move-window.c`).

    A bare `-t` takes a free index and refuses a taken one, which is
    tmux's answer as well: a person who meant to insert says `-a` or
    `-b`, and one who meant to trade places says `swap-window`. A
    window that moved in silence is worse than a refusal.

    `-b` puts the window at the index and `-a` after it, moving the
    windows that are in the way up. Only the run in the way moves --
    `make_room_at` stops at the first gap -- which is what tmux says
    about its own `-a`: "moving windows up if necessary".
    `-k` kills whatever is at the index and takes its place.
    tmux spells all three on this command (`cmd-move-window.c:93`),
    so a script written for tmux runs. Lillecarl/pymux#343.

    Across sessions the insert and the kill run against the
    destination: the room is made where the window lands, and what
    dies is what was in its way there. Lillecarl/pymux#533.

    A session and no index -- `work:`, or `work` alone when no window
    index reads that way -- puts the window after the last one there,
    which is what `prefix .` then a session name asks for. With `-a`
    or `-b` the index is that session's active window, as in tmux.
    `--session work` says the same with the session apart, and then
    `-t` is the bare index there.
    """
    window = the_window(pymux, args.src_window)

    src = pymux.session_of_window(window)
    assert src is not None  # `the_window` only returns windows in an order.

    target = args.dst_window
    if args.session is not None:
        if target is not None and ":" in target:
            raise CommandException("Can't move window: -t names a session, and so does --session.")
        target = "%s:%s" % (args.session, target or "")
    elif target is None:
        raise CommandException("Can't move window: say where, with -t or --session.")
    elif ":" not in target and not target.isdigit() and pymux.get_session(target) is not None:
        target += ":"
    dst, rest = session_part(pymux, target)
    if dst is None:
        raise CommandException("can't find session: %s" % (args.dst_window,))
    arrangement = dst.arrangement

    if rest == "":
        active = arrangement.get_active_window()
        if not (args.after or args.before) or active is None:
            if dst is not src:
                src.arrangement.move_window_to(window, arrangement)
            return
        new_index = active.index
    else:
        try:
            new_index = WindowIndex(int(rest))
        except ValueError:
            raise CommandException("Can't move window: bad index.") from None

    if args.after:
        new_index += 1

    occupant = arrangement.get_window_by_index(new_index)

    if occupant is window:
        return  # Already there. Nothing to make room for.

    if args.kill and occupant is not None:
        # The panes and not the window: a window is gone when its last
        # pane is, and that is the path `kill-window` takes.
        for pane in list(occupant.panes):
            pymux.kill_pane(pane)

    if args.after or args.before or args.kill:
        # **After the kill, not before it.** A kill can renumber the
        # windows (`renumber-windows`, Lillecarl/pymux#342), so what
        # is in the way is only known once it has happened.
        arrangement.make_room_at(new_index)
    elif occupant is not None:
        raise CommandException("Can't move window: index in use.")

    if dst is src:
        arrangement.move_window(window, new_index)
    else:
        src.arrangement.move_window_to(window, arrangement, new_index)


def register(subparsers: argparse._SubParsersAction[CommandParser]):
    parser = add_command(subparsers, move_window)
    parser.add_argument(
        "-s", dest="src_window", metavar="<src-window>", help="The window to move; the active one is the default."
    )
    parser.add_argument(
        "-t",
        dest="dst_window",
        metavar="[<dst-session>:]<dst-index>",
        help="The index to move to, with an optional session in front (`work:5`). A session alone (`work:`) takes the next free index there.",
    )
    parser.add_argument(
        "-S",
        "--session",
        dest="session",
        metavar="<target-session>",
        help="Move the window to this session: after its last window, or at the index -t names.",
    )
    parser.add_argument(
        "-a", dest="after", action="store_true", help="Insert after that index, moving the windows in the way up."
    )
    parser.add_argument(
        "-b", dest="before", action="store_true", help="Insert at that index, moving the windows in the way up."
    )
    parser.add_argument(
        "-k", dest="kill", action="store_true", help="Kill whatever is at that index, and take its place."
    )
