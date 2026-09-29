"""
A client that only watches: `attach-session -r`.

Two rules make it, and they are enforced in two different places
because a person reaches a session two different ways.

**The keys go to the widget.** A pane's key, a paste and the mouse all
end in `ptterm`, so `may_type` on the widget is what drops them, and
pymux answers it for whichever client the key came from.

**The commands go through `call_command_handler`.** Every route to a
command comes through that one function, so a command a watcher may
run is marked at `add_command` and refused there. The default is no,
and four commands are marked: the ones a watcher needs to leave, to
move, and to see who else is here.

Lillecarl/pymux#467.
"""

import sys

from prompt_toolkit.application.current import set_app
from prompt_toolkit.data_structures import Size

from session import once, over_connection

SIZE = Size(rows=24, columns=80)

#: A pane that stays up. The last window of a session ending takes the
#: session with it, which detaches the client this file is about.
WAITS = "%s -c 'import time; time.sleep(30)'" % (sys.executable,)


def _the_pane(pymux):
    return pymux.arrangement.get_active_window().active_pane


def _what_the_pane_reads(pymux) -> list:
    "Take the pane's input, so a test can read what a key wrote."
    written: list = []
    _the_pane(pymux).process.write_input = written.append
    return written


# ----------------------------------------------------------------------
# The keys.


async def test_a_key_from_an_ordinary_client_reaches_the_pane():
    "The control. Without it the test below passes on a broken keyboard."
    async with over_connection() as session:
        session.pymux.create_window(WAITS)
        state, _ = await session.attach("watcher", SIZE)
        written = _what_the_pane_reads(session.pymux)

        session.typed(state, "x")

        await once(lambda: written, 5.0, "an ordinary client typed nothing")
        assert written == ["x"]


async def test_a_key_from_a_read_only_client_never_reaches_the_pane():
    async with over_connection() as session:
        session.pymux.create_window(WAITS)
        state, _ = await session.attach("watcher", SIZE)
        state.read_only = True
        written = _what_the_pane_reads(session.pymux)

        session.typed(state, "x")
        # **And then the prefix, which pymux keeps for itself.** A
        # dropped key leaves nothing behind to wait for, and a test
        # that waited for nothing would pass with no guard at all --
        # measured, by taking the guard out. Both keys travel one
        # connection into one key processor, so the prefix landing
        # says the character before it has been handled.
        session.typed(state, "\x02")

        await once(lambda: state.has_prefix, 5.0, "the keys never reached the server")
        assert written == []


# ----------------------------------------------------------------------
# The commands.


async def test_a_read_only_client_is_refused_a_command_that_writes():
    async with over_connection() as session:
        pymux = session.pymux
        pymux.create_window(WAITS)
        state, _ = await session.attach("watcher", SIZE)
        state.read_only = True

        window = pymux.arrangement.get_active_window()
        with set_app(state.app):
            pymux.handle_command("rename-window not-mine")

        assert state.message == "client is read-only"
        assert window.name != "not-mine"


async def test_a_read_only_client_may_still_leave():
    """
    `detach-client` is marked, because the one key such a person needs
    would otherwise be the one they cannot press.
    """
    async with over_connection() as session:
        pymux = session.pymux
        pymux.create_window(WAITS)
        state, _ = await session.attach("watcher", SIZE)
        state.read_only = True

        with set_app(state.app):
            pymux.handle_command("detach-client")

        await once(
            lambda: state.connection._closed,
            5.0,
            "a read-only client could not detach itself",
        )
        assert state.message != "client is read-only"


async def test_a_read_only_client_may_still_read_the_clients():
    async with over_connection() as session:
        pymux = session.pymux
        pymux.create_window(WAITS)
        state, _ = await session.attach("watcher", SIZE)
        state.read_only = True

        with set_app(state.app):
            pymux.handle_command("list-clients")

        assert state.message != "client is read-only"


async def test_a_command_from_a_pane_is_nobody_and_is_not_refused():
    """
    A command that arrives over the socket runs under a client that
    draws nothing and stands for nobody, so the flag of the person
    watching says nothing about it. tmux reads the flag of the calling
    client in the same way (`server-client.c:2856`).
    """
    async with over_connection() as session:
        pymux = session.pymux
        pymux.create_window(WAITS)
        state, _ = await session.attach("watcher", SIZE)
        state.read_only = True

        window = pymux.arrangement.get_active_window()
        await session.command("rename-window from-a-pane")

        await once(
            lambda: window.name == "from-a-pane",
            5.0,
            "a command from a pane was refused for somebody else's flag",
        )
