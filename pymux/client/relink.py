"""
What an SSH client does while its link is gone: it shows the notice,
counts down, and opens the link again. And how it learns that the link
went while this machine slept.

`client/reconnect.py` holds the arithmetic and the lines of the notice.
This holds the client while it waits. Lillecarl/pymux#256.
"""

from __future__ import annotations

import sys
import time
from typing import TYPE_CHECKING

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
    from asyncssh import SSHClientConnection

    from .ssh import Link, SshClient

__all__ = [
    "CONNECT_TIMEOUT",
    "SUSPEND_GAP",
    "SUSPEND_POLL",
    "Relink",
    "Restarting",
]

#: How often to look for a gap between the clocks, in seconds.
SUSPEND_POLL = 1.0

#: How far the world may move ahead of the loop before this client
#: calls the link gone, in seconds.
#:
#: The number only has to be above the jitter of a poll that is late
#: under load, and below the shortest sleep worth noticing. A laptop
#: lid closes for minutes, never for four seconds.
SUSPEND_GAP = 4.0

#: How often the disconnected screen draws its countdown, in seconds.
COUNTDOWN_STEP = 1.0

#: How long one reconnection attempt may last, in seconds.
#:
#: **Without this the screen freezes on the last second of the
#: countdown.** asyncssh passes no timeout to the connect by default,
#: so a network that drops packets without refusing them -- an uplink
#: that went, a route that vanished -- leaves the TCP connect in the
#: kernel's hands for the minute or two `tcp_syn_retries` takes. The
#: countdown has already drawn "1 second" by then, and nothing moves
#: until the connect returns. A bound makes the retry loop turn, and
#: the wait grows the way it is meant to.
#:
#: **Only an attempt after a loss is bounded.** A person who just
#: typed `attach` would rather wait than be told no too soon, so the
#: first connect and a command keep asyncssh's own default. Eight
#: seconds is above a slow handshake -- a mobile link with a second of
#: round trip, a loaded far machine -- and well below the twenty a
#: keepalive already spends before it calls a link dead.
CONNECT_TIMEOUT = 8.0

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


class _Left:
    "The person stopped the client. A type, so a result can be told apart."


#: What a try returns when the person stopped the client. A sentinel
#: and not an exception, so nothing mistakes it for a failure to retry.
_LEFT = _Left()


def _how_long(seconds: float) -> str:
    "A sleep, in the largest unit that keeps it a small number."
    if seconds < 90:
        return "%d seconds" % (round(seconds),)
    if seconds < 90 * 60:
        return "%d minutes" % (round(seconds / 60),)
    return "%.1f hours" % (seconds / 3600,)


class Relink:
    "An SSH client's link while it is gone, and the watch that ends one this machine slept through."

    def __init__(self, client: SshClient) -> None:
        self.client = client
        #: Why this client ended its own link, when it did. Only the
        #: suspend watch sets it, and only `SshClient._attached` reads it.
        #: Lillecarl/pymux#445.
        self.slept_through: Exception | None = None

    def clocks(self) -> tuple[float, float]:
        """
        The world's clock and the loop's, read together.

        A method so that a test can move one of them without sleeping
        a machine.
        """
        return time.time(), anyio.current_time()

    async def watch_for_suspend(self, connection: SSHClientConnection) -> None:
        """
        End the link when this machine has been asleep.

        **A sleep is invisible to everything else here.** The loop's
        clock is `time.monotonic`, which on Linux does not run while
        the machine is suspended, so every timer that would notice a
        dead link is suspended with it: asyncssh's keepalive resumes
        counting from where it stopped and takes its full four
        intervals *after* the lid opens. The person looks at a frozen
        screen for that whole time, and the panes they are looking at
        have been reachable for most of it.

        So this watches the one thing a sleep cannot hide: the world
        moved and the loop did not. It needs no signal, no bus and no
        platform code, and it is right whichever way a platform
        chooses. Where the loop's clock keeps running through a sleep
        there is no gap to find, and there is nothing to fix either --
        the keepalive timers ran too, so they have already given up by
        the time anybody looks.

        **A gap ends the link rather than testing it.** A machine that
        has been asleep has almost always lost its TCP connections,
        and the two outcomes are not equal: reconnecting a link that
        was alive costs about a second and the panes are on the other
        machine anyway, while waiting on a link that is dead costs the
        twenty seconds the keepalive needs. A clock that was stepped
        forwards by something other than a sleep pays the same second.

        **A link this client ended reads as end of file either way**,
        and end of file is how a server says the person detached. So
        the reason is written down before the link goes, and
        `_attached` uses it in place of the ending it would otherwise
        read. `_packets` measured `ConnectionLost` for an abort, but
        that was the far side aborting; aborting our own gives a clean
        end here, and a client that trusted the measurement left
        instead of coming back.

        A reset is what the reason says, because that is what a sleep
        did to the socket, and `link_may_come_back` retries an
        `OSError`. Lillecarl/pymux#445.
        """
        was_wall, was_loop = self.clocks()

        while True:
            await anyio.sleep(SUSPEND_POLL)

            wall, loop = self.clocks()
            asleep = (wall - was_wall) - (loop - was_loop)
            was_wall, was_loop = wall, loop

            if asleep >= SUSPEND_GAP:
                self.slept_through = ConnectionResetError("This machine was asleep for %s." % (_how_long(asleep),))
                connection.abort()
                return

    async def again(self, stdin_fd: int, waits: Backoff, lost: Exception) -> Link | None:
        """
        Show that the server is gone, and open the link again.

        The connection and its reader, or `None` when the person
        stopped it. A failure that never comes back is raised, because
        there is nothing left for this client to do about it.

        **The screen keeps saying what is happening.** A probe runs
        beside the retries, so the notice can say whether this machine
        can reach the internet at all, and each attempt is drawn while
        it is in flight, because an attempt can outlast its wait by
        far. `reconnect.notice` holds the lines.
        """
        output = Vt100_Output.from_pty(sys.stdout)
        internet = Internet()
        outcome = None
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
                # its queue may be full until then. Lillecarl/pymux#750.
                while link_may_come_back(lost) or restarting():
                    if restart is not None and restarting():
                        outcome = await self._one_try(output, stdin_fd, RESTART_RETRY, restart, internet)
                    else:
                        outcome = await self._one_try(output, stdin_fd, waits.next(), lost, internet)
                    if not isinstance(outcome, Exception):
                        break
                    lost = outcome
                watching.cancel_scope.cancel()
        finally:
            output.show_cursor()
            output.flush()

        # Raised out here: inside the task group the person reads
        # "unhandled errors in a TaskGroup" and not the reason.
        if outcome is None or isinstance(outcome, Exception):
            raise lost
        return None if isinstance(outcome, _Left) else outcome

    async def _one_try(
        self, output: Vt100_Output, stdin_fd: int, wait: float, lost: Exception, internet: Internet
    ) -> Link | Exception | _Left:
        """
        Count down one wait, then attempt the link.

        `_LEFT`, the failure, or the connection. Any key but q ends the
        wait at once, which is what somebody does who knows the link
        is back.
        """
        ends_at = anyio.current_time() + wait

        while True:
            left = ends_at - anyio.current_time()
            if left <= 0:
                break

            self._draw(output, lost, internet, left)
            with anyio.move_on_after(min(left, COUNTDOWN_STEP)) as when:
                await anyio.wait_readable(stdin_fd)
            if when.cancelled_caught:
                continue  # Nothing typed. Draw the countdown again.

            typed = self._read_typed(stdin_fd)
            if typed is None or any(one in typed for one in LEAVE):
                return _LEFT  # No keyboard, or the person is done.
            if typed:
                break  # Somebody who knows the link is back already.

        return await self._attempt(output, stdin_fd, lost, internet)

    def _draw(
        self, output: Vt100_Output, lost: Exception, internet: Internet, seconds: float, trying: bool = False
    ) -> None:
        "Put the notice of this moment on the terminal."
        rows, columns = self.client.size()
        draw(
            output,
            rows,
            columns,
            notice(self.client.target.host, why(lost), seconds, internet.value, trying),
        )

    def _read_typed(self, stdin_fd: int) -> str | None:
        "What is typed right now, or None when the keyboard is gone."
        reader = self.client.stdin_reader
        with nonblocking(stdin_fd):
            typed = reader.read()
        if reader.closed:
            return None
        return typed

    async def _attempt(
        self, output: Vt100_Output, stdin_fd: int, lost: Exception, internet: Internet
    ) -> Link | Exception | _Left:
        """
        Open the link while the screen keeps saying it is trying.

        The connection, the failure, or `_LEFT`. The attempt runs as a
        task of its own, so this one can draw and listen beside it;
        cancelling the scope is how the winner ends the other.
        """
        link: Link | None = None
        failure: Exception | None = None
        left = False

        async with anyio.create_task_group() as tasks:

            async def connect() -> None:
                nonlocal link, failure
                try:
                    link = await self.client.connect(CONNECT_TIMEOUT)
                except Exception as error:
                    failure = error
                finally:
                    tasks.cancel_scope.cancel()

            tasks.start_soon(connect)

            while True:
                self._draw(output, lost, internet, 0.0, trying=True)
                with anyio.move_on_after(COUNTDOWN_STEP) as when:
                    await anyio.wait_readable(stdin_fd)
                if when.cancelled_caught:
                    continue

                typed = self._read_typed(stdin_fd)
                if typed is None or any(one in typed for one in LEAVE):
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
