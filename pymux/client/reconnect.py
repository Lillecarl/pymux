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

__all__ = [
    "Backoff",
    "draw",
    "link_may_come_back",
    "notice",
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


def notice(host: str, said: str, seconds: float) -> list[str]:
    "The lines of the screen a client shows while it has no server."
    return [
        "pymux lost the server on %s." % (host,),
        said,
        "",
        "The next try is in %s." % (_left(seconds),),
        "Press any key to try now. Press q to leave.",
    ]


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
