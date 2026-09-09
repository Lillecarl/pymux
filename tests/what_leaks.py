"""
What is still alive after a pane, a window or a client has gone.

Every other check here asks whether pymux draws the right cells. None
of them asks what pymux still holds afterwards, and a multiplexer is a
program a person leaves running for weeks: a pane's worth of objects
kept on every `kill-pane` is a leak nobody sees until the machine
swaps.

## Two questions, and the first one is exact

**Is it dead?** A `weakref` to every object a round makes -- the pane,
the widget, the screen, the process, the window, the client, its
application -- and after the round is torn down and `gc.collect()` has
run, every one of them has to be gone. That is not a measurement with
a tolerance. It is true or it is a leak, and `what_holds_it.py` names
what holds a survivor so that a red run points at a line rather than
at a program.

**Does it plateau?** Some leaks keep nothing dead: a list that grows a
row per write holds only live objects and still eats the machine. So
the same work runs twice and the count of tracked objects has to come
out the same. A count that grows by the same amount each time is a leak
whose size this says directly.

## The workload

Alacritty's recordings, which are the raw output of real programs --
vim, tmux, htop, fish, vttest -- and the largest honest workload in
this repository. `PTTERM_INSTRUCTIONS` already points at them for
`checks.ptterm-instructions`; here `PYMUX_LEAKS_RECORDINGS` does.

**A leak shows at any volume**, because the question is whether an
object died and not how many bytes it saw. So the gate feeds a little
-- 128 kB to each of eight panes, two rounds a side -- and the knobs
feed a lot.

A round does what a person does, and each part of it is a path that
allocates:

- open panes, split sideways and stacked, and turn one window into a
  strip;
- feed every pane the recordings, through the real `Stream` and
  `Screen` that a pty would feed;
- draw frames, which builds the rows and the cells;
- resize, which reflows the whole history of every pane;
- scroll the strip, which moves a view;
- then kill the panes, close the windows and detach the clients, and
  **wait for the loop to finish the teardown**. That last part is not
  optional, and `settle` says why at length.

## What is not measured, and why

**The `Char` cache is not a leak.** prompt_toolkit interns every cell
in a module-level `FastDictCache` of a million entries. Diverse bytes
saturate it and it never shrinks. So a warm-up round runs before the
first count, and `Char` is named in `NOT_A_LEAK` below.

**A client's output is a sink.** A real client writes to a socket. A
test that gave it an `io.StringIO` would grow by every byte the
renderer ever wrote, and call it a leak.

## Two routes, because a client can arrive two ways

**in-process**: `Pymux.add_client`, with no socket, no
`ServerConnection` and no tasks. It covers the panes, the screens, the
windows and the layout, which is where the objects and the bytes are.

**connection**: a real `ServerConnection` over the queues of
`pipes.memory`, which is the transport of `pymux integrated`. Its
background tasks, its `_ClientInput` and the `ClientState` the server
makes for it are all the real ones, and a detach is what a person
walking away is -- the client end closes and the server tears its side
down. This is the route where the one leak this repository has already
had lived: `test_server_tasks.py` exists because a `ServerConnection`
task was collected while still pending. Lillecarl/pymux#226.

A round cannot tell them apart, so a leak that only one route has
shows up as the same round passing on one and failing on the other.

## What it answers with

One survivor is a failure, and so is a type that grew by more than the
tolerance. **There is no budget file**, because there is no legitimate
growth: the two sides are the same work, so anything the second kept
the first kept too, and a healthy type is zero on both.

## The knobs

    nix build --file . checks.pymux-leaks.run   # and read result/log

    PYMUX_LEAKS_BYTES=1000000 \
      PYMUX_LEAKS_PANES=16 PYMUX_LEAKS_ROUNDS=8 \
      nix build --file . checks.pymux-leaks     # about 350 MB

    PYMUX_LEAKS_INCLUDE=vim     # one recording
    PYMUX_LEAKS_TOLERANCE=0     # no noise allowed
    PYMUX_LEAKS_TRACE=1         # count bytes as well, with tracemalloc
    PYMUX_LEAKS_ROUTE=connection  # one route, not both
"""

import asyncio
import contextvars
import gc
import io
import json
import os
import sys
import time
import tracemalloc
import weakref
from collections import Counter
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Dict, NamedTuple

sys.path.insert(0, str(Path(__file__).parent))
# And pymux itself, which the sandbox copies beside `tests` rather than
# installing. A script's own directory is what Python puts on the path,
# not the directory above it.
sys.path.insert(1, str(Path(__file__).parent.parent))

from prompt_toolkit.application.current import set_app  # noqa: E402
from prompt_toolkit.data_structures import Size  # noqa: E402
from prompt_toolkit.input import create_pipe_input  # noqa: E402
from prompt_toolkit.layout.mouse_handlers import MouseHandlers  # noqa: E402
from prompt_toolkit.layout.screen import Screen, WritePosition  # noqa: E402
from prompt_toolkit.output import ColorDepth  # noqa: E402
from prompt_toolkit.output.vt100 import Vt100_Output  # noqa: E402
from what_holds_it import why_it_is_alive  # noqa: E402

from pymux.main import Pymux  # noqa: E402
from pymux.pipes.memory import connect_in_memory  # noqa: E402
from pymux.server import ServerConnection  # noqa: E402

#: How much a type may grow over one side before the check fails, as a
#: number of objects.
#:
#: **There is no budget file, because there is no legitimate growth.**
#: A healthy type comes out at zero: the same work ran twice, so
#: anything the second pass kept, the first pass kept too. This number
#: is for noise -- a cache that had not quite finished filling, a
#: counter this script made itself -- and not an allowance. A real
#: leak grows by a pane's worth of objects per pane, which is
#: thousands.
DEFAULT_TOLERANCE = 64

#: The client this drives. Two of them, of different sizes, because a
#: view is per client and a client that sees part of the plane runs
#: different code (slice 5 of Lillecarl/pymux#217).
BIG = Size(rows=24, columns=100)
SMALL = Size(rows=14, columns=48)

#: How many panes a round opens.
PANES = int(os.environ.get("PYMUX_LEAKS_PANES") or 8)

#: How many rounds each of the two counts covers. The work runs three
#: times this: once to warm up, and twice to be compared.
ROUNDS = int(os.environ.get("PYMUX_LEAKS_ROUNDS") or 2)

#: Which recordings to feed. Empty means all of them.
INCLUDE = os.environ.get("PYMUX_LEAKS_INCLUDE", "")

#: How many bytes of recording each pane gets, per round.
#:
#: **A leak shows at any volume**, because the question is whether an
#: object died and not how many bytes it saw. So the gate feeds a
#: little and the knob feeds a lot: the whole set is about a megabyte
#: a pane, and eight panes of it three times over is slow enough that
#: nobody would run `checks.all`.
BYTES = int(os.environ.get("PYMUX_LEAKS_BYTES") or 128 * 1024)

#: Whether to count the bytes Python allocated, as well as the objects.
#:
#: Off, because `tracemalloc` hooks every allocation: it doubles the
#: run and the number it reports moves with the caches and the
#: interpreter's own arenas, so nothing judges it. It is here for a
#: person who wants to see it.
TRACE = bool(os.environ.get("PYMUX_LEAKS_TRACE"))

#: Where the recordings are.
RECORDINGS = os.environ.get("PYMUX_LEAKS_RECORDINGS", "")


#: Types that grow for a reason and are not a leak.
#:
#: `Char` is interned in a module-level cache of a million entries
#: (`prompt_toolkit.layout.screen._CHAR_CACHE`). Diverse bytes fill it
#: and it never shrinks, so it grows for as long as the workload finds
#: new cells and then stops. The warm-up round does most of it; naming
#: it here covers the rest.
#:
#: `_Char` and `FastDictCache` entries are the same cache from the
#: other side.
NOT_A_LEAK = frozenset(["Char", "_Char"])

#: How many types the log names. The ones that did not grow are the
#: answer, and printing four hundred of them hides it.
WORST = 25


class _Connection:
    "What `Pymux` asks a connection for, and nothing else."

    kitty_source_flags = 0
    pointer_shape = None
    graphics = None

    def set_pointer_shape(self, shape):
        pass

    def _send_packet(self, packet):
        pass


class _Sink(io.TextIOBase):
    """
    Somewhere for a client's output to go.

    A real client writes to a socket and forgets. An `io.StringIO`
    would keep every byte the renderer ever wrote and read as the
    largest leak in the run.
    """

    def write(self, data: str) -> int:
        return len(data)

    def flush(self) -> None:
        pass

    def isatty(self) -> bool:
        return True


def the_recordings() -> list[tuple[str, str]]:
    """
    Every recording, by name, as text.

    They are read once and held for the whole run, so the bytes are not
    part of what grows. A recording is what a program wrote, so it is
    decoded the way a pty's reader decodes it.
    """
    if not RECORDINGS:
        raise SystemExit(
            "PYMUX_LEAKS_RECORDINGS names the directory of recordings. "
            "`nix build --file . checks.pymux-leaks` sets it."
        )

    root = Path(RECORDINGS)
    found = []

    for directory in sorted(root.iterdir()):
        recording = directory / "alacritty.recording"
        if not recording.is_file():
            continue
        if INCLUDE and INCLUDE not in directory.name:
            continue
        found.append(
            (directory.name, recording.read_bytes().decode("utf-8", "replace"))
        )

    if not found:
        raise SystemExit("No recording of %r under %s." % (INCLUDE, root))

    return found


class Session(NamedTuple):
    """
    What a round drives: a server, a way in, a way out, and what it
    made.

    There are two of these, one per route, and a round cannot tell them
    apart. `in_this_process` calls `Pymux.add_client` directly;
    `over_a_connection` puts a real `ServerConnection` between the two
    halves. Everything else a round does is the same either way, which
    is the point: the difference between the routes is the transport
    and nothing else. Lillecarl/pymux#226.
    """

    #: The server.
    pymux: Any

    #: `await attach(name, size)` -> the client state and its size.
    attach: Callable

    #: `await detach(state)`: what a person walking away does.
    detach: Callable

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


@contextmanager
def in_this_process():
    """
    A session whose clients are attached with `Pymux.add_client`.

    No socket, no `ServerConnection`, no tasks: this is the route that
    a test takes and the one the server takes for nobody. It covers the
    panes, the screens, the windows and the layout, which is where the
    objects and the bytes are.
    """
    pymux = Pymux()
    watched: dict[str, "weakref.ref | None"] = {}
    watch = _watcher(watched)

    with create_pipe_input() as pipe:

        async def attach(name, size):
            output = Vt100_Output(stdout=_Sink(), get_size=lambda: size)
            state = pymux.add_client(
                output=output,
                input=pipe,
                color_depth=ColorDepth.DEPTH_8_BIT,
                connection=_Connection(),
            )
            watch("%s client" % name, state)
            watch("%s application" % name, state.app)
            watch("%s layout manager" % name, state.layout_manager)
            return state, size

        async def detach(state):
            pymux.remove_client(state.connection)

        try:
            yield Session(pymux, attach, detach, watch, watched)
        finally:
            pymux.stop()


async def _drain(end) -> None:
    """
    Read what the server writes to this client, and forget it.

    A real client draws the packets it is sent. Nobody has to read them
    here, but somebody has to take them off the queue: the queue of a
    memory connection has no limit, so a client that never reads is a
    growing list of every frame the server ever drew, and this check
    would call it the largest leak in the run.
    """
    while True:
        try:
            await end.read()
        except Exception:
            return


async def _once(question, seconds: float, complaint: str):
    "Wait for something the loop has to do first, or say it never did."
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        answer = question()
        if answer:
            return answer
        await asyncio.sleep(0.005)
    raise SystemExit(complaint)


@contextmanager
def over_a_connection():
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
    """
    pymux = Pymux()
    watched: dict[str, "weakref.ref | None"] = {}
    watch = _watcher(watched)

    #: The client half of each attached client, by the id of its state:
    #: the end of the connection it reads, and the task that drains it.
    ends: dict = {}

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

        drain = asyncio.create_task(_drain(client_end))

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

        state = await _once(
            lambda: connection.client_state,
            5.0,
            "the server never made a client for this connection",
        )
        ends[id(state)] = (client_end, drain)

        watch("%s client" % name, state)
        watch("%s application" % name, state.app)
        watch("%s layout manager" % name, state.layout_manager)
        return state, size

    async def detach(state):
        """
        Close the client end, and wait for the server to finish.

        The read loop of the connection wakes with a broken pipe, which
        is what `detach_and_close` is for. Waiting for it is the same
        rule `settle` holds for a pane: the freeing happens on the
        loop, and a check that looked before it finished would call
        every connection a leak.
        """
        connection = state.connection
        client_end, drain = ends.pop(id(state))

        client_end.close()
        await _once(
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
        # Same rule as `settle`, one layer up.
        await _once(
            lambda: not connection._tasks,
            5.0,
            "the connection still had background work after it closed",
        )

        # Nothing puts an end marker on the queue this reads, so the
        # drain is waiting on a queue that will never fill again.
        drain.cancel()

    try:
        yield Session(pymux, attach, detach, watch, watched)
    finally:
        for client_end, drain in ends.values():
            client_end.close()
            drain.cancel()
        ends.clear()
        pymux.stop()


#: The routes a round can attach over, by the name the knob takes.
ROUTES = {
    "in-process": in_this_process,
    "connection": over_a_connection,
}

#: Which of them to run. Empty means all of them.
ROUTE = os.environ.get("PYMUX_LEAKS_ROUTE", "")


def draw(pymux, state, size) -> None:
    "One frame of one client, the way its renderer draws one."
    with set_app(state.app):
        state.layout_manager.before_a_frame()
        screen = Screen()
        state.app.layout.container.write_to_screen(
            screen,
            MouseHandlers(),
            WritePosition(xpos=0, ypos=0, width=size.columns, height=size.rows),
            "",
            False,
            None,
        )
        screen.draw_all_floats()
        state.app.renderer._last_screen = screen


def feed(pane, recordings) -> None:
    """
    Give a pane what a program wrote, through the chain a pty feeds.

    `Process` calls `Stream.feed` with whatever the pty handed over, so
    this is the same call with no pty in front of it. The screen, the
    parser, the history and the pruning are all the real ones.

    It stops at `BYTES`, and it stops between two recordings rather
    than inside one: half a recording ends mid-sequence, and a parser
    left waiting for the rest of one is a different workload.
    """
    stream = pane.terminal.terminal_control.stream
    written = 0

    for _name, data in recordings:
        stream.feed(data)
        written += len(data)
        if written >= BYTES:
            return


async def settle(panes, seconds: float = 10.0) -> bool:
    """
    Give the loop the turns a teardown needs.

    **Killing a pane is a signal, and the freeing happens afterwards,
    on the loop.** The child is reaped in an executor; the reap closes
    the slave side of the pty; the master side then reads the end of
    the file; and `Process._read` closes the backend, which is what
    takes the reader off the loop. Nothing of the pane is freed until
    that last step, because the loop's selector holds the callback
    that holds the backend that holds the process that holds the pane.

    **A check that killed and looked straight away would call every
    pane a leak.** The first version of this did, and the chain it
    printed is the reason this function is here:

        process 0: held by method Process._read <- PosixBackend
        <- function PosixBackend.connect_reader.<locals>.ready
        <- Handle <- SelectorKey <- dict of 4[19]

    That dictionary is the selector's, keyed by file descriptor. It is
    the honest answer to "what holds this", and the honest reading of
    it is "the teardown has not finished", not "pymux leaks".

    False when a pane never reported itself terminated. That is worth
    saying rather than hiding: a check that timed out has measured
    nothing.
    """
    deadline = time.monotonic() + seconds

    while time.monotonic() < deadline:
        if all(pane.process.is_terminated for pane in panes):
            return True
        await asyncio.sleep(0.01)

    return False


async def a_round(session: Session, recordings) -> bool:
    """
    One round: open, feed, draw, resize, scroll, then tear it all down.

    Each part is a path that allocates, and the teardown is what the
    weak references are about.

    **It cannot tell which route it is on.** The session says how a
    client arrives and how it leaves, and everything between is the
    same work, so a leak that only one route has shows up as the same
    round passing on one and failing on the other.
    """
    pymux, watch = session.pymux, session.watch

    big, big_size = await session.attach("big", BIG)
    small, small_size = await session.attach("small", SMALL)

    with set_app(big.app):
        # A second window, so that closing one is exercised as well as
        # closing the last.
        pymux.handle_command("new-window")
        pymux.handle_command("set-option pane-border-status on")

        window = pymux.arrangement.get_active_window()
        watch("window", window)

        for number in range(PANES - 1):
            pymux.handle_command(
                "split-window -h" if number % 2 == 0 else "split-window -v"
            )

        panes = list(window.panes)
        for place, pane in enumerate(panes):
            watch("pane %d" % place, pane)
            watch("terminal %d" % place, pane.terminal)
            watch("screen %d" % place, pane.screen)
            watch("process %d" % place, pane.process)

        for pane in panes:
            feed(pane, recordings)

    draw(pymux, big, big_size)
    draw(pymux, small, small_size)

    with set_app(big.app):
        # A strip, which is the layout with a view that moves, and the
        # widths that a person cycles through.
        pymux.handle_command("set-window-option strip on")
        pymux.handle_command("switch-column-width")
        for _step in range(3):
            pymux.handle_command("select-pane -R")
            big.sync_focus()
            draw(pymux, big, big_size)

        # A resize reflows the whole history of every pane.
        pymux.handle_command("resize-window -x 60 -y 20")
        draw(pymux, big, big_size)
        draw(pymux, small, small_size)

        pymux.handle_command("set-window-option strip off")
        draw(pymux, big, big_size)

        # And the teardown.
        killed = list(panes)
        for other in list(pymux.arrangement.windows):
            for pane in list(other.panes):
                if pane not in killed:
                    killed.append(pane)
        for pane in killed:
            pymux.kill_pane(pane)

    for state in (big, small):
        await session.detach(state)

    return await settle(killed)


def counted() -> Counter:
    """
    How many tracked objects there are, by type.

    `gc.get_objects` misses anything without a `__dict__` that the
    collector does not track, which is most immutable scalars. What it
    does see is every container and every instance, which is what a
    leak is made of.
    """
    for _ in range(3):
        gc.collect()

    return Counter(type(one).__qualname__ for one in gc.get_objects())


def survivors(watched: dict) -> list[str]:
    """
    One line for each object that should be gone and is not.

    **Every survivor is passed as something to ignore.** A pane, its
    widget, its control, its process and its screen all point at each
    other, so a chain that may name them goes round that ring and
    never reaches what holds it. Ignoring them makes the walk step
    outside, which is where the answer is.
    """
    for _ in range(3):
        gc.collect()

    alive = []
    for name, ref in sorted(watched.items()):
        if ref is None:
            alive.append((name, None))
            continue
        obj = ref()
        if obj is not None:
            alive.append((name, obj))

    ring = [obj for _name, obj in alive if obj is not None]
    found = []

    for name, obj in alive:
        if obj is None:
            found.append("%s: cannot be watched, so nothing is known" % (name,))
            continue
        found.append(
            why_it_is_alive(name, obj, ignore=[watched, alive, ring, found] + ring)
        )

    del alive, ring
    return found


async def run(rounds: int, recordings) -> list[str]:
    """
    That many rounds, and whatever they left alive.

    **The questions are asked while the session is still standing.**
    This used to ask after the `with` block, when the `Pymux` of the
    round had gone as well, and a survivor that only the session held
    died with the session and passed. That is the leak that matters
    most, because a server outlives every pane in it: a pane left in
    `Arrangement.windows`, a client left in `Pymux._client_states` or
    a connection left in `Pymux.connections` is kept for as long as
    the person is logged in.

    **Every route runs in every round**, rather than a run of each.
    The two sides compare the same work, so interleaving them costs one
    warm-up instead of two, and a survivor says which route it came
    from.
    """
    alive = []

    for _round in range(rounds):
        for name, a_session in the_routes():
            with a_session() as session:
                if not await a_round(session, recordings):
                    alive.append(
                        "a pane never reported itself terminated, so this "
                        "round measured nothing"
                    )
                alive.extend(
                    "%s: %s" % (name, line) for line in survivors(session.watched)
                )

    return alive


def the_routes() -> list[tuple[str, Callable]]:
    "The routes this run covers, by name."
    if not ROUTE:
        return list(ROUTES.items())
    if ROUTE not in ROUTES:
        raise SystemExit(
            "PYMUX_LEAKS_ROUTE is one of %s, not %r"
            % (", ".join(sorted(ROUTES)), ROUTE)
        )
    return [(ROUTE, ROUTES[ROUTE])]


async def main() -> int:
    recordings = the_recordings()
    total = sum(len(data) for _name, data in recordings)
    each = min(total, BYTES)

    print(
        "%d recordings, %s of them, %s to each of %d panes, %d rounds each side."
        % (len(recordings), _bytes(total), _bytes(each), PANES, ROUNDS)
    )
    print("One side feeds %s." % (_bytes(each * PANES * ROUNDS),))

    if TRACE:
        tracemalloc.start()

    # Warm up: fill the caches that fill once, so that what they hold
    # is not counted as growth.
    print("\nwarming up...")
    await run(1, recordings)

    before = counted()
    first_bytes = _traced()

    print("first side...")
    alive = await run(ROUNDS, recordings)
    between = counted()

    print("second side...")
    alive += await run(ROUNDS, recordings)
    after = counted()

    last_bytes = _traced()
    if TRACE:
        tracemalloc.stop()

    # The growth of one round, from the side that had a warm cache
    # behind it. Both sides are compared, and the second is the one a
    # budget holds: a type that grew on the first side and not on the
    # second was still warming up.
    first = _grew(before, between)
    second = _grew(between, after)

    return report(alive, first, second, last_bytes - first_bytes)


def _grew(before: Counter, after: Counter) -> Counter:
    """
    What one side of the run kept, by type.

    A side is `ROUNDS` rounds, and the two sides are the same work, so
    a healthy type comes out at zero on both. The number is not divided
    by the rounds: a leak of one object per round and a leak of a
    thousand read differently, and a floor division turns the first
    into nothing.
    """
    growth = Counter()
    for name in set(before) | set(after):
        if name in NOT_A_LEAK:
            continue
        moved = after[name] - before[name]
        if moved > 0:
            growth[name] = moved
    return growth


def _traced() -> int:
    "How many bytes Python has allocated, or zero when nobody asked."
    return tracemalloc.get_traced_memory()[0] if TRACE else 0


def _bytes(count: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if count < 1024 or unit == "GB":
            return "%.1f %s" % (count, unit)
        count /= 1024.0
    return "%d B" % (count,)


def report(alive: list[str], first: Counter, second: Counter, bytes_grown) -> int:
    "Print what the run saw, and answer the exit status."
    print("\n--- what should be gone and is not ---")
    if alive:
        # Every round makes its own objects, so the same chain comes
        # back once per round. The chain is the finding and the count
        # is the size of it.
        for line, times in Counter(alive).most_common():
            print("%s%s" % (line, "" if times == 1 else "  [x%d]" % (times,)))
    else:
        print("nothing: every pane, window, client and screen died.")

    print("\n--- objects one side kept, by type ---")
    print("%-40s %8s %8s" % ("", "warm", "warmer"))
    names = sorted(set(first) | set(second), key=lambda n: -second[n])
    for name in names[:WORST]:
        print("%-40s %8d %8d" % (name, first[name], second[name]))
    if not names:
        print("nothing grew.")
    if len(names) > WORST:
        print("... and %d more types that grew less." % (len(names) - WORST,))

    # Bytes are printed and judged by nobody. `tracemalloc` counts what
    # Python allocated, which moves with the caches, the interpreter's
    # own arenas and the size of the recordings. A budget on it would
    # fail on a machine that reads a file differently.
    if TRACE:
        print(
            "\n%s of traced memory, over both sides. Nothing judges it."
            % (_bytes(bytes_grown),)
        )

    tolerance = int(os.environ.get("PYMUX_LEAKS_TOLERANCE") or DEFAULT_TOLERANCE)
    over = [
        "%s grew by %d, and %d is the most that is noise"
        % (name, second[name], tolerance)
        for name in second
        if second[name] > tolerance
    ]

    if over:
        print("\n--- grew, and did not stop ---")
        for line in sorted(over):
            print(line)

    if alive or over:
        print("\nThis run leaked. `tests/what_leaks.py` says how to read it.")
        return 1

    print("\nNothing leaked.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
