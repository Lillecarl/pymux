"""
What a person calls a terminal, rather than what pymux derived.

Lillecarl/pymux#335 gave every client a derived name,
`dynhetz:/dev/pts/7`, which is what `-t` selects one by. This is the
chosen one: `desk`, `phone`, `the big screen`.

**tmux has no such thing.** Its `c->name` is set once at identify time
from the tty path and nothing renames it (`server-client.c:2988`), so
the shape here is pymux's own. Lillecarl/pymux#340.
"""

import asyncio
import contextvars
import sys
import time
from contextlib import asynccontextmanager

import pytest
from prompt_toolkit.data_structures import Size

from pymux.client.memory import MemoryClient
from pymux.client.terminal import TerminalClient
from pymux.commands import CommandException
from pymux.commands.common import clients_named
from pymux.main import Pymux
from pymux.pipes.memory import connect_in_memory
from pymux.server import ServerConnection

from session import over_connection
from test_the_link_comes_back import Terminal

SIZE = Size(rows=24, columns=80)

#: A pane that stays up. One that ends at once takes its session with
#: it, which detaches the client this file is about.
WAITS = "%s -c 'import time; time.sleep(30)'" % (sys.executable,)

#: What a client announces to be called `desk`.
CALLED_DESK = [["name", "desk"]]


async def _lines(session, command) -> list:
    "What a command printed on the command line."
    session.pymux.command_output = []
    try:
        session.pymux.handle_command(command)
        return list(session.pymux.command_output)
    finally:
        session.pymux.command_output = None


# ----------------------------------------------------------------------
# Where a name comes from.


async def test_a_client_announces_the_name_it_was_given():
    async with over_connection() as session:
        state, _ = await session.attach("here", SIZE, client_options=CALLED_DESK)

        assert state.name == "desk"


async def test_a_client_that_was_given_none_has_none():
    async with over_connection() as session:
        state, _ = await session.attach("here", SIZE)

        assert state.name == ""


async def test_a_name_can_be_set_while_a_client_runs():
    "The thing #340 asked for: rename the terminal you are sitting at."
    async with over_connection() as session:
        state, _ = await session.attach("here", SIZE)

        session.pymux.handle_command("set-client-option name couch")

        assert state.name == "couch"


def test_the_flag_is_announced_after_the_file(tmp_path):
    """
    A configuration file belongs to one machine and a flag to the one
    terminal it was typed in, so the flag wins.
    """
    path = tmp_path / "pymux.conf"
    path.write_text("set-client-option name from-the-file\n")

    client = TerminalClient()
    client.config_file = str(path)
    client.chosen_name = "from-the-flag"

    assert client._client_options() == [
        ("name", "from-the-file"),
        ("name", "from-the-flag"),
    ]


def test_a_client_with_no_flag_announces_only_the_file(tmp_path):
    path = tmp_path / "pymux.conf"
    path.write_text("set-client-option name from-the-file\n")

    client = TerminalClient()
    client.config_file = str(path)

    assert client._client_options() == [("name", "from-the-file")]


@asynccontextmanager
async def a_real_client(monkeypatch, config_file, chosen_name):
    """
    A real client on a real pty, attached to a real server.

    The tests above hold one end each: `_client_options` says what a
    client announces, and `session.attach` writes the packet by hand.
    This is what joins them -- `_start_gui` reads the flag and the
    file, and the server takes what it sent. A client reads a keyboard
    and draws frames, so it needs a terminal to do it on.
    """
    terminal = Terminal()
    monkeypatch.setattr(sys, "stdin", terminal.stdin)
    monkeypatch.setattr(sys, "stdout", terminal.stdout)

    pymux = Pymux()
    pymux.test_mode = True

    async with pymux.running():
        pymux.create_window(WAITS)
        server_end, client_end = connect_in_memory()

        # A context of its own, the same as `run_integrated`.
        context = contextvars.copy_context()
        connection = context.run(lambda: ServerConnection(pymux, server_end))
        pymux.connections.append(connection)

        client = MemoryClient(client_end)
        client.config_file = str(config_file)
        client.chosen_name = chosen_name

        attach = asyncio.ensure_future(client.attach())
        try:
            ends_at = time.monotonic() + 20.0
            while connection.client_state is None and time.monotonic() < ends_at:
                if attach.done():
                    attach.result()
                    raise AssertionError("the attach ended before it attached")
                await asyncio.sleep(0.02)

            assert connection.client_state is not None, "the client never attached"
            yield pymux, connection.client_state
        finally:
            attach.cancel()
            terminal.close()
            pymux.stop()
            for window in list(pymux.arrangement.windows):
                for pane in list(window.panes):
                    if not pane.process.is_terminated:
                        pane.process.kill()


async def test_the_flag_reaches_the_server(monkeypatch, tmp_path):
    "`pymux attach -n desk`, all the way to what `-t desk` selects."
    empty = tmp_path / "pymux.conf"
    empty.write_text("")

    async with a_real_client(monkeypatch, empty, "desk") as (pymux, state):
        assert state.name == "desk"
        assert clients_named(pymux, "desk") == [state]


async def test_the_file_reaches_the_server(monkeypatch, tmp_path):
    "And the other half of `_client_options`, which no flag is behind."
    path = tmp_path / "pymux.conf"
    path.write_text("set-client-option name from-the-file\n")

    async with a_real_client(monkeypatch, path, None) as (_pymux, state):
        assert state.name == "from-the-file"


# ----------------------------------------------------------------------
# What it is called, and what selects it.


async def test_the_chosen_name_is_what_a_client_is_called():
    async with over_connection() as session:
        await session.attach("here", SIZE, client_options=CALLED_DESK)

        said = await _lines(session, "list-clients -F '#{client_name}'")

        assert said == ["desk"]


async def test_without_one_the_derived_name_is():
    "`#{client_name}` keeps its meaning: what a person can paste into `-t`."
    async with over_connection() as session:
        state, _ = await session.attach("here", SIZE)

        said = await _lines(session, "list-clients -F '#{client_name}'")

        assert said == [state.connection.name]


async def test_the_derived_name_is_always_reachable():
    "Renaming a terminal must not hide which terminal it is."
    async with over_connection() as session:
        state, _ = await session.attach("here", SIZE, client_options=CALLED_DESK)

        said = await _lines(session, "list-clients -F '#{client_tty}'")

        assert said == [state.connection.name]
        assert said != ["desk"]


async def test_either_name_selects_the_client():
    async with over_connection() as session:
        state, _ = await session.attach("here", SIZE, client_options=CALLED_DESK)

        assert clients_named(session.pymux, "desk") == [state]
        assert clients_named(session.pymux, state.connection.name) == [state]


async def test_a_name_nobody_holds_selects_nothing():
    async with over_connection() as session:
        await session.attach("here", SIZE, client_options=CALLED_DESK)

        with pytest.raises(CommandException):
            clients_named(session.pymux, "couch")


async def test_two_clients_may_share_a_name():
    """
    A derived name is not unique either -- two machines can each have a
    `/dev/pts/3` -- so `-t` answers with every match and
    `detach-client -t desk` detaches both terminals called `desk`.
    """
    async with over_connection() as session:
        one, _ = await session.attach("one", SIZE, client_options=CALLED_DESK)
        two, _ = await session.attach("two", SIZE, client_options=CALLED_DESK)

        assert clients_named(session.pymux, "desk") == [one, two]


async def test_detach_client_takes_the_chosen_name():
    async with over_connection() as session:
        state, _ = await session.attach("here", SIZE, client_options=CALLED_DESK)
        assert session.pymux.clients == [state]

        session.pymux.handle_command("detach-client -t desk")

        assert session.pymux.clients == []
