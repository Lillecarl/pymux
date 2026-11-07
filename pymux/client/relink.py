"""
A link that is opened again each time it goes: how long to wait, what
to show meanwhile, which failures are worth waiting for, and how to tell
that the link went while this machine slept.

Two links use it: an SSH client's link to its server, and a peer's link
to the server whose panes it mirrors (`mirror/peer.py`). Each one says
how to dial, how to hold one link, and what a person sees while it is
gone (`Waiting`); the rest is the same for both. `client/reconnect.py`
holds the arithmetic and the lines of the notice. Lillecarl/pyterm#258,
Lillecarl/pyterm#883.
"""

from __future__ import annotations

import sys
import time
from enum import Enum, auto
from functools import partial
from typing import TYPE_CHECKING, Protocol

import anyio
from prompt_toolkit.output.vt100 import Vt100_Output

from pymux.utils import nonblocking

from .reconnect import (
    WAN_INTERVAL,
    Backoff,
    Internet,
    draw,
    link_may_come_back,
    notice,
    watch_internet,
    why,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from .ssh import SshClient

__all__ = [
    "CONNECT_TIMEOUT",
    "STABLE_LINK",
    "SUSPEND_GAP",
    "SUSPEND_POLL",
    "Relink",
    "Restarting",
    "TerminalWaiting",
    "Wait",
    "Waiting",
    "clocks",
    "wait_for_sleep",
]

#: How often to look for a gap between the clocks, in seconds.
SUSPEND_POLL = 1.0

#: How far the world may move ahead of the loop before a link is called
#: gone, in seconds.
#:
#: The number only has to be above the jitter of a poll that is late
#: under load, and below the shortest sleep worth noticing. A laptop
#: lid closes for minutes, never for four seconds.
SUSPEND_GAP = 4.0

#: How often the notice draws its countdown, in seconds.
COUNTDOWN_STEP = 1.0

#: How long one reconnection attempt may last, in seconds.
#:
#: **Without this the notice freezes on the last second of the
#: countdown.** asyncssh passes no timeout to the connect by default,
#: so a network that drops packets without refusing them -- an uplink
#: that went, a route that vanished -- leaves the TCP connect in the
#: kernel's hands for the minute or two `tcp_syn_retries` takes. A
#: bound makes the retry loop turn, and the wait grows the way it is
#: meant to.
#:
#: **Only an attempt after a loss is bounded.** A person who just
#: typed `attach` or `connect` would rather wait than be told no too
#: soon, so the first connect keeps asyncssh's own default. Eight
#: seconds is above a slow handshake -- a mobile link with a second of
#: round trip, a loaded far machine -- and well below the twenty a
#: keepalive already spends before it calls a link dead.
CONNECT_TIMEOUT = 8.0

#: How long a link has to last before the backoff forgets it, in
#: seconds.
#:
#: **A link that opens and drops at once is not a link that came
#: back.** Resetting the backoff the moment the connect returns makes
#: a flapping server a storm at the first wait, and the person
#: watches "1 second" for ever. A link that held this long was really
#: there, and the next failure starts from the first wait again.
STABLE_LINK = 5.0

#: What a person types to stop trying.
LEAVE = ("q", "Q", "\x03")

#: Seconds between two tries while a server restarts. Its next image
#: answers within about one, so the backoff's long waits would only
#: keep the person waiting. The local client tries every 0.1 s.
RESTART_RETRY = 0.25


class Restarting(ConnectionResetError):
    """
    The server said it is restarting, and closed the link. The message
    is the reason it gave; `until` is when its window ends, on the
    loop's clock.
    """

    def __init__(self, why: str, until: float) -> None:
        super().__init__(why)
        self.until = until


class Wait(Enum):
    "What ended one step of a wait (`Waiting.wait`)."

    #: Nothing: the step ran out.
    ON = auto()
    #: Try at once.
    NOW = auto()
    #: Stop trying.
    LEAVE = auto()


class Waiting(Protocol):
    "What a link shows while it is gone, and what ends a wait early."

    def show(self, said: str, seconds: float, internet: bool | None, trying: bool) -> None:
        "The notice of this moment (`reconnect.notice`): the reason, the time left, or that a try runs."

    async def wait(self, seconds: float) -> Wait:
        "Wait at most `seconds` for a reason to try now or to stop."

    def done(self) -> None:
        "The wait is over, whichever way."


class _Left:
    "The person stopped trying. A type, so a result can be told apart."


#: What a try returns when the person stopped. A sentinel and not an
#: exception, so nothing mistakes it for a failure to retry.
_LEFT = _Left()


def clocks() -> tuple[float, float]:
    "The world's clock and the loop's, read together."
    return time.time(), anyio.current_time()


async def wait_for_sleep(read_clocks: Callable[[], tuple[float, float]]) -> str:
    """
    Return once this machine has been asleep, with a sentence that says
    for how long. `read_clocks` is `clocks`, or a test's.

    **A sleep is invisible to everything else here.** The loop's
    clock is `time.monotonic`, which on Linux does not run while
    the machine is suspended, so every timer that would notice a
    dead link is suspended with it: asyncssh's keepalive resumes
    counting from where it stopped and takes its full four
    intervals *after* the lid opens.

    So this watches the one thing a sleep cannot hide: the world
    moved and the loop did not. It needs no signal, no bus and no
    platform code. Where the loop's clock keeps running through a
    sleep there is no gap to find, and there is nothing to fix
    either: the keepalive timers ran too.

    **A gap ends a link rather than testing it.** A machine that has
    been asleep has almost always lost its TCP connections.
    Reconnecting a link that was alive costs about a second, while
    waiting on a link that is dead costs the twenty seconds the
    keepalive needs. Lillecarl/pyterm#447, Lillecarl/pyterm#877.
    """
    was_wall, was_loop = read_clocks()
    while True:
        await anyio.sleep(SUSPEND_POLL)
        wall, loop = read_clocks()
        asleep = (wall - was_wall) - (loop - was_loop)
        was_wall, was_loop = wall, loop
        if asleep >= SUSPEND_GAP:
            return "This machine was asleep for %s." % (_how_long(asleep),)


def _how_long(seconds: float) -> str:
    "A sleep, in the largest unit that keeps it a small number."
    if seconds < 90:
        return "%d seconds" % (round(seconds),)
    if seconds < 90 * 60:
        return "%d minutes" % (round(seconds / 60),)
    return "%.1f hours" % (seconds / 3600,)


class Relink[L]:
    """
    One link of type `L`, held and opened again each time it goes.

    `dial` opens one, bounded by the timeout it is passed. `abort` ends
    one at once, for the sleep watch. `watch_sleep` is False for a link
    a sleep cannot break: a unix socket on this machine, whose two ends
    slept together. Lillecarl/pyterm#882.
    """

    def __init__(
        self,
        waiting: Waiting,
        dial: Callable[[float], Awaitable[L]],
        abort: Callable[[L], object],
        watch_sleep: bool = True,
    ) -> None:
        self.waiting = waiting
        self.dial = dial
        self.abort = abort
        self.watch_sleep = watch_sleep
        self.backoff = Backoff()
        #: Why this side ended its own link, when it did. Only the
        #: sleep watch sets it. Lillecarl/pyterm#447.
        self.slept_through: Exception | None = None

    def clocks(self) -> tuple[float, float]:
        "The world's clock and the loop's (`clocks`). A method, so a test can move one."
        return clocks()

    async def watch_for_suspend(self, abort: Callable[[], object]) -> None:
        """
        Call `abort` when this machine has been asleep (`wait_for_sleep`).

        **A link this side ended can read as a clean end**, and a clean
        end is how a server says the person detached. So the reason is
        written down before the link goes, and `hold` uses it in place
        of the ending the link reports. A reset is what the reason
        says, because that is what a sleep did to the socket, and
        `link_may_come_back` retries an `OSError`. Lillecarl/pyterm#447.
        """
        # A lambda, so a test that moves `self.clocks` mid-watch is read.
        self.slept_through = ConnectionResetError(await wait_for_sleep(lambda: self.clocks()))
        abort()

    async def hold(self, link: L, hold_one: Callable[[L], Awaitable[Exception | None]]) -> None:
        """
        Hold `link` with `hold_one`, and open it again each time it goes.

        `hold_one` answers why one link went, or `None` when it ended on
        purpose, which ends this too. So does the person leaving. A
        failure that never comes back is raised.
        """
        self.backoff.reset()
        while True:
            lived_from = anyio.current_time()
            self.slept_through = None
            async with anyio.create_task_group() as tasks:
                if self.watch_sleep:
                    tasks.start_soon(self.watch_for_suspend, partial(self.abort, link))
                lost = await hold_one(link)
                tasks.cancel_scope.cancel()
            if self.slept_through is not None:
                lost = self.slept_through
            if lost is None:
                return
            if anyio.current_time() - lived_from >= STABLE_LINK:
                self.backoff.reset()
            again = await self.again(lost)
            if again is None:
                return
            link = again

    async def again(self, lost: Exception) -> L | None:
        """
        Show that the link is gone, and open it again.

        The new link, or `None` when the person stopped it. A failure
        that never comes back is raised, because there is nothing left
        to do about it.

        **The notice keeps saying what is happening.** A probe runs
        beside the retries, so the notice can say whether this machine
        can reach the internet at all, and each attempt is shown while
        it is in flight, because an attempt can outlast its wait by
        far.
        """
        internet = Internet()
        outcome: L | Exception | _Left | None = None
        restart = lost if isinstance(lost, Restarting) else None

        def restarting() -> bool:
            return restart is not None and anyio.current_time() < restart.until

        try:
            async with anyio.create_task_group() as watching:
                watching.start_soon(watch_internet, internet, WAN_INTERVAL)
                # **A restart's refusals are not answers.** A socket that
                # takes no channel is one that is not there, which never
                # comes back -- except in the window the server named:
                # the next image takes the socket over within it, and
                # its queue may be full until then. Lillecarl/pyterm#752.
                while link_may_come_back(lost) or restarting():
                    if restart is not None and restarting():
                        outcome = await self._one_try(RESTART_RETRY, restart, internet)
                    else:
                        outcome = await self._one_try(self.backoff.next(), lost, internet)
                    if not isinstance(outcome, Exception):
                        break
                    lost = outcome
                watching.cancel_scope.cancel()
        finally:
            self.waiting.done()

        # Raised out here: inside the task group the reader sees
        # "unhandled errors in a TaskGroup" and not the reason.
        if outcome is None or isinstance(outcome, Exception):
            raise lost
        return None if isinstance(outcome, _Left) else outcome

    async def _one_try(self, wait: float, lost: Exception, internet: Internet) -> L | Exception | _Left:
        "Count down one wait, then attempt the link: `_LEFT`, the failure, or the link."
        ends_at = anyio.current_time() + wait
        while (left := ends_at - anyio.current_time()) > 0:
            self.waiting.show(why(lost), left, internet.value, False)
            answer = await self.waiting.wait(min(left, COUNTDOWN_STEP))
            if answer is Wait.LEAVE:
                return _LEFT
            if answer is Wait.NOW:
                break
        return await self._attempt(lost, internet)

    async def _attempt(self, lost: Exception, internet: Internet) -> L | Exception | _Left:
        """
        Open the link while the notice keeps saying it is trying.

        The attempt runs as a task of its own, so this one can show and
        listen beside it; cancelling the scope is how the winner ends
        the other.
        """
        link: L | None = None
        failure: Exception | None = None
        left = False

        async with anyio.create_task_group() as tasks:

            async def connect() -> None:
                nonlocal link, failure
                try:
                    link = await self.dial(CONNECT_TIMEOUT)
                except Exception as error:
                    failure = error
                finally:
                    tasks.cancel_scope.cancel()

            tasks.start_soon(connect)

            while True:
                self.waiting.show(why(lost), 0.0, internet.value, True)
                if await self.waiting.wait(COUNTDOWN_STEP) is Wait.LEAVE:
                    left = True
                    tasks.cancel_scope.cancel()
                    break

        if left:
            return _LEFT
        if failure is not None:
            return failure
        # `connect` cancels the loop only after it set `link` or `failure`.
        assert link is not None
        return link


class TerminalWaiting:
    """
    An SSH client's wait: the notice fills the terminal, any key but q
    tries at once, and q leaves.
    """

    def __init__(self, client: SshClient) -> None:
        self.client = client
        self._output: Vt100_Output | None = None

    def show(self, said: str, seconds: float, internet: bool | None, trying: bool) -> None:
        if self._output is None:
            self._output = Vt100_Output.from_pty(sys.stdout)
        rows, columns = self.client.size()
        draw(self._output, rows, columns, notice(self.client.target.host, said, seconds, internet, trying))

    async def wait(self, seconds: float) -> Wait:
        stdin_fd = sys.stdin.fileno()
        with anyio.move_on_after(seconds) as when:
            await anyio.wait_readable(stdin_fd)
        if when.cancelled_caught:
            return Wait.ON
        typed = self._read_typed(stdin_fd)
        if typed is None or any(one in typed for one in LEAVE):
            # No keyboard, or the person is done.
            return Wait.LEAVE
        # Somebody who knows the link is back already.
        return Wait.NOW if typed else Wait.ON

    def done(self) -> None:
        if self._output is not None:
            self._output.show_cursor()
            self._output.flush()
            self._output = None

    def _read_typed(self, stdin_fd: int) -> str | None:
        "What is typed right now, or None when the keyboard is gone."
        reader = self.client.stdin_reader
        with nonblocking(stdin_fd):
            typed = reader.read()
        if reader.closed:
            return None
        return typed
