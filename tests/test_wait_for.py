"""
`wait-for`: two scripts taking turns on a named channel.

The four moves are read from tmux's `cmd-wait-for.c` and not from
memory: a signal with nobody waiting is remembered for the next
waiter, a signal with waiters wakes every one of them, `-L` waits
while somebody holds the lock, and `-U` hands the lock to the first
waiting locker rather than opening it.

**The deadlock is what this file is really about.** A command used to
be a plain function, so a wait could only be a wait on the server's
own event loop -- and the client that would signal the channel is
answered by that same loop. `wait-for done` in one pane and
`wait-for -S done` in another could never meet, so the command stayed
out of the tree rather than land as a hang. Lillecarl/pymux#302.

`test_two_clients_take_turns_over_the_socket` is that pair, run for
real over two connections. It is the test the issue asked for.
"""

import anyio

from prompt_toolkit.application.current import set_app
from prompt_toolkit.data_structures import Size

from pymux.commands import handle_command
from session import create_session, over_connection

SIZE = Size(rows=24, columns=80)


def _ask(pymux, state, command):
    "Run a command and answer with what it left to finish, if anything."
    with set_app(state.app):
        return handle_command(pymux, command)


# ----------------------------------------------------------------------
# Signal and wait.


async def test_a_signal_with_nobody_waiting_is_remembered():
    "tmux keeps exactly one, and the first waiter takes it."
    async with create_session() as (pymux, state):
        assert _ask(pymux, state, "wait-for -S done") is None

        assert _ask(pymux, state, "wait-for done") is None, "the signal was lost"


async def test_the_remembered_signal_is_taken_only_once():
    async with create_session() as (pymux, state):
        _ask(pymux, state, "wait-for -S done")
        _ask(pymux, state, "wait-for done")

        await _it_waits(_ask(pymux, state, "wait-for done"))


async def test_a_wait_with_no_signal_waits():
    async with create_session() as (pymux, state):
        waiting = _ask(pymux, state, "wait-for done")
        assert waiting is not None

        async with anyio.create_task_group() as tasks:
            tasks.start_soon(_finish, waiting)
            await _until(lambda: pymux.wait_channels["done"].waiters)

            _ask(pymux, state, "wait-for -S done")


async def test_a_signal_wakes_every_waiter():
    async with create_session() as (pymux, state):
        first = _ask(pymux, state, "wait-for shared")
        second = _ask(pymux, state, "wait-for shared")

        async with anyio.create_task_group() as tasks:
            tasks.start_soon(_finish, first)
            tasks.start_soon(_finish, second)
            await _until(lambda: len(pymux.wait_channels["shared"].waiters) == 2)

            _ask(pymux, state, "wait-for -S shared")


async def test_a_signal_that_woke_waiters_is_not_remembered_as_well():
    "The waiters took it. A later wait is a wait."
    async with create_session() as (pymux, state):
        waiting = _ask(pymux, state, "wait-for done")

        async with anyio.create_task_group() as tasks:
            tasks.start_soon(_finish, waiting)
            await _until(lambda: pymux.wait_channels["done"].waiters)
            _ask(pymux, state, "wait-for -S done")

        await _it_waits(_ask(pymux, state, "wait-for done"))


async def test_a_channel_nobody_needs_is_forgotten():
    "A dict that only grows is a leak with a name in it."
    async with create_session() as (pymux, state):
        _ask(pymux, state, "wait-for -S gone")
        assert "gone" in pymux.wait_channels

        _ask(pymux, state, "wait-for gone")
        assert "gone" not in pymux.wait_channels


# ----------------------------------------------------------------------
# The lock.


async def test_the_first_lock_is_taken_at_once():
    async with create_session() as (pymux, state):
        assert _ask(pymux, state, "wait-for -L one") is None


async def test_a_second_lock_waits_and_the_unlock_hands_it_over():
    async with create_session() as (pymux, state):
        _ask(pymux, state, "wait-for -L one")
        second = _ask(pymux, state, "wait-for -L one")
        assert second is not None

        async with anyio.create_task_group() as tasks:
            tasks.start_soon(_finish, second)
            await _until(lambda: pymux.wait_channels["one"].lockers)

            _ask(pymux, state, "wait-for -U one")

        # Handed over, not opened: the second holder has it now.
        assert pymux.wait_channels["one"].locked


async def test_unlocking_with_nobody_waiting_opens_the_channel():
    async with create_session() as (pymux, state):
        _ask(pymux, state, "wait-for -L one")
        _ask(pymux, state, "wait-for -U one")

        assert "one" not in pymux.wait_channels
        assert _ask(pymux, state, "wait-for -L one") is None


async def test_unlocking_a_channel_nobody_locked_is_an_error():
    "tmux says so rather than doing nothing, and so does this."
    async with create_session() as (pymux, state):
        pymux.command_error = []
        try:
            _ask(pymux, state, "wait-for -U never")
            said = pymux.command_error
        finally:
            pymux.command_error = None

        assert any("not locked" in line for line in said), said
        assert "never" not in pymux.wait_channels


# ----------------------------------------------------------------------
# The pair the issue named.


async def test_two_clients_take_turns_over_the_socket():
    """
    One client waits and another signals, both over their own
    connection. This is the deadlock Lillecarl/pymux#302 described:
    the loop that would answer the second client is the one the first
    was sleeping on.
    """
    async with over_connection() as session:
        await session.attach("the person", SIZE)

        waiting = await session.command("wait-for done")

        await _until(lambda: session.pymux.wait_channels.get("done"))
        assert not _exit_of(waiting), "the waiting client was answered at once"

        signalling = await session.command("wait-for -S done")

        await _until(lambda: _exit_of(signalling))
        await _until(lambda: _exit_of(waiting))


# ----------------------------------------------------------------------


async def _it_waits(answer) -> None:
    """
    That the command really waits: await it, and give up on it.

    Awaiting is what says it waits. Dropping the coroutine instead
    would say the same and warn at collection time, which is noise in
    a suite that is meant to be read.
    """
    assert answer is not None, "the command did not wait"

    with anyio.move_on_after(0.05) as gave_up:
        await answer

    assert gave_up.cancelled_caught, "the command answered without waiting"


async def _finish(answer) -> None:
    await answer


async def _until(question, seconds: float = 5.0) -> None:
    with anyio.fail_after(seconds):
        while not question():
            await anyio.sleep(0.005)


def _exit_of(packets) -> list:
    "The exit packets among what a command's connection sent back."
    import json

    found = []
    for packet in packets:
        text = packet.decode("utf-8") if isinstance(packet, bytes) else packet
        try:
            read = json.loads(text)
        except ValueError:
            continue
        if read.get("cmd") == "exit":
            found.append(read)
    return found
