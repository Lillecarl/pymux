import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from prompt_toolkit.data_structures import Size
from pymux.commands import CommandException
from pymux.commands import add_command
from pymux.enums import WindowSize


def resize_window(pymux: "Pymux", args: argparse.Namespace) -> None:
    """
    Say how big this window is, and stop following the clients.

    -x: how many columns. -y: how many rows.

    -L, -R: that many columns narrower or wider. -U, -D: that many
    rows shorter or taller.

    **This is the `manual` half of `window-size`**, and running it
    turns that option on: a person who names a size means it to stay.
    Decision 11 of `docs/layout-engine-plan.md`.

    A client bigger than the window draws background around it, and a
    client smaller than it moves its view over it, which is what every
    policy does. So a window may be made bigger than any terminal
    watching, and every pane of it is still reachable.

    **The size is the window's own**, so nothing comes off it for the
    status line: `-x 100 -y 40` is a hundred cells by forty. An axis
    that is not given keeps what it has.

    **A nudge counts from the size the window has now**, which is why
    it is the one a person binds to a key: naming an absolute size
    means knowing what the size is, so "a little wider" would be a
    look at the status line and then a command.

    An absolute and a nudge together are read in that order, so
    `-x 80 -R 10` is ninety columns. tmux takes one nudge at a time;
    several here cost nothing and say more.

    **A nudge stops at one cell and does not complain.** A key held
    down at the edge does nothing, the way it does nothing in
    `move-column`. An absolute size below one is a person asking for
    something that cannot exist, and that raises.

    tmux also takes `-A` and `-a` for the largest and smallest client.
    The four policies of `window-size` already say that and keep
    saying it, so whether those are worth having at all is
    Lillecarl/pymux#225.
    """
    window = pymux.arrangement.get_active_window()
    now = pymux.size_of_the_plane(window)

    def number(given, instead):
        if given is None:
            return instead
        try:
            return int(given)
        except ValueError:
            raise CommandException("Expecting an integer.")

    def asked_for(given, then):
        wanted = number(given, then)
        if wanted < 1:
            raise CommandException("A window is at least one cell.")
        return wanted

    columns = asked_for(args.columns, now.columns) + number(args.right, 0)
    rows = asked_for(args.rows, now.rows) + number(args.down, 0)
    columns -= number(args.left, 0)
    rows -= number(args.up, 0)

    window.manual_size = Size(rows=max(1, rows), columns=max(1, columns))
    window.window_size = WindowSize.MANUAL


def register(subparsers):
    parser = add_command(subparsers, resize_window)
    parser.add_argument("-x", dest="columns", metavar="<columns>", help="How many columns the window is.")
    parser.add_argument("-y", dest="rows", metavar="<rows>", help="How many rows the window is.")
    parser.add_argument("-L", dest="left", metavar="<left>", help="That many columns narrower.")
    parser.add_argument("-U", dest="up", metavar="<up>", help="That many rows shorter.")
    parser.add_argument("-D", dest="down", metavar="<down>", help="That many rows taller.")
    parser.add_argument("-R", dest="right", metavar="<right>", help="That many columns wider.")
