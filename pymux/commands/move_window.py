import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import CommandException
from pymux.commands import add_command


def move_window(pymux: "Pymux", args: argparse.Namespace) -> None:
    """
    Move this window to another index.

    A bare `-t` takes a free index and refuses a taken one, which is
    tmux's answer as well: a person who meant to insert says `-a` or
    `-b`, and one who meant to trade places says `swap-window`. A
    window that moved in silence is worse than a refusal.

    `-b` puts this window at the index and `-a` after it, moving the
    windows that are in the way up. Only the run in the way moves --
    `make_room_at` stops at the first gap -- which is what tmux says
    about its own `-a`: "moving windows up if necessary".
    `-k` kills whatever is at the index and takes its place.
    tmux spells all three on this command (`cmd-move-window.c:93`),
    so a script written for tmux runs. Lillecarl/pymux#343.
    """
    dst_window = args.dst_window
    try:
        new_index = int(dst_window)
    except ValueError:
        raise CommandException("Invalid window index: %r" % (dst_window,))

    if args.after:
        new_index += 1

    window = pymux.arrangement.get_active_window()
    occupant = pymux.arrangement.get_window_by_index(new_index)

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
        pymux.arrangement.make_room_at(new_index)
    elif occupant is not None:
        raise CommandException("Can't move window: index in use.")

    pymux.arrangement.move_window(window, new_index)


def register(subparsers):
    parser = add_command(subparsers, move_window)
    parser.add_argument("-t", dest="dst_window", metavar="<dst-window>", required=True, help="The index to move to.")
    parser.add_argument("-a", dest="after", action="store_true", help="Insert after that index, moving the windows in the way up.")
    parser.add_argument("-b", dest="before", action="store_true", help="Insert at that index, moving the windows in the way up.")
    parser.add_argument("-k", dest="kill", action="store_true", help="Kill whatever is at that index, and take its place.")
