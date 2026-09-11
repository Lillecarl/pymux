"""
Photograph what pymux draws around a pane, in a real terminal.

`take_a_picture.py` subtracts two pictures of the same program, one
bare and one in a pymux pane. It writes `set full-screen on` before
every fixture, which is right for what it asks -- anything pymux drew
around the pane would count as a difference on every fixture -- and it
means **no check has ever photographed pymux's own chrome**: the status
line, a pane title bar, the command palette, the overlay pane.

Nothing could, either. A headless compositor owns no input device and
the terminal's pty belongs to the terminal, so nothing could press a
key at pymux to open any of it. `drive_in_a_terminal.py` is the way
round: it runs as the terminal's child, runs pymux on a pty of its own,
copies what pymux writes to the terminal's tty, and types the keys.
Lillecarl/pymux#161.

    the keys ──▶ the relay's pty ──▶ pymux ──▶ the terminal's tty
                                                     │
                                                a picture

The keys are fenced the way `middleman.py` fences a write. The pane
runs the forwarder, the fence goes down the fifo behind the keys, and
the relay touches the fence file when the fence comes back. The
picture waits for that file: it is of a frame pymux finished, and not
of a moment that happened to be quiet. Lillecarl/pymux#275.

**This judges nothing.** It keeps a picture of each fixture for a
person to read, the way `photograph_vttest.py` does. A picture of
chrome has nothing to subtract: there is no bare side, because the
chrome is the thing pymux adds. Judging it needs a recorded image, and
recording one before anybody has looked at it would record whatever it
does today, faults and all. So this is not a gate, and reading the
pictures is the work.

    PYMUX_CHROME=palette nix build --file . checks.pymux-chrome-pictures

`PYMUX_CHROME` narrows the run to the fixtures whose name holds that
text, and `PYMUX_CHROME_TERMINALS` to the terminals whose name does.
"""

import os
import shlex
import sys
import time
from pathlib import Path

# `tests/`, for the harness beside this file, and the directory above
# it, for `pymux` itself. Running a script puts the script's own
# directory on the path and not the one it was started from.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(1, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pymux.style import THEMES  # noqa: E402
from pyterm_pytest.seats import SEATS  # noqa: E402

from middleman import FORWARDER  # noqa: E402
from take_a_picture import HOLD, TERMINALS, every_log  # noqa: E402

#: Where the pictures go. The check points this at `$out`.
PICTURES = Path(os.environ.get("PYMUX_CHROME_OUT", "chrome-pictures"))

#: Which fixtures and which terminals to run.
ONLY = os.environ.get("PYMUX_CHROME", "")
ONLY_TERMINALS = os.environ.get("PYMUX_CHROME_TERMINALS", "")

#: The relay, beside this file.
RELAY = Path(__file__).parent / "drive_in_a_terminal.py"

#: What every fixture turns on. The status line and a pane's title bar
#: are the two pieces of chrome a person looks at all day, and both are
#: what `full-screen on` takes away.
CHROME = "set-option status on\nset-option pane-border-status on\n"

#: The prefix, and the keys that reach a binding behind it. `rc.py` is
#: where these are bound: `"` splits a pane in two, top and bottom, and
#: `%` splits it left and right.
PREFIX = b"\x02"

def keys(*steps):
    """
    A keys file from (seconds to wait, bytes) pairs.

    The first wait is counted from pymux's first frame, which the
    relay waits for. The rest are counted from the step before them.
    `drive_in_a_terminal.py` says the format.
    """
    return "".join("%s %r\n" % (delay, one) for delay, one in steps)


def create_command(text):
    """
    The keys that run one pymux command from the command line.

    A window option cannot be set from a configuration file: that file
    is read before the first window is made, and a window option
    belongs to a window. Lillecarl/pymux#199.
    """
    return [
        (0.6, PREFIX),
        (0.4, b":"),
        (0.6, text.encode("ascii") + b"\r"),
    ]


#: What each fixture is: the configuration pymux reads, and the keys
#: pressed at it.
FIXTURES = {
    # The plainest one. One pane, and everything pymux draws around it.
    "bare": (CHROME, ""),
    # Two panes side by side, so a focused title bar and an unfocused
    # one are both in the picture, in their two colours.
    "two-panes": (CHROME, keys((0.0, PREFIX), (0.4, b"%"))),
    # A pane over a pane, which is the other border and the other
    # arrangement of the two title bars.
    "stacked-panes": (CHROME, keys((0.0, PREFIX), (0.4, b'"'))),
    # The command line as a bar along the bottom, which is what pymux
    # draws by default. It is left open, so the picture holds it.
    "command-line": (
        CHROME,
        keys((0.0, PREFIX), (0.4, b":"), (0.4, b"list-panes")),
    ),
    # And as a box in the middle of the screen. Lillecarl/pymux#158
    # built it, and a picture of it is what found four faults in it.
    "command-palette": (
        CHROME + "set-option command-palette on\n",
        keys((0.0, PREFIX), (0.4, b":"), (0.4, b"list-panes")),
    ),
    # The box that composes a key, with a modifier written and the keys
    # a keyboard leaves out under it. Lillecarl/pymux#220.
    "compose-a-key": (
        CHROME,
        keys(*create_command("compose-key"), (0.8, b"ctrl+")),
    ),
    # Both splits at once: a pane on the left, and two stacked on the
    # right. This is the default layout, and it is the one picture that
    # holds every line it draws -- the border down the middle, which
    # runs the whole height because the split that left it does, and
    # the border across the right column, which stops where that
    # column does. The stack also means every pane draws the bar below
    # it. Lillecarl/pymux#217, Lillecarl/pymux#211.
    "divided": (
        CHROME,
        keys(
            (0.0, PREFIX),
            (0.4, b"%"),
            (0.6, PREFIX),
            (0.4, b'"'),
        ),
    ),
    # One pane filling the window, with the other two behind it. Zoom
    # is a layout that wraps the layout underneath, so the pane keeps
    # the row its title bar hangs in and the bar carries the "Z".
    # Lillecarl/pymux#215.
    "zoomed": (
        CHROME,
        keys((0.0, PREFIX), (0.4, b"%"), *create_command("resize-pane -Z")),
    ),
    # A strip whose second column is two thirds of the window, with the
    # focus on the first. The pair does not fit, so the second one runs
    # off the right edge and is tinted: **the one case where what a
    # person sees is not what the program wrote**, and the only thing
    # on the screen that says so. Lillecarl/pymux#222.
    #
    # The `strip` fixture below cuts nothing. Half a window each means
    # two columns fit exactly, which is why a half is the default.
    "strip-cut": (
        CHROME + "set-window-option -g strip on\n",
        keys(
            (0.0, PREFIX),
            (0.4, b"%"),
            *create_command("switch-column-width"),
            *create_command("select-pane -L"),
        ),
    ),
    # A strip of three columns, which runs past the edge of the screen.
    # Lillecarl/pymux#198.
    "strip": (
        # `-g` says what every new window starts with, and it is the
        # only way a configuration file can set a window option: the
        # file is read before there is a window. Lillecarl/pymux#199.
        CHROME + "set-window-option -g strip on\n",
        keys(
            (0.0, PREFIX),
            (0.4, b"%"),
            (0.6, PREFIX),
            (0.4, b"%"),
        ),
    ),
    # The pane numbers, which `display-panes` puts up for a moment.
    "pane-numbers": (
        CHROME,
        keys(
            (0.0, PREFIX),
            (0.4, b"%"),
            (0.6, PREFIX),
            (0.4, b"q"),
        ),
    ),
    # The clock, which a pane draws over itself.
    "clock": (
        CHROME,
        keys((0.0, PREFIX), (0.4, b"t")),
    ),
    # An overlay pane, floating in the middle of the screen over two
    # panes. Its body runs a program, so its default-background cells
    # have to show the terminal's own background, the way a normal
    # pane does: a rule that named a colour behind it drew a slab of
    # chrome over the layout instead. Lillecarl/pymux#223.
    "overlay": (
        CHROME,
        keys(
            (0.0, PREFIX),
            (0.4, b"%"),
            (0.6, PREFIX),
            (0.4, b"g"),
        ),
    ),
    # Two lines of a configuration file that fail. Every error is joined
    # into one message and each names the file and the line, so the pair
    # runs past the width of the screen.
    #
    # The message toolbar used to draw from its end, so the first of the
    # two could not be read: a person fixed the one they could see and
    # met the other on the next run. It wraps now, and no keys are
    # pressed here because the message is up before anything is.
    # Lillecarl/pymux#205, Lillecarl/pymux#38.
    "startup-errors": (
        CHROME + "not-a-command-alpha\nnot-a-command-beta\n",
        "",
    ),
}


#: One picture for each theme, so a theme that is added later gets one
#: without anybody remembering to add it. `pymux/pymux/style.py` holds
#: them. Lillecarl/pymux#194, Lillecarl/pymux#195.
#:
#: The keys open a second pane and stop there. One picture then holds
#: the status line, the focused title bar, the unfocused one and the
#: focused pane's border, which is most of what a theme colours.
#:
#: **The command line is not opened, on purpose.** It takes the focus,
#: so every pane draws as unfocused, and it covers the status line. A
#: picture with it open showed two themes as the same grey, because
#: nothing a theme colours differently was on the screen.
for _name in THEMES:
    FIXTURES["theme-%s" % _name] = (
        CHROME + "set-option theme %s\n" % _name,
        keys((0.0, PREFIX), (0.4, b"%")),
    )

#: One theme from pygments, so the derivation of a whole scheme from
#: the handful of colours a style carries is judged as a picture, like
#: the hand themes. A pygments theme reaches the server by name:
#: `set-option theme pygments:<name>`. Lillecarl/pymux#194.
FIXTURES["theme-pygments-dracula"] = (
    CHROME + "set-option theme pygments:dracula\n",
    keys((0.0, PREFIX), (0.4, b"%")),
)


def every_fixture():
    return sorted(FIXTURES)


def chrome_command(
    keys_path,
    socket_path,
    config_path,
    log_path,
    error_path,
    fifo_path,
    size_path,
    forwarder_path,
    fence_path,
):
    """
    The command the terminal runs: the relay, and pymux under it.

    pymux runs through `sh -c` so that its own stderr goes to a file.
    It shares a terminal with the client in the integrated mode, so
    anything it writes there lands in the picture
    (Lillecarl/pymux#36), and the relay would copy a traceback onto
    the screen along with everything else. The relay's stderr goes to
    a file for the same reason, and the file is in the room, where
    `every_log` reads it.

    The integrated mode holds the server and the client in one
    process, so the picture is of the pymux this check built and not of
    a server that was already running. The pane runs the forwarder,
    which copies the fifo to its own output: that is the road the
    fence travels.
    """
    relay_error = Path(error_path).parent / "relay-error.log"
    inside = (
        "exec python3 -m pymux -S %s -f %s --log %s"
        " integrated python3 %s %s %s 2>%s"
        % (
            shlex.quote(str(socket_path)),
            shlex.quote(str(config_path)),
            shlex.quote(str(log_path)),
            shlex.quote(str(forwarder_path)),
            shlex.quote(str(fifo_path)),
            shlex.quote(str(size_path)),
            shlex.quote(str(error_path)),
        )
    )
    return "exec python3 %s %s %d %s %s 2>%s -- sh -c %s" % (
        shlex.quote(str(RELAY)),
        shlex.quote(str(keys_path)),
        HOLD,
        shlex.quote(str(fifo_path)),
        shlex.quote(str(fence_path)),
        shlex.quote(str(relay_error)),
        shlex.quote(inside),
    )


def picture_of(terminal, seat, name, work, out, fixtures=None):
    "One fixture, in one terminal, left as a picture."
    room = out / terminal.name / name
    room.mkdir(parents=True, exist_ok=True)

    if fixtures is None:
        fixtures = FIXTURES
    config, keys = fixtures[name]

    config_path = work / ("%s.conf" % name)
    config_path.write_text(config)

    keys_path = work / ("%s.keys" % name)
    keys_path.write_text(keys)

    # The pane runs the forwarder, which copies the fifo to its own
    # output: the fence goes down the fifo behind the keys and comes
    # back on the wire when pymux has done with them. middleman.py
    # says why. Every name carries the terminal's and the fixture's,
    # the way the socket's does: a run before left its own behind.
    forwarder_path = work / "chrome-forwarder.py"
    forwarder_path.write_text(FORWARDER)
    fifo_path = work / ("%s-%s.fifo" % (terminal.name, name))
    size_path = work / ("%s-%s-pane-size.txt" % (terminal.name, name))

    seat.picture_of(
        terminal,
        chrome_command(
            keys_path,
            # The name of the terminal is in it: every terminal runs
            # every fixture, and a socket a run before left behind is
            # a socket this one cannot bind.
            work / ("%s-%s.sock" % (terminal.name, name)),
            config_path,
            room / "pymux-server.log",
            room / "pymux-stderr.log",
            fifo_path,
            size_path,
            forwarder_path,
            # The file the relay touches when the fence comes back.
            room / "fence",
        ),
        work,
        room / "pymux.png",
        room / "pymux.log",
        # The relay touches the fence when pymux has done with the
        # keys. A settle before that keeps the screen from before the
        # keys and calls it finished.
        not_before=room / "fence",
    )

    return room / "pymux.png"


def main(fixtures=None, only=None, only_terminals=None, out=None, terminals=None):
    """
    Photograph every fixture, in every terminal.

    The arguments are the knobs of the run, and default to this
    module's own: `photograph_the_themes.py` passes its own fixtures
    and its own knob names, and the same machinery takes the pictures.
    """
    if fixtures is None:
        fixtures = FIXTURES
    if only is None:
        only = ONLY
    if only_terminals is None:
        only_terminals = ONLY_TERMINALS
    if out is None:
        out = PICTURES
    if terminals is None:
        terminals = TERMINALS

    work = Path(os.environ.get("TMPDIR", "/tmp")) / "pymux-chrome"
    work.mkdir(parents=True, exist_ok=True)
    out.mkdir(parents=True, exist_ok=True)

    names = [name for name in sorted(fixtures) if only in name]
    if not names:
        raise SystemExit("no fixture holds %r" % only)

    terminals = [t for t in terminals if only_terminals in t.name]
    if not terminals:
        raise SystemExit("no terminal holds %r" % ONLY_TERMINALS)

    missing = [t.name for t in terminals if not t.is_available()]
    if missing:
        raise SystemExit("these terminals are not here: %s" % ", ".join(missing))

    # The same directory the comparison check needs, and for the same
    # reason: two display servers run here at once and neither can make
    # it. `take_a_picture.py` says why. Lillecarl/pymux#177.
    Path("/tmp/.X11-unix").mkdir(parents=True, exist_ok=True)

    seats = {}
    taken = []
    try:
        for terminal in terminals:
            if terminal.seat not in seats:
                seats[terminal.seat] = SEATS[terminal.seat]().start(work)

        for terminal in terminals:
            for name in names:
                started = time.time()
                try:
                    path = picture_of(
                        terminal, seats[terminal.seat], name, work, out, fixtures
                    )
                except RuntimeError as reason:
                    room = out / terminal.name / name
                    print(
                        "%s %s: no picture (%s)\n%s"
                        % (terminal.name, name, reason, every_log(room)),
                        flush=True,
                    )
                else:
                    taken.append(path)
                    print(
                        "%s %s: %s (%.1fs)"
                        % (
                            terminal.name,
                            name,
                            path.relative_to(out),
                            time.time() - started,
                        ),
                        flush=True,
                    )
    finally:
        for seat in seats.values():
            seat.stop()

    print("")
    print("%d pictures of pymux's chrome, under:" % len(taken))
    print("    %s" % out)
    print("")
    print("Nothing here is judged. Reading them is the work.")

    # A run that photographed nothing at all is a broken run and not an
    # empty one.
    return 0 if taken else 1


if __name__ == "__main__":
    sys.exit(main())
