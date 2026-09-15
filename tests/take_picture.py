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
which xterm does draw, so 208 pixels differ. foot reads the colon form
itself, and there the two pictures are the same. Two seats, one answer:
the difference belongs to xterm.

**An image is the one exception to that rule, and the rule's own
reason says why.** An image has no glyphs. Its pixels are the
program's, and pymux re-encodes them for whichever protocol the client
terminal speaks: the kitty graphics protocol for kitty, sixel for foot.
What reaches the screen ought to be the same picture either way, and no
terminal here speaks both protocols, so the only way to ask is to put
one terminal's picture beside the other's. `two_protocols_of` is that
comparison and it holds the whole of the exception: it runs on
`IMAGE_FIXTURES` and nothing else, it cuts each drawing out of its
background so that where a terminal put it is not a difference, and it
refuses to give a number at all when the two drawings are not the same
size. Lillecarl/pymux#262.

**A pane takes an image in over two protocols as well as writing it
out over two**, so the image fixtures are a matrix: `sixel-image-*` is
a sixel going in and `kitty-image-*` is a kitty transmission going in,
and each terminal is one of the protocols coming out. The number is the
height of the image. A `kitty-image-*` in kitty and a `sixel-image-*`
in foot are the two whose bare side draws something, because there the
terminal already speaks what the program wrote; those two are the
strongest comparisons here.

**Two seats.** xterm speaks X and nothing else, so there is an Xvfb.
foot speaks Wayland and nothing else, so there is a headless `sway`,
which gives its one window the whole output. The Wayland
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

from PIL import Image
from pyte import escape
from pyte.images import PixelFormat
from pyte.sequences import csi
from pyte.modes import PrivateMode
from pyte.sequences import reset_mode

from pyterm_pytest.seats import (
    APPEAR_TIMEOUT,
    BLINK_FRAMES,
    BLINK_GAP,
    BLINK_START,
    SETTLE_TIMEOUT,
    NothingToCompare,
    TheSeatIsGone,
    _tail,
    changed_region,
    differences,
    fully_overlaps,
    open_the_seats,
    the_same_drawing,
    with_no_answer,
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

#: What the pymux side of a picture runs with.
#:
#: Full screen, so the pane covers every cell of the window. And
#: `set-clipboard on`, because the fence a fixture ends in is a pane
#: writing the clipboard: the value pymux ships refuses that, and the
#: settle then waits for a fence that pymux is holding back.
#: Lillecarl/pymux#378.
PICTURE_CONFIG = "set full-screen on\nset set-clipboard on\n"


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


#: The width of every image fixture, in pixels. A pane reserves
#: `ceil(width / cell) x ceil(height / cell)` cells for an image,
#: against the cell size its client reported. 120 is twelve columns of
#: every terminal here, because all three have a ten pixel cell.
IMAGE_WIDTH = 120

#: Two image heights, and what the pair is for.
#:
#: 114 is six rows of the nineteen pixel cell that kitty and foot have,
#: exactly. 120 is six rows of xterm's twenty pixel cell, exactly, and
#: seven rows of nineteen with six pixels left over. So between them the
#: two cover both shapes a reserved box can have: the image filling it
#: and the image leaving slack.
#:
#: **Neither is resampled anywhere any more.** Until
#: Lillecarl/pymux#369 a pane counted every image against a cell size it
#: had invented -- ten by twenty, which only xterm has -- and then the
#: client stretched the image to fill the cells: pymux with `scale_rgba`
#: for sixel, and the terminal itself for kitty, which was handed the
#: image and told the cell box. 120 measured what that cost. Now the
#: pane counts against the cell its client reported and the client draws
#: the image at its own size, so both heights come out exact, and the
#: pair is what says that the slack is left alone as well as the fit.
#:
#: Both are a multiple of six, which is one sixel band.
#:
#: The names say which cell each one fills, because neither fills both
#: and "scaled" and "unscaled" stopped being true of either.
FILLS_A_NINETEEN_PIXEL_CELL = 114
FILLS_A_TWENTY_PIXEL_CELL = 120

#: Where the edges of an image are. Neither is on a cell boundary by
#: accident: the vertical edge at 57 sits inside a ten pixel column, and
#: the horizontal edge at 63 sits inside a band, inside a twenty pixel
#: row and inside a nineteen pixel one. A crop or a scale that is off by
#: one moves one of them and the count changes.
EDGE_X, EDGE_Y = 57, 63

#: The four quadrant colours, as the percentages that sixel carries.
#: **Only six byte values survive the round trip**: 0, 51, 102, 153,
#: 204 and 255, which are 0, 20, 40, 60, 80 and 100 percent. pyte
#: decodes with `round(percent * 255 / 100)` and pymux encodes with
#: `round(byte * 100 / 255)`, so any other byte comes back rounded and
#: every pixel of a faithful re-encode would differ. Then the count
#: would measure the quantisation and nothing else.
#:
#: The kitty fixture carries bytes rather than percentages, and it uses
#: these same four colours for the same reason: it is re-encoded as a
#: sixel for a terminal that has no kitty graphics protocol, and that
#: encoding is exact only inside this set.
IMAGE_COLOURS = [
    (100, 0, 0),  # top left
    (0, 80, 20),  # top right
    (20, 20, 100),  # bottom left
    (100, 80, 0),  # bottom right
]


def as_bytes(percentages):
    "One sixel colour as the bytes pyte decodes it to."
    return bytes(round(value * 255 / 100) for value in percentages)


def _band(left_register, right_register, bits):
    "One sixel band: the left colour to `EDGE_X`, then the right one."
    return "#%d!%d%s#%d!%d%s" % (
        left_register,
        EDGE_X,
        chr(0x3F + bits),
        right_register,
        IMAGE_WIDTH - EDGE_X,
        chr(0x3F + bits),
    )


def sixel_image(fixture, height):
    """
    A sixel image, which a pane re-encodes for the client terminal.

    **This is the one fixture the client terminal does not draw the way
    the program wrote it.** A pane cannot draw pixels, so it decodes the
    sixel into RGBA and `pymux/graphics.py` writes it again in whatever
    the outer terminal speaks: the kitty graphics protocol where there
    is one, sixel where there is not, half blocks where there is
    neither. So the bare picture is the terminal's own sixel decoder and
    the pymux picture is ours, and in kitty they are two different
    protocols drawing one image. Lillecarl/pymux#262.

    **Neither re-encoding loses a pixel, and neither resamples one.**
    The sixel path decodes to RGBA and encodes it again, and the colours
    here survive that exactly; the kitty path does not even do that
    much, because it transmits the image untouched. Neither fits the
    image to the cells it covers: `_put_command` leaves the cell box out
    and `_sixel_for` skips `scale_rgba`, because the pane counted those
    cells against the cell this client really has. Lillecarl/pymux#369
    is where they stopped.

    The bytes are written here and not built with `pymux.sixel`. An
    encoder fault that survives its own decoder would be invisible if
    the encoder made both sides.
    """
    fixture.append(csi(escape.ED, 2) + csi(escape.CUP))

    # "P1=7" is the 1:1 aspect ratio, and the raster attributes say it
    # again: pyte ignores Pan and Pad, and a terminal that honours them
    # would draw the bare side at double height and agree with nothing.
    body = ["\x1bP7;1;0q", '"1;1;%d;%d' % (IMAGE_WIDTH, height)]
    for register, (red, green, blue) in enumerate(IMAGE_COLOURS, start=1):
        body.append("#%d;2;%d;%d;%d" % (register, red, green, blue))

    bands = []
    for band in range(0, height, 6):
        if band + 6 <= EDGE_Y:
            bands.append(_band(1, 2, 0b111111))
        elif band >= EDGE_Y:
            bands.append(_band(3, 4, 0b111111))
        else:
            # The band the horizontal edge falls inside. The top colours
            # light the rows above it, "$" returns to the left of the
            # same band, and the bottom colours light the rows below.
            above = (1 << (EDGE_Y - band)) - 1
            bands.append(_band(1, 2, above) + "$" + _band(3, 4, 0b111111 & ~above))

    # No "-" after the last band: it would move a real terminal's cursor
    # down one band more than the image is tall.
    body.append("-".join(bands))
    body.append("\x1b\\")
    fixture.append("".join(body))


#: How much base64 one kitty transmission carries. The protocol caps a
#: payload at 4096 characters, so the image goes in chunks: "m=1" on
#: every message but the last. `GraphicsState._assemble` joins them, and
#: so does the real kitty that draws the bare side.
KITTY_CHUNK = 4096


def kitty_rgb(height):
    "The same four quadrants as `sixel_image`, as raw RGB bytes."
    top = as_bytes(IMAGE_COLOURS[0]) * EDGE_X + as_bytes(IMAGE_COLOURS[1]) * (
        IMAGE_WIDTH - EDGE_X
    )
    bottom = as_bytes(IMAGE_COLOURS[2]) * EDGE_X + as_bytes(IMAGE_COLOURS[3]) * (
        IMAGE_WIDTH - EDGE_X
    )
    return top * EDGE_Y + bottom * (height - EDGE_Y)


def kitty_image(fixture, height):
    """
    The same image over the kitty graphics protocol, which is the other
    direction through pymux.

    `sixel_image` measures sixel in and both protocols out. This
    measures kitty in and both protocols out, and that is the half of
    the matrix nothing photographed before: every image fixture wrote a
    sixel, so the sixel decoder was always the first thing in the chain.
    Lillecarl/pymux#262.

    **It is the only image fixture whose bare side is not empty in
    kitty.** kitty speaks this protocol itself, so the bare picture is
    kitty drawing the program's own transmission and the pymux picture
    is kitty drawing pymux's re-transmission of it. Same terminal, same
    protocol, same pixels: the one image comparison here with no second
    decoder anywhere in it.

    **The colours are `IMAGE_COLOURS` and that is what makes the other
    direction exact too.** foot has no kitty graphics protocol, so pymux
    encodes the image as a sixel for it, and a sixel carries a channel
    as a percentage. Only 101 of 256 byte values survive that, which is
    why the transmitted bytes are taken from the six that do.

    The bytes are written here and not built with `pymux.graphics`, for
    the reason `sixel_image` gives: an encoder fault that survives its
    own decoder would be invisible if the encoder made both sides.
    """
    fixture.append(csi(escape.ED, 2) + csi(escape.CUP))

    payload = base64.b64encode(kitty_rgb(height)).decode("ascii")
    chunks = [
        payload[at : at + KITTY_CHUNK] for at in range(0, len(payload), KITTY_CHUNK)
    ]
    for index, chunk in enumerate(chunks):
        more = 1 if index < len(chunks) - 1 else 0
        if index == 0:
            # No "i" and no "I": a fixture here writes no query, and
            # `GraphicsState.handle` answers nothing that carries
            # neither. "q=2" asks a real kitty for the same silence.
            # "C=1" keeps the image from moving the cursor, so the two
            # sides cannot disagree about where it ended up.
            control = "a=T,f=%i,s=%i,v=%i,C=1,q=2,m=%i" % (
                PixelFormat.RGB,
                IMAGE_WIDTH,
                height,
                more,
            )
        else:
            control = "m=%i" % more
        fixture.append("\x1b_G%s;%s\x1b\\" % (control, chunk))


#: Every image fixture: the function that writes it and the height of
#: the image it writes. These are the only fixtures that may be compared
#: across two terminals, and `two_protocols_of` says why.
#:
#: The four of them are a two by two matrix. The protocol going in is
#: the fixture, the protocol coming out is the terminal, and the height
#: says whether the image fills the cells reserved for it or leaves
#: slack in them.
#:
#: **The height is in the name and not a word for it.** Two of these
#: were `-unscaled` while the other two were resampled; that stopped
#: being true, and a name that has to be explained is worse than a
#: number that cannot go stale.
IMAGE_FIXTURES = {
    "sixel-image-120": (sixel_image, FILLS_A_TWENTY_PIXEL_CELL),
    "sixel-image-114": (sixel_image, FILLS_A_NINETEEN_PIXEL_CELL),
    "kitty-image-120": (kitty_image, FILLS_A_TWENTY_PIXEL_CELL),
    "kitty-image-114": (kitty_image, FILLS_A_NINETEEN_PIXEL_CELL),
}


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
FIXTURES.update(
    {
        name: partial(writer, height=height)
        for name, (writer, height) in IMAGE_FIXTURES.items()
    }
)


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


def check_size_of(recording):
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
        check_size_of(recording)
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

    def asked(self, **extra):
        """
        The same terminal, with more asked of its command line.

        The argv it carries already holds its colours -- a light
        terminal is the same program with a background -- so this adds
        to that rather than building a new one.
        """
        return Terminal(
            self.name,
            self.program,
            partial(self._argv, **extra),
            seat=self.seat,
            window_class=self.window_class,
            environment=self.environment,
        )


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


def foot_argv(command, background="000000", foreground="ffffff", blink=True):
    """
    foot, a terminal that speaks Wayland and no X at all.

    The compositor gives it the whole output, so there is no geometry
    to ask for. Everything that could move a pixel is turned off, the
    same way as for xterm.

    **`blink=False` is for a run that keeps one picture instead of
    subtracting two.** A settle waits for two pictures in a row to be
    the same, and a cursor that blinks for ever makes sure they never
    are. `kitty_argv` says the rest; foot needed the same knob, and
    without it the chrome farm lost most of its pictures.
    Lillecarl/pymux#338, Lillecarl/pymux#362.
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
        "--override=cursor.blink=%s" % ("yes" if blink else "no",),
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


def kitty_argv(command, background="#000000", foreground="#ffffff", blink=True):
    """
    kitty, which is the terminal the faults get reported from.

    `--config NONE` because the configuration of whoever runs this must
    not reach a comparison. Everything that could move a pixel is then
    named here, the same way as for the other two.

    `cursor_stop_blinking_after=0` is the one that is not about pixels.
    kitty stops blinking the cursor after fifteen seconds with no key
    pressed, and nobody presses a key here.

    **`blink=False` is for a run that keeps one picture instead of
    subtracting two.** A settle waits for two pictures in a row to be
    the same, and a cursor that blinks for ever makes sure they never
    are: a chrome fixture that leaves a cursor on the screen cannot
    settle in kitty at all. The comparison run wants the blink,
    because one of its fixtures measures it, so the default stays.
    Lillecarl/pymux#338.
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
        "cursor_blink_interval=%s" % ("0.5" if blink else "0",),
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
#: `tests/photograph_themes.py` takes both lists.
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


def write_program(path, fixture_path, payload):
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
    write_program(program_path, fixture_path, payload)

    config_path = work / "full-screen.conf"
    config_path.write_text(PICTURE_CONFIG)

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
            not_before=token if seat.reads_fence else 0.0,
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
            not_before=token if seat.reads_fence else 0.0,
        )
    except RuntimeError as reason:
        raise RuntimeError("%s\n%s" % (reason, every_log(room, seat))) from None

    return differences(bare, through, room / "difference.png")


#: The two terminals that draw one image two ways, and the name the
#: comparison of them is recorded under.
#:
#: kitty is the only one here that speaks the kitty graphics protocol,
#: and foot the only one that draws a sixel of its own. **No terminal
#: speaks both**, which is why this comparison crosses two of them:
#: WezTerm's sixel is "preliminary and incomplete" and it documents no
#: kitty graphics, Ghostty has the kitty protocol and no sixel, Contour
#: has sixel and no kitty protocol. Lillecarl/pymux#262.
THE_TWO_PROTOCOLS = ("kitty", "foot")
BOTH_PROTOCOLS = "%s-against-%s" % THE_TWO_PROTOCOLS


def two_protocols_of(name, out):
    """
    One image, drawn by pymux down each of its two graphics paths, and
    how many pixels of it the two terminals put on the screen
    differently.

    **This is the one comparison that crosses two terminals**, and the
    docstring at the top of this file says why the others must not.
    Each terminal draws its own glyphs from its own font stack, so a
    difference between two of them says nothing about pymux. An image
    has no glyphs. The pixels are the program's, pymux re-encodes them
    for whichever protocol the terminal speaks, and what reaches the
    screen ought to be the same picture either way. That is what
    Lillecarl/pymux#262 asks for and what nothing else here can answer:
    `tests/test_both_graphics_paths_draw_the_same.py` compares what
    pymux *asks* for, with no terminal in it at all.

    Where each terminal puts the image is not a difference: a
    compositor gives foot the whole output and kitty centres its cells
    in it, so the two boxes are at different corners. `the_same_drawing`
    cuts each drawing out of its background first.

    The size of the image *is* a difference, and it stops the
    comparison rather than colouring it. Measured 2026-09-14: both
    terminals have a ten by nineteen cell for DejaVu Sans Mono at size
    12, so both draw the pane's twelve by six cells as 120 by 114
    pixels. If a font moves one of them there is nothing to line up,
    and those are the numbers to compare the complaint against.
    """
    room = out / BOTH_PROTOCOLS / name
    room.mkdir(parents=True, exist_ok=True)

    first, second = THE_TWO_PROTOCOLS
    drawn = {side: out / side / name / "pymux.png" for side in THE_TWO_PROTOCOLS}
    difference = room / "difference.png"
    count, first_box, second_box = the_same_drawing(
        drawn[first], drawn[second], difference
    )

    for side, box in ((first, first_box), (second, second_box)):
        leave_it_where_it_can_be_seen(drawn[side], box, room / ("%s.png" % side))
    # The difference is already cut to the drawing, and it is the one
    # that most needs the size: what it marks here is a single row.
    leave_it_where_it_can_be_seen(difference, None, difference)
    return count, first_box, second_box


#: How much bigger the cut out drawing is written. An image fixture is
#: a hundred pixels or so inside a screenshot of a whole output, and
#: nobody can see one at that size.
BIG_ENOUGH_TO_SEE = 4


def leave_it_where_it_can_be_seen(path, box, into):
    """
    The drawing alone, enlarged, beside the pictures it came from.
    `box` is the part to keep, or `None` for the whole picture.

    **A count is not a picture.** Every other fixture of this farm draws
    over the whole screen, so the screenshots are worth opening on their
    own. An image fixture is a hundred pixels in the corner of a 1024 by
    768 output, and two of those that agree look exactly like two that
    do not. This is what a person opens.

    Nearest neighbour, because a smooth enlargement would invent the
    very thing this comparison is about: the row where one path
    interpolates and the other does not.
    """
    drawing = Image.open(path).convert("RGB")
    if box is not None:
        drawing = drawing.crop(box)
    drawing.resize(
        (drawing.width * BIG_ENOUGH_TO_SEE, drawing.height * BIG_ENOUGH_TO_SEE),
        Image.NEAREST,
    ).save(into)


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
    config_path.write_text(PICTURE_CONFIG)

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


def read_recorded():
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


def write_recorded(path, found):
    "The list of differences that a run saw, ready to be recorded."
    lines = [
        "# Every difference between a picture with pymux and one without.",
        "# `tests/take_picture.py` says what this is and how to write it.",
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

    standing = read_recorded()
    seen = {}
    blinks = {}

    # Where an X server puts the socket of its display.
    #
    # **Two display servers run here at once**, because a terminal
    # needs X or Wayland and this check has both kinds: Xvfb for
    # xterm, and sway, which brings Xwayland with it. A build sandbox
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
    try:
        seats = open_the_seats(terminals, work)
    except TheSeatIsGone as reason:
        return with_no_answer(str(reason))

    try:
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
    except Exception as reason:
        # **Ask the seat before believing the fixture.** A display
        # server that went away makes every picture after it fail, and
        # the terminal's complaint names the display and not the cause.
        # A run that cannot draw has no answer to give.
        # Lillecarl/pymux#216.
        try:
            seats[terminal.seat].still_there()
        except TheSeatIsGone as gone:
            return with_no_answer(str(gone))
        raise reason
    finally:
        for seat in seats.values():
            seat.stop()

    # One image, the two protocols pymux writes it in, and two real
    # terminals drawing them. `two_protocols_of` says why this one
    # comparison may cross two terminals when no other may.
    ran = {terminal.name for terminal in terminals}
    both_ran = ran.issuperset(THE_TWO_PROTOCOLS)
    cannot = []
    for name in names:
        if name not in IMAGE_FIXTURES:
            continue
        if not both_ran:
            # **Say so rather than pass over it.** A run that kept
            # quiet here would write a list with no comparison in it,
            # and the recorded answer would go the next time somebody
            # copied that list over the recorded one.
            cannot.append(
                "%s %s: this needs %s and %s, and only %s ran"
                % (
                    BOTH_PROTOCOLS,
                    name,
                    THE_TWO_PROTOCOLS[0],
                    THE_TWO_PROTOCOLS[1],
                    ", ".join(sorted(ran)),
                )
            )
            continue
        try:
            count, first_box, second_box = two_protocols_of(name, out)
        except NothingToCompare as reason:
            cannot.append("%s %s: %s" % (BOTH_PROTOCOLS, name, reason))
            continue
        seen[(BOTH_PROTOCOLS, name)] = count
        where = " and ".join(
            "%s at %d,%d" % (side, box[0], box[1])
            for side, box in zip(THE_TWO_PROTOCOLS, (first_box, second_box))
        )
        print(
            "%s %s: %d pixels differ. Both draw it %dx%d, %s"
            % (
                BOTH_PROTOCOLS,
                name,
                count,
                first_box[2] - first_box[0],
                first_box[3] - first_box[1],
                where,
            ),
            flush=True,
        )

    # Keep the list that this run saw, beside the pictures, whatever the
    # verdict is. The run of this check does not fail because a picture
    # differed, so what it leaves is there to read either way.
    write_recorded(out / "picture-differences.txt", seen)

    # Judge the run against the list. A difference either way matters:
    # one that grew is a regression, and one that went is a fix that
    # nobody wrote down.
    # A comparison that could not be made is a fault of its own. It is
    # not a picture that differs, it is the instrument saying it has
    # nothing to measure, and a run that passed over it in silence
    # would be a check that quietly stopped checking.
    wrong = list(cannot)

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
