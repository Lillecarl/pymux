"""
Take a picture of a real terminal, with pymux in it and without it.

Every other check in this collection stops at the cell. It says what
the cell holds. It does not say what a real terminal paints when pymux
writes that cell out again, and two bugs got past every one of them
that way: a cursor stopped blinking, and an underline appeared where
none belonged.

So this runs the same program twice in the same terminal emulator, on
a display server of its own:

* bare, the program straight in the terminal;
* through pymux, in a pane that covers every cell.

Then it takes a picture of each window and subtracts one from the
other. A difference is a bug in pymux, or a deviation that somebody
chose. The harness does not say which. It says that there is one,
which nothing else here does.

**Two terminals are not compared against each other.** Each one draws
its own glyphs from its own font stack, so a difference between two
terminals says nothing. What the pair says is whether pymux changes
what a terminal draws, and that answer is worth having from more than
one terminal, because a terminal can be wrong on its own.

The underline fixture is what that is for. xterm draws no underline at
all for "CSI 4:1 m", and a pane turns the same request into "CSI 4 m",
which xterm does draw, so 206 pixels differ. foot reads the colon form
itself, and there the two pictures are the same. Two seats, one answer:
the difference belongs to xterm.

**Two seats.** xterm speaks X and nothing else, so there is an Xvfb.
foot speaks Wayland and nothing else, so there is a `cage`, a kiosk
compositor that gives its one window the whole output. The Wayland
seat is the better shape for this work: one window, no decoration, no
window to find, and `grim` takes the output. The X seat has to find
its window among the ones that ran before it.

Not every difference is a fault. A pane reads what a program asked for
and writes the request again in the form the terminal of the user
understands, so a pane can draw more than that terminal draws on its
own. `tests/picture-differences.txt` records each difference that
stands and says why, and the check judges a run against that list. A
difference in either direction fails, so a regression and a fix are
both visible.

The result of the check is a directory. Every run leaves its pictures
in it:

    $out/<terminal>/<fixture>/bare.png
    $out/<terminal>/<fixture>/pymux.png
    $out/<terminal>/<fixture>/difference.png

Run with:

    nix build --file . checks.pymux-pictures
    PYMUX_PICTURES=underlines nix build --file . checks.pymux-pictures

`PYMUX_PICTURES` narrows the run to the fixtures whose name holds that
text.

**A fixture can be a recording of a real program.** Drop one in
`tests/recordings/` and it becomes a fixture called `recorded-<name>`.
That is how a program whose output depends on the machine it runs on
gets in here: it is recorded once, outside the sandbox, and the bytes
are replayed on both sides. `tests/recordings/README.md` says how to
make one and what to watch for.

Every run keeps its pictures and writes the list of differences it saw,
whatever the verdict, because the run of a check does not fail when the
suite fails. So looking at a difference and recording one are the same
command:

    nix build --file . checks.pymux-pictures.run
    cp result/picture-differences.txt pymux/tests/picture-differences.txt
"""

import base64
import json
import os
import shutil
import subprocess
import sys
import time
from functools import partial
from pathlib import Path
from pyte import escape
from pyte.sequences import csi
from pyte.modes import PrivateMode
from pyte.sequences import reset_mode

from pyterm_pytest.seats import (
    APPEAR_TIMEOUT,
    BLINK_FRAMES,
    BLINK_GAP,
    BLINK_START,
    SEATS,
    SETTLE_TIMEOUT,
    _tail,
    changed_region,
    differences,
    fully_overlaps,
)

REPO_ROOT = Path(__file__).parent.parent

#: Where the pictures go. The check points this at `$out`.
PICTURES = Path(os.environ.get("PYMUX_PICTURES_OUT", "pictures"))

#: Which fixtures to run. Empty means all of them.
ONLY = os.environ.get("PYMUX_PICTURES", "")

#: The differences that stand. Not every difference is a fault: a pane
#: draws what a program asked for, and a terminal that cannot parse the
#: request draws less on its own. Each line is a terminal, a fixture
#: and how many pixels differ, and a comment says why.
#:
#: A run is judged against this list and a difference in either
#: direction fails, so a regression and a fix are both visible.
RECORDED = Path(__file__).parent / "picture-differences.txt"

#: The size of the terminal, in cells. Both runs use it, and the pane
#: covers all of it.
ROWS, COLUMNS = 24, 80

#: How long the program holds the window open after it has drawn. A
#: terminal closes its window when its program ends, and the picture is
#: taken after the program ran, so it has to still be there.
#:
#: It is a bound and not a parking space. The two waits above are the
#: longest this can need, and anything left behind by a run that went
#: wrong is gone in a minute rather than an hour.
HOLD = APPEAR_TIMEOUT + SETTLE_TIMEOUT + 10


# ----------------------------------------------------------------------
# The fixtures. Each one is the bytes that a program wrote.
#
# A fixture here writes no query. A query is answered into the tty, and
# a tty that echoes puts the answer on the screen as text. The program
# both sides run turns the echo off for that reason, so a recording,
# which does hold queries, is safe as well.


def sgr(fixture):
    "Colours and attributes, on a screen that never scrolls."
    lines = [
        csi(escape.ED, 2) + csi(escape.CUP),
        (
            csi(escape.SGR, 1)
            + "bold"
            + csi(escape.SGR, 22)
            + " "
            + csi(escape.SGR, 3)
            + "italic"
            + csi(escape.SGR, 23)
            + " "
            + csi(escape.SGR, 4)
            + "underline"
            + csi(escape.SGR, 24)
            + "\r\n"
        ),
        (
            csi(escape.SGR, 7)
            + "reverse"
            + csi(escape.SGR, 27)
            + " "
            + csi(escape.SGR, 9)
            + "struck"
            + csi(escape.SGR, 29)
            + " "
            + csi(escape.SGR, 2)
            + "faint"
            + csi(escape.SGR, 22)
            + "\r\n"
        ),
    ]
    for number in range(8):
        lines.append("\x1b[3%dm%d\x1b[39m " % (number, number))
    lines.append("\r\n")
    for number in range(8):
        lines.append("\x1b[4%dm %d \x1b[49m" % (number, number))
    lines.append("\r\n")
    lines.append(
        csi(escape.SGR, 38, 5, 208)
        + "256"
        + csi(escape.SGR, 39)
        + " "
        + csi(escape.SGR, 38, 2, 30, 170, 90)
        + "truecolour"
        + csi(escape.SGR, 39)
        + "\r\n"
    )
    lines.append(
        csi(escape.SGR, 48, 2, 40, 40, 90)
        + csi(escape.SGR, 38, 2, 250, 250, 120)
        + " on a colour "
        + csi(escape.SGR, 0)
        + "\r\n"
    )
    fixture.extend(lines)


def underlines(fixture):
    "The shapes of an underline, and the colour of one."
    fixture.append(csi(escape.ED, 2) + csi(escape.CUP))
    for shape, name in enumerate(
        ["none", "straight", "double", "curly", "dotted", "dashed"]
    ):
        fixture.append("\x1b[4:%dm%s\x1b[4:0m\r\n" % (shape, name))
    fixture.append("\x1b[4:3m\x1b[58:2::255:0:0mred curly\x1b[59m\x1b[4:0m\r\n")
    fixture.append("\x1b[4:1m\x1b[58:5:33mblue straight\x1b[59m\x1b[4:0m\r\n")


def wide_characters(fixture):
    "Characters that take two cells, next to ones that take one."
    fixture.append(csi(escape.ED, 2) + csi(escape.CUP))
    fixture.append("abc 你好漢 def\r\n")
    fixture.append("ＡＢ ｶﾅ ghi\r\n")
    fixture.append("äéñ straight after\r\n")


def box_drawing(fixture):
    "The line drawing set, which fills every edge of a cell."
    fixture.append(csi(escape.ED, 2) + csi(escape.CUP))
    fixture.append("┌" + "─" * 20 + "┐\r\n")
    for _ in range(3):
        fixture.append("│" + " " * 20 + "│\r\n")
    fixture.append("└" + "─" * 20 + "┘\r\n")


#: Each fixture is a name and the function that writes it. The cursor
#: is hidden first and shown again at the end, in one place, so that a
#: still picture does not depend on where a blink was in its cycle.
#: The fixtures that are about the cursor say so and turn that off.
FIXTURES = {
    "sgr": sgr,
    "underlines": underlines,
    "wide-characters": wide_characters,
    "box-drawing": box_drawing,
}


# ----------------------------------------------------------------------
# The fixtures that are about time.
#
# Every fixture above is a still picture, and each one hides the cursor
# in its first byte so that it can be one. A cursor is the opposite: it
# is only itself when it changes, so it is measured by taking several
# pictures and asking whether they differ.
#
# These are programs and not streams of bytes, because what they measure
# is what happens while something keeps drawing.


#: The fixture draws once and then holds the screen. The only thing
#: that can change a pixel after that is the cursor. A redraw loop
#: stood here once, for a burst taken on the clock, and it is gone by
#: measurement: the compositor damages on every frame the terminal
#: submits, whether a pixel differs or not, so the loop's rewrites
#: spent the burst's frames on pictures of nothing -- a burst of eight
#: covered eight tenths of a second and held one blink inside it.
BLINK_FIXTURES = {
    "cursor-blink": (
        "printf '\\033[2J\\033[H'\n"
        "printf 'the cursor is after this: '\n"
        "printf '\\033[10;1Hx'\n"
        "sleep 5\n"
    ),
}


def blink_program(name):
    "The program of one blink fixture."
    return BLINK_FIXTURES[name]


# ----------------------------------------------------------------------
# The recordings.
#
# A fixture above is bytes that somebody wrote by hand, and it can only
# hold what that person thought to write. A recording holds what a real
# program really drew, on a real machine, with the configuration of the
# person who runs it. That is the only way to get a program like Claude
# Code in here: what it draws depends on a login, a theme and a project,
# none of which exist in a build sandbox.
#
# `tests/recordings/README.md` says how to make one.


#: Where a recording lives. `<name>.bin` is what the program wrote.
RECORDINGS = Path(__file__).parent / "recordings"


def recorded_fixtures():
    "Every recording there is, as {fixture name: the .bin file}."
    if not RECORDINGS.is_dir():
        return {}
    return {
        "recorded-%s" % path.stem: path for path in sorted(RECORDINGS.glob("*.bin"))
    }


RECORDED_FIXTURES = recorded_fixtures()


def check_the_size_of(recording):
    """
    A recording is only worth replaying at the size it was made at.

    The recorder writes the size it used beside the bytes. A recording
    made at another size draws its own idea of where the edges are, and
    the two pictures would then differ for a reason that is nobody's
    fault.
    """
    beside = recording.with_suffix(".reads.json")
    if not beside.exists():
        return
    made = json.loads(beside.read_text())
    if (made.get("lines"), made.get("columns")) != (ROWS, COLUMNS):
        raise SystemExit(
            "%s was recorded at %sx%s and this harness is %dx%d.\n"
            "Record it again with --lines %d --columns %d."
            % (
                recording.name,
                made.get("lines"),
                made.get("columns"),
                ROWS,
                COLUMNS,
                ROWS,
                COLUMNS,
            )
        )


def every_fixture():
    "The name of every fixture, written by hand or recorded."
    return list(FIXTURES) + list(RECORDED_FIXTURES)


def fixture_bytes(name):
    "The bytes of one fixture, with the cursor put out of the way."
    recording = RECORDED_FIXTURES.get(name)
    if recording is not None:
        check_the_size_of(recording)
        # The hide goes on both ends. Before, because a recording takes
        # a while to reach its own, and the two runs do not reach it at
        # the same moment. After, because a program often gives the
        # cursor back when it ends, and a still picture cannot hold one
        # that blinks.
        return b"\x1b[?25l" + recording.read_bytes() + b"\x1b[?25l"

    pieces = [
        reset_mode(PrivateMode.SHOW_CURSOR)
    ]  # No cursor: it is not what this measures.
    FIXTURES[name](pieces)
    return "".join(pieces).encode("utf-8")


# ----------------------------------------------------------------------
# The terminals.


class Terminal:
    """
    One terminal emulator, and how to run a shell command in it.

    `argv` gets the command and gives back the whole command line.

    `seat` names the display server this terminal needs. xterm speaks
    X and nothing else; foot speaks Wayland and nothing else; the rest
    speak both, and each one is listed under the seat it is native on.

    `window_class` is how the X seat finds the window. A Wayland seat
    does not need it: the compositor there holds one window and the
    whole output is that window.
    """

    def __init__(
        self,
        name,
        program,
        argv,
        seat="x",
        window_class="",
        environment=None,
    ):
        self.name = name
        self.program = program
        self.seat = seat
        self.window_class = window_class
        self._argv = argv
        self.environment = environment or {}

    def is_available(self):
        return shutil.which(self.program) is not None

    def argv(self, command):
        return self._argv(command)


def xterm_argv(command, background="black", foreground="white"):
    """
    xterm, with everything that could move a pixel turned off.

    No scrollbar, no border and no internal border, so the window is
    exactly the cells. A fixed font at a fixed size, because the
    default depends on what the machine has installed.
    """
    return [
        "xterm",
        "-geometry",
        "%dx%d+0+0" % (COLUMNS, ROWS),
        "-fa",
        "DejaVu Sans Mono",
        "-fs",
        "12",
        "-bg",
        background,
        "-fg",
        foreground,
        # As for foot: a cursor that never blinks cannot be measured.
        "-bc",
        "-b",
        "0",  # No internal border.
        "-bw",
        "0",  # No window border.
        "+sb",  # No scrollbar.
        "-xrm",
        "xterm*cursorBlink: false",
        "-xrm",
        # The fixture ends in an OSC 52, and xterm drops the clipboard
        # escape while this is off: no clipboard, no fence.
        "xterm*allowWindowOps: true",
        "-e",
        "sh",
        "-c",
        command,
    ]


def foot_argv(command, background="000000", foreground="ffffff"):
    """
    foot, a terminal that speaks Wayland and no X at all.

    The compositor gives it the whole output, so there is no geometry
    to ask for. Everything that could move a pixel is turned off, the
    same way as for xterm.
    """
    return [
        "foot",
        "--font=DejaVu Sans Mono:size=12",
        # `colors` is the older name of this section, and foot writes a
        # line on its own screen saying so. Anything a terminal writes
        # there before the program runs is one more thing that has to
        # come out the same on both sides.
        "--override=colors-dark.background=%s" % (background,),
        "--override=colors-dark.foreground=%s" % (foreground,),
        # A cursor that never blinks cannot be measured. Every fixture
        # but the blink ones hides it, so this changes nothing for them.
        #
        # A terminal stops blinking when its window loses focus. The
        # seat's keyboard holder gives the window focus, but a blink
        # that stops when the holder dies would still differ between
        # the sides, so the cursor of an unfocused window is asked to
        # stay as it is.
        "--override=cursor.blink=yes",
        "--override=cursor.unfocused-style=unchanged",
        # The fence a fixture ends in is an OSC 52 write, and foot
        # gates it behind this without the option. The focus itself is
        # the holder's doing; the seat waits for it.
        "--override=security.osc52=enabled",
        "--override=main.pad=0x0",
        "--override=scrollback.lines=0",
        "sh",
        "-c",
        command,
    ]


def kitty_argv(command, background="#000000", foreground="#ffffff"):
    """
    kitty, which is the terminal the faults get reported from.

    `--config NONE` because the configuration of whoever runs this must
    not reach a comparison. Everything that could move a pixel is then
    named here, the same way as for the other two.

    `cursor_stop_blinking_after=0` is the one that is not about pixels.
    kitty stops blinking the cursor after fifteen seconds with no key
    pressed, and nobody presses a key here.
    """
    return [
        "kitty",
        "--config",
        "NONE",
        "-o",
        "font_family=DejaVu Sans Mono",
        "-o",
        "font_size=12",
        "-o",
        "background=%s" % (background,),
        "-o",
        "foreground=%s" % (foreground,),
        "-o",
        "window_padding_width=0",
        "-o",
        "scrollback_lines=0",
        "-o",
        "shell_integration=no",
        "-o",
        "cursor_blink_interval=0.5",
        "-o",
        "cursor_stop_blinking_after=0",
        "sh",
        "-c",
        command,
    ]


TERMINALS = [
    Terminal("xterm", "xterm", xterm_argv, seat="x", window_class="XTerm"),
    Terminal("foot", "foot", foot_argv, seat="wayland"),
    Terminal(
        "kitty",
        "kitty",
        kitty_argv,
        seat="wayland",
        # kitty draws with OpenGL, and a build sandbox has no graphics
        # card. llvmpipe is what draws instead.
        environment={"LIBGL_ALWAYS_SOFTWARE": "1"},
    ),
]

#: The same three on a light background. The pictures of every theme
#: run them beside the dark ones, because a theme that read well on
#: the black it was written on may be unreadable on white, and the
#: light schemes of pygments want a light terminal to be read on.
#: `tests/photograph_the_themes.py` takes both lists.
LIGHT_TERMINALS = [
    Terminal(
        "xterm-light",
        "xterm",
        partial(xterm_argv, background="white", foreground="black"),
        seat="x",
        window_class="XTerm",
    ),
    Terminal(
        "foot-light",
        "foot",
        partial(foot_argv, background="ffffff", foreground="000000"),
        seat="wayland",
    ),
    Terminal(
        "kitty-light",
        "kitty",
        partial(kitty_argv, background="#ffffff", foreground="#000000"),
        seat="wayland",
        environment={"LIBGL_ALWAYS_SOFTWARE": "1"},
    ),
]


# ----------------------------------------------------------------------
# The two runs.


def write_the_program(path, fixture_path, payload):
    """
    The program that both runs execute, as a shell script.

    It is a file and not a command line so that the two sides run the
    same bytes. It also keeps the quoting out of the way: a pane takes
    its command as text, and `Pymux._create_pane` splits that text on
    whitespace (Lillecarl/pymux#39).

    `stty -echo` because a tty that echoes puts the answer to a query
    on the screen as text, and the two sides answer differently.
    `sleep` because a terminal closes its window when the program ends,
    and the picture is taken after the program ran. `HOLD` says how
    long, and it is a bound: a run that goes wrong leaves nothing behind
    for longer than that.

    The clear is what makes the two sides start alike. A terminal may
    write on its own screen before the program runs: foot says which of
    its settings are deprecated, and another one will say something
    else. pymux paints every cell, so that message is gone on one side
    and stays on the other, and everything below it sits one row lower.
    Every fixture written by hand clears the screen itself; a recording
    of a program that does not is what found this.

    The last thing it writes is the fence: an OSC 52, the clipboard
    escape, carrying a payload that is this run's alone. The escape
    draws nothing, and the clipboard holds what the terminal decoded
    it to only when the outermost terminal has acted on the bytes --
    the bare side straight, the pane side through pymux -- which is
    the evidence the settle waits for. A settle that starts before the
    bytes are written can settle on the screen from before them, both
    sides alike, agreeing on nothing. Lillecarl/pymux#281.
    """
    path.write_text(
        "stty -echo\n"
        "printf '\\033[2J\\033[H'\n"
        "cat %s\n"
        # The wait before the fence: foot's window is focused when the
        # compositor sends the keyboard enter, which is after the
        # surface maps, and this shell starts before any of that. The
        # escape foot sees while it is unfocused is refused, and the
        # clipboard holds nothing. Measured: a fence without the wait
        # never comes, one after a second always has.
        "sleep 1\n"
        "printf '\\033]52;c;%s\\007'\n"
        "exec sleep %d\n" % (fixture_path, payload, HOLD)
    )


def bare_command(program_path):
    "The program, straight in the terminal."
    return "exec sh %s" % program_path


def pymux_command(program_path, socket_path, config_path, log_path, error_path):
    """
    The same program, in a pane that covers every cell.

    The integrated mode holds the server and the client in one process,
    so the picture is of the pymux that this check built, and not of a
    server that was already running.

    Its own stderr goes to a file. It shares a terminal with the client
    in this mode, so anything it writes there lands in the picture
    (Lillecarl/pymux#36), and a traceback would be lost in it.
    """
    # No "--" before the command: the mode word takes what follows it
    # as the program of the first pane, and a "--" reaches the pane as
    # the first word of that program (Lillecarl/pymux#41).
    return "exec python3 -m pymux -S %s -f %s --log %s integrated sh %s 2>%s" % (
        socket_path,
        config_path,
        log_path,
        program_path,
        error_path,
    )


def every_log(room, seat=None):
    """
    The end of every log in this room, for a run that could not finish.

    The seat's own log is not in the room -- one server serves every
    picture, so its log lives beside the run -- and it is the one that
    says why a terminal could not open a display. So a seat that has
    died puts its reason in front of the rest.
    """
    logs = [_tail(path) for path in sorted(room.glob("*.log")) if path.stat().st_size]

    trouble = seat.trouble() if seat is not None else ""
    if trouble:
        logs.insert(0, trouble)

    return "\n".join(logs)


def compare_one(terminal, seat, name, work, out):
    "Run one fixture both ways, and say how many pixels differ."
    room = out / terminal.name / name
    room.mkdir(parents=True, exist_ok=True)

    fixture_path = work / ("%s.bin" % name)
    fixture_path.write_bytes(fixture_bytes(name))

    # The fence. The escape carries base64, and the clipboard holds
    # what the terminal decoded it to: readable bytes, this run's
    # alone, so a fence a run before left in the clipboard cannot
    # stand in for this one.
    token = os.urandom(8).hex()
    payload = base64.b64encode(token.encode()).decode()

    program_path = work / ("%s.sh" % name)
    write_the_program(program_path, fixture_path, payload)

    config_path = work / "full-screen.conf"
    config_path.write_text("set full-screen on\n")

    bare = room / "bare.png"
    through = room / "pymux.png"
    try:
        seat.picture_of(
            terminal,
            bare_command(program_path),
            work,
            bare,
            room / "bare.log",
            # The terminal has the bytes when its clipboard holds the
            # token; a settle before that can settle on the screen from
            # before them, both sides alike, agreeing on nothing. Both
            # seats read one now; the guard stays for a seat that
            # cannot yet.
            not_before=token if seat.reads_the_fence else 0.0,
        )
        seat.picture_of(
            terminal,
            pymux_command(
                program_path,
                # The name of the terminal is in it: every terminal
                # runs every fixture, and a socket that a run before
                # left behind is a socket that this one cannot bind.
                work / ("%s-%s.sock" % (terminal.name, name)),
                config_path,
                room / "pymux-server.log",
                room / "pymux-stderr.log",
            ),
            work,
            through,
            room / "pymux.log",
            not_before=token if seat.reads_the_fence else 0.0,
        )
    except RuntimeError as reason:
        raise RuntimeError("%s\n%s" % (reason, every_log(room, seat))) from None

    return differences(bare, through, room / "difference.png")


def blink_of(terminal, seat, name, work, out):
    """
    Run one blink fixture both ways and say how the cursor moved.

    The fixture writes the same cell over and over, so the only thing
    that can differ between two pictures is the cursor. The frames
    come from the seat's burst: a seat that hears the screen takes one
    at every change, and one that cannot samples the clock. Either
    way the answer is a run of changes between frames that stay the
    same cell -- the cursor going and coming. Two in a row is a blink
    out and back; one change alone says nothing, and zero says the
    cursor held still.

    What matters is that the two sides agree. A terminal whose cursor
    never blinks says zero twice, which is an answer and not a fault.
    """
    room = out / terminal.name / name
    room.mkdir(parents=True, exist_ok=True)

    program_path = work / ("%s.sh" % name)
    program_path.write_text(blink_program(name))

    config_path = work / "full-screen.conf"
    config_path.write_text("set full-screen on\n")

    answers = []
    for side, command in (
        ("bare", bare_command(program_path)),
        (
            "pymux",
            pymux_command(
                program_path,
                work / ("%s-%s.sock" % (terminal.name, name)),
                config_path,
                room / "pymux-server.log",
                room / "pymux-stderr.log",
            ),
        ),
    ):
        try:
            shots = seat.burst_frames(
                terminal,
                command,
                work,
                room / ("%s.png" % side),
                room / ("%s.log" % side),
                BLINK_FRAMES,
            )
        except RuntimeError as reason:
            raise RuntimeError("%s\n%s" % (reason, every_log(room, seat))) from None

        # A change counts when it is the cursor: the same cell, again
        # and again. Anything else that moved makes its own box, and a
        # run of boxes that do not hold one another is more than a
        # cursor at work.
        boxes = []
        for number, (first, second) in enumerate(zip(shots, shots[1:])):
            count, box = changed_region(
                first, second, room / ("%s-diff-%d.png" % (side, number))
            )
            if box is not None:
                boxes.append(box)
        run = longest = 0
        for first, second in zip(boxes, boxes[1:]):
            run = run + 1 if fully_overlaps(first, second) else 0
            longest = max(longest, run)
        answers.append(longest)

    return tuple(answers)


def read_the_recorded():
    "The differences that stand, as {(terminal, fixture): pixels}."
    if not RECORDED.exists():
        return {}
    standing = {}
    for line in RECORDED.read_text().splitlines():
        line = line.split("#")[0].strip()
        if not line:
            continue
        terminal, fixture, pixels = line.split()
        standing[(terminal, fixture)] = int(pixels)
    return standing


def write_the_recorded(path, found):
    "The list of differences that a run saw, ready to be recorded."
    lines = [
        "# Every difference between a picture with pymux and one without.",
        "# `tests/take_a_picture.py` says what this is and how to write it.",
        "",
    ]
    for (terminal, fixture), pixels in sorted(found.items()):
        if pixels:
            lines.append("%s %s %d" % (terminal, fixture, pixels))
    path.write_text("\n".join(lines) + "\n")


def main():
    work = Path(os.environ.get("TMPDIR", "/tmp")) / "pymux-pictures"
    work.mkdir(parents=True, exist_ok=True)
    out = PICTURES
    out.mkdir(parents=True, exist_ok=True)

    names = [name for name in every_fixture() if ONLY in name]
    blink_names = [name for name in BLINK_FIXTURES if ONLY in name]
    if not names and not blink_names:
        raise SystemExit("no fixture holds %r" % ONLY)

    # A terminal that is not here is a hole in the check, not a
    # detail. The build brings every one of them in, so a missing one
    # says the inputs changed.
    missing = [t.name for t in TERMINALS if not t.is_available()]
    if missing:
        raise SystemExit("these terminals are not here: %s" % ", ".join(missing))
    terminals = list(TERMINALS)

    standing = read_the_recorded()
    seen = {}
    blinks = {}

    # Where an X server puts the socket of its display.
    #
    # **Two display servers run here at once**, because a terminal
    # needs X or Wayland and this check has both kinds: Xvfb for
    # xterm, and cage, which brings Xwayland with it. A build sandbox
    # has no such directory and a server cannot make one --
    # "_XSERVTransmkdir: ERROR: euid != 0" -- so each server falls
    # back to the abstract socket alone, neither can see that the
    # other took display zero, and xterm connects to whichever
    # answers: "xterm: Xt error: Can't open display: :0".
    #
    # With the directory there, the first server writes X0 into it and
    # the second one sees it and takes the next number.
    # Lillecarl/pymux#177.
    Path("/tmp/.X11-unix").mkdir(parents=True, exist_ok=True)

    # One seat for each kind of display server that a terminal here
    # needs, and none for a kind that nothing needs.
    seats = {}
    try:
        for terminal in terminals:
            if terminal.seat not in seats:
                seats[terminal.seat] = SEATS[terminal.seat]().start(work)

        for terminal in terminals:
            for name in names:
                found = compare_one(terminal, seats[terminal.seat], name, work, out)
                seen[(terminal.name, name)] = found
                print(
                    "%s %s: %d pixels differ" % (terminal.name, name, found),
                    flush=True,
                )

        # The fixtures that are about time. These are not counted in
        # pixels, so they are judged on their own: a cursor has to
        # behave the same with pymux as without it.
        for terminal in terminals:
            for name in blink_names:
                bare, through = blink_of(
                    terminal, seats[terminal.seat], name, work, out
                )
                blinks[(terminal.name, name)] = (bare, through)
                print(
                    "%s %s: the cursor changed one cell %d times bare, "
                    "%d with pymux" % (terminal.name, name, bare, through),
                    flush=True,
                )
    finally:
        for seat in seats.values():
            seat.stop()

    # Keep the list that this run saw, beside the pictures, whatever the
    # verdict is. The run of this check does not fail because a picture
    # differed, so what it leaves is there to read either way.
    write_the_recorded(out / "picture-differences.txt", seen)

    # Judge the run against the list. A difference either way matters:
    # one that grew is a regression, and one that went is a fix that
    # nobody wrote down.
    wrong = []
    for key, found in sorted(seen.items()):
        expected = standing.get(key, 0)
        if found != expected:
            wrong.append(
                "%s %s: %d pixels differ, %d were recorded"
                % (key[0], key[1], found, expected)
            )

    # A cursor has to behave the same in a pane as without one. How
    # often a terminal blinks is the terminal's business, and one that
    # does not blink at all says zero on both sides, which is an
    # answer and not a fault.
    for key, (bare, through) in sorted(blinks.items()):
        if bare >= 2 and through < 2:
            wrong.append("%s %s: the cursor blinks bare and not in a pane" % key)
        elif through >= 2 and bare < 2:
            wrong.append("%s %s: the cursor blinks in a pane and not bare" % key)

    # "The same on both sides" is satisfied by a cursor that never
    # blinks anywhere, so on its own it would pass while measuring
    # nothing. One terminal has to blink, or the comparison above said
    # nothing at all: kitty's cursor blinks on its own clock, and
    # nothing here needs a window's focus to see it.
    if blinks and not any(bare >= 2 for bare, _ in blinks.values()):
        wrong.append(
            "no terminal blinked a cursor, so nothing was compared. "
            "A blink fixture needs a terminal that blinks one: see "
            "`cursor_stop_blinking_after` and whether the window has "
            "the focus."
        )

    if wrong:
        print("\n--- pymux draws something else ---", file=sys.stderr)
        for line in wrong:
            print(line, file=sys.stderr)
        print(
            "\nLook at difference.png, and if the difference is right, take\n"
            "the list this run wrote and say why in a comment:\n"
            "    nix build --file . checks.pymux-pictures.run\n"
            "    cp result/picture-differences.txt "
            "pymux/tests/picture-differences.txt",
            file=sys.stderr,
        )
        raise SystemExit(1)

    print("Every picture is what it was recorded to be.")


if __name__ == "__main__":
    main()
