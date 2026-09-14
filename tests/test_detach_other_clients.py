"""
`detach-client -a` and `-s`: leaving on somebody else's behalf.

Before this, `detach-client` took no arguments at all. It detached
whoever ran it and there was no way to reach any other client, so a
person who found a session attached twice could only leave it
themselves or kill the server.

These two select clients without naming one, which is why they are
here and `-t <client>` is not: a client has no name yet, and what it
should be called is Lillecarl/pymux#335.

tmux's rules, from `cmd-detach-client.c`:

- `-s <session>`: every client whose session is that one, the caller
  included.
- `-a`: every client but the target, and only those attached to
  something. The target is the caller, and for a command that came
  from a pane it is the client that owns the pane -- which is why this
  reads `the_client_to_tell` and not `get_client_state`.
"""

import argparse

import pytest

from prompt_toolkit.application.current import set_app
from prompt_toolkit.data_structures import Size

from pymux.commands import CommandException
from pymux.commands.detach_client import detach_client

from session import in_this_process, over_connection

SIZE = Size(rows=24, columns=80)


def run_at(state, text):
    "A command typed at this client, the way its command line runs one."
    with set_app(state.app):
        state.pymux.handle_command(text)


async def test_a_detaches_the_others_and_keeps_the_one_that_asked():
    async with in_this_process() as session:
        pymux = session.pymux
        here, _ = await session.attach("here", SIZE)
        await session.attach("there", SIZE)
        await session.attach("elsewhere", SIZE)
        assert len(pymux.clients) == 3

        run_at(here, "detach-client -a")

        assert pymux.clients == [here]


async def test_a_leaves_a_lone_client_alone():
    "The common case: one terminal, and nothing else to detach."
    async with in_this_process() as session:
        pymux = session.pymux
        here, _ = await session.attach("here", SIZE)

        run_at(here, "detach-client -a")

        assert pymux.clients == [here]


async def test_a_from_a_pane_keeps_the_terminal_a_person_is_at():
    """
    A command from a pane's CLI runs under a temporary client that
    draws nothing. Keeping *that* one would detach every real
    terminal, so the target is the client a person used last.
    Lillecarl/pymux#272 is the same rule for dialogs.
    """
    async with in_this_process() as session:
        pymux = session.pymux
        await session.attach("here", SIZE)
        there, _ = await session.attach("there", SIZE)
        with set_app(there.app):
            kept = pymux.the_client_to_tell()

        session.command("detach-client -a")

        assert pymux.clients == [kept]
        assert kept.temporary is False


async def test_s_detaches_every_client_of_that_session():
    "Including the one that asked, which is what tmux does."
    async with in_this_process() as session:
        pymux = session.pymux
        here, _ = await session.attach("here", SIZE)
        await session.attach("there", SIZE)

        run_at(here, "detach-client -s %s" % (here.session.name,))

        assert pymux.clients == []


async def test_s_leaves_the_clients_of_another_session():
    async with in_this_process() as session:
        pymux = session.pymux
        here, _ = await session.attach("here", SIZE)
        run_at(here, "new-session -d -s other")

        other = pymux.get_session("other")
        assert other is not None and here.session is not other

        run_at(here, "detach-client -s other")

        assert pymux.clients == [here], "it detached a client of this session"


async def test_s_says_so_when_no_session_carries_that_name():
    async with in_this_process() as session:
        pymux = session.pymux
        here, _ = await session.attach("here", SIZE)

        # The command function, not `handle_command`: that one turns a
        # `CommandException` into a message for the person who typed
        # it. `test_environment.py` asks the same way.
        with set_app(here.app), pytest.raises(CommandException, match="no-such-session"):
            detach_client(
                pymux,
                argparse.Namespace(
                    all_but_this_one=False, target_session="no-such-session"
                ),
            )

        assert pymux.clients == [here], "a bad name detached somebody"


async def test_with_no_argument_it_still_detaches_only_the_caller():
    "The old behaviour, which the two flags must not have moved."
    async with in_this_process() as session:
        pymux = session.pymux
        here, _ = await session.attach("here", SIZE)
        there, _ = await session.attach("there", SIZE)

        run_at(here, "detach-client")

        assert pymux.clients == [there]


async def test_a_reaches_the_other_client_over_the_wire():
    "The socket route, where a detach closes a real connection."
    async with over_connection() as session:
        pymux = session.pymux
        here, _ = await session.attach("here", SIZE)
        await session.attach("there", SIZE)
        assert len(pymux.clients) == 2

        run_at(here, "detach-client -a")

        assert pymux.clients == [here]
