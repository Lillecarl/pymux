"""
A command handler may be a coroutine, and what that means on each route.

Lillecarl/pymux#87 settles the shape. A handler that only reads the
server is a plain function, and about a hundred of them are. A handler
that has to wait -- `wait-for` for a channel, `run-shell` for a program
-- is a coroutine, and `call_command_handler` answers with it instead
of with `None`.

Two routes then do two different things with that answer, and the
difference is the whole point:

**the socket route awaits it.** `pymux wait-for done` typed in a pane
holds the client that typed it, the way tmux does, and the server keeps
serving every other client while it waits.

**a sync route spawns it.** A key binding, a hook and a configuration
file all run commands from code that cannot wait: a key press has to
return before the next one arrives. The work goes in the server's task
group and the press is done.

The order of a line holds either way: `a ; b` means b after a, and it
still does when a is one that waits.
"""

import json
import sys
from pathlib import Path

import anyio
import pytest
from prompt_toolkit.data_structures import Size

sys.path.insert(0, str(Path(__file__).parent))

from pymux.commands import (  # noqa: E402
    CommandException,
    add_command,
    call_command_handler,
    handle_command,
    parser_tree,
)

from session import create_session, over_connection  # noqa: E402

#: What a client of the socket route reports.
SIZE = Size(rows=24, columns=80)

#: What the commands below leave behind, in the order they ran.
RAN: list = []

#: What `test-waits` waits for. A test lets it through when it has
#: seen what it wanted to see, so the order a test reads is the order
#: by construction and not by the clock of the machine it runs on.
LET_THROUGH: list = []


def _waits(pymux, args):
    "A handler that waits, and says so at both ends."

    async def wait() -> None:
        RAN.append("waiting")
        await LET_THROUGH[0].wait()
        RAN.append("waited")

    return wait()


def _at_once(pymux, args) -> None:
    "A handler of the ordinary kind: everything it does is done."
    RAN.append(args.mark)


def _fails_late(pymux, args):
    "A handler that fails after it waited. The message is a command's."

    async def fail() -> None:
        await anyio.sleep(0)
        raise CommandException("it went wrong after the wait")

    return fail()


@pytest.fixture(autouse=True)
def commands_of_this_file():
    """
    Mount three commands of this file on the parser tree, and take
    them away again. The tree is built once for the process.
    """
    RAN.clear()
    LET_THROUGH[:] = [anyio.Event()]

    _parser, subparsers = parser_tree()

    waits = add_command(subparsers, _waits, name="test-waits")

    at_once = add_command(subparsers, _at_once, name="test-at-once")
    at_once.add_argument("mark")

    add_command(subparsers, _fails_late, name="test-fails-late")

    try:
        yield
    finally:
        for name in ("test-waits", "test-at-once", "test-fails-late"):
            subparsers.choices.pop(name, None)
            subparsers._name_parser_map.pop(name, None)
        subparsers._choices_actions = [
            action
            for action in subparsers._choices_actions
            if not action.metavar.startswith("test-")
        ]


# ----------------------------------------------------------------------
# What the dispatch answers with.


async def test_an_ordinary_handler_answers_with_nothing():
    "The hundred handlers that are already there. Nothing changes."
    async with create_session() as (pymux, _state):
        assert call_command_handler("test-at-once", pymux, ["one"]) is None
        assert RAN == ["one"]


async def test_a_handler_that_waits_answers_with_the_rest_of_the_work():
    async with create_session() as (pymux, _state):
        answer = handle_command(pymux, "test-waits")

        assert answer is not None
        assert RAN == [], "the waiting started before anybody awaited it"

        LET_THROUGH[0].set()
        await answer
        assert RAN == ["waiting", "waited"]


async def test_the_order_of_a_line_holds_across_a_wait():
    "`a ; b` means b after a, and a is the one that waits."
    async with create_session() as (pymux, _state):
        answer = handle_command(pymux, "test-waits ; test-at-once after")

        assert RAN == [], "the second command ran before the first waited"

        LET_THROUGH[0].set()
        await answer
        assert RAN == ["waiting", "waited", "after"]


async def test_what_a_handler_raises_after_the_wait_is_a_command_error():
    async with create_session() as (pymux, _state):
        pymux.command_error = []

        await handle_command(pymux, "test-fails-late")

        assert pymux.command_error == ["pymux: it went wrong after the wait"]


# ----------------------------------------------------------------------
# The sync route: the key press returns, the work goes on.


async def test_a_sync_route_spawns_and_returns_at_once():
    """
    `Pymux.handle_command` is what a key binding, a hook and a
    configuration file call. It answers with nothing, because nobody
    there can wait.
    """
    async with create_session() as (pymux, _state):
        assert pymux.handle_command("test-waits") is None

        # The work is in the server's task group, and it is not done.
        await _until(lambda: RAN == ["waiting"])

        LET_THROUGH[0].set()
        await _until(lambda: RAN == ["waiting", "waited"])


# The dropped work says so as well: closing the outer coroutine leaves
# the handler's own, and Python warns that it was never awaited. That
# warning is the point on a real misuse and noise here.
@pytest.mark.filterwarnings("ignore::RuntimeWarning")
async def test_a_command_outside_a_server_is_not_left_half_run():
    """
    Work nobody finishes is a command that did half of what it says,
    and quietly. A server that is not serving says so instead.
    """
    async with create_session() as (pymux, _state):
        pymux.tasks = None
        with pytest.raises(RuntimeError):
            pymux.handle_command("test-waits")
        assert RAN == []


# ----------------------------------------------------------------------
# The socket route: the client waits, the server does not.


async def test_the_client_of_a_socket_command_waits_for_it():
    """
    The exit packet is the last thing a command's connection sends.
    Here it must arrive after the command finished, which is what
    makes `pymux wait-for done` mean anything.
    """
    async with over_connection() as session:
        await session.attach("the client", SIZE)

        answered = await session.command("test-waits")

        await _until(lambda: RAN == ["waiting"])
        assert not _exit_of(answered), "the client was answered before the wait"

        LET_THROUGH[0].set()
        await _until(lambda: any(_exit_of(answered)))
        assert RAN == ["waiting", "waited"]


async def test_the_server_keeps_serving_while_a_client_waits():
    "The waiting is one client's. The clock of the server still turns."
    async with over_connection() as session:
        await session.attach("the client", SIZE)

        answered = await session.command("test-waits")

        await _until(lambda: RAN == ["waiting"])
        # Another command, on another connection, while the first one
        # is still waiting.
        second = await session.command("test-at-once meanwhile")
        await _until(lambda: any(_exit_of(second)))

        assert RAN == ["waiting", "meanwhile"]

        LET_THROUGH[0].set()
        await _until(lambda: any(_exit_of(answered)))


# ----------------------------------------------------------------------


def _exit_of(packets) -> list:
    "The exit packets among what a command's connection sent back."
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


async def _until(question, seconds: float = 2.0) -> None:
    "Wait for something the server does in a task of its own."
    with anyio.fail_after(seconds):
        while not question():
            await anyio.sleep(0.005)
