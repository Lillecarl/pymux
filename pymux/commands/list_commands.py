import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import add_command
from pymux.commands import add_commands_to
from pymux.commands.common import answer


def list_commands(pymux: "Pymux", args: argparse.Namespace) -> None:
    """
    One line per command: the name, and what it does.
    """
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    add_commands_to(subparsers)

    # argparse records the help of each command on a pseudo action of
    # the subparsers action, not on the parser it built.
    lines = [
        "%-24s %s" % (action.metavar, action.help or "")
        for action in sorted(subparsers._choices_actions, key=lambda a: a.metavar)
    ]
    answer(pymux, "\n".join(lines))


def register(subparsers):
    add_command(subparsers, list_commands)
