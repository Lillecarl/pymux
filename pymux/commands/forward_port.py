import argparse
import shlex
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import CommandException, CommandParser, add_command
from pymux.forwarding import (
    MAY_NARROW,
    BadForward,
    Direction,
    parse_forward,
    the_far_side_may_narrow,
)
from pymux.options import ForwardMode
from libpymux.protocol import Field, Packet


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

    `forward-mode` says whether it is confirmed first. A forward that
    binds off loopback, and one a program in a pane asked for, are
    confirmed whatever the option says. Lillecarl/pymux#440.
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

    if pymux.forward_mode == ForwardMode.OFF:
        raise CommandException("forward-mode is off, so pymux forwards nothing.")

    asker = pymux.forwarding_client()

    # **`-c` counts only from a person.** It is the answer to a
    # question, and a question is answered at a keyboard. A pane that
    # sends `-c` itself answered nothing, so the flag means nothing
    # there -- otherwise the gate is one word to walk around.
    confirmed = args.confirmed and asker.in_person

    if not confirmed and pymux.forward_needs_asking(forward, asker.in_person):
        # The question names both ends. "Forward a port?" is not enough
        # to answer safely: what matters is where it listens and what it
        # reaches. A yes runs this same command with -c, so it is asked
        # once. Lillecarl/pymux#440.
        #
        # **And it says where the answer is not ours to give.** The one
        # bind worth confirming is the one openssh may quietly narrow,
        # so a question that did not say so would be asking about
        # something that may not happen. Lillecarl/pymux#444.
        caveat = " -- %s" % (MAY_NARROW,) if the_far_side_may_narrow(forward) else ""
        asker.client_state.ask(
            "Forward %s%s? (y/n)" % (forward.spell(), caveat),
            "forward-port -c %s %s" % (direction.flag, shlex.quote(spec)),
        )
        return

    pymux.forward_through(
        asker.client_state,
        {
            Field.CMD: Packet.FORWARD,
            Field.DIRECTION: str(forward.direction),
            Field.LISTEN_HOST: forward.listen_host,
            Field.LISTEN_PORT: forward.listen_port,
            Field.DEST_HOST: forward.dest_host,
            Field.DEST_PORT: forward.dest_port,
        },
    )


def register(subparsers: "argparse._SubParsersAction[CommandParser]"):
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
    parser.add_argument(
        "-c",
        dest="confirmed",
        action="store_true",
        help="Forward without asking again.",
    )
