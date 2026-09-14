"""
`pymux attach -d` takes the session away from the other clients.

The flag rides on the `start-gui` packet, which is where the server
reads it. **The session, and not the server**: tmux's rule is one
line, and `detach-client -a` is the gesture for every client there is.
Lillecarl/pymux#344, Lillecarl/pymux#345.
"""

import contextvars
import json
import sys
from contextlib import asynccontextmanager

import anyio

from pymux.main import Pymux
from pymux.pipes.memory import connect_in_memory
from pymux.server import ServerConnection

#: A pane that stays. It has to outlive the test: the last window of a
#: session ending takes the session with it, and that detaches its
#: clients -- which is not what this file is about, and reads exactly
#: like the bug it judges.
WAITS = "%s -c 'import time; time.sleep(30)'" % (sys.executable,)


@asynccontextmanager
async def a_server():
    """
    A server that is stopped whatever the test does to it.

    Without the `finally` a failed assertion leaves `running()` waiting
    on connections nobody closed, and one red test hangs the suite
    rather than failing it.
    """
    pymux = Pymux()
    pymux.test_mode = True

    async with pymux.running():
        try:
            yield pymux
        finally:
            for connection in list(pymux.connections):
                connection.detach_and_close()
            pymux.stop()
            for window in list(pymux.arrangement.windows):
                for pane in list(window.panes):
                    if not pane.process.is_terminated:
                        pane.process.kill()


def _start_gui(detach_others: bool) -> str:
    return json.dumps(
        {
            "cmd": "start-gui",
            "detach-others": detach_others,
            "color-depth": None,
            "term": "xterm-256color",
            "colorterm": "",
            "hostname": "somewhere-else",
            "data": "",
        }
    )


async def _attach(pymux: Pymux, detach_others: bool = False) -> ServerConnection:
    "One client, the way both real transports make one."
    server_end, client_end = connect_in_memory()

    # A context of its own, which is what both real transports do.
    # Lillecarl/pymux#230.
    context = contextvars.copy_context()
    connection = context.run(lambda: ServerConnection(pymux, server_end))
    pymux.connections.append(connection)

    client_end.write_nowait(_start_gui(detach_others))

    with anyio.fail_after(5.0):
        while connection.client_state is None and not connection._closed:
            await anyio.sleep(0.005)

    return connection


def _running_a_command(pymux: Pymux) -> ServerConnection:
    """
    A connection that arrived to run a command and has not answered
    yet: no client state, and in `pymux.connections` all the same.
    """
    server_end, _client_end = connect_in_memory()
    connection = ServerConnection(pymux, server_end)
    pymux.connections.append(connection)
    return connection


async def test_the_client_that_asked_stays():
    """
    A connection is in the list from the moment the server accepts it,
    which is before its `start-gui` arrives. So a loop over the list
    reaches the client that asked, and `attach -d` used to put that
    person back at their shell.
    """
    async with a_server() as pymux:
        second = await _attach(pymux, detach_others=True)

        assert not second._closed, "attach -d detached the client that asked"
        assert second.client_state is not None


async def test_the_other_clients_go():
    async with a_server() as pymux:
        first = await _attach(pymux)
        assert not first._closed

        second = await _attach(pymux, detach_others=True)

        assert first._closed, "attach -d left the other client attached"
        assert not second._closed


async def test_without_the_flag_nobody_goes():
    async with a_server() as pymux:
        first = await _attach(pymux)
        second = await _attach(pymux)

        assert not first._closed
        assert not second._closed


async def test_it_reaches_this_session_and_no_other():
    """
    tmux detaches the clients of the session being attached to and
    nobody else: `if (c_loop->session != s || c == c_loop) continue;`
    (`cmd-attach-session.c:127`). `detach-client -a` is the gesture for
    the whole server. Lillecarl/pymux#345.

    **The rule, not the roles.** A client with no session named lands
    on the one a person looked at last, so which of these two is taken
    over is the server's to say; what this judges is that the ones on
    that session go and the ones elsewhere stay.
    """
    async with a_server() as pymux:
        one = await _attach(pymux)
        two = await _attach(pymux)

        # A window of its own, because a client draws a status line and
        # a session with no window has nothing to put in it.
        elsewhere = pymux.create_session()
        pymux.create_window(WAITS, session=elsewhere)
        pymux.attach_client_to(two.client_state, elsewhere)

        was_on = {one: one.client_state.session, two: two.client_state.session}
        assert was_on[one] is not was_on[two], "this needs two sessions"

        taking_over = await _attach(pymux, detach_others=True)
        taken = taking_over.client_state.session
        assert taken in was_on.values(), "nobody was on the session taken over"

        for connection, session in was_on.items():
            assert connection._closed is (session is taken), (
                "attach -d closed a client on another session"
                if session is not taken
                else "attach -d left a client on the session it took"
            )
        assert not taking_over._closed


async def test_a_command_that_is_running_is_not_a_client():
    """
    `pymux.connections` holds every connection and not every client. A
    connection that arrived to run a command was closed mid-answer, so
    somebody's `list-sessions` died and read as a server that went
    away. Lillecarl/pymux#345.
    """
    async with a_server() as pymux:
        asking = _running_a_command(pymux)

        taking_over = await _attach(pymux, detach_others=True)

        assert not asking._closed, "attach -d closed a connection running a command"
        assert not taking_over._closed
