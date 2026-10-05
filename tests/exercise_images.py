"""
Exercise an image through a move and two resizes, and photograph each step.

Every image check here draws once and holds still. An image a program
really shows does not: yazi draws its preview, draws it somewhere else
when the selection moves, and draws it again when the terminal changes
size. Graphics are write-once bytes and not cells, so every one of
those steps asks pymux to retain the image and write it out again --
and nothing here has ever asked whether it does.

So this runs one scenario in kitty, twice, the way
`tests/take_picture.py` runs every fixture twice:

* bare, the emitter straight in the terminal;
* through pymux, in a pane that covers every cell.

and takes a picture after each of four steps: drawn, moved, smaller,
restored. A difference at any step is pymux drawing something else
than the terminal draws on its own.

**kitty is the only terminal here, and that is the point.** It
authored the graphics protocol, so what it draws bare is what the
bytes mean; foot draws sixel and xterm draws neither. The emitter
speaks kitty graphics in both runs, the same bytes both times.

The steps are the ones a preview lives through. `drawn` puts the
image at the top, the way a preview first appears. `moved` deletes it
and puts it lower, the way a new selection redraws: an `a=d` for the
old image and a fresh transmission under the same id, because that is
what yazi writes. `smaller` shrinks the kitty window itself, through
the compositor, so both sides take a real resize: kitty reconfigures,
the client takes SIGWINCH, and the server reflows the pane. `restored`
puts the window back to the whole output.

The window is resized and not the pane, because the bare side has no
pane to resize. A `resize-pane` key would reach an empty binding bare
and a layout change in a pane, and the two sides would stop running
the same scenario. The outer resize reaches both sides through the
same configure event instead, and what differs after it is the
reflow: kitty's own against the pane's.

**The resize goes through `swaymsg`, which the seat never promised.**
`pyterm_pytest.seats` gives a director `take_one` and `ended`, and
nothing else: no handle on the compositor, no IPC. The socket of this
run's compositor is beside its display in `seat._where`, and the
director finds it there. If the seat ever stops keeping it there,
this is the place that breaks.

The move is fenced and the resizes are not. The emitter writes an
OSC 52 after each transmission, and the seat reads it out of the
compositor's clipboard, so a picture after it is a picture of those
bytes drawn. A resize writes nothing new: the settle watches the
pixels stop instead, which is what it is for.

The result of the check is a directory. Every run leaves its
pictures in it:

    $out/kitty/<side>/drawn.png, moved.png, smaller.png, restored.png
    $out/kitty/difference-<step>.png

Run with:

    nix build --file . checks.pymux-image-exercise
    PYMUX_IMAGE_EXERCISE=move nix build --file . checks.pymux-image-exercise

`PYMUX_IMAGE_EXERCISE` narrows the run to the scenarios whose name
holds that text. There is one scenario.

`tests/exercise-differences.txt` records each difference that stands
and says why, and the check judges a run against that list. A
difference in either direction fails, so a regression and a fix are
both visible. Looking at a difference and recording one are the same
command:

    nix build --file . checks.pymux-image-exercise.run
    cp result/exercise-differences.txt pymux/tests/exercise-differences.txt
"""

from __future__ import annotations

import base64
import contextlib
import os
import subprocess
import sys
import time
from pathlib import Path

from pyte import escape
from pyte.images import PixelFormat
from pyte.sequences import csi
from pyterm_pytest.seats import (
    TheSeatIsGone,
    _settle,
    differences,
    open_the_seats,
    with_no_answer,
)
from take_picture import (
    IMAGE_WIDTH,
    KITTY_CHUNK,
    PICTURE_CONFIG,
    TERMINALS,
    bare_command,
    every_log,
    kitty_rgb,
    pymux_command,
)

#: Where the pictures go. The check points this at `$out`.
PICTURES = Path(os.environ.get("PYMUX_IMAGE_EXERCISE_OUT", "exercise-pictures"))

#: Which scenarios run. Empty means all of them.
ONLY = os.environ.get("PYMUX_IMAGE_EXERCISE", "")

#: The differences that stand. Each line is a terminal, a step and how
#: many pixels differ, and a comment says why.
RECORDED = Path(__file__).parent / "exercise-differences.txt"

#: The scenario. The image is the 120 by 114 of the picture farm: six
#: rows of kitty's nineteen pixel cell, exactly, so neither side has
#: anything to resample.
SCENARIO = "kitty-image-move-resize"

#: The height of the image, in pixels.
IMAGE_HEIGHT = 114

#: The row the image moves to. Twenty plus six rows of image is still
#: inside the small window's thirty-one rows.
MOVE_TO_ROW = 20

#: The small window, in pixels. Eighty columns of kitty's ten pixel
#: cell, and thirty-one rows of its nineteen pixel one.
SMALL_WIDTH, SMALL_HEIGHT = 800, 600

#: How long the emitter holds the screen. It has to cover four
#: settles and two resizes, and it is a bound: a run that goes wrong
#: leaves nothing behind for longer than this.
HOLD = 180

#: The image id both transmissions carry. The move deletes this id
#: and puts it again, the way a preview redraws.
IMAGE_ID = 1


def _put(at_row=None, delete=False):
    "One kitty transmission: the image, or the delete of it."
    if delete:
        return "\x1b_Ga=d,i=%d\x1b\\" % IMAGE_ID
    payload = base64.b64encode(kitty_rgb(IMAGE_HEIGHT)).decode("ascii")
    chunks = [payload[at : at + KITTY_CHUNK] for at in range(0, len(payload), KITTY_CHUNK)]
    pieces = []
    if at_row is not None:
        pieces.append(csi(escape.CUP, at_row, 1))
    for index, chunk in enumerate(chunks):
        more = 1 if index < len(chunks) - 1 else 0
        if index == 0:
            # No "i" question and no answer: the picture farm's
            # fixtures write no query, and neither does this. "C=1"
            # keeps the image from moving the cursor, so the two sides
            # cannot disagree about where it ended up.
            control = "a=T,f=%i,s=%i,v=%i,i=%i,C=1,q=2,m=%i" % (
                PixelFormat.RGB,
                IMAGE_WIDTH,
                IMAGE_HEIGHT,
                IMAGE_ID,
                more,
            )
        else:
            control = "m=%i" % more
        pieces.append("\x1b_G%s;%s\x1b\\" % (control, chunk))
    return "".join(pieces)


def _payload(token):
    "This step's token, as the base64 an OSC 52 carries."
    return base64.b64encode(token.encode()).decode()


def _token():
    return os.urandom(8).hex()


def write_emitter(room, fifo):
    """
    The program both sides run, as a shell script and two byte files.

    It draws the image, fences, and waits on the fifo: the director
    writes one line to move it on to the move. Then it draws the image
    lower, fences again, and holds the screen through the two resizes,
    which need nothing from it. A resize writes no bytes; the pane
    reflows underneath and writes the image out again, or it does not,
    which is what the pictures ask.

    The fence waits a second after the bytes, the way the fence of
    `take_picture.py` does. The shell starts before the seat's keyboard
    holder gives the window its focus, and a terminal that sees the
    clipboard escape while it is unfocused refuses it: a fence
    without the wait never comes.
    """
    draw_token, move_token = _token(), _token()
    hide = "\x1b[?25l"  # No cursor: a blink phase is not a difference.
    (room / "draw.bin").write_bytes((hide + csi(escape.ED, 2) + csi(escape.CUP) + _put()).encode())
    (room / "move.bin").write_bytes((_put(delete=True) + _put(at_row=MOVE_TO_ROW)).encode())
    program = room / "emitter.sh"
    program.write_text(
        "stty -echo\n"
        "cat %s\n"
        "sleep 1\n"
        "printf '\\033]52;c;%s\\007'\n"
        "exec 9<%s\n"
        "read ignored <&9\n"
        "cat %s\n"
        "sleep 1\n"
        "printf '\\033]52;c;%s\\007'\n"
        "exec sleep %d\n"
        % (
            room / "draw.bin",
            _payload(draw_token),
            fifo,
            room / "move.bin",
            _payload(move_token),
            HOLD,
        )
    )
    return program, draw_token, move_token


def sway(room, *args):
    """
    Ask this run's compositor to do something to its window.

    The seat keeps the room and the display of the run beside the
    clipboard reader, in `seat._where`, and the compositor's IPC
    socket is in that room. The module docstring says why this reads
    a private.
    """
    socks = sorted(room.glob("sway-ipc.*"))
    if not socks:
        raise RuntimeError("the compositor of %s holds no IPC socket" % room)
    # swaymsg finds its socket through SWAYSOCK, and falls back to the
    # display. Neither names this room's socket, so say which one.
    answer = subprocess.run(
        ["swaymsg", *args],
        capture_output=True,
        timeout=10,
        env={**os.environ, "XDG_RUNTIME_DIR": str(room), "SWAYSOCK": str(socks[0])},
    )
    if answer.returncode:
        raise RuntimeError("swaymsg %s: %s" % (" ".join(args), answer.stderr.decode().strip()))


def direct(seat, fifo, draw_token, move_token, work, side_room, log_path, what):
    """
    The four steps, as a director: settle, picture, next.

    The fifo moves the emitter from the draw to the move. The resizes
    go straight to the compositor: the window shrinks, both sides
    take a real configure event, and the settle watches the pixels
    stop. Then the window goes back to the whole output.
    """

    def director(take_one, ended):
        where, _display = seat._where
        _settle(
            work,
            side_room / "drawn.png",
            take_one,
            ended,
            what,
            log_path,
            draw_token,
            seat.clipboard,
            None,
            seat.how_busy,
        )
        with open(fifo, "w") as out:
            out.write("next\n")
        _settle(
            work,
            side_room / "moved.png",
            take_one,
            ended,
            what,
            log_path,
            move_token,
            seat.clipboard,
            None,
            seat.how_busy,
        )
        sway(where, "floating", "enable")
        sway(where, "resize", "set", "%d" % SMALL_WIDTH, "%d" % SMALL_HEIGHT)
        sway(where, "move", "position", "center")
        time.sleep(0.5)
        _settle(
            work,
            side_room / "smaller.png",
            take_one,
            ended,
            what,
            log_path,
            how_busy=seat.how_busy,
        )
        sway(where, "floating", "disable")
        time.sleep(0.5)
        _settle(
            work,
            side_room / "restored.png",
            take_one,
            ended,
            what,
            log_path,
            how_busy=seat.how_busy,
        )

    return director


def exercise(seat, terminal, work, out):
    "Run the scenario both ways, and say how many pixels differ at each step."
    room = out / terminal.name / SCENARIO
    room.mkdir(parents=True, exist_ok=True)

    fifo = work / ("%s.fifo" % SCENARIO)
    with contextlib.suppress(FileExistsError):
        os.mkfifo(fifo)

    program, draw_token, move_token = write_emitter(work, fifo)

    config_path = work / "full-screen.conf"
    config_path.write_text(PICTURE_CONFIG)

    seen = {}
    for side, command in (
        ("bare", bare_command(program)),
        (
            "pymux",
            pymux_command(
                program,
                work / ("%s.sock" % SCENARIO),
                config_path,
                room / "pymux-server.log",
                room / "pymux-stderr.log",
            ),
        ),
    ):
        side_room = room / side
        side_room.mkdir(parents=True, exist_ok=True)
        what = "%s of %s %s" % (side, terminal.name, SCENARIO)
        try:
            seat.running(
                terminal,
                command,
                work,
                room / ("%s.log" % side),
                direct(
                    seat,
                    fifo,
                    draw_token,
                    move_token,
                    work,
                    side_room,
                    room / ("%s.log" % side),
                    what,
                ),
            )
        except RuntimeError as reason:
            raise RuntimeError("%s\n%s" % (reason, every_log(room, seat))) from None

    for step in ("drawn", "moved", "smaller", "restored"):
        seen[(terminal.name, step)] = differences(
            room / "bare" / ("%s.png" % step),
            room / "pymux" / ("%s.png" % step),
            room / ("difference-%s.png" % step),
        )
        print(
            "%s %s %s: %d pixels differ" % (terminal.name, SCENARIO, step, seen[(terminal.name, step)]),
            flush=True,
        )
    return seen


def read_recorded():
    "The differences that stand, as {(terminal, step): pixels}."
    if not RECORDED.exists():
        return {}
    standing = {}
    for line in RECORDED.read_text().splitlines():
        line = line.split("#")[0].strip()
        if not line:
            continue
        terminal, step, pixels = line.split()
        standing[(terminal, step)] = int(pixels)
    return standing


def write_recorded(path, found):
    "The list of differences that a run saw, ready to be recorded."
    lines = [
        "# Every difference between a picture with pymux and one without,",
        "# at each step of each exercised scenario.",
        "# `tests/exercise_images.py` says what this is and how to write it.",
        "",
    ]
    for (terminal, step), pixels in sorted(found.items()):
        if pixels:
            lines.append("%s %s %d" % (terminal, step, pixels))
    path.write_text("\n".join(lines) + "\n")


def main():
    work = Path(os.environ.get("TMPDIR", "/tmp")) / "pymux-exercise-images"
    work.mkdir(parents=True, exist_ok=True)
    out = PICTURES
    out.mkdir(parents=True, exist_ok=True)

    if ONLY not in SCENARIO:
        raise SystemExit("no scenario holds %r" % ONLY)

    kitty = next(term for term in TERMINALS if term.name == "kitty")
    if not kitty.is_available():
        raise SystemExit("kitty is not here: the build brings it in, so the inputs changed")

    standing = read_recorded()
    seen = {}

    try:
        seats = open_the_seats([kitty], work)
    except TheSeatIsGone as reason:
        return with_no_answer(str(reason))

    try:
        for key, found in sorted(exercise(seats[kitty.seat], kitty, work, out).items()):
            seen[key] = found
    except TheSeatIsGone as gone:
        return with_no_answer(str(gone))
    except Exception as reason:
        try:
            seats[kitty.seat].still_there()
        except TheSeatIsGone as gone:
            return with_no_answer(str(gone))
        raise reason
    finally:
        for seat in seats.values():
            seat.keep_the_log(out)
            seat.stop()

    # Keep the list that this run saw, beside the pictures, whatever
    # the verdict is.
    write_recorded(out / "exercise-differences.txt", seen)

    # Judge the run against the list. A difference either way matters:
    # one that grew is a regression, and one that went is a fix that
    # nobody wrote down.
    wrong = []
    for key, found in sorted(seen.items()):
        expected = standing.get(key, 0)
        if found != expected:
            wrong.append("%s %s: %d pixels differ, %d were recorded" % (key[0], key[1], found, expected))

    if wrong:
        print("\n--- pymux draws something else ---", file=sys.stderr)
        for line in wrong:
            print(line, file=sys.stderr)
        print(
            "\nLook at difference-<step>.png, and if the difference is right, take\n"
            "the list this run wrote and say why in a comment:\n"
            "    nix build --file . checks.pymux-image-exercise.run\n"
            "    cp result/exercise-differences.txt "
            "pymux/tests/exercise-differences.txt",
            file=sys.stderr,
        )
        raise SystemExit(1)

    print("Every step is what it was recorded to be.")


if __name__ == "__main__":
    main()
