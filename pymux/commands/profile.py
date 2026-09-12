import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import CommandException
from pymux.commands import add_command
from pymux import introspect


def profile(pymux: "Pymux", args: argparse.Namespace) -> None:
    """
    Watch this server for a few seconds, and write down where its time went.

    **Where the time of an await goes, and not only of a call.**
    pyinstrument attributes the time of a coroutine that is waiting to
    the frame that awaits, so a server that spends its life in
    `epoll_wait` still says which work the waiting was for.
    """
    try:
        seconds = float(args.seconds or introspect.HOW_LONG_TO_WATCH)
    except ValueError:
        raise CommandException(
            "Not a number of seconds: %r" % (args.seconds,)
        )

    try:
        path = introspect.start_watching(pymux, seconds)
    except ImportError:
        raise CommandException(
            "pyinstrument is not installed, so this server cannot profile itself."
        )

    pymux.print_command_line(str(path))
    pymux.show_message(
        "Watching for %.1f seconds. It lands in %s" % (seconds, path.name)
    )


def register(subparsers):
    parser = add_command(subparsers, profile)
    parser.add_argument("seconds", nargs="?", metavar="<seconds>", help="How long to watch, in seconds.")
