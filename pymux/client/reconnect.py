"""
What a client does when it loses the server.

A multiplexer keeps the panes when the terminal goes. The client over
SSH is the one that makes that worth something: the panes run on the
other machine and they survive a link that drops, so a drop should cost
a pause here and not the whole client. This holds the three parts of
that pause -- how long to wait, which failures are worth waiting for,
and what the person looks at meanwhile. Lillecarl/pymux#256.

**The unix socket does not use this.** There the server is on this
machine and its socket is gone with it, so a link that ends is a server
that ended. A client that waits for that one to come back waits for
ever.
"""

from __future__ import annotations

import math

import random as _random

import anyio

__all__ = [
    "Backoff",
    "Internet",
    "draw",
    "internet_reachable",
    "link_may_come_back",
    "notice",
    "watch_internet",
    "why",
]

#: The wait before the first retry, in seconds.
FIRST_WAIT = 0.5

#: The longest wait between two retries, in seconds.
LONGEST_WAIT = 30.0

#: How much of a wait is random. A machine that comes back usually
#: takes every client it lost at once -- a laptop that wakes, a link
#: that returns -- and they all count from the same moment.
JITTER = 0.5

#: What the client asks to tell "no internet" from "no server".
#:
#: **An address that answers from anywhere.** Quad9 is a public
#: resolver, and a DNS server answers a TCP connection on port 53 the
#: way it answers a query. A TCP connection needs no privilege, where
#: ICMP needs one a person's client does not have, and a name would
#: need DNS -- the thing that is not answering when the internet is
#: the problem.
WAN_HOST = "9.9.9.9"
WAN_PORT = 53

#: How long the probe waits before it calls the address silent, in
#: seconds.
WAN_TIMEOUT = 2.0

#: How often the probe measures again while the client is disconnected,
#: in seconds. A network that comes back should show in the notice
#: without the person doing anything.
WAN_INTERVAL = 3.0

#: What the notice says about that answer. Both name the address, so a
#: reader knows the claim is about reaching one place and not the
#: whole internet.
INTERNET_UP = "9.9.9.9 answers, so this machine has a network."
INTERNET_DOWN = "9.9.9.9 does not answer, so this machine looks offline."


class Backoff:
    """
    How long to wait before the next try. It doubles, up to a cap.

    `next()` is the wait for one try, and `reset()` says the link came
    back and the next failure starts from the first wait again.
    """

    def __init__(
        self,
        first: float = FIRST_WAIT,
        longest: float = LONGEST_WAIT,
        random=_random.random,
    ) -> None:
        self.first = first
        self.longest = longest
        self._random = random
        self._tries = 0

    def reset(self) -> None:
        self._tries = 0

    def next(self) -> float:
        wait = min(self.longest, self.first * 2**self._tries)
        if wait < self.longest:
            # Counting past the cap changes no wait and grows the
            # power: a client left overnight reaches `2**2880`.
            self._tries += 1
        return wait - wait * JITTER * self._random()


def link_may_come_back(error: BaseException) -> bool:
    """
    Whether to open the link again after this failure.

    **A definite answer from the other side never comes back; a failure
    to get one might.** A refused key and a socket that is not there
    are answers: the far machine heard the question and said no, and
    asking again says no again. A connection that is lost, a name that
    does not resolve and a machine that does not answer are not
    answers, and the link that carries them is the thing that breaks.

    A failure that retries when it should not is worse than one that
    does not: `asyncssh.PermissionDenied` in a loop is a machine for
    locking an account out.
    """
    import asyncssh

    if isinstance(error, asyncssh.ConnectionLost):
        # This one is a `DisconnectError` and still the reason to wait:
        # it is what asyncssh says for a link that went away, and what
        # a missed keepalive becomes.
        return True

    if isinstance(error, (asyncssh.DisconnectError, asyncssh.ChannelOpenError)):
        return False

    return isinstance(error, (OSError, TimeoutError))


def why(error: BaseException) -> str:
    "What to tell the person about this failure, in one line."
    said = str(error).strip()
    return said or type(error).__name__


class Internet:
    """
    The last answer to "can this machine reach the internet".

    `value` is `None` until the first probe answers, so the notice can
    say nothing rather than guess.
    """

    def __init__(self) -> None:
        self.value: bool | None = None


async def internet_reachable(
    host: str = WAN_HOST, port: int = WAN_PORT, timeout: float = WAN_TIMEOUT
) -> bool:
    """
    Whether a TCP connection to this address opens.

    **A connection, not a ping.** ICMP needs a privilege a person's
    client does not have, and it answers about a different thing: a
    network can pass an echo and still refuse every connection. What
    matters here is whether a connection leaves this machine at all.
    """
    try:
        with anyio.fail_after(timeout):
            stream = await anyio.connect_tcp(host, port)
        await stream.aclose()
        return True
    except Exception:
        # Every failure means the same thing to the person reading it:
        # no connection left this machine. Which errno did it is not
        # worth a line of the notice.
        return False


async def watch_internet(state: Internet, every: float = WAN_INTERVAL) -> None:
    "Keep `state` saying whether the internet answers, until cancelled."
    while True:
        state.value = await internet_reachable()
        await anyio.sleep(every)


def notice(
    host: str,
    said: str,
    seconds: float,
    internet: bool | None = None,
    trying: bool = False,
) -> list[str]:
    """
    The lines of the screen a client shows while it has no server.

    `internet` is what the probe last saw: `True` when this machine
    reached `WAN_HOST`, `False` when it did not, and `None` before the
    first answer. It is a line of its own because a lost link and a
    lost server look the same from here, and a person needs to know
    which one they have.

    `trying` is the moment a connection is in flight. The countdown
    has run out, and `asyncssh.connect` can sit in a blackholed
    network far longer than any wait, so the screen says what is
    happening instead of holding the last second of the countdown.
    """
    lines = [
        "pymux lost the server on %s." % (host,),
        said,
    ]
    if internet is not None:
        lines.append(INTERNET_UP if internet else INTERNET_DOWN)
    lines.append("")
    if trying:
        lines.append("Trying to reach %s now." % (host,))
        lines.append("Press q to leave.")
    else:
        lines.append("The next try is in %s." % (_left(seconds),))
        lines.append("Press any key to try now. Press q to leave.")
    return lines


def _left(seconds: float) -> str:
    whole = max(1, math.ceil(seconds))
    return "1 second" if whole == 1 else "%d seconds" % (whole,)


def draw(output, rows: int, columns: int, lines: list[str]) -> None:
    """
    Put those lines in the middle of the terminal.

    The block is centred as a block, so the lines keep their left edge
    and stay readable next to each other.
    """
    widest = max(len(one) for one in lines)
    left = max(1, (columns - widest) // 2 + 1)
    top = max(1, (rows - len(lines)) // 2 + 1)

    output.reset_attributes()
    output.hide_cursor()
    output.erase_screen()

    for number, one in enumerate(lines):
        output.cursor_goto(top + number, left)
        output.write(one[: columns - left + 1])

    output.flush()
