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
text. `PYMUX_PICTURES_KEEP=1` keeps the pictures of a run that found a
difference, because nix takes the output of a build that failed away.
`PYMUX_PICTURES_RECORD=1` writes the list of differences instead of
judging the run:

    PYMUX_PICTURES_RECORD=1 nix build --file . checks.pymux-pictures
    cp result/picture-differences.txt pymux/tests/picture-differences.txt
"""
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent

#: Where the pictures go. The check points this at `$out`.
PICTURES = Path(os.environ.get("PYMUX_PICTURES_OUT", "pictures"))

#: Which fixtures to run. Empty means all of them.
ONLY = os.environ.get("PYMUX_PICTURES", "")

#: Report a difference and end well anyway. Nix takes the output of a
#: build that failed away, so a run that judges leaves nothing to look
#: at. Set this and the result of the build is the pictures.
#:
#: `PYMUX_PICTURES_RECORD` keeps the pictures as well, and writes the
#: list too. Use this one to look at a difference before deciding
#: whether it belongs in the list.
KEEP = os.environ.get("PYMUX_PICTURES_KEEP", "") != ""

#: Write the list of differences that stand today, instead of judging
#: the run against it. The result of the build is that list, ready to
#: copy over `tests/picture-differences.txt`.
RECORD = os.environ.get("PYMUX_PICTURES_RECORD", "")

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

#: The screen of the X server. It only has to be larger than the
#: window; nothing is placed against its edges.
SCREEN = "1280x800x24"

#: How long to wait for a window to appear, and for what it draws to
#: stop changing.
APPEAR_TIMEOUT = 20.0
SETTLE_TIMEOUT = 15.0


# ----------------------------------------------------------------------
# The fixtures. Each one is the bytes that a program wrote.
#
# A fixture writes no query. A query is answered into the tty, and a
# tty that echoes puts the answer on the screen as text; the answer
# differs on the two sides, so the echo would too. `record_a_session.py`
# is how a program that does ask gets in, and that is a later step.


def sgr(fixture):
    "Colours and attributes, on a screen that never scrolls."
    lines = [
        "\x1b[2J\x1b[H",
        "\x1b[1mbold\x1b[22m \x1b[3mitalic\x1b[23m \x1b[4munderline\x1b[24m\r\n",
        "\x1b[7mreverse\x1b[27m \x1b[9mstruck\x1b[29m \x1b[2mfaint\x1b[22m\r\n",
    ]
    for number in range(8):
        lines.append("\x1b[3%dm%d\x1b[39m " % (number, number))
    lines.append("\r\n")
    for number in range(8):
        lines.append("\x1b[4%dm %d \x1b[49m" % (number, number))
    lines.append("\r\n")
    lines.append("\x1b[38;5;208m256\x1b[39m \x1b[38;2;30;170;90mtruecolour\x1b[39m\r\n")
    lines.append("\x1b[48;2;40;40;90m\x1b[38;2;250;250;120m on a colour \x1b[0m\r\n")
    fixture.extend(lines)


def underlines(fixture):
    "The shapes of an underline, and the colour of one."
    fixture.append("\x1b[2J\x1b[H")
    for shape, name in enumerate(
        ["none", "straight", "double", "curly", "dotted", "dashed"]
    ):
        fixture.append("\x1b[4:%dm%s\x1b[4:0m\r\n" % (shape, name))
    fixture.append("\x1b[4:3m\x1b[58:2::255:0:0mred curly\x1b[59m\x1b[4:0m\r\n")
    fixture.append("\x1b[4:1m\x1b[58:5:33mblue straight\x1b[59m\x1b[4:0m\r\n")


def wide_characters(fixture):
    "Characters that take two cells, next to ones that take one."
    fixture.append("\x1b[2J\x1b[H")
    fixture.append("abc 你好漢 def\r\n")
    fixture.append("ＡＢ ｶﾅ ghi\r\n")
    fixture.append("äéñ straight after\r\n")


def box_drawing(fixture):
    "The line drawing set, which fills every edge of a cell."
    fixture.append("\x1b[2J\x1b[H")
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


def fixture_bytes(name):
    "The bytes of one fixture, with the cursor put out of the way."
    pieces = ["\x1b[?25l"]  # No cursor: it is not what this measures.
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


def xterm_argv(command):
    """
    xterm, with everything that could move a pixel turned off.

    No scrollbar, no border and no internal border, so the window is
    exactly the cells. A fixed font at a fixed size, because the
    default depends on what the machine has installed.
    """
    return [
        "xterm",
        "-geometry", "%dx%d+0+0" % (COLUMNS, ROWS),
        "-fa", "DejaVu Sans Mono",
        "-fs", "12",
        "-bg", "black",
        "-fg", "white",
        "-b", "0",  # No internal border.
        "-bw", "0",  # No window border.
        "+sb",  # No scrollbar.
        "-xrm", "xterm*cursorBlink: false",
        "-xrm", "xterm*allowWindowOps: false",
        "-e", "sh", "-c", command,
    ]


def foot_argv(command):
    """
    foot, a terminal that speaks Wayland and no X at all.

    The compositor gives it the whole output, so there is no geometry
    to ask for. Everything that could move a pixel is turned off, the
    same way as for xterm.
    """
    return [
        "foot",
        "--font=DejaVu Sans Mono:size=12",
        "--override=colors.background=000000",
        "--override=colors.foreground=ffffff",
        "--override=cursor.blink=no",
        "--override=main.pad=0x0",
        "--override=scrollback.lines=0",
        "sh", "-c", command,
    ]


TERMINALS = [
    Terminal("xterm", "xterm", xterm_argv, seat="x", window_class="XTerm"),
    Terminal("foot", "foot", foot_argv, seat="wayland"),
]


# ----------------------------------------------------------------------
# The seats: a display server, and how to take a picture on it.


def _tail(path, lines=40):
    "The end of a log file, for a message that has to say why."
    try:
        text = Path(path).read_text(errors="replace")
    except OSError:
        return "(no log)"
    return "--- %s ---\n%s" % (path, "\n".join(text.splitlines()[-lines:]))


class Seat:
    """
    A display server, and how to take a picture of a terminal on it.

    The two seats differ in more than the protocol.

    The X seat runs one server for the whole run, and every terminal
    opens a window on it. So a picture is of one window among several,
    and the seat has to find the right one.

    The Wayland seat runs a compositor for each picture, and that
    compositor gives its one window the whole output. So a picture is
    of the output, and there is nothing to find. That is the shape the
    harness wants, and it is what a kiosk compositor is for.
    """

    name = ""

    def start(self, work):
        "Open the seat. Returns itself."
        return self

    def stop(self):
        "Close it."

    def picture_of(self, terminal, command, work, path, log_path):
        "Run one command in one terminal and leave its picture at `path`."
        raise NotImplementedError


def _settle(work, path, take_one, ended, what, log_path):
    """
    Take pictures until two in a row are the same, and keep the last.

    A fixed wait would be a race on a slow machine and a delay on a
    fast one. `ended` gives back the exit code when whatever draws has
    gone, and `None` while it is still there.
    """
    previous = work / "settle.png"
    deadline = time.time() + SETTLE_TIMEOUT
    take_one(previous)
    while time.time() < deadline:
        time.sleep(0.4)
        gone = ended()
        if gone is not None:
            raise RuntimeError(
                "%s ended while it was drawing (exit %s)\n%s"
                % (what, gone, _tail(log_path))
            )
        take_one(path)
        if differences(previous, path) == 0:
            return
        shutil.copy(path, previous)
    raise RuntimeError("%s never settled\n%s" % (what, _tail(log_path)))


class XSeat(Seat):
    "An X server of its own, with nothing else on it."

    name = "x"

    def __init__(self):
        self.number = None
        self._process = None

    def start(self, work):
        read_fd, write_fd = os.pipe()
        self._process = subprocess.Popen(
            ["Xvfb", "-displayfd", str(write_fd), "-screen", "0", SCREEN],
            pass_fds=(write_fd,),
            stdout=open(work / "xvfb.log", "wb"),
            stderr=subprocess.STDOUT,
        )
        os.close(write_fd)

        with os.fdopen(read_fd, "rb") as reader:
            deadline = time.time() + APPEAR_TIMEOUT
            number = b""
            while b"\n" not in number and time.time() < deadline:
                piece = reader.read(1)
                if not piece:
                    break
                number += piece
        if not number.strip():
            raise RuntimeError("Xvfb never said which display it took")

        self.number = ":%s" % number.strip().decode()
        return self

    def stop(self):
        if self._process is not None:
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()

    def _windows(self, window_class):
        "Every window of this class that is on the display now."
        found = subprocess.run(
            ["xdotool", "search", "--class", window_class],
            capture_output=True,
            timeout=30,
            env={**os.environ, "DISPLAY": self.number},
        )
        return set(found.stdout.split())

    def _wait_for_a_new_window(self, window_class, already, process, log_path):
        """
        Wait for a window of this class that was not there before.

        The identifier of a window that has gone is still an
        identifier, and taking a picture of one waits for a window that
        never draws again. So a run never reuses the one before it.
        """
        deadline = time.time() + APPEAR_TIMEOUT
        while time.time() < deadline:
            if process.poll() is not None:
                raise RuntimeError(
                    "the terminal ended before it drew anything (exit %s)\n%s"
                    % (process.returncode, _tail(log_path))
                )
            new = self._windows(window_class) - already
            if new:
                return sorted(new)[-1].decode()
            time.sleep(0.2)
        raise RuntimeError(
            "no %s window appeared on %s" % (window_class, self.number)
        )

    def _take(self, window, path):
        subprocess.run(
            ["import", "-display", self.number, "-window", window, str(path)],
            check=True,
            capture_output=True,
            timeout=30,
        )

    def picture_of(self, terminal, command, work, path, log_path):
        already = self._windows(terminal.window_class)

        log = open(log_path, "wb")
        process = subprocess.Popen(
            terminal.argv(command),
            stdout=log,
            stderr=subprocess.STDOUT,
            env={
                **os.environ,
                **terminal.environment,
                "DISPLAY": self.number,
            },
        )
        try:
            window = self._wait_for_a_new_window(
                terminal.window_class, already, process, log_path
            )
            _settle(
                work,
                path,
                lambda where: self._take(window, where),
                lambda: process.poll(),
                "the window of %s" % terminal.name,
                log_path,
            )
        finally:
            _end(process)
            log.close()


class WaylandSeat(Seat):
    """
    A kiosk compositor, one for each picture.

    `cage` runs a single application and gives it the whole output,
    with no decoration of any kind. That is what this harness asks a
    display server for, so there is no window to find and no geometry
    to crop: `grim` takes the output, and the output is the terminal.

    It renders with pixman. A build sandbox has no graphics card, and a
    software renderer draws the same pixels on every machine, which is
    what a comparison of pictures needs.

    The compositor lives for one picture, because `cage` ends when the
    application it runs ends. Each one gets a runtime directory of its
    own, so the socket that appears in it is its own.
    """

    name = "wayland"

    def __init__(self):
        self._runs = 0

    def _room(self, work):
        self._runs += 1
        room = work / ("wayland-%d" % self._runs)
        room.mkdir(parents=True, exist_ok=True)
        return room

    @staticmethod
    def _wait_for_the_socket(room, process, log_path):
        "The name of the display that the compositor put in this room."
        deadline = time.time() + APPEAR_TIMEOUT
        while time.time() < deadline:
            if process.poll() is not None:
                raise RuntimeError(
                    "the compositor ended before it drew anything "
                    "(exit %s)\n%s" % (process.returncode, _tail(log_path))
                )
            sockets = sorted(room.glob("wayland-*"))
            sockets = [s for s in sockets if not s.name.endswith(".lock")]
            if sockets:
                return sockets[0].name
            time.sleep(0.2)
        raise RuntimeError(
            "the compositor never opened a display\n%s" % _tail(log_path)
        )

    @staticmethod
    def _take(room, display, path):
        subprocess.run(
            ["grim", str(path)],
            check=True,
            capture_output=True,
            timeout=30,
            env={
                **os.environ,
                "XDG_RUNTIME_DIR": str(room),
                "WAYLAND_DISPLAY": display,
            },
        )

    def picture_of(self, terminal, command, work, path, log_path):
        room = self._room(work)

        log = open(log_path, "wb")
        process = subprocess.Popen(
            ["cage", "--"] + terminal.argv(command),
            stdout=log,
            stderr=subprocess.STDOUT,
            env={
                **os.environ,
                **terminal.environment,
                "XDG_RUNTIME_DIR": str(room),
                # No graphics card in a build sandbox, and no input
                # devices either.
                "WLR_BACKENDS": "headless",
                "WLR_RENDERER": "pixman",
                "WLR_LIBINPUT_NO_DEVICES": "1",
                "LIBSEAT_BACKEND": "noop",
                # A terminal that speaks both must not reach the X
                # server that the other seat is running.
                "DISPLAY": "",
            },
        )
        try:
            display = self._wait_for_the_socket(room, process, log_path)
            _settle(
                work,
                path,
                lambda where: self._take(room, display, where),
                lambda: process.poll(),
                "the output of %s" % terminal.name,
                log_path,
            )
        finally:
            _end(process)
            log.close()


def _end(process):
    "Stop a process, and do not wait for ever."
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()


SEATS = {
    "x": XSeat,
    "wayland": WaylandSeat,
}


def differences(first, second, into=None):
    """
    How many pixels differ between two pictures.

    `compare` writes the count on stderr and exits non zero when there
    is one, so the count is what this reads and not the exit code.
    """
    command = ["compare", "-metric", "AE", str(first), str(second)]
    command.append(str(into) if into is not None else "null:")
    answer = subprocess.run(command, capture_output=True, timeout=60)
    text = answer.stderr.decode().strip().split()
    if not text:
        raise RuntimeError("compare said nothing: %r" % answer.stderr)
    try:
        return int(float(text[0]))
    except ValueError:
        raise RuntimeError("compare said %r" % answer.stderr.decode())


# ----------------------------------------------------------------------
# The two runs.


def write_the_program(path, fixture_path):
    """
    The program that both runs execute, as a shell script.

    It is a file and not a command line so that the two sides run the
    same bytes. It also keeps the quoting out of the way: a pane takes
    its command as text, and `Pymux._create_pane` splits that text on
    whitespace (Lillecarl/pymux#39).

    `stty -echo` because a tty that echoes puts the answer to a query
    on the screen as text, and the two sides answer differently.
    `sleep` because a terminal closes its window when the program ends,
    and the picture is taken after the program ran.
    """
    path.write_text(
        "stty -echo\n"
        "cat %s\n"
        "exec sleep 3600\n" % fixture_path
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
    return (
        "exec python3 -m pymux -S %s -f %s --log %s integrated sh %s 2>%s"
        % (socket_path, config_path, log_path, program_path, error_path)
    )


def every_log(room):
    "The end of every log in this room, for a run that could not finish."
    return "\n".join(
        _tail(path) for path in sorted(room.glob("*.log")) if path.stat().st_size
    )


def compare_one(terminal, seat, name, work, out):
    "Run one fixture both ways, and say how many pixels differ."
    room = out / terminal.name / name
    room.mkdir(parents=True, exist_ok=True)

    fixture_path = work / ("%s.bin" % name)
    fixture_path.write_bytes(fixture_bytes(name))

    program_path = work / ("%s.sh" % name)
    write_the_program(program_path, fixture_path)

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
        )
    except RuntimeError as reason:
        raise RuntimeError("%s\n%s" % (reason, every_log(room))) from None

    return differences(bare, through, room / "difference.png")


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

    names = [name for name in FIXTURES if ONLY in name]
    if not names:
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

    # One seat for each kind of display server that a terminal here
    # needs, and none for a kind that nothing needs.
    seats = {}
    try:
        for terminal in terminals:
            if terminal.seat not in seats:
                seats[terminal.seat] = SEATS[terminal.seat]().start(work)

        for terminal in terminals:
            for name in names:
                found = compare_one(
                    terminal, seats[terminal.seat], name, work, out
                )
                seen[(terminal.name, name)] = found
                print(
                    "%s %s: %d pixels differ" % (terminal.name, name, found),
                    flush=True,
                )
    finally:
        for seat in seats.values():
            seat.stop()

    if RECORD:
        write_the_recorded(Path(RECORD) / "picture-differences.txt", seen)
        print("Wrote the list of differences.")
        return

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

    if wrong:
        print("\n--- pymux draws something else ---", file=sys.stderr)
        for line in wrong:
            print(line, file=sys.stderr)
        print(
            "\nA difference that is right belongs in "
            "tests/picture-differences.txt, with a comment saying why:\n"
            "    PYMUX_PICTURES_RECORD=1 nix build --file . "
            "checks.pymux-pictures\n"
            "    cp result/picture-differences.txt "
            "pymux/tests/picture-differences.txt",
            file=sys.stderr,
        )
        if KEEP:
            print(
                "PYMUX_PICTURES_KEEP is set, so the result of this build is "
                "the pictures. Look at difference.png.",
                file=sys.stderr,
            )
            return
        print(
            "Run it again with PYMUX_PICTURES_KEEP=1 to keep the pictures: "
            "nix takes the output of a build that failed away.",
            file=sys.stderr,
        )
        raise SystemExit(1)

    print("Every picture is what it was recorded to be.")


if __name__ == "__main__":
    main()
