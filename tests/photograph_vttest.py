"""
Photograph every screen vttest draws, with pymux in the chain and without.

`take_a_picture.py` runs a fixture somebody wrote and subtracts two
pictures of it. This runs vttest, which is the program that exists to
draw the awkward screens: double sized rows, national character sets,
origin mode, the character set of a VT52, the reports of a VT420. A
person wrote four fixtures. vttest holds five hundred screens.

    the walker ── vttest's bytes ──▶ a real terminal ──▶ a picture
         │                                                  │
    ptterm parses                                     bare.png
         │
    "screen 7 is finished"           ... and the same walk again,
                                     with a pymux pane in the middle,
                                     giving pymux.png

**Why the walker has to be in the middle.** vttest is interactive: it
draws a screen and waits for a key. Nothing outside can tell a screen
that is finished from a screen that is still arriving, so nothing
outside knows when to take the picture. `drive_with_vttest.py` does
know, because it owns the pty vttest runs on and reads
`/proc/<pid>/syscall`. So the walker drives vttest, models what it
drew with ptterm, and passes the same bytes on to the terminal this
runs in. That file says how.

**A blinking screen has no still picture.** The terminal lights a cell
that carries SGR 5 and puts it out again on a clock of its own, and no
byte of that phase is on the wire. Two runs minutes apart are lit and
dark at moments nobody can line up, so a pixel count of one is a coin
toss: "2 Test of screen features #13" came out 6108 pixels apart while
both sides were behaving. The walker knows which screens those are,
because ptterm parses the attribute, and it says so. Such a screen gets
a burst on each side and the question becomes whether both sides blink,
which is a fact and not a phase. That answer is recorded in the same
file as a pixel count, because a gate that is red for a reason
everybody already knows is a gate everybody learns to ignore.

**How the two sides are lined up.** The walker stops at each screen it
keeps and waits for this program to say the picture is taken. It names
the screen by the menu path that reached it, which is the same name in
both runs, and writes the whole ordered list to `paths.txt`. Two runs
that do not write the same list are two runs whose pictures cannot be
compared, so this checks that first and fails on the difference rather
than on a pixel count.

**One terminal, by default.** Five hundred screens twice is not a
minute's work, and each picture waits for the screen to stop changing.
xterm is the default seat, and `WANTED` says what was measured: it is
the only one of the three here that draws a double sized line at all.

The result is a directory:

    $out/<terminal>/<side>/0001.png       every screen, in order
    $out/<terminal>/<side>/paths.txt      what each number is
    $out/<terminal>/<side>/walker.log     what the walker said
    $out/<terminal>/differ/0007.png       where the two differ
    $out/vttest-picture-differences.txt   the list this run saw

Run it:

    nix build --file . checks.pymux-vttest-pictures.run
    PYMUX_VTTEST_INCLUDE='^9 ' nix build --file . checks.pymux-vttest-pictures.run

`PYMUX_VTTEST_INCLUDE` is the walker's own `PTTERM_VTTEST_INCLUDE`: a
regular expression against an item of vttest's main menu, written as
"N title". `PYMUX_VTTEST_TERMINALS` is a comma separated list of
terminal names.

**This is not a gate yet, and it should not be made one yet.** The
chain is proven: two runs of the default item give the same verdicts,
and the first outing found three faults that no other check here can
see (Lillecarl/pymux#115, #116 and #117). The judging is not proven.
Item 2 of the main menu still flaps, and the reason is #117 itself: it
is a single change at a fixed moment after a screen is drawn, so
whether a measurement holds it depends on when the measurement starts.
A check that flaps is worse than no check, so `checks.all` leaves this
out and a person runs it.

**What a difference means here is what it means there.** A pane reads
what a program asked for and writes the request again in the form the
terminal understands, so a pane can draw more than the terminal draws
on its own. `tests/vttest-picture-differences.txt` records each
difference that stands and says why, and a run is judged against it in
both directions.
"""

import os
import shlex
import shutil
import sys
import time
from pathlib import Path

from pyterm_pytest.seats import (
    BLINK_FRAMES,
    BLINK_GAP,
    SEATS,
    _settle,
    _tail,
    differences,
)

from take_a_picture import TERMINALS, every_log, pymux_command

#: Where the pictures go. The check points this at `$out`.
PICTURES = Path(os.environ.get("PYMUX_VTTEST_OUT", "vttest-pictures"))

#: The walker, and the vttest binary it drives. The check names both.
WALKER = os.environ.get("PYMUX_VTTEST_WALKER", "")
VTTEST = os.environ.get("PYMUX_VTTEST", "")

#: Which item of vttest's main menu to walk. It is the walker's own
#: `PTTERM_VTTEST_INCLUDE`, and it defaults to the double sized
#: characters: six screens, and the ones where a pane most recently
#: changed what a terminal draws (Lillecarl/pymux#65).
#:
#: The whole of vttest is five hundred screens, and each of them is
#: photographed twice. That is a run of some twenty minutes for one
#: terminal, which is a thing to ask for and not a default.
INCLUDE = os.environ.get("PYMUX_VTTEST_INCLUDE", "^4 ")

#: Which terminals to run in, by name. One by default, for the same
#: reason: every terminal doubles the run.
#:
#: xterm is that one, which is the other way round from
#: `take_a_picture.py`. Measured on item 4 of vttest's main menu:
#:
#: * xterm draws the DEC line attributes, honours DECCOLM by resizing
#:   its window, and settles, because it does not blink a cursor in a
#:   window with no focus. Six screens, three of them different.
#: * foot draws no double sized line at all. It logs nothing and shows
#:   nothing: every "ESC # 6" row comes out ordinary. So the six
#:   screens agree, and they agree about nothing.
#: * kitty says "Unhandled Esc # code" for each of them, and its cursor
#:   blinks, so a still picture of it never settles at all.
#:
#: vttest was written for a VT100 and xterm is what answers it.
WANTED = [
    name.strip()
    for name in os.environ.get("PYMUX_VTTEST_TERMINALS", "xterm").split(",")
    if name.strip()
]

#: The differences that stand, as a file of "terminal identity verdict".
RECORDED = Path(__file__).parent / "vttest-picture-differences.txt"

#: What a screen says when nothing is wrong with it. A still screen is
#: judged in pixels and says "0". A blinking screen is judged on whether
#: it blinks at all, and says this when the two sides agree, which
#: covers a screen that blinks on both sides and one that blinks on
#: neither.
#:
#: Both are written down when they are not what a run found, the same
#: way and in the same file. A gate that is red for a reason everybody
#: already knows is a gate everybody learns to ignore, so what stands is
#: recorded and only a change fails (Lillecarl/pymux#46).
SAME = "0"
BLINK_AGREE = "blink-agree"

#: How long to wait for the walker to name its next screen, in seconds.
#: The walk itself has a budget of its own; this covers the walker
#: dying without a word, and the terminal never starting at all.
NEXT_SCREEN_TIMEOUT = 120.0

#: How often to look at the fifo while waiting.
TICK = 0.02

#: How long to let a blinking screen stand still before the burst, and
#: how far apart to look, in seconds.
#:
#: `_settle` insists that a screen stops changing and fails when it does
#: not. A blinking screen may never stop, so this waits for the same
#: thing and gives up quietly. It is not optional: without it the first
#: picture of the burst races the paint. Measured on "4 Test of
#: double-sized characters #6", where the pane was still scrolling and
#: the two sides came out 2848 pixels apart instead of 371.
STILL_BOUND = 4.0
STILL_GAP = 0.4


def walker_script(walker_room: Path, side_room: Path) -> str:
    """
    The script that runs the walker, whole.

    It is the same file on both sides. One side runs it straight and
    the other runs it as the program of a pymux pane, and that is the
    only difference between the two pictures.

    Four things matter here.

    `stty -echo` is inside this script and not around it, because the
    pty it has to reach is the one the walker really reads. On the
    pymux side that is the pane's pty, which the terminal's own `stty`
    never touches. Every query vttest sends is answered into it, and a
    tty that echoes puts the answer on the pane's screen as text.

    The environment is exported in this script and not inherited from
    this process. On the pymux side a pane is a child of a server that
    is a child of a client, and nothing should depend on a variable
    surviving all three.

    The walker's own words go to a file. Its stdout is vttest's screen,
    and a word of ours on that screen would be a word in the picture.

    And the exit status is written down. A terminal emulator does not
    carry the status of the program it ran, and neither does pymux, so
    without this a walk that died at the third screen would look like a
    walk of three screens.
    """
    settings = {
        "PTTERM_VTTEST": VTTEST,
        "PTTERM_VTTEST_INCLUDE": INCLUDE,
        "PTTERM_VTTEST_THROUGH": "1",
        "PTTERM_VTTEST_PAUSE": str(walker_room),
        "PTTERM_VTTEST_OUT": str(side_room),
        # The walker writes vttest's own log under TMPDIR, and the two
        # sides run one after the other in the same sandbox.
        "TMPDIR": str(side_room),
        "HOME": str(side_room),
    }
    exports = "".join(
        "export %s=%s\n" % (name, shlex.quote(value))
        for name, value in settings.items()
    )
    return (
        "stty -echo\n"
        + exports
        + "python3 %s 2>%s\n"
        % (shlex.quote(WALKER), shlex.quote(str(side_room / "walker.log")))
        + "echo $? > %s\n" % shlex.quote(str(side_room / "status"))
    )


def outer_script(inner: str) -> str:
    """
    What the terminal itself runs, around the walker.

    The clear is what makes the two sides start alike. A terminal may
    write on its own screen before the program runs, foot says which of
    its settings are deprecated, and pymux paints every cell, so that
    message is gone on one side and stays on the other.

    `stty -echo` because the terminal outside answers the queries that
    pass through it, and a tty that echoes would put those answers on
    the screen as text.
    """
    return "stty -echo\nprintf '\\033[2J\\033[H'\n%s\n" % inner


class Camera:
    """
    The near end of the walker's `Shutter`, and the pictures it takes.

    The walker writes the identity of each screen into `ready.fifo` and
    then waits for a line on `go.fifo`. This reads the first, takes a
    picture, and writes the second.

    Both fifos are opened for reading and writing, for the reason the
    walker's `Shutter` gives: a fifo opened one way alone reports end
    of file whenever the other side does not hold it open, and that is
    not the same answer as a side that has gone. Neither open blocks
    either, so the two programs can start in any order.

    So the end of the walk is not an end of file. It is the status file
    that the walker's own script writes, or failing that the terminal
    process ending. The status file comes first because it is exact:
    pymux does not have to have noticed that its last pane went, and
    neither does the compositor.
    """

    def __init__(self, room: Path, into: Path, status: Path) -> None:
        self.room = room
        self.into = into
        self.status = status
        self.ready = -1
        self.go = -1
        #: The identity of each screen photographed, in order.
        self.identities: list[str] = []
        #: How often the screen changed during the burst, for each
        #: screen the walker said blinks. A still screen is not in it.
        self.blinked: dict[str, int] = {}
        self.rest = b""

    def open(self) -> None:
        self.room.mkdir(parents=True, exist_ok=True)
        for name in ("ready.fifo", "go.fifo"):
            path = self.room / name
            if path.exists():
                path.unlink()
            os.mkfifo(path)
        self.ready = os.open(str(self.room / "ready.fifo"), os.O_RDWR)
        self.go = os.open(str(self.room / "go.fifo"), os.O_RDWR)
        os.set_blocking(self.ready, False)

    def close(self) -> None:
        for handle in (self.ready, self.go):
            if handle >= 0:
                os.close(handle)
        self.ready = self.go = -1

    def written(self) -> bool:
        "Whether the walker's script has written its exit status yet."
        try:
            return bool(self.status.read_text().strip())
        except OSError:
            return False

    def _next_identity(self):
        "The next line the walker wrote, or None if it has written none."
        while b"\n" not in self.rest:
            try:
                piece = os.read(self.ready, 65536)
            except BlockingIOError:
                return None
            if not piece:
                return None
            self.rest += piece
        line, self.rest = self.rest.split(b"\n", 1)
        return line.decode("utf-8", "replace")

    @staticmethod
    def _hold_still(work, take_one, ended, identity, path, log_path) -> None:
        """
        Wait for a blinking screen to stop moving, and do not insist.

        `_settle` asks the same question and fails when the answer does
        not come, which is right for a still screen and wrong for one
        that blinks: a screen that never stops is what a blink is. This
        waits for the same silence, gives up quietly, bounds the wait,
        and leaves the last picture it took at `path`.

        A terminal that has gone is still a failure. A picture of a
        window that is not there says nothing.
        """
        scratch = work / "still.png"
        take_one(path)
        deadline = time.monotonic() + STILL_BOUND
        while time.monotonic() < deadline:
            time.sleep(STILL_GAP)
            gone = ended()
            if gone is not None:
                raise RuntimeError(
                    "%r ended while it was drawing (exit %s)\n%s"
                    % (identity, gone, _tail(log_path))
                )
            take_one(scratch)
            same = differences(path, scratch) == 0
            shutil.copy(scratch, path)
            if same:
                return

    def _burst(self, take_one, ended, identity, number, log_path) -> int:
        """
        Photograph a blinking screen several times, and count the changes.

        A blinking screen has no still picture. The terminal lights the
        cell and puts it out again on a clock of its own, nothing about
        that phase is on the wire, and the two runs of a comparison
        happen minutes apart. So one picture of each side compares two
        coin tosses, which is how "2 Test of screen features #13" came
        out 6108 pixels apart while both sides were behaving.

        What can be compared is the behaviour. This counts how many of
        the consecutive pictures differ: zero is a screen that does not
        blink, and anything above it is one that does. `compare_one`
        then asks only whether the two sides agree about that.
        """
        shots = []
        for frame in range(BLINK_FRAMES):
            if frame:
                time.sleep(BLINK_GAP)
            gone = ended()
            if gone is not None:
                raise RuntimeError(
                    "%r ended while it was blinking (exit %s)\n%s"
                    % (identity, gone, _tail(log_path))
                )
            shot = self.into / ("%04d.%d.png" % (number, frame))
            take_one(shot)
            shots.append(shot)
        return sum(
            1
            for first, second in zip(shots, shots[1:])
            if differences(first, second) != 0
        )

    def take_them(self, work: Path, log_path: Path):
        """
        The director: photograph each screen until the walker has gone.

        A screen the walker marks as blinking gets a burst instead of a
        still picture, and `_burst` says why.

        `_settle` is the fence for the rest, and it is a placeholder. It
        takes pictures until two in a row are the same, which says the
        terminal has stopped drawing but not that it drew what the
        walker last sent. A real paint fence is `wlr-screencopy` with
        `copy_with_damage`, which returns on the next committed frame;
        `grim` cannot ask for that.
        """

        def director(take_one, ended):
            deadline = time.monotonic() + NEXT_SCREEN_TIMEOUT
            while True:
                identity = self._next_identity()
                if identity is None:
                    # The status has to hold something. The shell
                    # creates the file and then writes the number, and
                    # over five hundred screens the gap between those
                    # two is a race that turns up.
                    if self.written() or ended() is not None:
                        return self.identities
                    if time.monotonic() > deadline:
                        raise RuntimeError(
                            "the walker named no screen in %g seconds\n%s"
                            % (NEXT_SCREEN_TIMEOUT, _tail(log_path))
                        )
                    time.sleep(TICK)
                    continue

                identity, _, mark = identity.partition("\t")
                self.identities.append(identity)
                number = len(self.identities)
                if mark == "blinks":
                    # The burst first, and it starts at the fence. What
                    # happens to a screen after it is drawn happens at
                    # a fixed moment after the draw, so a window that
                    # starts anywhere else holds it only sometimes:
                    # measured on "2 Test of screen features", where a
                    # settle before the burst swallowed the one change
                    # of Lillecarl/pymux#117 in one run of two.
                    #
                    # Then the still picture, taken once everything
                    # that was going to happen has happened.
                    self.blinked[identity] = self._burst(
                        take_one, ended, identity, number, log_path
                    )
                    self._hold_still(
                        work,
                        take_one,
                        ended,
                        identity,
                        self.into / ("%04d.png" % number),
                        log_path,
                    )
                else:
                    _settle(
                        work,
                        self.into / ("%04d.png" % number),
                        take_one,
                        ended,
                        "the screen of %r" % identity,
                        log_path,
                    )
                os.write(self.go, b"go\n")
                deadline = time.monotonic() + NEXT_SCREEN_TIMEOUT

        return director


def one_side(terminal, seat, side, into_pane, work, room):
    """
    Walk vttest once, in one terminal, and photograph every screen.

    `into_pane` says how the walker is started: straight, or as the
    program of a pymux pane. Gives back the camera, which holds the
    identities in the order they were drawn and how often each blinking
    screen changed.
    """
    side_room = room / side
    side_room.mkdir(parents=True, exist_ok=True)
    status = side_room / "status"

    walker_room = work / ("%s-%s-fifos" % (terminal.name, side))
    camera = Camera(walker_room, side_room, status)
    camera.open()

    walker = work / ("%s-%s-walk.sh" % (terminal.name, side))
    walker.write_text(walker_script(walker_room, side_room))

    script = work / ("%s-%s.sh" % (terminal.name, side))
    script.write_text(outer_script(into_pane(walker)))

    log_path = side_room / "terminal.log"
    try:
        seat.running(
            terminal,
            "exec sh %s" % shlex.quote(str(script)),
            work,
            log_path,
            camera.take_them(work, log_path),
        )
    finally:
        camera.close()

    if not status.exists():
        raise RuntimeError(
            "the %s walk left no status behind\n%s" % (side, every_log(side_room))
        )
    if status.read_text().strip() != "0":
        raise RuntimeError(
            "the %s walk ended with %s\n%s"
            % (side, status.read_text().strip(), every_log(side_room))
        )
    return camera


def compare_one(terminal, seat, work, out):
    """
    Walk vttest both ways in one terminal, and subtract the pictures.

    Gives back {identity: pixels that differ}.
    """
    room = out / terminal.name
    room.mkdir(parents=True, exist_ok=True)

    config_path = work / "full-screen.conf"
    config_path.write_text("set full-screen on\n")

    # The pane covers every cell, and `pymux_command` writes the same
    # line the still pictures use, so both harnesses put pymux in the
    # chain the same way.
    def in_a_pane(walker):
        return pymux_command(
            walker,
            work / ("%s-vttest.sock" % terminal.name),
            config_path,
            room / "pymux-server.log",
            room / "pymux-stderr.log",
        )

    try:
        bare = one_side(
            terminal,
            seat,
            "bare",
            lambda walker: "exec sh %s" % shlex.quote(str(walker)),
            work,
            room,
        )
        through = one_side(terminal, seat, "pymux", in_a_pane, work, room)
    except RuntimeError as reason:
        raise RuntimeError("%s\n%s" % (reason, every_log(room))) from None

    if bare.identities != through.identities:
        raise RuntimeError(
            "the two walks drew different screens, so no picture of one "
            "can be compared with a picture of the other.\n%s"
            % first_difference(bare.identities, through.identities)
        )

    differ = room / "differ"
    differ.mkdir(parents=True, exist_ok=True)
    found = {}
    for number, identity in enumerate(bare.identities, start=1):
        blinking = identity in bare.blinked

        # The pixels. A blinking screen gets its still picture after the
        # burst, once everything that was going to happen has happened,
        # so every screen has one and the name is the same. Without it a
        # screen that blinks anywhere would be judged on its blinking
        # alone, and a fault such as Lillecarl/pymux#115, the cursor two
        # columns to the left, would not be measured at all.
        name = "%04d.png" % number
        pixels = differences(
            room / "bare" / name,
            room / "pymux" / name,
            differ / name,
        )
        found[identity] = (str(pixels), SAME)

        if not blinking:
            continue

        # And the behaviour, as a verdict of its own. The number of
        # blinks is not judged: how often a terminal blinks in two
        # seconds is the terminal's business. Whether it blinks at all
        # is not.
        lit = bool(bare.blinked[identity])
        panel = bool(through.blinked.get(identity))
        if lit == panel:
            verdict = BLINK_AGREE
        elif lit:
            verdict = "blinks-bare-only"
        else:
            verdict = "blinks-in-a-pane-only"
        found["%s (blink)" % identity] = (verdict, BLINK_AGREE)
    return found


def first_difference(bare, through):
    "Where two lists of screens stop agreeing, in words."
    for number, (one, other) in enumerate(zip(bare, through), start=1):
        if one != other:
            return "screen %d is %r bare and %r with pymux" % (
                number,
                one,
                other,
            )
    longer, shorter = (
        ("bare", "pymux")
        if len(bare) > len(through)
        else (
            "pymux",
            "bare",
        )
    )
    return "%s drew %d screens and %s drew %d" % (
        longer,
        max(len(bare), len(through)),
        shorter,
        min(len(bare), len(through)),
    )


def read_the_recorded():
    """
    The differences that stand, as {(terminal, identity): verdict}.

    A verdict is a number of pixels for a still screen, and a word for
    a blinking one. Both are text here, because both are compared
    against what a run said and neither is arithmetic.

    A comment is a line that starts with a hash, not a line with a hash
    anywhere. The name of a screen ends in one: "#3" is the third
    screen of that menu item, and the walk numbers them that way so
    that one screen more in an item does not rename every screen after
    it.
    """
    if not RECORDED.exists():
        return {}
    standing = {}
    for line in RECORDED.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        terminal, rest = line.split(None, 1)
        identity, verdict = rest.rsplit(None, 1)
        standing[(terminal, identity)] = verdict
    return standing


def write_the_recorded(path, found):
    "The list of differences a run saw, ready to be recorded."
    lines = [
        "# Every difference between a picture of vttest with pymux in the",
        "# chain and one without it. A line is a terminal, the screen by the",
        "# menu path that reached it, and the verdict: how many pixels differ",
        "# for a still screen, and a word for a blinking one.",
        "# `tests/photograph_vttest.py` says what this is and how to write it.",
        "",
    ]
    for (terminal, identity), (verdict, default) in sorted(found.items()):
        if verdict != default:
            lines.append("%s %s %s" % (terminal, identity, verdict))
    path.write_text("\n".join(lines) + "\n")


def main():
    if not WALKER or not VTTEST:
        raise SystemExit(
            "PYMUX_VTTEST_WALKER and PYMUX_VTTEST name the walker and the "
            "vttest binary, and one of them is not set."
        )

    work = Path(os.environ.get("TMPDIR", "/tmp")) / "pymux-vttest-pictures"
    work.mkdir(parents=True, exist_ok=True)
    out = PICTURES
    out.mkdir(parents=True, exist_ok=True)

    by_name = {terminal.name: terminal for terminal in TERMINALS}
    unknown = [name for name in WANTED if name not in by_name]
    if unknown:
        raise SystemExit(
            "no terminal is called %s. There is %s."
            % (", ".join(unknown), ", ".join(sorted(by_name)))
        )
    terminals = [by_name[name] for name in WANTED]

    missing = [t.name for t in terminals if not t.is_available()]
    if missing:
        raise SystemExit("these terminals are not here: %s" % ", ".join(missing))

    standing = read_the_recorded()
    seen = {}

    seats = {}
    try:
        for terminal in terminals:
            if terminal.seat not in seats:
                seats[terminal.seat] = SEATS[terminal.seat]().start(work)

        for terminal in terminals:
            found = compare_one(terminal, seats[terminal.seat], work, out)
            for identity, verdict in found.items():
                seen[(terminal.name, identity)] = verdict
            print(
                "%s: %d screens, %d of them differ"
                % (
                    terminal.name,
                    len(found),
                    sum(1 for one, default in found.values() if one != default),
                ),
                flush=True,
            )
    finally:
        for seat in seats.values():
            seat.stop()

    write_the_recorded(out / "vttest-picture-differences.txt", seen)

    # Judge the run against the list, in both directions: a difference
    # that grew is a regression, and one that went is a fix nobody wrote
    # down. Only the screens this run drew are judged, because the
    # include narrows which ones those are.
    wrong = []
    for key, (verdict, default) in sorted(seen.items()):
        expected = standing.get(key, default)
        if verdict != expected:
            wrong.append(
                "%s %s: this run says %s, and %s was recorded"
                % (key[0], key[1], verdict, expected)
            )

    if wrong:
        print("\n--- pymux draws something else ---", file=sys.stderr)
        for line in wrong:
            print(line, file=sys.stderr)
        print(
            "\nLook at differ/<number>.png, and if the difference is right,\n"
            "take the list this run wrote and say why in a comment:\n"
            "    nix build --file . checks.pymux-vttest-pictures.run\n"
            "    cp result/vttest-picture-differences.txt "
            "pymux/tests/vttest-picture-differences.txt",
            file=sys.stderr,
        )
        raise SystemExit(1)

    print("Every screen of vttest is what it was recorded to be.")


if __name__ == "__main__":
    main()
