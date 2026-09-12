import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import CommandException, add_command


def display_menu(pymux: "Pymux", args: argparse.Namespace) -> None:
    """
    A menu where a person is looking, with a key for each line.

    The entries are the rest of the line, in tmux's order: a name, a
    key, and the command the key runs, in threes. -T names the title
    bar. -x and -y take tmux's positions and change nothing: the menu
    draws where the other boxes of pymux draw, because a box's place
    is fixed at build time.

    The menu is modal: a key of an entry runs it, Escape or ctrl+c
    leaves without taking anything, and clicking a line runs it.
    Lillecarl/pymux#297.
    """
    entries: list[tuple[str, str, str]] = []
    rest = list(args.entries)
    while len(rest) >= 3:
        name, key, command = rest[:3]
        entries.append((key, name, command))
        rest = rest[3:]

    if rest:
        raise CommandException(
            "A menu entry is a name, a key and a command; %r is not." % " ".join(rest)
        )

    try:
        state = pymux.get_client_state()
    except ValueError:
        return  # A command from the command line: nobody to show it to.

    state.layout_manager.display_menu(entries, args.T or "")


def register(subparsers):
    parser = add_command(subparsers, display_menu)
    parser.add_argument("-T", dest="T", metavar="<title>", help="The name on the title bar.")
    parser.add_argument("-x", dest="x", metavar="<position>", help="Accepted for tmux and changes nothing: the box draws where the other boxes draw.")
    parser.add_argument("-y", dest="y", metavar="<position>", help="Accepted for tmux and changes nothing: the box draws where the other boxes draw.")
    parser.add_argument("entries", nargs=argparse.REMAINDER, metavar="<name> <key> <command>", help="One entry is a name, a key and a command; the rest of the line is entries.")
