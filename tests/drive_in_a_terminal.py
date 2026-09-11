"""
Run a program on a pty of our own inside a terminal, and press keys at it.

    drive_in_a_terminal.py <keys-file> <hold-seconds> -- <argv...>

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
nothing. Bytes go one way from the program to the tty, and keys go the
other way on a schedule. A query the program sends reaches the real
terminal, and the answer reaches the program, because the tty is the
one the terminal is drawing.

The keys file is one step a line: how long to wait, then the bytes as a
Python literal. Everything after a "#" is a comment, and a blank line
is nothing.

    0.5  b"\\x02"     # the prefix
    0.1  b'"'         # split the pane

`hold-seconds` is how long to keep copying after the last step, so the
picture is taken while the program is still on the screen. It is a
bound as well: a run that goes wrong leaves nothing behind for longer.
"""

import ast
import fcntl
import os
import pty
import selectors
import signal
import struct
import sys
import termios
import time

#: How much to move at once.
CHUNK = 65536


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


def set_the_size(fd, rows, columns):
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0))


def stop_the_echo(fd):
    """
    Take the echo off this pty.

    A program that draws a screen turns the echo off itself, and this
    is about the moment before it does. The terminal answers the
    queries that program sends, those answers arrive here as input, and
    a pty that echoes puts them on the screen as text. `\x1b[?62;4;22c`
    written across the top row is what that looks like, and it is still
    there when the picture is taken.

    `take_a_picture.py` writes `stty -echo` into every program it runs
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


def read_the_keys(path):
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


def relay(argv, steps, hold):
    """
    Run `argv` on a pty, copy it to this terminal, and press the keys.

    Returns what the program's exit status was, or `None` when it was
    still running when the hold ran out. Either is a normal end: the
    programs this drives are the ones a picture is taken of, and a
    picture is taken while they are still up.
    """
    rows, columns = size_of(sys.stdout.fileno())

    pid, master = pty.fork()
    if pid == 0:
        # The child. Its stdin, stdout and stderr are the pty already.
        try:
            set_the_size(sys.stdout.fileno(), rows, columns)
            os.execvp(argv[0], argv)
        except BaseException:
            os._exit(126)

    set_the_size(master, rows, columns)
    stop_the_echo(master)

    started = time.monotonic()
    when = started
    waiting = list(steps)
    finished = None

    selector = selectors.DefaultSelector()
    selector.register(master, selectors.EVENT_READ)

    out = sys.stdout.buffer

    try:
        while True:
            now = time.monotonic()

            # The keys that are due. Each delay is counted from the
            # step before it, so a script reads as a sequence of waits
            # and not as a list of absolute times.
            while waiting and now >= when + waiting[0][0]:
                delay, keys = waiting.pop(0)
                when += delay
                os.write(master, keys)

            if waiting:
                until = when + waiting[0][0] - now
            elif finished is None:
                until = max(0.0, started + hold - now)
                if until == 0.0:
                    break
            else:
                # The program ended. Keep copying for a moment so that
                # what it wrote last is on the screen, then stop.
                until = 0.1

            for _ in selector.select(timeout=until):
                try:
                    piece = os.read(master, CHUNK)
                except OSError:
                    piece = b""

                if not piece:
                    # The pty closed, so the program has gone.
                    if finished is None:
                        finished = wait_for(pid)
                    if not waiting:
                        return finished
                    break

                out.write(piece)
                out.flush()
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

    if len(mine) != 2 or not theirs:
        raise SystemExit(__doc__.strip().splitlines()[2].strip())

    steps = read_the_keys(mine[0])
    hold = float(mine[1])

    status = relay(theirs, steps, hold)

    # A program that was still running when the hold ran out is the
    # normal case, and it is not a failure.
    if status is None:
        return 0
    if os.WIFEXITED(status):
        return os.WEXITSTATUS(status)
    return 128 + os.WTERMSIG(status)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
