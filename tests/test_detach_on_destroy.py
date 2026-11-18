"""
`detach-on-destroy` says where a client goes when its session is
destroyed, with tmux's five answers (`server-fn.c`,
server_destroy_session). pymux's default is `off`, the session used
last; tmux's is `on`, which detaches. Lillecarl/pyterm#1035.
"""

from __future__ import annotations

from session import in_this_process
from test_sessions import SIZE, WAITING, _command, _server


class Connection:
    "What a client's connection does when it detaches, recorded."

    def __init__(self) -> None:
        self.closed = False

    def detach_and_close(self, hang_up: bool = False) -> None:
        self.closed = True


async def three_sessions(session, option: str):
    "Sessions 0, a and b; the client watches a, with `detach-on-destroy` set on it."
    pymux = session.pymux
    state, _ = await session.attach("only", SIZE)
    state.client.connection = Connection()
    await _command(pymux, state, "new-session -d -s b '%s'" % (WAITING,))
    await _command(pymux, state, "new-session -s a '%s'" % (WAITING,))
    await _command(pymux, state, "set detach-on-destroy %s" % (option,))
    assert state.session.name == "a"
    return pymux, state


async def test_off_moves_the_client_to_the_session_used_last():
    async with in_this_process(_server()) as session:
        pymux, state = await three_sessions(session, "off")
        await _command(pymux, state, "kill-session -t a")
        assert not state.client.connection.closed
        assert state.session.name in ("0", "b")


async def test_on_detaches_the_client():
    async with in_this_process(_server()) as session:
        pymux, state = await three_sessions(session, "on")
        await _command(pymux, state, "kill-session -t a")
        assert state.client.connection.closed


async def test_previous_and_next_go_by_name():
    async with in_this_process(_server()) as session:
        pymux, state = await three_sessions(session, "next")
        await _command(pymux, state, "kill-session -t a")
        assert state.session.name == "b"

    async with in_this_process(_server()) as session:
        pymux, state = await three_sessions(session, "previous")
        await _command(pymux, state, "kill-session -t a")
        assert state.session.name == "0"


async def test_no_detached_goes_to_a_session_nobody_watches():
    async with in_this_process(_server()) as session:
        pymux, state = await three_sessions(session, "no-detached")
        other, _ = await session.attach("other", SIZE)
        await _command(pymux, other, "switch-client -t 0")
        await _command(pymux, state, "kill-session -t a")
        assert state.session.name == "b"
        assert not state.client.connection.closed
