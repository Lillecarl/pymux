from __future__ import annotations

import argparse
import shlex
from collections.abc import Awaitable, Coroutine
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pymux.core.main import Pymux
    from pymux.mirror.peer import Peer


from libpymux.protocol import Field, Message, Packet

from pymux.commands import CommandException, CommandParser, add_command, this_client
from pymux.commands.common import answer, find_pane
from pymux.core.forward_requests import ThroughPeer
from pymux.core.forwarding import (
    BadForward,
    Direction,
    Forward,
    parse_forward,
    parse_socks,
    question,
)
from pymux.enums import ForwardMode


def forward_port(pymux: Pymux, args: argparse.Namespace) -> Awaitable[None] | None:
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

    **A person who reaches the server through a server of their own**
    (`connect`) holds the SSH connection there. Then the request goes
    along the peer link, and that server asks them. There, `-p address`
    forwards through the link to the server `connect` named, with `-R`
    listening on that server's machine. Lillecarl/pyterm#976.

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

    if args.peer is not None:
        return _through_link(pymux, peer_named(pymux, args.peer), forward, args.confirmed)

    asker = pymux.forward_requests.client()
    packet = _packet(forward)

    if isinstance(asker, ThroughPeer):
        # The peer asks the person attached there: nobody here chose it.
        caller = find_pane(pymux, None)
        if caller is not None:
            packet[Field.PANE_ID] = int(caller.pane_id)
        pymux.forward_requests.send(asker, packet)
        answer(
            pymux,
            "Asked %s to forward %s. The person there confirms it." % (asker.link.host or "the peer", forward.spell()),
        )
        return None

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
        # A yes runs this same command with -c, so it is asked once.
        asker.client_state.ask(question(forward), "forward-port -c %s %s" % (forward.flag, shlex.quote(spec)))
        return None

    pymux.forward_requests.send(asker, packet)
    return None


def peer_named(pymux: Pymux, address: str) -> Peer:
    "The peer `connect` linked at `address`."
    peer = pymux.peers.by_address.get(address)
    if peer is None:
        raise CommandException("This server has no link to %s." % (address,))
    return peer


def _through_link(pymux: Pymux, peer: Peer, forward: Forward, confirmed: bool) -> Awaitable[None] | None:
    in_person = pymux.a_person_asked()
    if not (confirmed and in_person) and pymux.forward_requests.needs_asking(forward, in_person):
        asking = this_client(pymux) or pymux.latest_client()
        if asking is None:
            raise CommandException("Nobody is attached to confirm the forward.")
        asking.ask(question(forward), confirm_through(peer, forward))
        return None
    return answered(pymux, peer.forward(forward))


def confirm_through(peer: Peer, forward: Forward) -> str:
    "The command a yes runs, to make `forward` through the link to `peer`."
    return shlex.join(["forward-port", "-c", "-p", peer.address, *forward.spell().split()])


def answered(pymux: Pymux, said: Coroutine[Any, Any, str]) -> Awaitable[None]:
    """
    Run `said` and show what it says to whoever ran this command. The
    asker and the client are read now: the coroutine runs later, when
    another command may be current.
    """
    asker, client = pymux.answer, this_client(pymux)

    async def run() -> None:
        text = await said
        if asker is not None:
            asker.output.append(text)
        elif client is not None:
            client.message = text
        else:
            pymux.show_message(text)

    return run()


def _packet(forward: Forward) -> Message:
    return {
        Field.CMD: Packet.FORWARD,
        Field.DIRECTION: str(forward.direction),
        Field.LISTEN_HOST: forward.listen_host,
        Field.LISTEN_PORT: forward.listen_port,
        Field.DEST_HOST: forward.dest_host,
        Field.DEST_PORT: forward.dest_port,
    }


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
    parser.add_argument(
        "-p",
        dest="peer",
        metavar="address",
        help="Forward through this server's link to that server, as `connect` named it.",
    )
