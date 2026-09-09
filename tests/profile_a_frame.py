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
- **output**, a line written into one pane before each frame. That is
  what a program printing costs, end to end.
- **keys**, `select-pane` left and right with a frame between. The path
  a person's hand is on, and the one that asks the layout where the
  panes are.

## Reading it

    PYMUX_PROFILE_PANES=16 nix build --file . checks.pymux-profile.run
    less result/log
    $BROWSER result/idle.html

`result/log` holds the text profile of each phase, and each phase also
leaves an HTML flame graph beside it. `PYMUX_PROFILE_PANES` says how
many panes to open and `PYMUX_PROFILE_FRAMES` how many frames to draw
in each phase.
"""

import io
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(1, str(Path(__file__).parent.parent))

from prompt_toolkit.application.current import set_app  # noqa: E402
from prompt_toolkit.data_structures import Size  # noqa: E402
from prompt_toolkit.input import create_pipe_input  # noqa: E402
from prompt_toolkit.output import ColorDepth  # noqa: E402
from prompt_toolkit.output.vt100 import Vt100_Output  # noqa: E402
from pyinstrument import Profiler  # noqa: E402

from pymux.main import Pymux  # noqa: E402

#: The client this draws for: a terminal nobody resized.
ROWS = 24
COLUMNS = 80

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


class _Connection:
    "What `Pymux` asks a connection for, and nothing else."

    kitty_source_flags = 0
    pointer_shape = None
    graphics = _Graphics()

    def set_pointer_shape(self, shape):
        pass

    def _send_packet(self, packet):
        pass


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


PHASES = (("idle", idle), ("output", output), ("keys", keys))


def main() -> int:
    panes = int(os.environ.get("PYMUX_PROFILE_PANES", "") or 8)
    frames = int(os.environ.get("PYMUX_PROFILE_FRAMES", "") or 200)
    out = Path(os.environ.get("PYMUX_PROFILE_OUT", "") or ".")

    print(
        "%d panes, %d frames per phase, sampling every %.1f ms.\n"
        % (panes, frames, INTERVAL * 1000)
    )

    for name, phase in PHASES:
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
            print(profiler.output_text(unicode=True, color=False, show_all=False))

            (out / ("%s.html" % name)).write_text(profiler.output_html())
        finally:
            stop_the_panes(pymux)
            pipe.__exit__(None, None, None)

    return 0


if __name__ == "__main__":
    sys.exit(main())
