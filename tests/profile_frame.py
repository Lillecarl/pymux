"""
Where the time of a frame goes, as a profile and not as a budget.

`tests/measure_frame.py` counts what a frame costs and holds it to a
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
- **scroll**, one scroll step of `tests/scroll_app.py` before each
  frame: the exact bytes the viewer emits moving a line, fed into the
  pane. That is what scrolling an alt-screen program costs -- parse,
  screen, diff and escape writer -- at whatever width the run sizes.
   `PYMUX_PROFILE_SCROLL_MODE` picks `redraw` (the whole viewport again,
   what `vim` and `fzf` emit), `scroll` (one inserted or deleted line
   in a scroll region, what `less` emits moving a line), or `region`
   (the middle scrolls inside chrome that never moves, what an agent
   harness emits), and `PYMUX_PROFILE_SCROLL_STYLED=0` turns the
   colours off.
  `PYMUX_PROFILE_SCROLL_START=bottom` starts on the last viewport and
  `PYMUX_PROFILE_SCROLL_AT_END=stay` keeps pushing past the end,
  feeding nothing: the fling that hits the bottom, where scrolling is
  slow and no line is even moving.
- **emit**, a burst of `tests/emit_app.py` before each frame: what a
  program flooding the terminal costs. `PYMUX_PROFILE_EMIT_MODE`
  picks `wide` (the terminal wraps), `broken` (the program broke the
  lines), or the `alt-` variants on the alternate screen, and
  `PYMUX_PROFILE_EMIT_LINES` how many lines each burst holds.
- **redraw**, a forced full redraw before each frame: the C-l of the
  viewport, what resize, reconnect and flicker recovery cost. The
  viewport is painted once outside the profile and the committed
  screen is dropped before every frame, so the diff rediffs against
  stale state and the wire carries everything.
  `PYMUX_PROFILE_REDRAW_STYLED=0` turns the colours off.

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

from __future__ import annotations

import io
import os
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

import anyio

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(1, str(Path(__file__).parent.parent))

from emit_app import emit_chunk
from prompt_toolkit.application.current import set_app
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import ColorDepth
from prompt_toolkit.output.vt100 import Vt100_Output
from pyinstrument import Profiler
from scroll_app import (
    FOOTER_ROWS,
    HEADER_ROWS,
    LINES,
    region_scroll_step,
    region_viewport_bytes,
    scroll_step,
    viewport_bytes,
)
from session import Connection, over_connection

from pymux.commands import handle_command
from pymux.main import Pymux

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


@asynccontextmanager
async def server(panes: int):
    """
    A server with one client and that many panes, and the client's
    application.

    The panes are opened the way a person opens them, alternating the
    two splits, so the tree is the shape a hand builds. The panes run
    a program that never writes, so no turn of the loop changes what
    a frame draws.
    """
    pymux = Pymux()
    output = Vt100_Output(stdout=io.StringIO(), get_size=lambda: Size(rows=ROWS, columns=COLUMNS))
    async with pymux.running():
        with create_pipe_input() as pipe:
            state = pymux.add_client(
                output=output,
                input=pipe,
                color_depth=ColorDepth.DEPTH_8_BIT,
                connection=_Connection(),
            )
            with set_app(state.app):
                # What attaching brings: the bindings, and the window
                # the profile draws.
                await pymux.startup()
                for command in CHROME:
                    answer = handle_command(pymux, command)
                    if answer is not None:
                        await answer
                answer = handle_command(pymux, "new-window '%s'" % QUIET)
                if answer is not None:
                    await answer

                for number in range(1, panes):
                    answer = handle_command(pymux, "split-window %s '%s'" % ("-h" if number % 2 else "-v", QUIET))
                    if answer is not None:
                        await answer
            try:
                yield pymux, state
            finally:
                stop_panes(pymux)


def create_frame(state):
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


def stop_panes(pymux) -> None:
    "Kill every process, so nothing outlives the profile."
    for window in list(pymux.arrangement.windows):
        for pane in list(window.panes):
            process = getattr(pane, "process", None)
            if process is not None and not process.is_terminated:
                process.kill()


def idle(pymux, state, frames: int):
    "Frames with nothing changed. Nothing here needs to be paid."
    draw = create_frame(state)
    return lambda: [draw() for _ in range(frames)]


def output(pymux, state, frames: int):
    "A line into one pane before each frame, which is what a program does."
    draw = create_frame(state)
    window = pymux.arrangement.get_active_window()
    pane = window.panes[0]

    def work() -> None:
        for number in range(frames):
            pane.terminal.terminal_control.stream.feed("a line of output, number %d\r\n" % number)
            draw()

    return work


def keys(pymux, state, frames: int):
    "The focus moving left and right, with a frame after each move."
    draw = create_frame(state)

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
    draw = create_frame(state)
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


#: How the `scroll` phase moves: `redraw` rewrites the whole viewport
#: every step, `scroll` inserts or deletes one line in a scroll region,
#: and `region` scrolls only the middle inside chrome that never
#: moves. `tests/scroll_app.py` says which programs each one stands
#: in for.
SCROLL_MODE = os.environ.get("PYMUX_PROFILE_SCROLL_MODE", "") or "redraw"

#: Whether the `scroll` phase colours what it scrolls. Styled cells
#: cost the diff and the escape writer more, so the default measures
#: the program a person actually scrolls.
SCROLL_STYLED = os.environ.get("PYMUX_PROFILE_SCROLL_STYLED", "") or "1"

#: The document line the `scroll` phase starts on. `1` walks from the
#: top; `bottom` starts on the last viewport, where every step down
#: pushes past the end.
SCROLL_START = os.environ.get("PYMUX_PROFILE_SCROLL_START", "") or "1"

#: What a step past either end does. `turn` walks back the way it
#: came; `stay` keeps pushing past the end. What the program feeds
#: there follows the mode -- the same viewport again in `redraw`,
#: nothing in `scroll` -- and the frame draws the screen again either
#: way. That is the fling that hits the bottom and keeps going.
SCROLL_AT_END = os.environ.get("PYMUX_PROFILE_SCROLL_AT_END", "") or "turn"


def scroll(pymux, state, frames: int):
    """
    One scroll step of the viewer before each frame, and nothing else.

    The bytes are the viewer's own: the viewport function draws the
    first screen and the step function moves it a line, so the
    profile holds byte for byte what scrolling the program costs.
    The viewport fills the pane exactly -- its size is read off the
    pane at construction, after the frame `main` draws first let the
    plan size it the way a client would, so no line wraps and the
    width sweep measures the screen.

    Past either end a step feeds what the program would: the same
    viewport again in `redraw` mode, nothing at all in the scrolling
    modes. The frame draws the screen again either way -- changed in
    one case, identical in both -- and that no-change frame is the
    one this phase is really for: what scrolling costs when no line
    is even moving.
    """
    draw = create_frame(state)
    window = pymux.arrangement.get_active_window()
    pane = window.panes[0]
    control = pane.terminal.terminal_control
    rows = control.screen.lines
    columns = control.screen.columns
    mode = SCROLL_MODE
    styled = SCROLL_STYLED != "0"
    if mode == "region":
        paint = region_viewport_bytes
    elif mode in ("redraw", "scroll"):
        paint = viewport_bytes
    else:
        raise SystemExit("no such scroll mode: %r (have redraw, scroll, region)" % (mode,))
    span = rows - HEADER_ROWS - FOOTER_ROWS if mode == "region" else rows - 1
    last = LINES - span + 1
    first = 1 if SCROLL_START == "1" else last
    if SCROLL_START not in ("1", "bottom"):
        raise SystemExit("no such scroll start: %r (have 1, bottom)" % (SCROLL_START,))
    at_end = SCROLL_AT_END
    if at_end not in ("turn", "stay"):
        raise SystemExit("no such scroll end: %r (have turn, stay)" % (at_end,))

    def work() -> None:
        top = first
        direction = +1
        control.stream.feed(paint(top, rows, columns, styled=styled).decode())
        for _ in range(frames):
            coming = top + direction
            if coming < 1 or coming > last:
                if at_end == "stay":
                    # Past the end: what the program would feed --
                    # the same viewport in `redraw` mode, nothing in
                    # the scrolling modes -- and the frame draws again.
                    if mode == "redraw":
                        control.stream.feed(paint(top, rows, columns, styled=styled).decode())
                    draw()
                    continue
                direction = -direction
                coming = top + direction
            if mode == "region":
                control.stream.feed(region_scroll_step(top, direction, rows, columns, styled=styled).decode())
            else:
                control.stream.feed(scroll_step(top, direction, rows, columns, mode=mode, styled=styled).decode())
            top = coming
            draw()

    return work


#: How the `emit` phase writes: `wide` lines run past the edge and
#: the terminal wraps them, `broken` lines are broken at the width by
#: the program. An `alt-` prefix prints on the alternate screen,
#: where a line past the bottom scrolls. `tests/emit_app.py` says
#: which programs each one stands in for.
EMIT_MODE = os.environ.get("PYMUX_PROFILE_EMIT_MODE", "") or "broken"

#: How many lines the `emit` phase writes before each frame. The
#: default is a screenful: a program flooding the terminal.
EMIT_LINES = int(os.environ.get("PYMUX_PROFILE_EMIT_LINES", "") or 0)

#: Whether the `emit` phase colours what it writes.
EMIT_STYLED = os.environ.get("PYMUX_PROFILE_EMIT_STYLED", "") or "1"


def emit(pymux, state, frames: int):
    """
    One burst of the emitter before each frame, and nothing else.

    The bytes are the emitter's own: `emit_chunk` writes numbered
    lines, so the profile holds byte for byte what a program flooding
    the terminal costs -- parse, scrollback, diff and escape writer.
    The lines are numbered ever upwards, which is an endless scroll a
    test can read the position of.

    In `wide` mode the terminal wraps each line itself; in `broken`
    mode the program broke them. On the alternate screen there is no
    scrollback and a line past the bottom scrolls instead.
    """
    draw = create_frame(state)
    window = pymux.arrangement.get_active_window()
    pane = window.panes[0]
    control = pane.terminal.terminal_control
    rows = control.screen.lines
    columns = control.screen.columns
    mode = EMIT_MODE
    if mode not in ("wide", "broken", "alt-wide", "alt-broken"):
        raise SystemExit("no such emit mode: %r (have wide, broken, alt-wide, alt-broken)" % (mode,))
    alt = mode.startswith("alt-")
    base = mode[4:] if alt else mode
    per = EMIT_LINES or rows
    styled = EMIT_STYLED != "0"

    def work() -> None:
        number = 1
        if alt:
            control.stream.feed("\x1b[?1049h")
        for _ in range(frames):
            control.stream.feed(emit_chunk(number, per, columns, mode=base, styled=styled).decode())
            number += per
            draw()

    return work


#: Whether the `redraw` phase colours what it repaints. Styled cells
#: cost the diff and the escape writer more, so the default measures
#: the program a person actually looks at.
REDRAW_STYLED = os.environ.get("PYMUX_PROFILE_REDRAW_STYLED", "") or "1"


def redraw(pymux, state, frames: int):
    """
    A forced full redraw before each frame: the C-l of the viewport.

    The viewport is painted once, outside the profile, and the frame
    after it commits, so every profiled frame starts from a painted
    screen. A redraw repaints what is already there -- resize,
    reconnect, flicker recovery -- so no bytes flow and the parse
    costs nothing; what is measured is the render and the wire.

    Each frame drops the committed screen first, which is what resize
    does to it (`Renderer.render` forgets it when the size changes).
    The diff rediffs against stale state: every row rebuilds, every
    measure is fresh, the wire carries everything, and none of the
    reuse paths -- carried widths, stable rows, row equality -- can
    help it. That is the number the rotation and scroll-op work will
    move, so it is taken before they land.
    """
    draw = create_frame(state)
    window = pymux.arrangement.get_active_window()
    pane = window.panes[0]
    control = pane.terminal.terminal_control
    rows = control.screen.lines
    columns = control.screen.columns
    styled = REDRAW_STYLED != "0"
    control.stream.feed(viewport_bytes(1, rows, columns, styled=styled).decode())
    draw()

    def work() -> None:
        for _ in range(frames):
            state.app.renderer._last_screen = None
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
    draw = create_frame(state)
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
    ("scroll", scroll),
    ("emit", emit),
    ("redraw", redraw),
)

#: The `animated` phase runs a real program in the pane, the way
#: `tests/what_busy_pane_costs.py` does, and profiles the live loop.
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

    def read_packet(packet) -> None:
        raw = packet if isinstance(packet, (bytes, bytearray)) else str(packet).encode()
        counts["wire"] += len(raw)

    async with over_connection(read_packet=read_packet) as session:
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
            await pymux.create_window(command)
        await anyio.sleep(1.5)

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
            await anyio.sleep(seconds)
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


async def main() -> int:
    panes = int(os.environ.get("PYMUX_PROFILE_PANES", "") or 8)
    frames = int(os.environ.get("PYMUX_PROFILE_FRAMES", "") or 200)
    out = Path(os.environ.get("PYMUX_PROFILE_OUT", "") or ".")
    chosen = os.environ.get("PYMUX_PROFILE_PHASES", "")
    names = [n for n, _ in PHASES] + (["animated"] if ANIMATED else [])
    phases = [(n, f) for n, f in PHASES if not chosen or n in chosen.split(",")]
    unknown = {n for n in chosen.split(",") if n} - set(names)
    if unknown:
        raise SystemExit("no such phase: %s (have %s)" % (", ".join(sorted(unknown)), ", ".join(names)))

    print(
        "%dx%d, %d panes, %d frames per phase, sampling every %.1f ms.\n"
        % (COLUMNS, ROWS, panes, frames, INTERVAL * 1000)
    )

    if ANIMATED:
        await _animated(ANIMATED, ANIMATED_SECONDS, out)

    for name, phase in phases:
        async with server(panes) as (pymux, state):
            with set_app(state.app):
                # One frame outside the profile, and before the phase
                # is built. The first frame of a client draws every
                # cell and builds every container, and that is startup
                # and not a frame -- and the plan gives each pane its
                # rectangle while it draws, so a phase that fills its
                # pane reads the size a client would give it rather
                # than the size with no client. Lillecarl/pymux#224.
                create_frame(state)()

                work = phase(pymux, state, frames)

                wire = state.output.stdout
                written = wire.tell()
                profiler = Profiler(interval=INTERVAL)
                profiler.start()
                started = time.perf_counter()
                work()
                took = time.perf_counter() - started
                profiler.stop()
                written = wire.tell() - written

            print("=" * 70)
            print(
                "%s: %d frames in %.3fs, %.2f ms and %d characters to the terminal each"
                % (name, frames, took, 1000 * took / frames, written // frames)
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

    return 0


if __name__ == "__main__":
    sys.exit(anyio.run(main))
