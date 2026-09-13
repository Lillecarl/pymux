"""
Many sessions in one server. Lillecarl/pymux#323.

tmux says of itself: "all sessions are managed by a single server".
pymux held one, and the whole surface -- new-, attach-, kill-, rename-,
has- and list-sessions, `#{session_name}`, `#{session_id}`, `$0` --
answered for that one. These tests are what says it does not any more.

The panes run a program that waits, so no shell starts and no pane
ends while a test is looking. A pane that ends takes its session with
it, which `test_a_session_that_empties_goes` is about.
"""

import sys

from prompt_toolkit.application.current import set_app
from prompt_toolkit.data_structures import Size

from pymux.main import Pymux
from session import in_this_process, once, over_connection

SIZE = Size(rows=24, columns=80)

#: A program that stays. A pane that ends takes its window, and the
#: last window takes the session, so a test about sessions needs panes
#: that are still there when it looks.
WAITING = "sleep 30"

#: A program that ends at once, for the one test about a pane ending.
ENDING = "%s -c pass" % (sys.executable,)


def _server():
    "A server whose first window runs a program that waits."
    return Pymux(startup_command=WAITING)


def _command(pymux, state, text):
    "Run a command as the person at this client."
    with set_app(state.app):
        pymux.handle_command(text)


async def test_a_server_starts_with_one_session():
    async with in_this_process(_server()) as session:
        pymux = session.pymux
        assert len(pymux.sessions) == 1
        assert pymux.sessions[0].name == "0"
        assert pymux.sessions[0].session_id == 0


async def test_new_session_adds_one_and_moves_the_client():
    async with in_this_process(_server()) as session:
        pymux = session.pymux
        state, _ = await session.attach("only", SIZE)

        first = state.session
        _command(pymux, state, "new-session -s work '%s'" % (WAITING,))

        assert [s.name for s in pymux.sessions] == ["0", "work"]
        assert state.session.name == "work"
        assert state.session is not first


async def test_new_session_with_d_leaves_the_client_where_it_is():
    async with in_this_process(_server()) as session:
        pymux = session.pymux
        state, _ = await session.attach("only", SIZE)

        _command(pymux, state, "new-session -d -s work '%s'" % (WAITING,))

        assert len(pymux.sessions) == 2
        assert state.session.name == "0"


async def test_a_second_session_cannot_take_a_name_that_is_taken():
    async with in_this_process(_server()) as session:
        pymux = session.pymux
        state, _ = await session.attach("only", SIZE)

        _command(pymux, state, "new-session -d -s work '%s'" % (WAITING,))
        _command(pymux, state, "new-session -d -s work '%s'" % (WAITING,))

        assert len(pymux.sessions) == 2
        assert "duplicate session: work" in state.message


async def test_each_session_holds_its_own_windows():
    async with in_this_process(_server()) as session:
        pymux = session.pymux
        state, _ = await session.attach("only", SIZE)

        _command(pymux, state, "new-session -d -s work '%s'" % (WAITING,))
        _command(pymux, state, "new-window '%s'" % (WAITING,))

        first, work = pymux.sessions
        assert len(first.arrangement.windows) == 2
        assert len(work.arrangement.windows) == 1


async def test_a_client_switches_between_sessions():
    async with in_this_process(_server()) as session:
        pymux = session.pymux
        state, _ = await session.attach("only", SIZE)

        _command(pymux, state, "new-session -d -s work '%s'" % (WAITING,))

        _command(pymux, state, "switch-client -t work")
        assert state.session.name == "work"

        _command(pymux, state, "switch-client -l")
        assert state.session.name == "0"

        _command(pymux, state, "attach-session -t work")
        assert state.session.name == "work"


async def test_two_clients_watch_two_sessions_at_once():
    """
    The thing a server with one session could not do.

    Each client draws its own status line, and the name in it is the
    name of the session that client is on -- not the session of
    whichever client asked last. That is the render path the whole
    change turns on.
    """
    async with in_this_process(_server()) as session:
        pymux = session.pymux
        here, _ = await session.attach("here", SIZE)
        there, _ = await session.attach("there", SIZE)

        _command(pymux, here, "new-session -d -s work '%s'" % (WAITING,))
        _command(pymux, there, "switch-client -t work")

        assert here.session.name == "0"
        assert there.session.name == "work"

        # Drawn for one client while the other is the current one.
        with set_app(here.app):
            assert there.layout_manager._get_status_left_tokens() == "[work] "
            assert here.layout_manager._get_status_left_tokens() == "[0] "


async def test_list_sessions_names_every_session():
    async with in_this_process(_server()) as session:
        pymux = session.pymux
        state, _ = await session.attach("only", SIZE)

        _command(pymux, state, "new-session -d -s work '%s'" % (WAITING,))

        pymux.command_output = []
        _command(pymux, state, "list-sessions -F '#{session_id} #{session_name}'")
        assert pymux.command_output == ["$0 0", "$1 work"]
        pymux.command_output = None


async def test_has_session_answers_for_every_session():
    async with in_this_process(_server()) as session:
        pymux = session.pymux
        state, _ = await session.attach("only", SIZE)

        _command(pymux, state, "new-session -d -s work '%s'" % (WAITING,))

        state.message = None
        _command(pymux, state, "has-session -t work")
        assert state.message is None

        _command(pymux, state, "has-session -t play")
        assert "can't find session: play" in state.message


async def test_rename_session_leaves_the_other_alone():
    async with in_this_process(_server()) as session:
        pymux = session.pymux
        state, _ = await session.attach("only", SIZE)

        _command(pymux, state, "new-session -d -s work '%s'" % (WAITING,))
        _command(pymux, state, "rename-session -t work office")

        assert [s.name for s in pymux.sessions] == ["0", "office"]


async def test_kill_session_takes_its_clients_somewhere_else():
    async with in_this_process(_server()) as session:
        pymux = session.pymux
        state, _ = await session.attach("only", SIZE)

        _command(pymux, state, "new-session -s work '%s'" % (WAITING,))
        assert state.session.name == "work"

        _command(pymux, state, "kill-session -t work")

        assert [s.name for s in pymux.sessions] == ["0"]
        assert state.session.name == "0"


async def test_killing_a_session_moves_both_its_clients():
    """
    Two clients on one session, and one of them kills it.

    Each client is focused into its own application. They are focused
    from the client that asked, whose application is the current one,
    so a client focused under that one would be looking into another
    client's layout.
    """
    async with in_this_process(_server()) as session:
        pymux = session.pymux
        here, _ = await session.attach("here", SIZE)
        there, _ = await session.attach("there", SIZE)

        _command(pymux, here, "new-session -d -s work '%s'" % (WAITING,))
        _command(pymux, here, "switch-client -t work")
        _command(pymux, there, "switch-client -t work")

        _command(pymux, here, "kill-session -t work")

        assert [s.name for s in pymux.sessions] == ["0"]
        assert here.session.name == "0"
        assert there.session.name == "0"
        for state in (here, there):
            pane = state.session.arrangement.get_active_pane_for(state.app)
            assert state.app.layout.has_focus(pane.terminal)


async def test_a_command_from_a_pane_runs_in_that_pane_s_session():
    """
    The run-command packet carries the pane the words were typed in.
    Without reading it, the fake client of the command lands on the
    session a person used last, and `pymux new-window` typed in one
    session opens a window in another.
    """
    async with over_connection(_server()) as session:
        pymux = session.pymux
        state, _ = await session.attach("only", SIZE)

        _command(pymux, state, "new-session -d -s work '%s'" % (WAITING,))
        work = pymux.get_session("work")
        pane = work.arrangement.windows[0].panes[0]

        # The client is on "0", and it is the one a person used last.
        await session.command("new-window '%s'" % (WAITING,), pane.pane_id)

        await once(
            lambda: len(work.arrangement.windows) == 2,
            5.0,
            "the window did not open in the session the command came from",
        )
        assert len(pymux.sessions[0].arrangement.windows) == 1


async def test_the_session_environment_belongs_to_the_session():
    async with in_this_process(_server()) as session:
        pymux = session.pymux
        state, _ = await session.attach("only", SIZE)

        _command(pymux, state, "set-environment HERE first")
        _command(pymux, state, "new-session -s work '%s'" % (WAITING,))

        assert state.session.environment == {}
        assert pymux.sessions[0].environment == {"HERE": "first"}


async def test_a_target_reaches_a_window_of_another_session():
    "`-t session:window`, and the client goes where the window is."
    async with in_this_process(_server()) as session:
        pymux = session.pymux
        state, _ = await session.attach("only", SIZE)

        _command(pymux, state, "new-session -d -s work '%s'" % (WAITING,))
        _command(pymux, state, "select-window -t work:1")

        assert state.session.name == "work"


async def test_a_target_that_names_no_session_finds_nothing():
    """
    And does not quietly answer with a window of the session the
    client is on, which is what stripping the session part did.
    """
    async with in_this_process(_server()) as session:
        pymux = session.pymux
        state, _ = await session.attach("only", SIZE)

        _command(pymux, state, "select-window -t nowhere:1")

        assert "Can't find window: nowhere:1" in state.message


async def test_a_pane_id_reaches_across_the_sessions():
    "A pane id names one pane of the server, so it takes no session."
    async with in_this_process(_server()) as session:
        pymux = session.pymux
        state, _ = await session.attach("only", SIZE)

        _command(pymux, state, "new-session -d -s work '%s'" % (WAITING,))
        work = pymux.sessions[1]
        pane = work.arrangement.windows[0].panes[0]

        _command(pymux, state, "kill-pane -t %%%s" % (pane.pane_id,))

        assert work.arrangement.windows == []
        assert len(pymux.sessions[0].arrangement.windows) == 1


async def test_the_chooser_lists_every_session():
    async with in_this_process(_server()) as session:
        pymux = session.pymux
        state, _ = await session.attach("only", SIZE)

        _command(pymux, state, "new-session -d -s work '%s'" % (WAITING,))
        with set_app(state.app):
            state.layout_manager.display_chooser()
            labels = state.layout_manager.chooser_entries()

        assert any(label.startswith("0:1") for label in labels)
        assert any(label.startswith("work:1") for label in labels)


async def test_choosing_a_window_of_another_session_moves_the_client():
    async with in_this_process(_server()) as session:
        pymux = session.pymux
        state, _ = await session.attach("only", SIZE)

        _command(pymux, state, "new-session -d -s work '%s'" % (WAITING,))
        with set_app(state.app):
            state.layout_manager.display_chooser()
            # Pointing at it is the switch: the chooser has no picture
            # of a window, it moves the client onto it.
            state.layout_manager.point_at(1)
            assert state.session.name == "work"
            state.layout_manager.choose_pointed_window()

        assert state.session.name == "work"


async def test_a_session_that_empties_goes():
    """
    A pane that ends takes its window, and the last window takes the
    session. The server stops only when the last session goes.

    Over a connection, because this is the callback that runs when a
    process ends: an event loop has to turn for it to happen at all.
    """
    async with over_connection(_server()) as session:
        pymux = session.pymux
        state, _ = await session.attach("only", SIZE)

        _command(pymux, state, "new-session -d -s work '%s'" % (ENDING,))

        await once(
            lambda: [s.name for s in pymux.sessions] == ["0"],
            5.0,
            "the session did not go when its last pane ended",
        )

        # And the server is still there, because one session is.
        assert not pymux.done.is_set()


async def test_an_overlay_stays_in_its_own_session():
    """
    `display-popup` and the three lock commands open an overlay. It
    belonged to the server, so a popup opened in one session covered
    the screen of every client of the process. Lillecarl/pymux#324.
    """
    async with in_this_process(_server()) as session:
        pymux = session.pymux
        here, _ = await session.attach("here", SIZE)
        there, _ = await session.attach("there", SIZE)

        _command(pymux, here, "new-session -d -s work '%s'" % (WAITING,))
        _command(pymux, there, "switch-client -t work")

        _command(pymux, here, "display-popup '%s'" % (WAITING,))

        assert here.session.overlay_pane is not None
        assert there.session.overlay_pane is None

        # And only the client on that session is focused into it.
        popup = here.session.overlay_pane
        assert pymux._has_focus(here, popup) is True
        assert pymux._has_focus(there, popup) is False

        _command(pymux, here, "close-popup")
        assert here.session.overlay_pane is None


async def test_lock_server_covers_every_session():
    """
    An overlay is one session's, so "the server" means one on each.
    lock-session and lock-client cover the asking client's session
    alone. Lillecarl/pymux#324.
    """
    async with in_this_process(_server()) as session:
        pymux = session.pymux
        state, _ = await session.attach("only", SIZE)
        pymux.lock_command = WAITING

        _command(pymux, state, "new-session -d -s work '%s'" % (WAITING,))

        _command(pymux, state, "lock-session")
        assert pymux.sessions[0].overlay_pane is not None
        assert pymux.sessions[1].overlay_pane is None

        _command(pymux, state, "close-popup")

        _command(pymux, state, "lock-server")
        assert all(s.overlay_pane is not None for s in pymux.sessions)
