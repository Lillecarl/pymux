"""
Photograph what pymux draws around a pane, in a real terminal.

`take_picture.py` subtracts two pictures of the same program, one
bare and one in a pymux pane. It writes `set full-screen on` before
every fixture, which is right for what it asks -- anything pymux drew
around the pane would count as a difference on every fixture -- and it
means **no check has ever photographed pymux's own chrome**: the status
line, a pane title bar, the command palette, the overlay pane.

Nothing could, either. A headless compositor owns no input device and
the terminal's pty belongs to the terminal, so nothing could press a
key at pymux to open any of it. `drive_in_terminal.py` is the way
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

**This does not judge what the chrome draws.** It keeps a picture of
each fixture for a person to read, the way `photograph_vttest.py`
does. A picture of chrome has nothing to subtract: there is no bare
side, because the chrome is the thing pymux adds. Judging it needs a
recorded image, and recording one before anybody has looked at it
would record whatever it does today, faults and all. So this is not a
gate, and reading the pictures is the work.

**It does judge the state.** Each fixture says how many panes it ends
with, what is drawn over the active pane, and whether the client still
holds the prefix. The server is asked before the picture is kept, so a
run whose split never landed leaves no picture. The fence cannot say
any of this -- it proves pymux finished the keys, not that a key
arrived. Lillecarl/pymux#353, Lillecarl/pymux#363.

    PYMUX_CHROME=palette nix build --file . checks.pymux-chrome-pictures

`PYMUX_CHROME` narrows the run to the fixtures whose name holds that
text, and `PYMUX_CHROME_TERMINALS` to the terminals whose name does.
`PYMUX_CHROME_LIST` and `PYMUX_CHROME_TERMINALS_LIST` name them
exactly, comma separated, and beat the substrings: three fixtures that
share no substring are what a probe of the judge asks for.
"""

import os
import shlex
import sys
import time
from collections import Counter
from functools import partial
from pathlib import Path
from typing import NamedTuple

# `tests/`, for the harness beside this file, and the directory above
# it, for `pymux` itself. Running a script puts the script's own
# directory on the path and not the one it was started from.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(1, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pyterm_pytest.seats import SEATS  # noqa: E402

from middleman import FORWARDER, run_cli  # noqa: E402
from take_picture import (  # noqa: E402
    HOLD,
    TERMINALS,
    every_log,
)

#: The terminals whose cursor blinks for ever, and the argv that asks
#: each one to hold still.
#:
#: This run keeps one picture of each fixture; it subtracts nothing,
#: so it has no use for a blinking cursor, and a settle waits for two
#: pictures in a row to be the same. Both are configured to blink for
#: ever, because `take_picture.py` has a fixture that measures the
#: blink.
#:
#: kitty was the first to show it: `chooser-search`, the first chrome
#: fixture to leave a cursor on the screen, never settled there and
#: took no picture at all (Lillecarl/pymux#338). foot was the same
#: fault and it cost far more, because every chrome fixture leaves a
#: cursor somewhere: 16 of 20 pictures lost in one measured run.
#: Lillecarl/pymux#362.
STILL = ("kitty", "foot")


def holding_still(terminals):
    "The same terminals, with every cursor asked to hold still."
    return [
        one.asked(blink=False) if one.program in STILL else one
        for one in terminals
    ]


CHROME_TERMINALS = holding_still(TERMINALS)

#: Where the pictures go. The check points this at `$out`.
PICTURES = Path(os.environ.get("PYMUX_CHROME_OUT", "chrome-pictures"))

#: Which fixtures and which terminals to run, as a piece of a name.
ONLY = os.environ.get("PYMUX_CHROME", "")
ONLY_TERMINALS = os.environ.get("PYMUX_CHROME_TERMINALS", "")


def exact_list(env):
    """
    An exact list of names from the environment, or None.

    The gallery builds in pieces, one derivation per terminal and per
    batch of themes, and a piece names what it holds exactly: a
    substring would run a theme in every combo that holds a piece of
    its name. Lillecarl/pymux#284.

    A person reaches for it too, and for the other reason: a probe of
    the judge wants `which-key,clock,copy-mode`, three fixtures that
    share no substring. Lillecarl/pymux#365.
    """
    value = os.environ.get(env, "")
    names = [one for one in value.split(",") if one]
    return names or None

#: The relay, beside this file.
RELAY = Path(__file__).parent / "drive_in_terminal.py"

#: The demo, beside this file: the program a fixture types at a
#: pane, the way the theme pictures put it there. A pane that has run
#: it holds rows a person can read, which is what the copy-mode
#: fixture needs.
DEMO = Path(__file__).parent / "demo_application.py"

#: What every fixture turns on. The status line and a pane's title bar
#: are the two pieces of chrome a person looks at all day, and both are
#: what `full-screen on` takes away.
#:
#: **`test-mode` pins the clock** to 13:37 on the 14th of March.
#: `Pymux.displayed_now` is where every clock a person reads goes
#: through, and the status line draws one by default: a picture taken
#: either side of a second is a different picture, for a reason no
#: fixture chose. `photograph_themes.py` takes one of these for every
#: theme, so the race was paid for on every one of them.
#:
#: **`paint-screen` gives the theme the whole screen.** A pane's own
#: cells carry the scheme's background instead of the terminal's, so a
#: picture shows what a theme does rather than what it does around the
#: edges. That is the point of photographing a theme at all.
#: `photograph_themes.py` set it on two fixtures by hand before this,
#: which is the same thing said once.
CHROME = (
    "set-option status on\n"
    "set-option pane-border-status on\n"
    "set-option test-mode on\n"
    "set-option paint-screen on\n"
)

#: The prefix, and the keys that reach a binding behind it. `rc.py` is
#: where these are bound: `"` splits a pane in two, top and bottom, and
#: `%` splits it left and right.
PREFIX = b"\x02"

def keys(*steps):
    """
    A keys file from (seconds to wait, bytes) pairs.

    The first wait is counted from pymux's first frame, which the
    relay waits for. The rest are counted from the step before them.
    `drive_in_terminal.py` says the format.
    """
    return "".join("%s %r\n" % (delay, one) for delay, one in steps)


class Fixture(NamedTuple):
    """
    One picture: the configuration pymux reads, the keys pressed at
    it, and the arrangement those keys ask for.

    `panes` is how many panes each window holds, window 0 first. It is
    what the fence cannot prove. **The fence says pymux finished the
    keys, never that a key arrived**: it travels through the forwarder
    pane, which is alive whether or not a split happened, so a run
    whose `%` reached nothing comes back fenced, settles on a still
    screen and keeps a picture of one pane. One run of
    `cut-follows-the-terminal` did exactly that, and its server log has
    no second process in it. Lillecarl/pymux#353.

    An overlay pane is not counted. It belongs to the session and not
    to a window's arrangement, so `list-panes` never lists it.

    `mode` is what the keys draw over the active pane, and `prefix` is
    whether they leave the client waiting for the key after the prefix.
    Both default to the quiet answer, so **every fixture is judged on
    them**: a run that opened copy mode where none was asked for goes
    red too. Lillecarl/pymux#363.

    Only the two modes tmux names belong in `mode`. pymux draws more
    over a pane than tmux does -- the chooser, the command palette, the
    overlay -- and nothing has named those yet.
    """

    config: str
    keys: str = ""
    panes: tuple = (1,)
    mode: str = ""
    prefix: bool = False


def count_panes(listing):
    """
    How many panes each window holds, window 0 first, from the lines
    of `list-panes -a -F "#{window_index}"`.

    One line per pane, holding the number of the window it is in. The
    numbers are sorted as numbers: a server with ten windows lists
    "10" before "2" otherwise.
    """
    counted = Counter(listing.split())
    return tuple(counted[index] for index in sorted(counted, key=int))


def answers_to(socket_path, argv):
    """
    What the server says, one line per thing it listed.

    The server answers over its own socket. An integrated server
    refuses an attach and answers a command, and this is a command.
    Lillecarl/pymux#159.
    """
    done = run_cli(socket_path, argv)
    if done.returncode != 0:
        raise RuntimeError(
            "%s exited %d: %s"
            % (
                argv[0],
                done.returncode,
                done.stderr.decode("utf-8", "replace").strip(),
            )
        )
    return done.stdout.decode("utf-8", "replace").splitlines()


def panes_now(socket_path):
    "How many panes each window of this server holds, window 0 first."
    return count_panes(
        " ".join(
            answers_to(socket_path, ["list-panes", "-a", "-F", "#{window_index}"])
        )
    )


def mode_now(socket_path):
    """
    What is drawn over the active pane of the window in view, or "".

    The active pane, and not any pane: a fixture that opens copy mode
    opens it on the pane that took the keyboard, which is the one the
    split made and never the forwarder. `list-panes` with no `-a` lists
    that window alone, so there is exactly one active pane to read.
    """
    lines = answers_to(
        socket_path, ["list-panes", "-F", "#{pane_active}\t#{pane_mode}"]
    )
    active = [line.split("\t", 1)[1] for line in lines if line.startswith("1\t")]
    if len(active) != 1:
        raise RuntimeError("the window has %d active panes: %r" % (len(active), lines))
    return active[0]


def prefix_now(socket_path):
    "Whether a client is waiting for the key after the prefix."
    lines = answers_to(socket_path, ["list-clients", "-F", "#{client_prefix}"])
    if not lines:
        raise RuntimeError("the server has no client to ask about the prefix")
    return "1" in lines


def judge_the_fixture(socket_path, fixture):
    "Raise unless the server holds the state the keys asked for."
    found = panes_now(socket_path)
    if found != tuple(fixture.panes):
        raise RuntimeError(
            "the keys ask for %r panes per window and the server holds %r"
            % (tuple(fixture.panes), found)
        )

    mode = mode_now(socket_path)
    if mode != fixture.mode:
        raise RuntimeError(
            "the keys ask for %r over the active pane and the server draws %r"
            % (fixture.mode, mode)
        )

    prefix = prefix_now(socket_path)
    if prefix != fixture.prefix:
        raise RuntimeError(
            "the keys ask for the prefix held=%r and the client holds it=%r"
            % (fixture.prefix, prefix)
        )


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


def demo_keys():
    """
    Split the window in two, and run the demo in the pane that took
    the keyboard.

    The command is typed, so the pane shows a shell that received it
    and then the program that answered, which is what a pane looks
    like in use rather than at rest. **The split is also what keeps
    the fence alive**: the first pane stays the forwarder, and the
    fence needs it -- a pane whose program is suspended carries
    nothing back, and copy mode suspends the one it opens on.
    """
    return keys(
        (0.0, PREFIX),
        (0.4, b"%"),
        (0.8, ("python %s\n" % (shlex.quote(str(DEMO)),)).encode("ascii")),
    )


#: What `demo_keys` builds, for a fixture's `panes`. It lives beside
#: the keys so the two stay in step.
DEMO_PANES = (2,)


#: What each fixture is. `Fixture` says what the three parts are.
FIXTURES = {
    # The plainest one. One pane, and everything pymux draws around it.
    "bare": Fixture(CHROME),
    # Two panes side by side, so a focused title bar and an unfocused
    # one are both in the picture, in their two colours.
    "two-panes": Fixture(CHROME, keys((0.0, PREFIX), (0.4, b"%")), (2,)),
    # A pane over a pane, which is the other border and the other
    # arrangement of the two title bars.
    "stacked-panes": Fixture(CHROME, keys((0.0, PREFIX), (0.4, b'"')), (2,)),
    # The command line as a bar along the bottom, which is what pymux
    # draws by default. It is left open, so the picture holds it.
    "command-line": Fixture(
        CHROME,
        keys((0.0, PREFIX), (0.4, b":"), (0.4, b"list-panes")),
    ),
    # And as a box in the middle of the screen. Lillecarl/pymux#158
    # built it, and a picture of it is what found four faults in it.
    "command-palette": Fixture(
        CHROME + "set-option command-palette on\n",
        keys((0.0, PREFIX), (0.4, b":"), (0.4, b"list-panes")),
    ),
    # The box that composes a key, with a modifier written and the keys
    # a keyboard leaves out under it. Lillecarl/pymux#220.
    "compose-a-key": Fixture(
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
    "divided": Fixture(
        CHROME,
        keys(
            (0.0, PREFIX),
            (0.4, b"%"),
            (0.6, PREFIX),
            (0.4, b'"'),
        ),
        (3,),
    ),
    # One pane filling the window, with the other two behind it. Zoom
    # is a layout that wraps the layout underneath, so the pane keeps
    # the row its title bar hangs in and the bar carries the "Z".
    # Lillecarl/pymux#215.
    # Zoom is a flag on the window and not a layout that drops the
    # others, so both panes are still in the arrangement.
    "zoomed": Fixture(
        CHROME,
        keys((0.0, PREFIX), (0.4, b"%"), *create_command("resize-pane -Z")),
        (2,),
    ),
    # A strip whose second column is two thirds of the window, with the
    # focus on the first. The pair does not fit, so the second one runs
    # off the right edge and is tinted: **the one case where what a
    # person sees is not what the program wrote**, and the only thing
    # on the screen that says so. Lillecarl/pymux#222.
    #
    # The `strip` fixture below cuts nothing. Half a window each means
    # two columns fit exactly, which is why a half is the default.
    "strip-cut": Fixture(
        CHROME + "set-window-option -g strip on\n",
        keys(
            (0.0, PREFIX),
            (0.4, b"%"),
            *create_command("switch-column-width"),
            *create_command("select-pane -L"),
        ),
        (2,),
    ),
    # A strip of three columns, which runs past the edge of the screen.
    # Lillecarl/pymux#198.
    "strip": Fixture(
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
        (3,),
    ),
    # The pane numbers, which `display-panes` puts up for a moment.
    "pane-numbers": Fixture(
        CHROME,
        keys(
            (0.0, PREFIX),
            (0.4, b"%"),
            (0.6, PREFIX),
            (0.4, b"q"),
        ),
        (2,),
    ),
    # The clock, which a pane draws over itself.
    "clock": Fixture(
        CHROME,
        keys((0.0, PREFIX), (0.4, b"t")),
        mode="clock-mode",
    ),
    # The keys a prefix leads to, while the prefix waits. The popup
    # draws on the view, diagonally opposite the cursor: the pane runs
    # a script that printed nothing, so the cursor sits at the top
    # left and the box belongs at the bottom right. The empty step
    # after the prefix is a wait, so the frame the prefix asked for is
    # on the screen before the fence comes back.
    # Lillecarl/pymux#29.
    "which-key": Fixture(
        CHROME + "set-option which-key on\n",
        keys((0.0, PREFIX), (0.8, b"")),
        prefix=True,
    ),
    # The window chooser: a bar across the top, over the window it
    # points at. It is not a box, and the preview is the switch itself
    # -- the rows under the bar are the real window, drawn by the
    # layout that owns it. Lillecarl/pymux#325, Lillecarl/pymux#326,
    # Lillecarl/pymux#327.
    #
    # Three windows, so the bar has a list to wrap, and the one it
    # opens on is split in two, so the preview is a layout and not an
    # empty shell. **`-d` leaves the new windows unfocused**, which
    # keeps the client on window 0: that is where the forwarder pane
    # runs, and the fence comes back through it.
    "chooser": Fixture(
        CHROME,
        keys(
            (0.0, PREFIX),
            (0.4, b"%"),
            *create_command("new-window -d -n logs"),
            *create_command("new-window -d -n build"),
            (0.8, PREFIX),
            (0.6, b"w"),
        ),
        (2, 1, 1),
    ),
    # The same bar with a search typed into it, which narrows the list
    # and points at what is left. Pointing switches, so this is also
    # the picture of a preview of a window the client was not on.
    "chooser-search": Fixture(
        CHROME,
        keys(
            (0.0, PREFIX),
            (0.4, b"%"),
            *create_command("new-window -d -n logs"),
            *create_command("new-window -d -n build"),
            (0.8, PREFIX),
            (0.6, b"w"),
            (0.6, b"/"),
            (0.8, b"bui"),
        ),
        (2, 1, 1),
    ),
    # A question waiting for an answer. `prefix x` is
    # `confirm-before -p "kill-pane #P?" kill-pane`, so the prompt is
    # also the one piece of chrome that expands a format string into
    # itself. Two panes, so the pane it names is not the only one and
    # the number in the question means something.
    "confirm": Fixture(
        CHROME,
        keys((0.0, PREFIX), (0.4, b"%"), (0.8, PREFIX), (0.6, b"x")),
        (2,),
    ),
    # The demo in a pane, and copy mode over it. The two hold the
    # same rows a person can read, and the difference between the two
    # pictures is what copy mode does.
    "pane-text": Fixture(CHROME, demo_keys(), (2,)),
    "copy-mode": Fixture(
        CHROME,
        demo_keys() + keys((1.2, PREFIX), (0.6, b"[")),
        (2,),
        mode="copy-mode",
    ),
    # An overlay pane, floating in the middle of the screen over two
    # panes. Its body runs a program, so its default-background cells
    # have to show the terminal's own background, the way a normal
    # pane does: a rule that named a colour behind it drew a slab of
    # chrome over the layout instead. Lillecarl/pymux#223.
    # The overlay itself is not among the counted panes: it belongs to
    # the session and not to a window's arrangement.
    "overlay": Fixture(
        CHROME,
        keys(
            (0.0, PREFIX),
            (0.4, b"%"),
            (0.6, PREFIX),
            (0.4, b"g"),
        ),
        (2,),
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
    "startup-errors": Fixture(
        CHROME + "not-a-command-alpha\nnot-a-command-beta\n",
    ),
}


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
    # xterm hands its children the invoking user's login shell in
    # $SHELL, whatever this harness was started with, and in the build
    # sandbox that passwd shell is /noshell: every pane died at exec
    # and the keys typed after it went nowhere. The shell the harness
    # itself carries is the one pymux gives a pane.
    shell = os.environ.get("SHELL")
    inside = (
        (
            "export SHELL=%s\n" % shlex.quote(shell)
            if shell is not None
            else ""
        )
        + "exec python3 -m pymux -S %s -f %s --log %s"
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
    fixture = fixtures[name]
    config_path = work / ("%s.conf" % name)
    config_path.write_text(fixture.config)

    keys_path = work / ("%s.keys" % name)
    keys_path.write_text(fixture.keys)

    # The name of the terminal is in it: every terminal runs every
    # fixture, and a socket a run before left behind is a socket this
    # one cannot bind.
    socket_path = work / ("%s-%s.sock" % (terminal.name, name))

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
            socket_path,
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
        # And the fence cannot say a key arrived, so the server is
        # asked what the keys built. Lillecarl/pymux#353.
        judge=partial(judge_the_fixture, socket_path, fixture),
    )

    return room / "pymux.png"


def main(
    fixtures=None,
    only=None,
    only_terminals=None,
    out=None,
    terminals=None,
    only_list=None,
    only_terminals_list=None,
):
    """
    Photograph every fixture, in every terminal.

    The arguments are the knobs of the run, and default to this
    module's own: `photograph_themes.py` passes its own fixtures
    and its own knob names, and the same machinery takes the pictures.

    `only_list` and `only_terminals_list` are exact names, and beat
    the substrings: a derivation that builds one terminal and a batch
    of themes needs to name them exactly, where a person typing a
    knob narrows with a piece of a name.
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
        terminals = CHROME_TERMINALS

    work = Path(os.environ.get("TMPDIR", "/tmp")) / "pymux-chrome"
    work.mkdir(parents=True, exist_ok=True)
    out.mkdir(parents=True, exist_ok=True)

    if only_list is not None:
        wanted = set(only_list)
        names = [name for name in sorted(fixtures) if name in wanted]
        if not names:
            raise SystemExit("no fixture is one of %r" % sorted(wanted))
    else:
        names = [name for name in sorted(fixtures) if only in name]
        if not names:
            raise SystemExit("no fixture holds %r" % only)

    if only_terminals_list is not None:
        wanted = set(only_terminals_list)
        terminals = [t for t in terminals if t.name in wanted]
        if not terminals:
            raise SystemExit("no terminal is one of %r" % sorted(wanted))
    else:
        terminals = [t for t in terminals if only_terminals in t.name]
        if not terminals:
            raise SystemExit("no terminal holds %r" % ONLY_TERMINALS)

    missing = [t.name for t in terminals if not t.is_available()]
    if missing:
        raise SystemExit("these terminals are not here: %s" % ", ".join(missing))

    # The same directory the comparison check needs, and for the same
    # reason: two display servers run here at once and neither can make
    # it. `take_picture.py` says why. Lillecarl/pymux#177.
    Path("/tmp/.X11-unix").mkdir(parents=True, exist_ok=True)

    seats = {}
    taken = []
    lost = []
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
                    lost.append("%s %s" % (terminal.name, name))
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
    # Only the arrangement is judged, and a picture of the wrong one is
    # not kept. What the chrome looks like is still for a person.
    print("What these draw is not judged. Reading them is the work.")
    if lost:
        print("")
        print("%d took no picture:" % len(lost))
        for one in lost:
            print("    %s" % one)

    # A run that photographed nothing at all is a broken run and not an
    # empty one.
    return 0 if taken else 1


if __name__ == "__main__":
    sys.exit(
        main(
            only_list=exact_list("PYMUX_CHROME_LIST"),
            only_terminals_list=exact_list("PYMUX_CHROME_TERMINALS_LIST"),
        )
    )
