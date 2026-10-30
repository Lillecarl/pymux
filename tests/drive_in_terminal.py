"""
Run a program on a pty of our own inside a terminal, and press keys at it.

    drive_in_terminal.py <keys-file> <hold-seconds> [<fifo> <fence-seen>] -- <argv...>

`checks.pymux-pictures` photographs a terminal, and nothing in it can
press a key. A headless compositor owns no input device
(`WLR_LIBINPUT_NO_DEVICES=1`), and the terminal's pty belongs to the
terminal, so there is nowhere to write. Every fixture is therefore
bytes a program writes, which photographs what a pane draws and never
what pymux draws around it: the status line, a title bar, the command
palette, the overlay pane. Lillecarl/pymux#161.

This is the way round. It runs as the terminal's child, opens a pty of
its own, runs the program on that, and copies what the program writes
to the terminal's own tty. The terminal paints what the program really
emitted, so a picture of the terminal is a picture of the program. And
because this side owns the pty, it can write keys into it.

**It is a relay and not a terminal.** It parses nothing and answers
nothing. Bytes go from the program to the tty, and two things go the
other way into the pty: the keys, on a schedule, and whatever the
terminal says.

**The terminal's answers are the second of those, and they used to be
dropped.** A query the program writes reaches the terminal, and the
terminal answers on the tty's input -- which is this process's stdin,
not the program's. Nothing read it, so every answer was lost: the
colours the terminal draws with, the keyboard flags, the graphics
protocol, the cell size. A picture then showed a pymux that had asked
its terminal everything and heard nothing back. Lillecarl/pymux#350.

The keys file is one step a line: how long to wait, then the bytes as a
Python literal. Everything after a "#" is a comment, and a blank line
is nothing.

    0.5  b"\\x02"     # the prefix
    0.1  b'"'         # split the pane

`hold-seconds` is how long to keep copying after the last step, so the
picture is taken while the program is still on the screen. It is a
bound as well: a run that goes wrong leaves nothing behind for longer.

## The fence

With `<fifo>` and `<fence-seen>` given, the keys are fenced the way
`middleman.py` fences a write. The program runs the forwarder, which
copies the fifo to its own output, and after the last key the fence --
an OSC 99 -- goes down the fifo. Seeing it on the wire proves the
program has done with everything before it. The fence is taken back
out of what the terminal is given, and `<fence-seen>` is touched. A
picture taken after the file is a picture of a finished frame, and a
run whose fence never comes is a run that photographs nothing: the
file's absence after the harness's wait says so.

The keys are not pressed into a program that has not drawn once. With
the fifo given, the boot is fenced the same way, and the first step is
counted from the fence rather than from the clock: the first bytes a
program writes are its questions to the terminal, and a key pressed
then reaches a program that is not reading keys yet.

## Asking the terminal

With `PYMUX_RELAY_ASK` naming a directory, the relay asks the terminal
for its modes, with DECRQM, and writes what it answered to
`<label>.json` there. A line written to the fifo `ask` in that
directory asks with that line as the label, and the end of the program
asks once more as `detached`. Beside the answers goes what the program
meant: the last `CSI ? N h` or `l` it wrote for each mode. The answers
are taken out of what the program is given. Lillecarl/pymux#800.

## The timeline

Every step is written to stderr as it happens: when the first frame
came, when each key went in and how late it was, and whether the fence
came back. A run that photographed the wrong state has that file and
the program's own log beside it, and the two read together say which
key the program never acted on. Before this the file was empty, and a
lost key was a guess. Lillecarl/pymux#353.
"""

from __future__ import annotations

import ast
import base64
import contextlib
import fcntl
import json
import os
import pty
import re
import select
import selectors
import signal
import struct
import sys
import termios
import time
from pathlib import Path

# The fence, its quiet window and its first-byte bound are
# middleman's, and the relay adopts them: `middleman.py` says what
# each one is for.
from middleman import FENCE, FIRST_BYTE, QUIET, SHOWS_OSC_99, fence_sequence

#: How much to move at once.
CHUNK = 65536

#: How long the program may take to draw its first frame, in seconds.
#: A program that never draws is a run that photographs nothing, and
#: the keys are pressed at the end of this rather than never.
BOOT_TIMEOUT = 10.0

#: The modes asked of the terminal. Lillecarl/pymux#800.
ASKED_MODES = (1, 25, 1000, 1002, 1003, 1004, 1006, 1016, 1049, 2004, 2026, 2031, 2048)
_MODE_REPORT = re.compile(rb"\x1b\[\?(\d+);(\d)\$y")
_ATTRIBUTES = re.compile(rb"\x1b\[\?[\d;]*c")
_PRIVATE_MODE = re.compile(rb"\x1b\[\?([\d;]+)([hl])")

#: How long the terminal may take to answer, in seconds.
ASK_TIMEOUT = 5.0

#: The two fences, and they have to differ. Both stay in `seen` after
#: they come back -- only the copy the terminal is given has them
#: taken out -- so one token would have the keys' fence answered by
#: the boot's.
BOOT_TOKEN = base64.b64encode(b"boot")
KEYS_TOKEN = base64.b64encode(b"keys")


def note(started, text):
    "One line of the timeline, stamped from the start of the run."
    print("%7.3f  %s" % (time.monotonic() - started, text), file=sys.stderr, flush=True)


def size_of(fd):
    """
    The rows and columns of this terminal.

    The pty the program runs on has to be the size of the terminal that
    draws it, or the program lays its screen out for one size and a
    person looks at another.
    """
    try:
        packed = fcntl.ioctl(fd, termios.TIOCGWINSZ, struct.pack("HHHH", 0, 0, 0, 0))
    except OSError:
        return 24, 80
    rows, columns, _, _ = struct.unpack("HHHH", packed)
    return rows or 24, columns or 80


def set_size(fd, rows, columns):
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0))


def stop_echo(fd):
    """
    Take the echo off this pty.

    A program that draws a screen turns the echo off itself, and this
    is about the moment before it does. The terminal answers the
    queries that program sends, those answers arrive here as input, and
    a pty that echoes puts them on the screen as text. `\x1b[?62;4;22c`
    written across the top row is what that looks like, and it is still
    there when the picture is taken.

    `take_picture.py` writes `stty -echo` into every program it runs
    for the same reason. The pty belongs to this side, so it belongs
    here and not in each script.
    """
    try:
        attributes = termios.tcgetattr(fd)
    except termios.error:
        return

    attributes[3] &= ~(termios.ECHO | termios.ECHONL)
    with contextlib.suppress(termios.error):
        termios.tcsetattr(fd, termios.TCSANOW, attributes)


def take_input(fd):
    """
    Read this terminal's answers as they arrive, and never echo them.

    Two modes are in the way. **The echo** puts everything the terminal
    says on the screen as text, which is `\x1b[?62;4;22c` written
    across the top row of the picture. **The line discipline** holds
    input until a newline, and an answer carries none, so a read would
    wait for a key nobody is going to press.

    A terminal that this cannot be done to is one whose answers stay
    unread, which is where this started. Lillecarl/pymux#350.
    """
    try:
        attributes = termios.tcgetattr(fd)
    except termios.error:
        return

    attributes[3] &= ~(termios.ECHO | termios.ECHONL | termios.ICANON)
    attributes[6][termios.VMIN] = 0
    attributes[6][termios.VTIME] = 0
    with contextlib.suppress(termios.error):
        termios.tcsetattr(fd, termios.TCSANOW, attributes)


def carry_the_answer(master, stdin_fd, ready) -> None:
    "Give the program whatever the terminal said, if it said anything."
    if stdin_fd is None or stdin_fd not in ready:
        return
    try:
        piece = os.read(stdin_fd, CHUNK)
    except OSError:
        return
    ANSWERS.write(master, piece.replace(FOCUS_IN, b""))


#: What a terminal says when it takes the focus. The relay keeps it from
#: the program: the fence is a notification, and pymux keeps one from a
#: window a person looks at. A terminal has the focus until it says
#: otherwise, so the relay says it looked away (`frame_and_fence`), and a
#: terminal that reports its focus once pymux asks would undo that. Only
#: notifications and a pane's focus reports read the focus, so no
#: picture changes. Lillecarl/pymux#799, Lillecarl/pymux#806.
FOCUS_IN = b"\x1b[I"
FOCUS_OUT = b"\x1b[O"


class TerminalStream:
    """
    What the terminal says to the program, and where in an escape
    sequence it stopped.

    **The relay's own report goes between two sequences, never into
    one.** A terminal's answers arrive in pieces, cut anywhere, and a
    focus report written between two pieces broke the answer it landed
    in. pymux never counted that answer to its status report, held
    every frame after it for good, and the fence behind a frame never
    came. Lillecarl/pymux#806.
    """

    def __init__(self) -> None:
        self.state = "ground"
        self.looking_away = False

    def write(self, master: int, data: bytes) -> None:
        if data:
            os.write(master, data)
        for byte in data:
            self._step(byte)
        if self.looking_away and self.state == "ground":
            # And says it shows OSC 99, which a fence is: xterm shows
            # none, and pymux would keep the fence. Lillecarl/pymux#497.
            os.write(master, FOCUS_OUT + SHOWS_OSC_99)
            self.looking_away = False

    def look_away(self, master: int) -> None:
        "Tell the program the terminal lost the focus, as soon as that splits no sequence."
        self.looking_away = True
        self.write(master, b"")

    def _step(self, byte: int) -> None:
        if self.state == "ground":
            if byte == 0x1B:
                self.state = "escape"
        elif self.state == "escape":
            if byte == ord("["):
                self.state = "csi"
            elif byte == ord("O"):
                self.state = "ss3"
            elif byte in b"]P_^X":
                self.state = "string"
            else:
                self.state = "ground"
        elif self.state == "ss3":
            self.state = "ground"
        elif self.state == "csi":
            if 0x40 <= byte <= 0x7E:
                self.state = "ground"
        elif self.state == "string":
            if byte == 0x07:
                self.state = "ground"
            elif byte == 0x1B:
                self.state = "string escape"
        elif self.state == "string escape":
            self.state = "ground" if byte == ord("\\") else "string"


ANSWERS = TerminalStream()


#: Modes that are one setting in xterm and kitty: mouse tracking, and
#: mouse encoding. Setting one resets the others, so 1003 after 1000
#: leaves 1000 answered reset.
_ONE_OF = (frozenset({9, 1000, 1001, 1002, 1003}), frozenset({1005, 1006, 1015, 1016}))


def meant_modes(seen):
    "The last setting the program wrote for each private mode."
    meant = {}
    for match in _PRIVATE_MODE.finditer(seen):
        for number in match.group(1).split(b";"):
            if not number:
                continue
            mode = int(number)
            on = match.group(2) == b"h"
            if on:
                for group in _ONE_OF:
                    if mode in group:
                        meant.update(dict.fromkeys(group & meant.keys(), False))
            meant[mode] = on
    return meant


def ask_modes(stdin_fd, master, out):
    """
    What the terminal answers DECRQM for each of `ASKED_MODES`.

    The primary device attributes go last, as the fence: every terminal
    answers them, and in order. Whatever else the terminal says on the
    way goes on to the program, when there still is one.
    """
    out.write(b"".join(b"\x1b[?%d$p" % mode for mode in ASKED_MODES) + b"\x1b[c")
    out.flush()
    heard = bytearray()
    deadline = time.monotonic() + ASK_TIMEOUT
    while not _ATTRIBUTES.search(heard) and time.monotonic() < deadline:
        if not select.select([stdin_fd], [], [], 0.05)[0]:
            continue
        try:
            piece = os.read(stdin_fd, CHUNK)
        except OSError:
            break
        if not piece:
            break
        heard.extend(piece)
    answers = {int(match.group(1)): int(match.group(2)) for match in _MODE_REPORT.finditer(heard)}
    rest = _ATTRIBUTES.sub(b"", _MODE_REPORT.sub(b"", bytes(heard)), count=1).replace(FOCUS_IN, b"")
    if rest and master is not None:
        with contextlib.suppress(OSError):
            ANSWERS.write(master, rest)
    return answers


def answer_the_ask(ask_dir, label, stdin_fd, master, out, seen):
    "Ask the terminal, and write `<label>.json` whole, so a reader never finds half of it."
    report = {"answers": ask_modes(stdin_fd, master, out), "meant": meant_modes(bytes(seen))}
    path = ask_dir / ("%s.json" % label)
    part = path.with_suffix(".part")
    part.write_text(json.dumps(report, indent=1, sort_keys=True))
    part.rename(path)


def open_the_asks(ask_dir):
    """
    The fifo the asks come down, open for reading.

    Opened for writing as well, so a writer that comes and goes is never
    an end of file that wakes the loop for ever.
    """
    ask_dir.mkdir(parents=True, exist_ok=True)
    fifo = ask_dir / "ask"
    with contextlib.suppress(FileExistsError):
        os.mkfifo(fifo)
    return os.open(fifo, os.O_RDWR | os.O_NONBLOCK)


def wait_for_program(master, stdin_fd, timeout) -> bool:
    """
    Wait for the program to write, carrying the terminal's answers the
    other way while we wait. True when the program has something.

    **Every wait in this file goes through here.** The answers arrive
    while the program is drawing its first frame, which is the longest
    wait of the run: a loop that watched only the program would hold
    them until it happened to look somewhere else.
    """
    watching = [master] if stdin_fd is None else [master, stdin_fd]
    ready = select.select(watching, [], [], timeout)[0]
    carry_the_answer(master, stdin_fd, ready)
    return master in ready


class Quiet(float):
    """
    A step's wait that ends when the program has written nothing for
    this many seconds, rather than after them.

    A key sent a fixed time after a program starts races it: the demo
    redraws when the terminal's colour replies come in, so copy mode
    opened over one line of history more or less depending on load.
    Lillecarl/pymux#541. A quiet wait gives up after `QUIET_LIMIT`, so
    a program that never stops writing cannot hold a run.
    """


#: The longest a quiet wait waits.
QUIET_LIMIT = 10.0


def read_keys(path):
    """
    The steps of a keys file, as (seconds to wait, bytes) pairs.

    A line that does not parse is a fault and not something to skip
    over: a script whose keys silently did nothing would photograph the
    screen before them and look like a pass.

    `quiet <seconds>` on a line of its own waits for the program to be
    quiet that long, and presses nothing.
    """
    steps = []

    for number, line in enumerate(open(path), start=1):
        line = line.split("#", 1)[0].strip()
        if not line:
            continue

        try:
            delay, keys = line.split(None, 1)
            if delay == "quiet":
                steps.append((Quiet(keys), b""))
                continue
            steps.append((float(delay), ast.literal_eval(keys)))
        except (ValueError, SyntaxError) as reason:
            raise SystemExit("%s line %d: %s" % (path, number, reason))

        if not isinstance(steps[-1][1], bytes):
            raise SystemExit("%s line %d: the keys are not bytes" % (path, number))

    return steps


def due(delay, when, last_output):
    "When a step waiting `delay` after the step at `when` is due."
    if isinstance(delay, Quiet):
        return min(max(when, last_output) + delay, when + QUIET_LIMIT)
    return when + delay


def take_fifo(fifo, started):
    """
    Open the fifo for writing, which lets the forwarder in the pane
    past its own open.

    The forwarder opens the read end and blocks there, so the pane is
    running from the moment this returns. A program that never starts
    its first pane is a run that photographs nothing, and this says so
    instead of blocking for ever.
    """
    deadline = started + BOOT_TIMEOUT
    while True:
        try:
            return os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
        except OSError:
            if time.monotonic() > deadline:
                raise SystemExit("the pane never took %s" % fifo)
            time.sleep(0.05)


def read_from(master, seen):
    """
    Read what the program wrote, into `seen`, and hand it back.

    An empty answer is a pty that is gone, which is the program's end
    and the caller's to notice.
    """
    try:
        piece = os.read(master, CHUNK)
    except OSError:
        return b""
    seen.extend(piece)
    return piece


def settle(master, seen, out, copied, stdin_fd=None):
    """
    Copy until the wire has been quiet for a while, and give back
    where the copying stopped.

    A frame can arrive in pieces, and the redraw behind it can be one
    the program postponed. The fence has already done the waiting, so
    this waits for silence rather than for the clock, and it is short.
    `middleman.py` settles a write the same way.

    **The terminal speaking is not the program going quiet.**
    `wait_for_program` wakes for either and says only whether the
    program had something, so an answer arriving from the terminal used
    to end the settle at once. It ends it at the worst moment: pymux
    writes its queries, the terminal answers them, and the first frame
    is called finished after 256 bytes of questions. The keys then go
    into a pymux that has not drawn and is not reading them yet. So the
    quiet is measured, and only real silence counts.
    Lillecarl/pymux#353.

    **A window pane is never silent.** Its program draws, and pymux
    sends every frame on, so the quiet gives up after `QUIET_LIMIT` as
    a quiet step does. Lillecarl/pymux#806.
    """
    quiet_since = time.monotonic()
    deadline = quiet_since + FIRST_BYTE
    limit = quiet_since + QUIET_LIMIT
    while time.monotonic() < min(deadline, limit):
        if not wait_for_program(master, stdin_fd, QUIET):
            if time.monotonic() - quiet_since >= QUIET:
                return copied
            continue
        piece = read_from(master, seen)
        if not piece:
            return copied
        out.write(piece)
        out.flush()
        copied += len(piece)
        quiet_since = time.monotonic()
        deadline = quiet_since + FIRST_BYTE
    return copied


def wait_for_first_frame(master, seen, out, copied, started, writer, stdin_fd=None):
    """
    Wait until the program has drawn once, and say when that was.

    A key pressed into a pymux that is still starting reaches nothing,
    so the keys are counted from here and not from the clock.

    **The first bytes are not the first frame.** pymux writes its
    terminal queries before it draws anything -- 256 bytes of
    questions, measured -- and then waits for the answers. Taking those
    for the frame pressed the prefix into a program that was not
    reading keys yet, and one run of `cut-follows-the-terminal` lost
    the split exactly there. Lillecarl/pymux#353.

    So the boot is fenced, the way the keys are fenced at the end. The
    pane runs the forwarder, so the fence comes back on the wire only
    once the program has read the pane's output, drawn it, and written
    the frame to the terminal. That is the proof that the application
    is up and reading.

    A program that never draws waits out the boot, and the keys are
    pressed then: the hold covers the run that photographs nothing.

    **The fence goes again every `BOOT_RETRY` seconds.** pymux keeps a
    notification from a window a terminal with the focus shows, and a
    pymux still starting read the relay's focus report only after the
    first fence: that fence was kept, and the boot waited out all ten
    seconds. Lillecarl/pymux#806.
    """
    deadline = started + BOOT_TIMEOUT
    came = False
    while not came and time.monotonic() < deadline:
        came, copied = frame_and_fence(
            master,
            seen,
            out,
            copied,
            writer,
            None,
            BOOT_TOKEN,
            min(deadline, time.monotonic() + BOOT_RETRY),
            stdin_fd,
        )
    return came, time.monotonic(), copied


#: How long the boot fence is waited for before it goes again.
BOOT_RETRY = 1.0


def copy_unfenced(seen, out, copied):
    """
    Copy what arrived while a fence is on its way, with the fences
    taken out, and give back where the copying stopped.

    **Held back, the frame deadlocks the fence.** pymux puts a status
    report behind each frame and draws no next frame until the terminal
    answers it, and a pane's notification goes out behind a frame. A
    terminal that never saw the question never answers, so the fence
    never came once a window pane drew. Only a tail that could still
    become a fence waits for more. Lillecarl/pymux#806.
    """
    end = len(seen)
    start = seen.rfind(b"\x1b", copied)
    if start != -1:
        tail = bytes(seen[start:])
        opening = tail.startswith(FENCE_OPENING) and not FENCE.fullmatch(tail)
        if opening or FENCE_OPENING.startswith(tail):
            end = start
    if end > copied:
        out.write(FENCE.sub(b"", bytes(seen[copied:end])))
        out.flush()
    return max(copied, end)


#: How every fence starts, as `fence_sequence` writes it.
FENCE_OPENING = b"\x1b]99;"


def frame_and_fence(master, seen, out, copied, writer, mark, token, deadline, stdin_fd=None):
    """
    The frame for what came before, then the fence, then the quiet
    after it. Gives back whether the fence came, and where the copying
    stopped.

    `middleman.py` fences a write by putting an OSC 99 behind it:
    seeing it on the wire proves the pane consumed what came before.
    The keys here are the payload, the fifo is the way in, and the
    fence proves the program has done with every one of them. The
    frame can still follow the fence, because a redraw may be
    postponed, so a quiet window comes after it, and it is short
    because the fence has already done the waiting.

    `mark` is how many bytes had arrived when the payload went in, and
    the wait for one more is the frame that payload asked for. The
    boot has none: nothing was sent, and what is waited for is the
    program's own first drawing.

    What arrives while the fence is on its way goes to the terminal
    with the fence taken back out (`copy_unfenced`): a stream that
    holds our own scaffolding is a stream nobody can read.
    """
    while mark is not None and len(seen) <= mark:
        if time.monotonic() > deadline:
            return False, copied
        if not wait_for_program(master, stdin_fd, 0.05):
            continue
        piece = read_from(master, seen)
        if not piece:
            return False, copied
        out.write(piece)
        out.flush()
        copied += len(piece)

    # Before every fence and not once: pymux drops a focus report that
    # comes before the client it belongs to, and a client starts out
    # focused. The settle gives pymux the time to read it first.
    ANSWERS.look_away(master)
    copied = settle(master, seen, out, copied, stdin_fd)

    os.write(writer, fence_sequence(token))
    while token not in seen:
        if time.monotonic() > deadline:
            return False, copied
        if not wait_for_program(master, stdin_fd, 0.05):
            continue
        if not read_from(master, seen):
            return False, copied
        copied = copy_unfenced(seen, out, copied)

    # `copied` counts the bytes of `seen`, fences and all: pymux writes
    # an OSC 99 of its own, so a count of the cleaned stream drifts.
    out.write(FENCE.sub(b"", bytes(seen[copied:])))
    out.flush()
    copied = len(seen)

    copied = settle(master, seen, out, copied, stdin_fd)
    return True, copied


def relay(argv, steps, hold, fifo=None, fence_seen=None, ask_dir=None):
    """
    Run `argv` on a pty, copy it to this terminal, and press the keys.

    Returns what the program's exit status was, or `None` when it was
    still running when the hold ran out. Either is a normal end: the
    programs this drives are the ones a picture is taken of, and a
    picture is taken while they are still up.

    With the fifo and the fence file given, the keys are fenced:
    `frame_and_fence` says how, and the file is touched when
    the fence has come back.
    """
    rows, columns = size_of(sys.stdout.fileno())

    pid, master = pty.fork()
    if pid == 0:
        # The child. Its stdin, stdout and stderr are the pty already.
        try:
            set_size(sys.stdout.fileno(), rows, columns)
            os.execvp(argv[0], argv)
        except BaseException:
            os._exit(126)

    set_size(master, rows, columns)
    stop_echo(master)

    # This terminal's own input, where its answers arrive. A run with
    # no terminal -- a test that gives this a pipe -- has none, and
    # then the program hears nothing back, as before.
    # Lillecarl/pymux#350.
    stdin_fd = sys.stdin.fileno()
    if not os.isatty(stdin_fd):
        stdin_fd = None
    else:
        take_input(stdin_fd)

    started = time.monotonic()
    when = started
    waiting = list(steps)
    finished = None
    seen = bytearray()
    copied = 0

    out = sys.stdout.buffer

    if fifo is not None:
        # The fifo is ours to make, the way middleman.py makes its
        # own. Opening for writing is what lets the forwarder in the
        # pane past its own open, and the first frame is what the keys
        # are counted from.
        with contextlib.suppress(FileExistsError):
            os.mkfifo(fifo)
        writer = take_fifo(fifo, started)
        note(started, "the pane took the fifo")
        # Long before the boot fence, which `frame_and_fence` alone
        # raced: the fence came 3ms ahead of its focus report.
        ANSWERS.look_away(master)
        drawn, when, copied = wait_for_first_frame(master, seen, out, copied, started, writer, stdin_fd)
        note(
            started,
            "the first frame: the boot fence came back, %d bytes" % (len(seen),)
            if drawn
            else "the first frame: no boot fence in %gs, pressing the keys anyway" % (BOOT_TIMEOUT,),
        )
    else:
        writer = None
        note(started, "no fifo, so the keys are counted from the clock")

    # The fence that proves the keys were consumed, and the file that
    # says it happened.
    fence_pending = fence_seen is not None
    last_output = when

    selector = selectors.DefaultSelector()
    selector.register(master, selectors.EVENT_READ)
    if stdin_fd is not None:
        selector.register(stdin_fd, selectors.EVENT_READ)
    asks = None
    if ask_dir is not None and stdin_fd is not None:
        asks = open_the_asks(ask_dir)
        selector.register(asks, selectors.EVENT_READ)

    try:
        while True:
            now = time.monotonic()

            # The keys that are due. Each delay is counted from the
            # step before it -- or, for the first one, from the first
            # frame -- so a script reads as a sequence of waits and not
            # as a list of absolute times.
            mark = None
            while waiting and now >= due(waiting[0][0], when, last_output):
                delay, keys = waiting.pop(0)
                if isinstance(delay, Quiet):
                    note(started, "quiet for %gs" % (delay,) if now < when + QUIET_LIMIT else "never quiet")
                    when = now
                    continue
                when += delay
                os.write(master, keys)
                # How late says whether the schedule held. A step
                # pressed on time into a program that was busy is the
                # one that gets lost.
                note(
                    started,
                    "key %r, %.3fs late" % (keys, now - when) if keys else "a wait, %.3fs late" % (now - when,),
                )
                # The mark is where the wire was when the keys went in,
                # and the fence waits for one byte past it. **A step
                # that presses nothing asks for no frame**: it is a
                # wait, and the frame for the key before it arrived
                # during that wait. Taking the mark there left the
                # fence waiting for a frame nothing was going to draw,
                # and `which-key` never produced a picture.
                # Lillecarl/pymux#364.
                mark = len(seen) if keys else None

            if waiting:
                until = max(0.0, due(waiting[0][0], when, last_output) - now)
            elif fence_pending:
                fence_pending = False
                came, copied = frame_and_fence(
                    master,
                    seen,
                    out,
                    copied,
                    writer,
                    mark,
                    KEYS_TOKEN,
                    started + hold,
                    stdin_fd,
                )
                note(started, "the fence came" if came else "the fence never came")
                if came:
                    fence_seen.touch()
                until = max(0.0, started + hold - now)
            elif finished is None:
                until = max(0.0, started + hold - now)
                if until == 0.0:
                    break
            else:
                # The program ended. Keep copying for a moment so that
                # what it wrote last is on the screen, then stop.
                until = 0.1

            for key, _events in selector.select(timeout=until):
                if key.fd == asks:
                    for label in os.read(asks, CHUNK).decode().split():
                        answer_the_ask(ask_dir, label, stdin_fd, master, out, seen)
                        note(started, "asked the terminal, as %s" % (label,))
                    continue
                if key.fd != master:
                    carry_the_answer(master, stdin_fd, [stdin_fd])
                    continue

                piece = read_from(master, seen)

                if not piece:
                    # The pty closed, so the program has gone.
                    if finished is None:
                        finished = wait_for(pid)
                        note(started, "the program ended, status %s" % (finished,))
                        if asks is not None:
                            answer_the_ask(ask_dir, "detached", stdin_fd, None, out, seen)
                            note(started, "asked the terminal, as detached")
                    if not waiting and not fence_pending:
                        return finished
                    break

                out.write(piece)
                out.flush()
                copied += len(piece)
                last_output = time.monotonic()
    finally:
        selector.close()
        os.close(master)
        if asks is not None:
            os.close(asks)
        if finished is None:
            os.kill(pid, signal.SIGKILL)
            wait_for(pid)

    return finished


def wait_for(pid):
    "The exit status of the child, or `None` when there is none to get."
    try:
        _, status = os.waitpid(pid, 0)
    except OSError:
        return None
    return status


def main(argv):
    if "--" not in argv:
        raise SystemExit(__doc__.strip().splitlines()[2].strip())

    mine = argv[: argv.index("--")]
    theirs = argv[argv.index("--") + 1 :]

    if len(mine) not in (2, 4) or not theirs:
        raise SystemExit(__doc__.strip().splitlines()[2].strip())

    steps = read_keys(mine[0])
    hold = float(mine[1])
    fifo = mine[2] if len(mine) == 4 else None
    fence_seen = Path(mine[3]) if len(mine) == 4 else None

    ask_dir = os.environ.get("PYMUX_RELAY_ASK")
    status = relay(theirs, steps, hold, fifo, fence_seen, Path(ask_dir) if ask_dir else None)

    # A program that was still running when the hold ran out is the
    # normal case, and it is not a failure.
    if status is None:
        return 0
    if os.WIFEXITED(status):
        return os.WEXITSTATUS(status)
    return 128 + os.WTERMSIG(status)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
