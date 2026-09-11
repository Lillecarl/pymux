"""
A server with clients attached to it, the two ways a client arrives.

A measurement that drives pymux needs a server, a client and a way to
type at it. There are two ways a client arrives, and a driver should
not be able to tell them apart:

**in-process**: `Pymux.add_client`, with no socket, no
`ServerConnection` and no tasks. It covers the panes, the screens, the
windows and the layout, which is where the objects and the bytes are.

**connection**: a real `ServerConnection` over the queues of
`pipes.memory`, which is the transport of `pymux integrated`. Its
background tasks, its `_ClientInput` and the `ClientState` the server
makes for it are all the real ones, and a detach is what a person
walking away is -- the client end closes and the server tears its side
down. Lillecarl/pymux#226.

`what_leaks.py` drives both and asks what survived. `count_the_turns.py`
drives the connection route and counts what the event loop did.

`create_session` is the plain session on top of the in-process route:
one client, one window, the shape most tests want. `in_a_loop` runs a
coroutine test while pymux carries no anyio.
"""

import asyncio
import contextvars
import io
import json
import sys
import time
import weakref
from contextlib import asynccontextmanager, contextmanager
from functools import wraps
from typing import Any, Callable, Dict, NamedTuple

from prompt_toolkit.application.current import set_app
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import ColorDepth
from prompt_toolkit.output.vt100 import Vt100_Output

from pymux.keys import KittyVt100Parser
from pymux.main import Pymux
from pymux.pipes.memory import connect_in_memory
from pymux.server import ServerConnection


class Connection:
    """
    What `Pymux` asks a connection for, and nothing else.

    A key press invalidates, and an invalidate tells every connection
    about the pointer and the keyboard, so a stub that a test feeds
    keys through needs both methods here; the tests that press no key
    never call them. This is the one such stub, and every in-process
    session in the suite builds one.
    """

    kitty_source_flags = 0
    pointer_shape = None
    graphics = None

    def set_pointer_shape(self, shape):
        pass

    def _send_packet(self, packet):
        pass


#: The size a fake CLI reports. Nothing draws in it; a window that
#: sizes itself by the latest client must never read it.
DEFAULT_SIZE = Size(rows=24, columns=80)

#: A command whose pane ends at once and holds a real screen while it
#: lives. The window the plain sessions make runs it.
NOTHING = "%s -c pass" % (sys.executable,)


class _Sink(io.TextIOBase):
    """
    Somewhere for a client's output to go.

    A real client writes to a socket and forgets. An `io.StringIO`
    would keep every byte the renderer ever wrote, and a leak check
    would read it as the largest leak in the run.
    """

    def write(self, data: str) -> int:
        return len(data)

    def flush(self) -> None:
        pass

    def isatty(self) -> bool:
        return True


class Session(NamedTuple):
    """
    What a driver drives: a server, a way in, a way out, and what it
    made.

    There are two of these, one per route, and a driver cannot tell
    them apart. Everything a driver does is the same either way, which
    is the point: the difference between the routes is the transport
    and nothing else. A fault that only one route has shows up as the
    same work passing on one and failing on the other.
    """

    #: The server.
    pymux: Any

    #: `await attach(name, size)` -> the client state and its size.
    attach: Callable

    #: `await detach(state)`: what a person walking away does.
    detach: Callable

    #: `typed(state, text)`: what a person at that client's keyboard
    #: types. The connection route writes the packet a real client
    #: writes; the in-process route has no packets, so it feeds the
    #: pipe input that the client's application reads.
    typed: Callable

    #: `create_command(text)`: a command over a connection of its own, the
    #: way a pane's CLI sends one. The server runs it under the fake
    #: CLI it makes for such a command, and what that connection
    #: received comes back: the packets of the answer, and nothing a
    #: browser was meant to read.
    command: Callable

    #: `watch(name, obj)` -> obj, remembered by a weak reference.
    watch: Callable

    #: Every watched object, by name.
    watched: Dict[str, "weakref.ref | None"]


def _watcher(watched: dict) -> Callable:
    "A `watch` that remembers into this dictionary."

    def watch(name, obj):
        try:
            watched[name] = weakref.ref(obj)
        except TypeError:
            # Not every object takes a weak reference. One that does
            # not cannot be watched, and saying so is better than
            # pretending it passed.
            watched[name] = None
        return obj

    return watch


async def once(question, seconds: float, complaint: str):
    "Wait for something the loop has to do first, or say it never did."
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        answer = question()
        if answer:
            return answer
        await asyncio.sleep(0.005)
    raise SystemExit(complaint)


def in_a_loop(test):
    """
    Run this test in an event loop of its own.

    pymux does not carry anyio and does not turn on `anyio_mode`, so
    pytest here answers a coroutine test with "async def functions are
    not natively supported". Lillecarl/pymux#87 is the move that would
    make this decorator go away.
    """

    @wraps(test)
    def run(*arguments, **named):
        asyncio.run(test(*arguments, **named))

    return run


@contextmanager
def in_this_process(pymux=None):
    """
    A session whose clients are attached with `Pymux.add_client`.

    No socket, no `ServerConnection`, no tasks: this is the route that
    a test takes and the one the server takes for nobody. It covers the
    panes, the screens, the windows and the layout, which is where the
    objects and the bytes are.
    """
    pymux = pymux if pymux is not None else Pymux()
    # The clock a screen shows is pinned, so no test races the minute
    # it runs in. This is what test-mode is for. A Pymux a caller
    # brought is the caller's to pin.
    pymux.test_mode = True
    watched: dict[str, "weakref.ref | None"] = {}
    watch = _watcher(watched)

    with create_pipe_input() as pipe:
        # The parser the server puts on a client's input. It is the one
        # that reads the key encoding of the kitty keyboard protocol,
        # so a test that feeds bytes has to have it, and a driver
        # cannot tell the routes apart without it. See
        # `pymux.server._ClientInput`.
        pipe.vt100_parser = KittyVt100Parser(pipe._buffer.append)

        async def attach(name, size):
            output = Vt100_Output(stdout=_Sink(), get_size=lambda: size)
            state = pymux.add_client(
                output=output,
                input=pipe,
                color_depth=ColorDepth.DEPTH_8_BIT,
                connection=Connection(),
            )
            watch("%s client" % name, state)
            watch("%s application" % name, state.app)
            watch("%s layout manager" % name, state.layout_manager)
            return state, size

        async def detach(state):
            pymux.remove_client(state.connection)

        def typed(state, text):
            state.app.input.send_text(text)

        def create_command(text):
            """
            A command under the fake CLI of a socket, the way the
            server runs one that arrived from a pane. The client state
            it made comes back, so a test can ask what the command did
            to it before it is taken away.
            """
            output = Vt100_Output(stdout=_Sink(), get_size=lambda: DEFAULT_SIZE)
            state = pymux.add_client(
                output=output,
                input=pipe,
                color_depth=ColorDepth.DEPTH_8_BIT,
                connection=Connection(),
                temporary=True,
            )
            watch("command client", state)
            try:
                with set_app(state.app):
                    pymux.handle_command(text)
            finally:
                pymux.remove_client(state.connection)
            return state

        try:
            yield Session(pymux, attach, detach, typed, create_command, watch, watched)
        finally:
            pymux.stop()


@asynccontextmanager
async def create_session(pymux=None, window=NOTHING):
    """
    A server with one client and one window, the plain session.

    The window runs `NOTHING` before the client arrives, so the client
    attaches to it the way a person's terminal attaches to a running
    one. `window=None` skips it, for a test that counts windows
    itself. It yields the server and the client state; a driver that
    wants to type, run a command or hold two clients uses
    `in_this_process` instead.
    """
    with in_this_process(pymux) as session:
        if window is not None:
            session.pymux.create_window(window)
        state, _size = await session.attach("the client", DEFAULT_SIZE)
        yield session.pymux, state


@contextmanager
def over_a_connection(pymux=None, read_a_packet=None):
    """
    A session whose clients attach the way a real one does.

    The transport is `pipes.memory`, which is what `pymux integrated`
    uses: two queues rather than a unix socket. Everything else of the
    socket route is the real thing -- the `ServerConnection`, its
    background tasks, its `_ClientInput` and the `ClientState` the
    server makes for it -- and none of an operating system's is, so
    this runs in a build sandbox. Lillecarl/pymux#226.

    Three things live here that the other route never makes, and each
    one could keep a client alive after it detached:

    - `ServerConnection._tasks`, the background coroutines. A task that
      never finishes keeps its connection.
    - `Pymux._client_states` and `Pymux.connections`, both keyed by or
      holding the connection.
    - The `_ClientInput` pipe input, whose parser holds callbacks back
      into the connection.

    A detach here is what a person walking away is: the client end
    closes, the server's read of it ends, and the server tears its side
    down. Nothing calls `remove_client` by hand.

    **Somebody has to take the packets off the queue.** The queue of a
    memory connection has no limit, so a client that never reads is a
    growing list of every frame the server ever drew. `read_a_packet`
    is called with each one, from the task that takes it; nobody has to
    draw them, and a caller that wants to know when a frame arrived
    reads the moment there. It defaults to dropping them.
    """
    pymux = pymux if pymux is not None else Pymux()
    # The clock a screen shows is pinned, so no test races the minute
    # it runs in. This is what test-mode is for. A Pymux a caller
    # brought is the caller's to pin.
    pymux.test_mode = True
    watched: dict[str, "weakref.ref | None"] = {}
    watch = _watcher(watched)

    #: The client half of each attached client, by the id of its state:
    #: the end of the connection it reads, and the task that drains it.
    ends: dict = {}

    #: The same, for the connections of commands: each one closes
    #: itself when its command answers, and its drain waits on a queue
    #: that will never fill again.
    command_ends: list = []

    async def drain(end) -> None:
        while True:
            try:
                packet = await end.read()
            except Exception:
                return
            if read_a_packet is not None:
                read_a_packet(packet)

    async def attach(name, size):
        server_end, client_end = connect_in_memory()

        # A context of its own, which is what both real routes do:
        # `connection_cb` for the socket and `run_integrated` for the
        # queues. A `prompt_toolkit.Application` becomes active in it.
        context = contextvars.copy_context()
        connection = context.run(lambda: ServerConnection(pymux, server_end))

        # The transport holds the connection, and the transport is
        # this function. Both real routes do this line.
        pymux.connections.append(connection)

        watch("%s connection" % name, connection)
        watch("%s connection input" % name, connection._pipeinput)

        draining = asyncio.create_task(drain(client_end))

        # What `client/terminal.py` sends when it attaches, in the
        # order it sends it.
        client_end.write_nowait(
            json.dumps({"cmd": "size", "data": [size.rows, size.columns]})
        )
        client_end.write_nowait(
            json.dumps(
                {
                    "cmd": "start-gui",
                    "detach-others": False,
                    "color-depth": ColorDepth.DEPTH_8_BIT,
                    "term": "xterm-256color",
                    "colorterm": "",
                    "data": "",
                }
            )
        )

        state = await once(
            lambda: connection.client_state,
            5.0,
            "the server never made a client for this connection",
        )
        ends[id(state)] = (client_end, draining)

        watch("%s client" % name, state)
        watch("%s application" % name, state.app)
        watch("%s layout manager" % name, state.layout_manager)
        return state, size

    async def detach(state):
        """
        Close the client end, and wait for the server to finish.

        The read loop of the connection wakes with a broken pipe, which
        is what `detach_and_close` is for. Waiting for it is the same
        rule a pane's teardown holds: the freeing happens on the loop,
        and a check that looked before it finished would call every
        connection a leak.
        """
        connection = state.connection
        client_end, draining = ends.pop(id(state))

        client_end.close()
        await once(
            lambda: connection._closed,
            5.0,
            "the connection never closed after its client went",
        )

        # **And then wait for the work it started.** `_close_connection`
        # asks the application to exit and cancels the tasks of the
        # connection, and both of those are requests: the application
        # stops on a later turn of the loop, not on this one. Until it
        # has, it is still the current application of its context, and
        # it holds the layout, which holds every pane's widget. A check
        # that looked here would report the whole session as a leak.
        await once(
            lambda: not connection._tasks,
            5.0,
            "the connection still had background work after it closed",
        )

        # Nothing puts an end marker on the queue this reads, so the
        # drain is waiting on a queue that will never fill again.
        draining.cancel()

    def typed(state, text):
        client_end, _draining = ends[id(state)]
        client_end.write_nowait(json.dumps({"cmd": "in", "data": text}))

    async def create_command(text, pane_id=None):
        """
        A command over a connection of its own, the way a pane's CLI
        sends one. The packets that connection's client end received
        come back, so a test can say that nothing meant for a browser
        went there.
        """
        server_end, client_end = connect_in_memory()

        # A context of its own, for the reason `attach` gives.
        context = contextvars.copy_context()
        connection = context.run(lambda: ServerConnection(pymux, server_end))
        pymux.connections.append(connection)
        watch("command connection", connection)

        got: list = []

        async def drain_command() -> None:
            while True:
                try:
                    packet = await client_end.read()
                except Exception:
                    return
                got.append(packet)

        draining = asyncio.create_task(drain_command())
        command_ends.append((client_end, draining))

        client_end.write_nowait(
            json.dumps({"cmd": "run-command", "data": text, "pane_id": pane_id})
        )
        return got

    try:
        yield Session(pymux, attach, detach, typed, create_command, watch, watched)
    finally:
        for client_end, draining in ends.values():
            client_end.close()
            draining.cancel()
        for client_end, draining in command_ends:
            client_end.close()
            draining.cancel()
        ends.clear()
        command_ends.clear()
        pymux.stop()


#: The routes a client can arrive over, by the name a knob takes.
ROUTES = {
    "in-process": in_this_process,
    "connection": over_a_connection,
}


def routes(chosen: str, knob: str) -> list:
    "The routes a run covers, by name. Empty means all of them."
    if not chosen:
        return list(ROUTES.items())
    if chosen not in ROUTES:
        raise SystemExit(
            "%s is one of %s, not %r" % (knob, ", ".join(sorted(ROUTES)), chosen)
        )
    return [(chosen, ROUTES[chosen])]
