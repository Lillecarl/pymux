from __future__ import annotations

import argparse
import shlex
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.core.main import Pymux


from libpymux.protocol import Field, Packet

from pymux.commands import CommandException, CommandParser, add_command
from pymux.core.forwarding import (
    MAY_NARROW,
    BadForward,
    Direction,
    Forward,
    parse_forward,
    parse_socks,
    the_far_side_may_narrow,
)
from pymux.enums import ForwardMode


def forward_port(pymux: Pymux, args: argparse.Namespace) -> None:
    """
    Forward a port through the SSH connection of this client.

    `-L` listens on the machine the person sits at and reaches a
    service beside the server: a dev server on the workstation, opened
    in the browser of the laptop. `-R` listens on the server's machine
    and reaches back: how a program in a pane drives Chrome over CDP on
    the laptop, which is behind NAT and listening on loopback.

    The spelling is openssh's, so `-L 8080:localhost:3000` means here.
    A listening port of 0 takes any free one and the message says which.

    A SOCKS proxy names no destination, as in openssh. `-R 1080`
    listens on the server's machine and connects from the person's:
    a browser on a server that websites block goes out from the
    laptop's address. `-D 1080` is the other way round.
    Lillecarl/pyterm#974.

    **The server forwards nothing itself.** It holds no SSH connection;
    the client does. So this asks the client, and the client answers
    with what it is forwarding. Lillecarl/pyterm#438.

    `forward-mode` says whether it is confirmed first. A forward that
    binds off loopback, and one a program in a pane asked for, are
    confirmed whatever the option says. Lillecarl/pyterm#442.
    """
    given = [spec for spec in (args.local, args.remote, args.socks) if spec]
    if len(given) > 1:
        raise CommandException("Give one of -L, -R and -D.")
    if not given:
        raise CommandException("Give a forward: -L or -R [host:]port:host:port, or -R or -D [host:]port.")
    (spec,) = given

    try:
        if args.socks:
            forward = parse_socks(Direction.LOCAL, spec)
        else:
            forward = parse_forward(Direction.LOCAL if args.local else Direction.REMOTE, spec)
    except BadForward as error:
        raise CommandException(str(error)) from None

    if pymux.options.forward_mode == ForwardMode.OFF:
        raise CommandException("forward-mode is off, so pymux forwards nothing.")

    asker = pymux.forward_requests.client()

    if forward.socks and not asker.client_state.client.answers_socks:
        raise CommandException(
            "%s runs a pymux older than SOCKS forwards. Upgrade the pymux it attaches with."
            % (asker.client_state.client.connection.name,)
        )

    # **`-c` counts only from a person.** It is the answer to a
    # question, and a question is answered at a keyboard. A pane that
    # sends `-c` itself answered nothing, so the flag means nothing
    # there -- otherwise the gate is one word to walk around.
    confirmed = args.confirmed and asker.in_person

    if not confirmed and pymux.forward_requests.needs_asking(forward, asker.in_person):
        # The question names both ends. "Forward a port?" is not enough
        # to answer safely: what matters is where it listens and what it
        # reaches. A yes runs this same command with -c, so it is asked
        # once. Lillecarl/pyterm#442.
        #
        # **And it says where the answer is not ours to give.** The one
        # bind worth confirming is the one openssh may quietly narrow,
        # so a question that did not say so would be asking about
        # something that may not happen. Lillecarl/pyterm#446.
        caveat = " -- %s" % (MAY_NARROW,) if the_far_side_may_narrow(forward) else ""
        asker.client_state.ask(
            "Forward %s%s%s? (y/n)" % (forward.spell(), _what_socks_reaches(forward), caveat),
            "forward-port -c %s %s" % (forward.flag, shlex.quote(spec)),
        )
        return

    pymux.forward_requests.send(
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


def _what_socks_reaches(forward: Forward) -> str:
    """
    What a SOCKS proxy is worth saying in the question: it reaches
    everything the machine it connects from reaches, that machine's own
    loopback too, as openssh's does.
    """
    if not forward.socks:
        return ""
    if forward.direction is Direction.REMOTE:
        return " as SOCKS, connecting from this machine"
    return " as SOCKS, connecting from the server's machine"


def register(subparsers: argparse._SubParsersAction[CommandParser]):
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
        metavar="[host:]port[:host:port]",
        help="Listen on the server's machine and reach a service on this one; with no destination, SOCKS that connects from this one.",
    )
    parser.add_argument(
        "-D",
        dest="socks",
        metavar="[host:]port",
        help="Listen on this machine as SOCKS, and connect from the server's machine.",
    )
    parser.add_argument(
        "-c",
        dest="confirmed",
        action="store_true",
        help="Forward without asking again.",
    )
