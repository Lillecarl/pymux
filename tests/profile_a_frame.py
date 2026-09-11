"""
Where the time of a frame goes, as a profile and not as a budget.

`tests/measure_a_frame.py` counts what a frame costs and holds it to a
number. That says *whether* something got dearer, and it cannot say
*where*: it counts bytecode, and bytecode is not time. This is the
other half. It runs a real server with real panes and samples the stack
while it draws, so a person reading it sees which function the seconds
are in.

**Nothing here is a gate and nothing is judged.** A sampling profiler
reports wall clock, and a build sandbox runs beside fifteen other jobs,
so the numbers move between runs. They are for reading.

## What it profiles

One process, holding the server and a client at once. Carl: "the pymux
integrated mode runs both the server and client in the same process so
it can answer both server and client side". That is what this is -- the
arrangement, the layout, the panes and the render are all in the
profile, so a frame is followed the whole way rather than up to a
socket.

A frame is the real one: `before_render`, `Renderer.render` against the
screen of the frame before it, and `after_render`. So the profile holds
the diff and the escape sequences the client writes, which no other
measurement here reaches.

Three phases, because "what a frame costs" is three questions:

- **idle**, frames with nothing changed. This is the one that says
  whether pymux is paying for work it does not need: nothing moved, so
  everything in this profile is either a diff that found nothing or a
  measurement that was made again for no reason.
- **sparse**, a few scattered cells changing every frame. That is what
  an animating pane is -- cmatrix rain, a clock, a spinner -- and it
  says what a frame costs when the change is small and the screen is
  not.
- **output**, a line written into one pane before each frame. That is
  what a program printing costs, end to end.
- **keys**, `select-pane` left and right with a frame between. The path
  a person's hand is on, and the one that asks the layout where the
  panes are.

And **animated**, which is different: it runs a real program in the
pane and profiles the live loop for a few seconds, counting renders
separately from frames sent. The program comes from
`PYMUX_PROFILE_ANIMATED` (a command line; `cmatrix -u 2` by default)
and the seconds from `PYMUX_PROFILE_ANIMATED_SECONDS`. It needs the
program in the check's inputs.

## Reading it

    PYMUX_PROFILE_PANES=16 nix build --file . checks.pymux-profile.run
    less result/log
    $BROWSER result/idle.html

`result/log` holds the text profile of each phase, and each phase also
leaves an HTML flame graph beside it. `PYMUX_PROFILE_PANES` says how
many panes to open and `PYMUX_PROFILE_FRAMES` how many frames to draw
in each phase. `PYMUX_PROFILE_ROWS` and `PYMUX_PROFILE_COLUMNS` size
the client's terminal, and `PYMUX_PROFILE_PHASES` narrows the run to
named phases, comma separated:

    PYMUX_PROFILE_PANES=1 PYMUX_PROFILE_ROWS=59 PYMUX_PROFILE_COLUMNS=187 \
      PYMUX_PROFILE_PHASES=sparse,output PYMUX_PROFILE_FRAMES=200 \
      nix build --file . checks.pymux-profile.run
"""

import asyncio
import io
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(1, str(Path(__file__).parent.parent))

from a_session import Connection, over_a_connection  # noqa: E402
from prompt_toolkit.application.current import set_app  # noqa: E402
from prompt_toolkit.data_structures import Size  # noqa: E402
from prompt_toolkit.input import create_pipe_input  # noqa: E402
from prompt_toolkit.output import ColorDepth  # noqa: E402
from prompt_toolkit.output.vt100 import Vt100_Output  # noqa: E402
from pyinstrument import Profiler  # noqa: E402

from pymux.main import Pymux  # noqa: E402

#: The client this draws for. A terminal nobody resized, unless the
#: environment said otherwise: the cost of a frame is the cost of the
#: screen it covers.
ROWS = int(os.environ.get("PYMUX_PROFILE_ROWS", "") or 24)
COLUMNS = int(os.environ.get("PYMUX_PROFILE_COLUMNS", "") or 80)

#: A pane that stays up and draws nothing of its own, so the profile
#: holds no work but pymux's own.
QUIET = "%s -c 'import time; time.sleep(600)'" % (sys.executable,)

#: What the panes are laid out as. The pane status is on, because the
#: title bars are what ask the layout where every pane is, and that is
#: the interesting part of a frame.
CHROME = ["set-option status on", "set-option pane-border-status on"]

#: How fine the sampling is. A frame of a few panes takes a millisecond
#: or two, so anything coarser than this puts a whole frame in one
#: sample.
INTERVAL = 0.0002


class _Graphics:
    """
    A terminal that draws no images.

    The frame after each render offers the panes to the graphics
    protocol, and a terminal that does not speak it says so. This is
    the honest half of a client: the offer is made and declined, which
    is what a frame in a terminal without kitty graphics costs.
    """

    supported = False


class _Connection(Connection):
    """
    What `Pymux` asks a connection for, with graphics offered and
    declined. The frame after each render offers the panes to the
    graphics protocol, and a terminal that does not speak it says so,
    which is what a frame in a terminal without kitty graphics costs.
    """

    graphics = _Graphics()


def a_server(panes: int):
    """
    A server with one client and that many panes, and the client's
    application.

    The panes are opened the way a person opens them, alternating the
    two splits, so the tree is the shape a hand builds.
    """
    pymux = Pymux()
    output = Vt100_Output(
        stdout=io.StringIO(), get_size=lambda: Size(rows=ROWS, columns=COLUMNS)
    )
    pipe = create_pipe_input()
    state = pymux.add_client(
        output=output,
        input=pipe.__enter__(),
        color_depth=ColorDepth.DEPTH_8_BIT,
        connection=_Connection(),
    )

    with set_app(state.app):
        for command in CHROME:
            pymux.handle_command(command)
        pymux.handle_command("new-window '%s'" % QUIET)

        for number in range(1, panes):
            pymux.handle_command(
                "split-window %s '%s'" % ("-h" if number % 2 else "-v", QUIET)
            )

    return pymux, state, pipe


def a_frame(state):
    """
    One whole frame, the way the event loop draws one.

    `Application._redraw` is these three steps and a guard that says
    the application is running, which it is not here. The steps are
    what a frame is: forget what the frame before left, render against
    its screen, and then draw the images of the panes.
    """
    app = state.app

    def draw() -> None:
        app.render_counter += 1
        app.before_render.fire()
        app.renderer.render(app, app.layout)
        app.after_render.fire()

    return draw


def self_time_ranking(profiler: Profiler, limit: int = 40):
    """
    The hot path flat: every frame in the tree, by its own time.

    The tree view collapses and hides, and a hot path made of many
    small functions hides best of all. This is the same samples as a
    ranking of self time, which nothing can hide in.
    """
    ranking: dict = {}

    def walk(node, inherited: str) -> None:
        if node.function != "[self]":
            # The node's own total holds the samples its body spent
            # outside its children; the [self] leaves under it carry
            # the same time again, so only one of them counts.
            name = "%s  %s" % (
                node.function or inherited,
                node.code_position_short(),
            )
            ranking[name] = ranking.get(name, 0.0) + node.total_self_time
        for child in node.children:
            walk(child, node.function)

    walk(profiler.last_session.root_frame(), "")
    return sorted(ranking.items(), key=lambda item: -item[1])[:limit]


def self_time_report(name: str, profiler: Profiler) -> None:
    total = sum(profiler.last_session.root_frame().total_self_time for _ in (0,)) or 1
    ranking = self_time_ranking(profiler)
    if not ranking:
        return
    print("--- %s: the hot path, by self time ---" % name)
    for where, seconds in ranking:
        print("  %7.3fs  %s" % (seconds, where))
    print()


def stop_the_panes(pymux) -> None:
    "Kill every process, so nothing outlives the profile."
    for window in list(pymux.arrangement.windows):
        for pane in list(window.panes):
            process = getattr(pane, "process", None)
            if process is not None and not process.is_terminated:
                process.kill()


def idle(pymux, state, frames: int):
    "Frames with nothing changed. Nothing here needs to be paid."
    draw = a_frame(state)
    return lambda: [draw() for _ in range(frames)]


def output(pymux, state, frames: int):
    "A line into one pane before each frame, which is what a program does."
    draw = a_frame(state)
    window = pymux.arrangement.get_active_window()
    pane = window.panes[0]

    def work() -> None:
        for number in range(frames):
            pane.terminal.terminal_control.stream.feed(
                "a line of output, number %d\r\n" % number
            )
            draw()

    return work


def keys(pymux, state, frames: int):
    "The focus moving left and right, with a frame after each move."
    draw = a_frame(state)

    def work() -> None:
        for number in range(frames):
            pymux.handle_command("select-pane -%s" % ("L" if number % 2 else "R"))
            state.sync_focus()
            draw()

    return work


def sparse(pymux, state, frames: int):
    """
    A few scattered cells changing every frame, which is what an
    animating pane is: cmatrix rain, a clock, a spinner. Most of the
    screen holds still and a handful of cells move, so the profile says
    what a frame costs when the *change* is small and the screen is
    not.

    The positions cycle deterministically, so a run is the same run.
    """
    draw = a_frame(state)
    window = pymux.arrangement.get_active_window()
    pane = window.panes[0]
    columns = COLUMNS
    rows = ROWS

    def work() -> None:
        for number in range(frames):
            moves = []
            for spot in range(8):
                # A fixed stride walks the pane without repeating a
                # position, and never touches the first row (the title
                # bar) or the rows the status line and its margin own.
                cell = (number * 8 + spot) % max(1, (rows - 4) * (columns - 4))
                row = 2 + cell // (columns - 4)
                column = 2 + cell % (columns - 4)
                moves.append("\x1b[%d;%dH*" % (row, column))
            pane.terminal.terminal_control.stream.feed("".join(moves))
            draw()

    return work


#: How many cells a frame of the `rain` phase changes. cmatrix at
#: 187x59 measured 8.5 KB parsed a frame, which is about 700 styled
#: cells: a head and a trail cell per column, each with its colour.
RAIN_CELLS = int(os.environ.get("PYMUX_PROFILE_RAIN_CELLS", "") or 700)


def rain(pymux, state, frames: int):
    """
    The density of a real animating pane, drawn by hand.

    `sparse` changes eight cells and shows the floor; this changes
    `RAIN_CELLS` spread over every row, the way cmatrix does, so the
    profile attributes the cost that actually carries the 19 ms. It is
    synchronous on purpose: a flame graph can only follow `draw()` when
    the draw is not hidden behind an await.
    """
    draw = a_frame(state)
    window = pymux.arrangement.get_active_window()
    pane = window.panes[0]
    rows = ROWS
    columns = COLUMNS
    area = max(1, (rows - 4) * (columns - 4))

    def work() -> None:
        for number in range(frames):
            moves = []
            for spot in range(RAIN_CELLS):
                cell = (number * 7 + spot * 593) % area
                row = 2 + cell // (columns - 4)
                column = 2 + cell % (columns - 4)
                moves.append("\x1b[%d;%dH\x1b[32m#" % (row, column))
            pane.terminal.terminal_control.stream.feed("".join(moves))
            draw()

    return work


PHASES = (
    ("idle", idle),
    ("sparse", sparse),
    ("rain", rain),
    ("output", output),
    ("keys", keys),
)

#: The `animated` phase runs a real program in the pane, the way
#: `tests/what_a_busy_pane_costs.py` does, and profiles the live loop.
#: It counts renders separately from frames sent, because a render that
#: emits no packet is cost the frame counter never sees.
ANIMATED = os.environ.get("PYMUX_PROFILE_ANIMATED", "")

ANIMATED_SECONDS = float(os.environ.get("PYMUX_PROFILE_ANIMATED_SECONDS", "") or 4.0)


async def _animated(command: str, seconds: float, out: Path) -> None:
    """
    Run a real animating program in the pane of a real connection, and
    time the two things the server does with what it writes: parse it,
    and render the frame. The rest of the CPU -- the loop, the layout
    dispatch, the write out -- is what is left when the two are
    subtracted from the process time.

    A sampling profiler cannot see this path: the work runs in loop
    callbacks while this coroutine sleeps, so the timers are wrapped
    around the calls instead.
    """
    import shutil

    import pyte.streams

    if shutil.which(command.split()[0]) is None:
        raise SystemExit("no such program: %s" % (command.split()[0],))

    counts = {"renders": 0, "render_s": 0.0, "feed_s": 0.0, "fed": 0, "wire": 0}

    def read_a_packet(packet) -> None:
        raw = packet if isinstance(packet, (bytes, bytearray)) else str(packet).encode()
        counts["wire"] += len(raw)

    with over_a_connection(read_a_packet=read_a_packet) as session:
        pymux = session.pymux
        state, _size = await session.attach("only", Size(rows=ROWS, columns=COLUMNS))

        # Wrap before the pane exists: ptterm binds `receive` to
        # `stream.feed` when its Terminal is built, so a wrap that came
        # after would watch nothing.
        real_feed = pyte.streams.Stream.feed

        def timing_feed(self, data, *a):
            counts["fed"] += len(data)
            started = time.perf_counter()
            try:
                return real_feed(self, data, *a)
            finally:
                counts["feed_s"] += time.perf_counter() - started

        pyte.streams.Stream.feed = timing_feed

        with set_app(state.app):
            pymux.create_window(command)
        await asyncio.sleep(1.5)

        # Every render, timed, whether or not it emitted a frame.
        renderer = state.app.renderer
        real_render = renderer.render

        def timing_render(app, layout):
            started = time.perf_counter()
            try:
                return real_render(app, layout)
            finally:
                counts["renders"] += 1
                counts["render_s"] += time.perf_counter() - started

        renderer.render = timing_render

        try:
            frames_at_start = pymux.counters.frames
            wire_at_start = counts["wire"]
            cpu_at_start = time.process_time()

            started = time.perf_counter()
            await asyncio.sleep(seconds)
            took = time.perf_counter() - started

            cpu = time.process_time() - cpu_at_start
        finally:
            pyte.streams.Stream.feed = real_feed

        renders = counts["renders"]
        drawn = pymux.counters.frames - frames_at_start
        wire = counts["wire"] - wire_at_start
        render_ms = 1000 * counts["render_s"] / max(1, drawn)
        feed_ms = 1000 * counts["feed_s"] / max(1, drawn)
        cpu_ms = 1000 * cpu / max(1, drawn)
        print("=" * 70)
        print(
            "animated %r for %.1fs at %dx%d: %d renders, %d frames sent\n"
            "%d%% of a core; %d bytes parsed, %d wire bytes sent a frame\n"
            "a frame sent: render %.1f ms, parse %.1f ms, other %.1f ms "
            "(CPU %.1f ms, wall %.1f ms)"
            % (
                command,
                took,
                COLUMNS,
                ROWS,
                renders,
                drawn,
                round(100 * cpu / took),
                counts["fed"] // max(1, drawn),
                wire // max(1, drawn),
                render_ms,
                feed_ms,
                cpu_ms - render_ms - feed_ms,
                cpu_ms,
                1000 * took / max(1, drawn),
            )
        )
        print("=" * 70)


def main() -> int:
    panes = int(os.environ.get("PYMUX_PROFILE_PANES", "") or 8)
    frames = int(os.environ.get("PYMUX_PROFILE_FRAMES", "") or 200)
    out = Path(os.environ.get("PYMUX_PROFILE_OUT", "") or ".")
    chosen = os.environ.get("PYMUX_PROFILE_PHASES", "")
    names = [n for n, _ in PHASES] + (["animated"] if ANIMATED else [])
    phases = [(n, f) for n, f in PHASES if not chosen or n in chosen.split(",")]
    unknown = {n for n in chosen.split(",") if n} - set(names)
    if unknown:
        raise SystemExit(
            "no such phase: %s (have %s)"
            % (", ".join(sorted(unknown)), ", ".join(names))
        )

    print(
        "%dx%d, %d panes, %d frames per phase, sampling every %.1f ms.\n"
        % (COLUMNS, ROWS, panes, frames, INTERVAL * 1000)
    )

    if ANIMATED:
        asyncio.run(_animated(ANIMATED, ANIMATED_SECONDS, out))

    for name, phase in phases:
        pymux, state, pipe = a_server(panes)
        try:
            with set_app(state.app):
                work = phase(pymux, state, frames)

                # One frame outside the profile. The first frame of a
                # client draws every cell and builds every container,
                # and that is startup and not a frame.
                a_frame(state)()

                profiler = Profiler(interval=INTERVAL)
                profiler.start()
                started = time.perf_counter()
                work()
                took = time.perf_counter() - started
                profiler.stop()

            print("=" * 70)
            print(
                "%s: %d frames in %.3fs, %.2f ms each"
                % (name, frames, took, 1000 * took / frames)
            )
            print("=" * 70)
            # PYMUX_PROFILE_SHOW_ALL names the frames the default view
            # hides, which is where a hot path hides when it is made of
            # many small functions.
            show_all = os.environ.get("PYMUX_PROFILE_SHOW_ALL", "")
            print(
                profiler.output_text(
                    unicode=True,
                    color=False,
                    show_all=bool(show_all),
                )
            )
            self_time_report(name, profiler)

            (out / ("%s.html" % name)).write_text(profiler.output_html())
        finally:
            stop_the_panes(pymux)
            pipe.__exit__(None, None, None)

    return 0


if __name__ == "__main__":
    sys.exit(main())
