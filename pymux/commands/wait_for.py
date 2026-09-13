"""
`wait-for`: hold a client until somebody wakes it.

Two scripts that have to take turns need somewhere to say "I am done".
tmux gives them a named channel with four moves, and they are read
here from `cmd-wait-for.c` rather than from memory:

- `wait-for <name>` waits. A channel that was already signalled with
  nobody waiting lets the first waiter straight through and forgets
  the signal.
- `wait-for -S <name>` signals. With waiters it wakes every one of
  them; with none it remembers the signal for the next waiter.
- `wait-for -L <name>` takes the lock, and waits when somebody holds
  it.
- `wait-for -U <name>` gives it back. The lock passes to the first
  waiting locker, or the channel unlocks.

**The waiting is the whole issue.** A command used to be a plain
function, so a wait could only be a wait on the server's own event
loop -- and the client that would signal the channel is answered by
that same loop. The canonical pair (`wait-for done` in one pane,
`wait-for -S done` in another) was a deadlock, so this command stayed
out of the tree. Lillecarl/pymux#302.

A handler may answer later now, so the waiting is one task and the
server keeps serving. Lillecarl/pymux#87.
"""

import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


import anyio

from pymux.commands import CommandException, add_command


class WaitChannel:
    """
    One named channel: what it remembers, and who waits on it.

    `woken` is the signal that arrived before anybody waited. tmux
    keeps exactly one, and the first waiter takes it.
    """

    def __init__(self) -> None:
        self.woken = False
        self.locked = False
        self.waiters: list[anyio.Event] = []
        self.lockers: list[anyio.Event] = []

    @property
    def is_spent(self) -> bool:
        "Nothing is left to remember, so the channel need not exist."
        return not (self.woken or self.locked or self.waiters or self.lockers)


def wait_for(pymux: "Pymux", args: argparse.Namespace):
    """
    Wait on a named channel, or wake one.

    `wait-for done` holds until `wait-for -S done` runs somewhere
    else. -L takes a lock and -U gives it back.
    """
    if not (args.S or args.U):
        # Before the channel is made, so a refusal leaves nothing.
        _refuse_without_a_waiter(pymux, "lock" if args.L else "wait")

    channel = pymux.wait_channels.get(args.name)
    if channel is None:
        channel = WaitChannel()
        pymux.wait_channels[args.name] = channel

    if args.S:
        return _signal(pymux, args.name, channel)
    if args.L:
        return _lock(channel)
    if args.U:
        return _unlock(pymux, args.name, channel)
    return _wait(pymux, args.name, channel)


def _signal(pymux: "Pymux", name: str, channel: WaitChannel) -> None:
    if not channel.waiters:
        channel.woken = True
        return None

    for waiting in channel.waiters:
        waiting.set()
    channel.waiters = []
    _forget_if_spent(pymux, name, channel)
    return None


def _refuse_without_a_waiter(pymux: "Pymux", what: str) -> None:
    """
    A wait belongs to somebody who is waiting for the answer.

    tmux refuses a wait and a lock from a command with no client
    (`cmd_wait_for_wait`, "not able to wait"), and the reason holds
    here for a shape of its own: a sync route does not wait, it
    spawns, so `bind-key X wait-for done` would put a task in the
    server's group for every press and none of them would ever end.

    `command_output` is what says a client is reading the answer: the
    socket route sets it and a key binding, a hook and a configuration
    file do not. Lillecarl/pymux#302.
    """
    if pymux.command_output is None:
        raise CommandException("not able to %s" % (what,))


def _wait(pymux: "Pymux", name: str, channel: WaitChannel):
    if channel.woken:
        # The signal that arrived first. One waiter takes it.
        channel.woken = False
        _forget_if_spent(pymux, name, channel)
        return None

    woken = anyio.Event()
    channel.waiters.append(woken)

    async def until_somebody_signals() -> None:
        await woken.wait()

    return until_somebody_signals()


def _lock(channel: WaitChannel):
    if not channel.locked:
        channel.locked = True
        return None

    ours = anyio.Event()
    channel.lockers.append(ours)

    async def until_it_is_ours() -> None:
        await ours.wait()

    return until_it_is_ours()


def _unlock(pymux: "Pymux", name: str, channel: WaitChannel) -> None:
    if not channel.locked:
        _forget_if_spent(pymux, name, channel)
        raise CommandException("channel %s not locked" % (name,))

    if channel.lockers:
        # The lock passes rather than opens: the next locker holds it
        # from here, and the channel stays locked.
        channel.lockers.pop(0).set()
        return None

    channel.locked = False
    _forget_if_spent(pymux, name, channel)
    return None


def _forget_if_spent(pymux: "Pymux", name: str, channel: WaitChannel) -> None:
    if channel.is_spent:
        pymux.wait_channels.pop(name, None)


def register(subparsers):
    parser = add_command(subparsers, wait_for, aliases=["wait"])
    parser.add_argument("-S", dest="S", action="store_true", help="Signal the channel: every waiter goes on, or the next one does.")
    parser.add_argument("-L", dest="L", action="store_true", help="Lock the channel, and wait while somebody else holds it.")
    parser.add_argument("-U", dest="U", action="store_true", help="Unlock the channel, and hand it to the first waiting locker.")
    parser.add_argument("name", metavar="<channel>", help="The name of the channel.")
