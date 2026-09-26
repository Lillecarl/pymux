"""
What a forwarded port is, and how a person spells one.

**No SSH here, and no I/O.** This is the vocabulary that the command on
the server and the table on the client both need, so it sits where
neither owns it. `client/forwards.py` is the half that opens one.

The spelling is openssh's, because a person who wants a forward already
knows `ssh -L`. Lillecarl/pymux#436.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final, NamedTuple

__all__ = [
    "ANY_PORT",
    "BadForward",
    "Direction",
    "Forward",
    "LOOPBACK",
    "LOOPBACK_NAMES",
    "MAY_NARROW",
    "the_far_side_may_narrow",
    "parse_forward",
    "parse_listen",
]


class Direction(StrEnum):
    """
    Which machine listens.

    The names are the person's seat: `LOCAL` is the machine they are
    sitting at, which is the one the client runs on. It matches what
    `ssh -L` and `ssh -R` mean, and asyncssh's `forward_local_port` and
    `forward_remote_port` sit in the same position as openssh's, so the
    two agree all the way down.
    """

    LOCAL = "local"
    REMOTE = "remote"

    @property
    def flag(self) -> str:
        "The letter a person types for this direction."
        return "-L" if self is Direction.LOCAL else "-R"


#: Where a forward listens when the spelling named no address.
#:
#: **Never the empty string, which binds every interface.** A forward
#: reaches a service that chose to listen on loopback, and putting it on
#: a laptop's wifi address publishes it to the cafe. openssh defaults the
#: same way and makes the other choice explicit through `GatewayPorts`.
LOOPBACK: Final = "localhost"

#: The addresses that reach only the machine itself.
#:
#: A forward that binds one of these is private to whoever is at that
#: keyboard. A forward that binds anything else publishes a service to
#: the network the machine is on, which is a different decision and is
#: why `forward_needs_asking` separates them. Lillecarl/pymux#440.
LOOPBACK_NAMES: Final = frozenset({"localhost", "127.0.0.1", "::1", "ip6-localhost"})

#: The port number that asks the operating system for a free one. The
#: listener answers with the number it really got.
ANY_PORT: Final = 0


class Forward(NamedTuple):
    """
    One forwarded port: where it listens, and where it goes.

    Both ends are named from the machine that holds them. For `LOCAL`
    the listener is on the client's machine and the destination is
    resolved by the far sshd; for `REMOTE` it is the other way round.
    """

    direction: Direction
    listen_host: str
    listen_port: int
    dest_host: str
    dest_port: int

    @property
    def listen(self) -> str:
        "The listening end, as a person would type it."
        return _address(self.listen_host, self.listen_port)

    @property
    def dest(self) -> str:
        "The destination end, as a person would type it."
        return _address(self.dest_host, self.dest_port)

    def spell(self) -> str:
        "The whole forward, in the spelling that would make it again."
        return "%s %s:%s" % (self.direction.flag, self.listen, self.dest)


def _address(host: str, port: int) -> str:
    if port == ANY_PORT:
        return "%s:*" % (host,)
    return "%s:%d" % (host, port)


def the_far_side_may_narrow(forward: Forward) -> bool:
    """
    Whether the server may bind this somewhere smaller than it was
    asked to.

    **Only a remote forward, and only off loopback.** `GatewayPorts` is
    openssh's switch for it and `no` is the default, under which a
    `-R 0.0.0.0:...` binds loopback, reports success, and says nothing.

    **Nothing can tell afterwards, so this has to be said before.** The
    `tcpip-forward` reply carries one number and no address -- read in
    asyncssh 2.24.0, `connection.py`: a `get_uint32()` for the port and
    then `check_end()`. A listener reports back the address it asked
    for, because that is the only one it has. Lillecarl/pymux#444.
    """
    return (
        forward.direction is Direction.REMOTE
        and forward.listen_host not in LOOPBACK_NAMES
    )


#: What to tell a person about a bind the far side may narrow. It is
#: not a warning about pymux: openssh's default really does this, and
#: the person is the only one who can find out whether this server is
#: configured for it.
MAY_NARROW = "the server may bind it on loopback only"


class BadForward(ValueError):
    "What a spelling that cannot be read says."


def parse_forward(direction: Direction, spec: str) -> Forward:
    """
    Read `[listen_host:]listen_port:dest_host:dest_port`.

    openssh's syntax, and openssh's ambiguity with it: a three part
    spelling names no listening address, and a four part one does. A
    listening port of 0 asks for any free port, which is worth having
    here because the client answers with the number it was given.
    """
    parts = spec.split(":")

    if len(parts) == 3:
        listen_host, listen_port, dest_host, dest_port = (LOOPBACK, *parts)
    elif len(parts) == 4:
        listen_host, listen_port, dest_host, dest_port = parts
    else:
        raise BadForward(
            "%r is not a forward. Spell it "
            "[listen_host:]listen_port:host:port." % (spec,)
        )

    if not dest_host:
        raise BadForward("%r names no host to forward to." % (spec,))

    return Forward(
        direction=direction,
        listen_host=listen_host or LOOPBACK,
        listen_port=_port(listen_port, spec),
        dest_host=dest_host,
        dest_port=_port(dest_port, spec),
    )


def parse_listen(spec: str) -> tuple[str, int]:
    """
    Read the listening end alone: `[listen_host:]listen_port`.

    What it takes to name a forward that already exists, for removing
    one. The destination is not part of the name: two forwards cannot
    listen in the same place, so the listening end identifies it.
    """
    host, _, port = spec.rpartition(":")
    return host or LOOPBACK, _port(port, spec)


def _port(text: str, spec: str) -> int:
    try:
        port = int(text)
    except ValueError:
        raise BadForward("%r has %r where a port number goes." % (spec, text)) from None

    if not 0 <= port <= 65535:
        raise BadForward("%r is not a port number." % (port,))

    return port
