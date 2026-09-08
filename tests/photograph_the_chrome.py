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
from take_a_picture import (  # noqa: E402
    HOLD,
    SEATS,
    TERMINALS,
    every_log,
)

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

#: How long after the relay starts the first key is pressed. The
#: terminal has to be up and pymux has to have drawn once, and a key
#: pressed into a pymux that is still starting reaches nothing.
FIRST_KEY = 3.0


def keys(*steps):
    """
    A keys file from (seconds to wait, bytes) pairs.

    The first wait is `FIRST_KEY`, and the rest are counted from the
    step before them. `drive_in_a_terminal.py` says the format.
    """
    return "".join("%s %r\n" % (delay, one) for delay, one in steps)


def a_command(text):
    """
    The keys that run one pymux command from the command line.

    A window option cannot be set from a configuration file: that file
    is read before the first window is made, and a window option
    belongs to a window. Lillecarl/pymux#199.
    """
    return [
        (FIRST_KEY, PREFIX),
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
    "two-panes": (CHROME, keys((FIRST_KEY, PREFIX), (0.4, b"%"))),
    # A pane over a pane, which is the other border and the other
    # arrangement of the two title bars.
    "stacked-panes": (CHROME, keys((FIRST_KEY, PREFIX), (0.4, b'"'))),
    # The command line as a bar along the bottom, which is what pymux
    # draws by default. It is left open, so the picture holds it.
    "command-line": (
        CHROME,
        keys((FIRST_KEY, PREFIX), (0.4, b":"), (0.4, b"list-panes")),
    ),
    # And as a box in the middle of the screen. Lillecarl/pymux#158
    # built it, and a picture of it is what found four faults in it.
    "command-palette": (
        CHROME + "set-option command-palette on\n",
        keys((FIRST_KEY, PREFIX), (0.4, b":"), (0.4, b"list-panes")),
    ),
    # A strip of three columns, which runs past the edge of the screen.
    # The column on the right is cut off, and that is the point of it.
    # Lillecarl/pymux#198.
    "strip": (
        CHROME,
        keys(
            *a_command("set-window-option strip on"),
            (0.6, PREFIX),
            (0.4, b"%"),
            (0.6, PREFIX),
            (0.4, b"%"),
        ),
    ),
    # The pane numbers, which `display-panes` puts up for a moment.
    "pane-numbers": (
        CHROME,
        keys(
            (FIRST_KEY, PREFIX),
            (0.4, b"%"),
            (0.6, PREFIX),
            (0.4, b"q"),
        ),
    ),
    # The clock, which a pane draws over itself.
    "clock": (
        CHROME,
        keys((FIRST_KEY, PREFIX), (0.4, b"t")),
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
        keys((FIRST_KEY, PREFIX), (0.4, b"%")),
    )


def every_fixture():
    return sorted(FIXTURES)


def chrome_command(keys_path, socket_path, config_path, log_path, error_path):
    """
    The command the terminal runs: the relay, and pymux under it.

    pymux runs through `sh -c` so that its own stderr goes to a file.
    It shares a terminal with the client in the integrated mode, so
    anything it writes there lands in the picture
    (Lillecarl/pymux#36), and the relay would copy a traceback onto
    the screen along with everything else.

    The integrated mode holds the server and the client in one
    process, so the picture is of the pymux this check built and not of
    a server that was already running.
    """
    inside = "exec python3 -m pymux -S %s -f %s --log %s integrated sleep %d 2>%s" % (
        shlex.quote(str(socket_path)),
        shlex.quote(str(config_path)),
        shlex.quote(str(log_path)),
        HOLD,
        shlex.quote(str(error_path)),
    )
    return "exec python3 %s %s %d -- sh -c %s" % (
        shlex.quote(str(RELAY)),
        shlex.quote(str(keys_path)),
        HOLD,
        shlex.quote(inside),
    )


def last_key_at(keys):
    "When the last key of a script is pressed, in seconds from the start."
    when = 0.0
    for line in keys.splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            when += float(line.split(None, 1)[0])
    return when


def picture_of(terminal, seat, name, work, out):
    "One fixture, in one terminal, left as a picture."
    room = out / terminal.name / name
    room.mkdir(parents=True, exist_ok=True)

    config, keys = FIXTURES[name]

    config_path = work / ("%s.conf" % name)
    config_path.write_text(config)

    keys_path = work / ("%s.keys" % name)
    keys_path.write_text(keys)

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
        ),
        work,
        room / "pymux.png",
        room / "pymux.log",
        # Nothing moves while pymux waits for a key, so a settle would
        # keep the screen from before the keys and call it finished.
        not_before=last_key_at(keys) + 1.0,
    )

    return room / "pymux.png"


def main():
    work = Path(os.environ.get("TMPDIR", "/tmp")) / "pymux-chrome"
    work.mkdir(parents=True, exist_ok=True)
    out = PICTURES
    out.mkdir(parents=True, exist_ok=True)

    names = [name for name in every_fixture() if ONLY in name]
    if not names:
        raise SystemExit("no fixture holds %r" % ONLY)

    terminals = [t for t in TERMINALS if ONLY_TERMINALS in t.name]
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
                    path = picture_of(terminal, seats[terminal.seat], name, work, out)
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
