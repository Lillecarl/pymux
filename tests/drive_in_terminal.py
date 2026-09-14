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
an OSC 52 -- goes down the fifo. Seeing it on the wire proves the
program has done with everything before it. The fence is taken back
out of what the terminal is given, and `<fence-seen>` is touched. A
picture taken after the file is a picture of a finished frame, and a
run whose fence never comes is a run that photographs nothing: the
file's absence after the harness's wait says so.

The keys are not pressed into a program that has not drawn once. With
the fifo given, the first step is counted from the first frame, which
the bytes on the wire prove, and not from the clock.

## The timeline

Every step is written to stderr as it happens: when the first frame
came, when each key went in and how late it was, and whether the fence
came back. A run that photographed the wrong state has that file and
the program's own log beside it, and the two read together say which
key the program never acted on. Before this the file was empty, and a
lost key was a guess. Lillecarl/pymux#353.
"""

import ast
import base64
import fcntl
import os
import pty
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
from middleman import FENCE, FIRST_BYTE, QUIET

#: How much to move at once.
CHUNK = 65536

#: How long the program may take to draw its first frame, in seconds.
#: A program that never draws is a run that photographs nothing, and
#: the keys are pressed at the end of this rather than never.
BOOT_TIMEOUT = 10.0


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
    try:
        termios.tcsetattr(fd, termios.TCSANOW, attributes)
    except termios.error:
        pass


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
    try:
        termios.tcsetattr(fd, termios.TCSANOW, attributes)
    except termios.error:
        pass


def carry_the_answer(master, stdin_fd, ready) -> None:
    "Give the program whatever the terminal said, if it said anything."
    if stdin_fd is None or stdin_fd not in ready:
        return
    try:
        piece = os.read(stdin_fd, CHUNK)
    except OSError:
        return
    if piece:
        os.write(master, piece)


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


def read_keys(path):
    """
    The steps of a keys file, as (seconds to wait, bytes) pairs.

    A line that does not parse is a fault and not something to skip
    over: a script whose keys silently did nothing would photograph the
    screen before them and look like a pass.
    """
    steps = []

    for number, line in enumerate(open(path), start=1):
        line = line.split("#", 1)[0].strip()
        if not line:
            continue

        try:
            delay, keys = line.split(None, 1)
            steps.append((float(delay), ast.literal_eval(keys)))
        except (ValueError, SyntaxError) as reason:
            raise SystemExit("%s line %d: %s" % (path, number, reason))

        if not isinstance(steps[-1][1], bytes):
            raise SystemExit("%s line %d: the keys are not bytes" % (path, number))

    return steps


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
    """
    quiet_since = time.monotonic()
    deadline = quiet_since + FIRST_BYTE
    while time.monotonic() < deadline:
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


def wait_for_first_frame(master, seen, out, copied, started, stdin_fd=None):
    """
    Copy the program's first frame and its quiet, and say when it was.

    A key pressed into a pymux that is still starting reaches nothing,
    so the keys are counted from here and not from the clock. The
    first bytes the program writes are the frame, and the quiet after
    them is the whole of it. A program that never draws waits out the
    boot, and the keys are pressed then: the hold covers the run that
    photographs nothing.
    """
    deadline = started + BOOT_TIMEOUT
    while not seen and time.monotonic() < deadline:
        if not wait_for_program(master, stdin_fd, 0.05):
            continue
        piece = read_from(master, seen)
        if not piece:
            break
        out.write(piece)
        out.flush()
        copied += len(piece)
    copied = settle(master, seen, out, copied, stdin_fd)
    return time.monotonic(), copied


def frame_and_fence(
    master, seen, out, copied, writer, mark, token, deadline, stdin_fd=None
):
    """
    The frame for the last key, then the fence, then the quiet after
    it. Gives back whether the fence came, and where the copying
    stopped.

    `middleman.py` fences a write by putting an OSC 52 behind it:
    seeing it on the wire proves the pane consumed what came before.
    The keys here are the payload, the fifo is the way in, and the
    fence proves the program has done with every one of them. The
    frame can still follow the fence, because a redraw may be
    postponed, so a quiet window comes after it, and it is short
    because the fence has already done the waiting.

    Nothing is copied while the fence is on its way, and what has
    arrived when it does goes to the terminal with the fence taken
    back out: a stream that holds our own scaffolding is a stream
    nobody can read.
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

    copied = settle(master, seen, out, copied, stdin_fd)

    os.write(writer, b"\x1b]52;c;%s\x07" % token)
    while token not in seen:
        if time.monotonic() > deadline:
            return False, copied
        if not wait_for_program(master, stdin_fd, 0.05):
            continue
        if not read_from(master, seen):
            return False, copied

    clean = FENCE.sub(b"", bytes(seen))
    out.write(clean[copied:])
    out.flush()
    copied = len(clean)

    copied = settle(master, seen, out, copied, stdin_fd)
    return True, copied


def relay(argv, steps, hold, fifo=None, fence_seen=None):
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
        try:
            os.mkfifo(fifo)
        except FileExistsError:
            pass
        writer = take_fifo(fifo, started)
        note(started, "the pane took the fifo")
        when, copied = wait_for_first_frame(
            master, seen, out, copied, started, stdin_fd
        )
        note(started, "the first frame, %d bytes" % (len(seen),))
    else:
        writer = None
        note(started, "no fifo, so the keys are counted from the clock")

    # The fence, which proves the keys were consumed, and the file
    # that says it happened.
    token = base64.b64encode(b"fence")
    fence_pending = fence_seen is not None

    selector = selectors.DefaultSelector()
    selector.register(master, selectors.EVENT_READ)
    if stdin_fd is not None:
        selector.register(stdin_fd, selectors.EVENT_READ)

    try:
        while True:
            now = time.monotonic()

            # The keys that are due. Each delay is counted from the
            # step before it -- or, for the first one, from the first
            # frame -- so a script reads as a sequence of waits and not
            # as a list of absolute times.
            mark = None
            while waiting and now >= when + waiting[0][0]:
                delay, keys = waiting.pop(0)
                when += delay
                os.write(master, keys)
                # How late says whether the schedule held. A step
                # pressed on time into a program that was busy is the
                # one that gets lost.
                note(started, "key %r, %.3fs late" % (keys, now - when))
                if not waiting:
                    mark = len(seen)

            if waiting:
                until = when + waiting[0][0] - now
            elif fence_pending:
                fence_pending = False
                came, copied = frame_and_fence(
                    master,
                    seen,
                    out,
                    copied,
                    writer,
                    mark,
                    token,
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
                if key.fd != master:
                    carry_the_answer(master, stdin_fd, [stdin_fd])
                    continue

                piece = read_from(master, seen)

                if not piece:
                    # The pty closed, so the program has gone.
                    if finished is None:
                        finished = wait_for(pid)
                        note(started, "the program ended, status %s" % (finished,))
                    if not waiting and not fence_pending:
                        return finished
                    break

                out.write(piece)
                out.flush()
                copied += len(piece)
    finally:
        selector.close()
        os.close(master)
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

    status = relay(theirs, steps, hold, fifo, fence_seen)

    # A program that was still running when the hold ran out is the
    # normal case, and it is not a failure.
    if status is None:
        return 0
    if os.WIFEXITED(status):
        return os.WEXITSTATUS(status)
    return 128 + os.WTERMSIG(status)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
