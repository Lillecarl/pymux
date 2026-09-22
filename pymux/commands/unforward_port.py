import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import CommandException, add_command
from pymux.forwarding import BadForward, Direction, parse_listen


def unforward_port(pymux: "Pymux", args: argparse.Namespace) -> None:
    """
    Stop forwarding a port.

    The listening end names it, because that is what two forwards
    cannot share: `unforward-port -L 8080`. The destination is not part
    of the name.

    The client stops wanting it as well as closing it, so a later
    reconnect does not bring it back. Lillecarl/pymux#436.
    """
    if args.local and args.remote:
        raise CommandException("Give -L or -R, not both.")

    spec = args.local or args.remote
    if not spec:
        raise CommandException("Give a forward to stop: -L or -R [host:]port.")

    direction = Direction.LOCAL if args.local else Direction.REMOTE

    try:
        listen_host, listen_port = parse_listen(spec)
    except BadForward as error:
        raise CommandException(str(error)) from None

    client_state = pymux.forwarding_client()
    pymux.forward_through(
        client_state,
        {
            "cmd": "forward",
            "remove": True,
            "direction": str(direction),
            "listen_host": listen_host,
            "listen_port": listen_port,
        },
    )


def register(subparsers):
    parser = add_command(subparsers, unforward_port)
    parser.add_argument(
        "-L",
        dest="local",
        metavar="[host:]port",
        help="A forward that listens on this machine.",
    )
    parser.add_argument(
        "-R",
        dest="remote",
        metavar="[host:]port",
        help="A forward that listens on the server's machine.",
    )
