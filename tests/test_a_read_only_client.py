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

from session import in_this_process, once, over_connection

SIZE = Size(rows=24, columns=80)

#: One terminal with room, and one without. A person watching from the
#: small one must not take the columns away from the big one.
BIG = Size(rows=24, columns=100)
SMALL = Size(rows=12, columns=40)

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


# ----------------------------------------------------------------------
# What it may not do to anybody else.
#
# `detach-client` and `attach-session` are marked read only, and both
# take a target beyond the caller. A yes for the command is not a yes
# for that part of it.


async def _two_clients(session):
    "A person working, and a person watching over their shoulder."
    session.pymux.create_window(WAITS)
    working, _ = await session.attach("working", SIZE)
    watching, _ = await session.attach("watching", SIZE)
    watching.read_only = True
    return working, watching


async def test_a_read_only_client_cannot_detach_everybody_else():
    async with over_connection() as session:
        working, watching = await _two_clients(session)

        with set_app(watching.app):
            session.pymux.handle_command("detach-client -a")

        assert watching.message == "client is read-only"
        assert not working.connection._closed


async def test_a_read_only_client_cannot_detach_a_named_client():
    async with over_connection() as session:
        working, watching = await _two_clients(session)

        with set_app(watching.app):
            session.pymux.handle_command(
                "detach-client -t %s" % (working.connection.name,)
            )

        assert watching.message == "client is read-only"
        assert not working.connection._closed


async def test_a_read_only_client_cannot_take_the_session():
    """
    `attach-session -x` hangs up the terminal each other client was
    sitting in. tmux allows this for a read-only client: the loop at
    `cmd-attach-session.c:127` has no guard at all.
    """
    async with over_connection() as session:
        working, watching = await _two_clients(session)

        with set_app(watching.app):
            session.pymux.handle_command("attach-session -x")

        assert watching.message == "client is read-only"
        assert not working.connection._closed


async def test_an_ordinary_client_still_takes_the_session():
    "The control: the guard reads the flag, not the flags."
    async with over_connection() as session:
        working, watching = await _two_clients(session)
        watching.read_only = False

        with set_app(watching.app):
            session.pymux.handle_command("detach-client -a")

        await once(
            lambda: working.connection._closed,
            5.0,
            "an ordinary client could not detach the others",
        )


async def test_a_read_only_client_may_still_read_the_clients():
    async with over_connection() as session:
        pymux = session.pymux
        pymux.create_window(WAITS)
        state, _ = await session.attach("watcher", SIZE)
        state.read_only = True

        with set_app(state.app):
            pymux.handle_command("list-clients")

        assert state.message != "client is read-only"


# ----------------------------------------------------------------------
# The flag, and the two ways to ask for it.


async def test_attach_session_r_marks_the_client_that_ran_it():
    async with over_connection() as session:
        pymux = session.pymux
        pymux.create_window(WAITS)
        state, _ = await session.attach("watcher", SIZE)

        with set_app(state.app):
            pymux.handle_command("attach-session -r")

        assert state.read_only is True
        assert state.ignore_size is True


async def test_attaching_again_without_r_leaves_it_read_only():
    """
    tmux's `-r` only sets: `cmd-attach-session.c:118` adds both flags
    and nothing there takes either away. So an `attach-session` with
    no flag is not a way out of read-only.
    """
    async with over_connection() as session:
        pymux = session.pymux
        pymux.create_window(WAITS)
        state, _ = await session.attach("watcher", SIZE)

        with set_app(state.app):
            pymux.handle_command("attach-session -r")
            pymux.handle_command("attach-session")

        assert state.read_only is True


async def test_a_client_that_attached_with_the_flag_only_watches():
    """
    `pymux attach -r`, which is the flag on the `start-gui` packet. It
    has to be read before the first key arrives, so this is the whole
    of the route the command cannot cover: a person who attaches this
    way is never writable, not even for one frame.
    """
    async with over_connection() as session:
        session.pymux.create_window(WAITS)
        state, _ = await session.attach("watcher", SIZE, read_only=True)

        assert state.read_only is True
        assert state.ignore_size is True


async def test_the_flags_can_be_read_back():
    "`list-clients -F` is how anything outside pymux sees this."
    async with over_connection() as session:
        pymux = session.pymux
        pymux.create_window(WAITS)
        state, _ = await session.attach("watcher", SIZE, read_only=True)

        pymux.command_output = []
        try:
            with set_app(state.app):
                pymux.handle_command("list-clients -F '#{client_flags}'")
            lines = list(pymux.command_output)
        finally:
            pymux.command_output = None

        assert lines == ["read-only,ignore-size"]


# ----------------------------------------------------------------------
# The size of the plane.


async def test_a_read_only_client_does_not_shrink_the_plane():
    async with in_this_process() as session:
        pymux = session.pymux
        pymux.create_window(WAITS)
        await session.attach("working", BIG)
        watching, _ = await session.attach("watching", SMALL)
        watching.ignore_size = True

        assert pymux.plane_size().columns == BIG.columns


async def test_without_the_flag_the_small_client_still_wins():
    "The control: `window-size smallest` is the default."
    async with in_this_process() as session:
        pymux = session.pymux
        pymux.create_window(WAITS)
        await session.attach("working", BIG)
        await session.attach("watching", SMALL)

        assert pymux.plane_size().columns == SMALL.columns


async def test_a_read_only_client_on_its_own_still_sizes_the_plane():
    """
    Nobody left to leave it to. tmux falls back the same way, and for
    the same reason: the alternative is a plane no terminal here can
    show (`resize.c:95-107`).
    """
    async with in_this_process() as session:
        pymux = session.pymux
        pymux.create_window(WAITS)
        watching, _ = await session.attach("watching", SMALL)
        watching.ignore_size = True

        assert pymux.plane_size().columns == SMALL.columns


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
