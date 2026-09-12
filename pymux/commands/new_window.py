import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import add_command
from pymux.commands.common import print_object_format


def index(target):
    """
    The window number a target names, or None for one that is a name.

    tmux takes a window by number, by name, or by one of its own
    shorthands. Only the number places a window, so the rest read as
    "no number" and leave the placement to the active window.
    """
    try:
        return int(target)
    except (TypeError, ValueError):
        return None


def where_a_new_window_goes(pymux: "Pymux", args: argparse.Namespace):
    """
    The index a new window takes, from the options it was given.

    Four answers, and the first that applies wins:

    - `-b`, before the target: the target's own index. What is there
      moves up.
    - `-a`, after the target: one past it.
    - `-t` on its own: the index to create at, which is how tmux reads
      a bare target for this command.
    - Nothing: after the active window, which is the one a person is
      looking at.

    `None` means the lowest free index, and nothing returns it any
    more. It is still what `Arrangement.create_window` does without an
    index, because a session that is restored builds its windows by
    number and asks for none.

    A target nobody can find is the active window. tmux errors there,
    and a person who mistypes a window number while opening one does
    not want the window not to open.
    """
    number = index(args.target_window)

    where = None
    if number is not None:
        where = pymux.arrangement.get_window_by_index(number)
    if where is None:
        where = pymux.arrangement.get_active_window()

    if args.b:
        return where.index
    if args.a:
        return where.index + 1
    if number is not None:
        return number
    return where.index + 1


def new_window(pymux: "Pymux", args: argparse.Namespace) -> None:
    """
    Open a window, next to the one a person is on.

    **Next to it, and not in the lowest gap.** A new window took the
    first free index from `base-index` up, whatever window a person
    was looking at: on window five of one, two, five, `ctrl+b c` gave
    window three. A person who works across windows builds a map of
    what is where, and a new window arriving at the far end of it is a
    window they then have to go and find. Lillecarl/pymux#191.

    With no gaps the two rules agree, which is why this went so long
    without being noticed: the lowest free index is the one after the
    last window.

    `-a` and `-b` are tmux's, and name the side. `-t` names the window
    to sit next to, and the active one is the default. tmux reads a
    bare `-t` as the index to create at instead, and so does this.
    """
    executable = args.executable
    start_directory = args.start_directory
    name = args.name
    dont_select = args.d

    window = pymux.arrangement.get_active_window()
    pymux.create_window(
        executable,
        start_directory=start_directory,
        name=name,
        index=where_a_new_window_goes(pymux, args),
    )

    # **The one that is active, and not the last of the list.** A new
    # window went at the end while it always took the highest index,
    # and it can go anywhere now. `create_window` focuses it, which is
    # the only thing that says which one it is.
    new_window = pymux.arrangement.get_active_window()

    if dont_select:
        # Don't make the new window active.
        pymux.arrangement.set_active_window(window)

    if args.P:
        print_object_format(
            pymux,
            args.format,
            window=new_window,
            pane=new_window.active_pane,
        )


def register(subparsers):
    parser = add_command(subparsers, new_window)
    parser.add_argument("-a", dest="a", action="store_true", help="After the target window.")
    parser.add_argument("-b", dest="b", action="store_true", help="Before the target window.")
    parser.add_argument("-t", dest="target_window", metavar="<target-window>", help="The window to sit next to, or the index to create at.")
    parser.add_argument("-n", dest="name", metavar="<name>", help="The name of the window.")
    parser.add_argument("-c", dest="start_directory", metavar="<start-directory>", help="Where the program starts.")
    parser.add_argument("-d", dest="d", action="store_true", help="Leave the new window unfocused.")
    parser.add_argument("-P", dest="P", action="store_true", help="Print information about the new window.")
    parser.add_argument("-F", dest="format", metavar="<format>", help="The format to print with -P.")
    parser.add_argument("executable", nargs="?", metavar="<executable>")
