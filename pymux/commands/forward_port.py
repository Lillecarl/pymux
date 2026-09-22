import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import CommandException, add_command
from pymux.forwarding import BadForward, Direction, parse_forward


def forward_port(pymux: "Pymux", args: argparse.Namespace) -> None:
    """
    Forward a port through the SSH connection of this client.

    `-L` listens on the machine the person sits at and reaches a
    service beside the server: a dev server on the workstation, opened
    in the browser of the laptop. `-R` listens on the server's machine
    and reaches back: how a program in a pane drives Chrome over CDP on
    the laptop, which is behind NAT and listening on loopback.

    The spelling is openssh's, so `-L 8080:localhost:3000` means here.
    A listening port of 0 takes any free one and the message says which.

    **The server forwards nothing itself.** It holds no SSH connection;
    the client does. So this asks the client, and the client answers
    with what it is forwarding. Lillecarl/pymux#436.
    """
    if args.local and args.remote:
        raise CommandException("Give -L or -R, not both.")

    spec = args.local or args.remote
    if not spec:
        raise CommandException("Give a forward: -L or -R [host:]port:host:port.")

    direction = Direction.LOCAL if args.local else Direction.REMOTE

    try:
        forward = parse_forward(direction, spec)
    except BadForward as error:
        raise CommandException(str(error)) from None

    client_state = pymux.forwarding_client()
    pymux.forward_through(
        client_state,
        {
            "cmd": "forward",
            "direction": str(forward.direction),
            "listen_host": forward.listen_host,
            "listen_port": forward.listen_port,
            "dest_host": forward.dest_host,
            "dest_port": forward.dest_port,
        },
    )


def register(subparsers):
    parser = add_command(subparsers, forward_port)
    parser.add_argument(
        "-L",
        dest="local",
        metavar="[host:]port:host:port",
        help="Listen on this machine and reach a service beside the server.",
    )
    parser.add_argument(
        "-R",
        dest="remote",
        metavar="[host:]port:host:port",
        help="Listen on the server's machine and reach a service on this one.",
    )
