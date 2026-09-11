"""
What a running server can be asked about itself.

A pymux server is a daemon a person leaves running for weeks, and until
this there was no way to ask it anything. The log said what it had done;
nothing said what it was doing **now**. A server using half a core could
only be diagnosed by reproducing it somewhere else.

py-spy is the usual answer and it is not available here: it needs
`ptrace`, and a machine with `kernel.yama.ptrace_scope` at 1 lets only an
ancestor attach. A person can `sudo`; a person on another machine cannot.
So a server answers for itself. Lillecarl/pymux#249.

## Two ways in, because they fail differently

**A command**, over the socket a client already uses. `pymux dump-stacks`
reaches the server, the server looks at itself and writes a file. It
gives the most: the threads, every asyncio task and where each one is
waiting.

**A signal**, which is the case the command cannot answer. A wedged loop
never reads the packet, and that is exactly when a stack is worth having.
`SIGUSR1` goes through `faulthandler`, whose handler is C and runs when
the interpreter is stuck in a call that no Python code will return from.
It gives less -- the stacks and nothing else -- and it gives them always.

## Where a dump goes

Beside the log, because that is the directory a person already looks in,
and a dump is only useful with the log around it: the log says what led
here, and the dump says where "here" is.
"""

import faulthandler
import os
import signal
import sys
import threading
import time
import traceback
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING

from .log import logger, level, logfile

if TYPE_CHECKING:
    from pymux.main import Pymux

__all__ = [
    "Counters",
    "write_dump",
    "answer_a_signal",
    "stacks_file",
    "where_a_dump_goes",
]

#: The file `faulthandler` writes to, held open for the life of the
#: process. A signal handler cannot open a file: it runs between two
#: bytecodes, or inside a C call, and it may not allocate.
_stacks_file = None


class Counters:
    """
    What this server has done since it started.

    **The cheap half of a diagnosis, and the half a stack cannot give.**
    A stack says where the server is in one instant. These say what it
    has been doing for an hour: "eleven frames a second, and every one
    of them because an application asked" is a whole finding, and it
    names the fault before anybody reads a line of Python.

    It was read out of the log the first time, which cost an 86 MB file
    (Lillecarl/pymux#248). These are the same numbers with nothing
    written down.

    Two counts and no more, because each one has to be free. A server
    pays for them on every frame.
    """

    def __init__(self) -> None:
        self.started = time.time()

        #: How many times each `Woke` reason asked for a frame. The
        #: reason is the whole value of this: a count of invalidates
        #: says a server is busy, and the reasons say what is doing it.
        self.invalidates: "Counter[str]" = Counter()

        #: Frames that reached a client, and how many characters they
        #: were. A frame that went out is one a client had to draw, so
        #: this is the work of the whole route and not only of here.
        self.frames = 0
        self.frame_bytes = 0

    def invalidated(self, reason: str) -> None:
        self.invalidates[reason] += 1

    def frame_went_out(self, characters: int) -> None:
        self.frames += 1
        self.frame_bytes += characters


def where_a_dump_goes() -> Path:
    "The directory a dump is written to: the one the log is in."
    return (logfile() or Path.home() / ".local/state/pymux/server.log").parent


def stacks_file() -> Path:
    "Where a `SIGUSR1` writes."
    return where_a_dump_goes() / ("stacks-%d.log" % (os.getpid(),))


def answer_a_signal() -> Path | None:
    """
    Make `SIGUSR1` dump every thread's stack, and say where it goes.

    **`faulthandler` and not a handler of our own.** A Python handler
    runs between two bytecodes, so a loop stuck inside one C call never
    reaches it -- and a stuck loop is the reason somebody sends this
    signal. `faulthandler` installs a handler in C that writes the
    stacks with no interpreter of its own, which works either way.

    It is not the richer answer. It has no tasks and no counters,
    because a signal handler may not build them. `pymux dump-stacks`
    is where those are, and it needs a loop that still turns.

    `None` when the signal could not be taken: `signal.signal` only
    works on the main thread, and a test that builds a server on
    another one is not a server.
    """
    global _stacks_file

    path = stacks_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _stacks_file = open(path, "a", buffering=1)
        faulthandler.register(
            signal.SIGUSR1, file=_stacks_file, all_threads=True, chain=False
        )
    except (OSError, ValueError, RuntimeError):
        # A read only home, a full disk, or not the main thread.
        _stacks_file = None
        return None

    logger.info("SIGUSR1 dumps the stacks of this server to %s", path)
    return path


#: `prctl` numbers, from `linux/prctl.h`. ctypes has no header to read.
_PR_SET_PTRACER = 0x59616D61
_PR_SET_PTRACER_ANY = -1


def let_a_debugger_attach(allowed: bool) -> bool:
    """
    Say whether another process of this user may attach to this server,
    and answer with what the kernel took.

    Python 3.14 can attach a debugger to a running process: `python -m
    pdb -p <pid>`, and `sys.remote_exec` under it (PEP 768). py-spy
    reads a process the same way. Both need `ptrace` of the target, and
    a machine with `kernel.yama.ptrace_scope` at 1 gives that only to an
    ancestor. `PR_SET_PTRACER` is how a process says otherwise.

    **Off unless a person asks, because it is not a small permission.**
    `PR_SET_PTRACER_ANY` lets any process running as this user read this
    one's memory and write to it, and a pymux server holds the
    scrollback of every pane. `pymux dump-stacks` and `SIGUSR1` need
    none of that, and they answer most questions.

    False on a kernel with no `prctl` and on anything that is not Linux,
    which is the honest answer rather than a failure: the server keeps
    running, and nothing can attach.
    """
    # Here, not at the top: a server that nobody debugs should not load
    # ctypes at all.
    import ctypes

    try:
        libc = ctypes.CDLL(None, use_errno=True)
        took = libc.prctl(
            _PR_SET_PTRACER,
            ctypes.c_ulong(_PR_SET_PTRACER_ANY if allowed else 0),
            0,
            0,
            0,
        )
    except (AttributeError, OSError, TypeError):
        logger.info("This system has no prctl, so nothing can attach to it.")
        return False

    if took != 0:
        logger.warning("The kernel refused PR_SET_PTRACER: %d", ctypes.get_errno())
        return False

    if allowed:
        logger.warning(
            "Any process of this user may now attach to this server: "
            "`python -m pdb -p %d`. It can read every pane's scrollback.",
            os.getpid(),
        )
    else:
        logger.info("Nothing outside this server may attach to it.")

    return allowed


def write_dump(pymux: "Pymux") -> Path:
    """
    Write down what this server is doing now, and answer with the file.

    Everything it knows, in one file, because a person reading it is
    looking for something they cannot name yet.
    """
    path = where_a_dump_goes() / ("dump-%d-%s.txt" % (os.getpid(), _now()))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(what_it_is_doing(pymux))
    logger.info("Wrote a dump of this server to %s", path)
    return path


#: How long `pymux profile` watches, in seconds, when nobody says.
#:
#: Long enough to hold a few hundred samples at a millisecond, short
#: enough that a person waits for it rather than forgetting it is on.
HOW_LONG_TO_WATCH = 5.0

#: How often the profiler takes the stack, in seconds. The same
#: interval `tests/profile_a_frame.py` uses.
HOW_OFTEN_TO_LOOK = 0.001


def start_watching(pymux: "Pymux", seconds: float = HOW_LONG_TO_WATCH) -> Path:
    """
    Watch this server for a few seconds, and answer with the file it
    will land in.

    **It returns at once and the server keeps running.** A profile of a
    server has to be taken while the server serves; stopping it to look
    at it would measure a different program. The loop is asked to stop
    the profiler later, so this call costs the client nothing.

    **`async_mode="disabled"`, which is the one that sees everything.**
    pyinstrument's default attributes the time of an `await` to the
    function that awaited, and it can only do that for the async context
    it started in. This starts inside the task that is running the
    command, and that task ends immediately -- so the default would
    report the whole server as `<out-of-context>`. Disabled interleaves
    every coroutine and the loop's own machinery, which is exactly what
    "where does this server's time go" asks for.

    The tasks are written beside it, taken at the end. Between them they
    are the whole async picture: the profile says which code burned the
    processor, and the tasks say what everything else was waiting for.
    """
    # Here, not at the top of the file: a server that never profiles
    # anything should not pay to import this.
    from pyinstrument import Profiler

    where = where_a_dump_goes()
    where.mkdir(parents=True, exist_ok=True)
    stem = "profile-%d-%s" % (os.getpid(), _now())
    written = where / ("%s.txt" % (stem,))

    counters = pymux.counters
    before = (counters.frames, counters.frame_bytes, Counter(counters.invalidates))

    profiler = Profiler(interval=HOW_OFTEN_TO_LOOK, async_mode="disabled")
    profiler.start()

    def stop() -> None:
        profiler.stop()
        try:
            written.write_text(
                "\n".join(
                    [
                        _server(pymux),
                        "",
                        _over_the_window(counters, before, seconds),
                        "",
                        profiler.output_text(unicode=True, color=False, show_all=False),
                        "",
                        _tasks(pymux),
                        "",
                    ]
                )
            )
            (where / ("%s.html" % (stem,))).write_text(profiler.output_html())
        except Exception:
            logger.exception("Could not write the profile of this server.")
            return

        logger.info("Watched this server for %.1fs, and wrote %s", seconds, written)

    pymux.loop.call_later(seconds, stop)
    return written


def _over_the_window(counters: "Counters", before, seconds: float) -> str:
    "What the server did while the profiler watched, and nothing before."
    frames, frame_bytes, invalidates = before
    moved = Counter(counters.invalidates)
    moved.subtract(invalidates)

    lines = [
        "--- what it did over these %.1f seconds ---" % (seconds,),
        "",
        "%-46s %10s %9s" % ("", "total", "per second"),
        "%-46s %10d %9.2f"
        % (
            "frames out",
            counters.frames - frames,
            (counters.frames - frames) / seconds,
        ),
        "",
        "%-46s %10s %9s" % ("what asked for a frame", "total", "per second"),
    ]
    for reason, times in moved.most_common():
        if times > 0:
            lines.append("%-46s %10d %9.2f" % (reason[:46], times, times / seconds))

    return "\n".join(lines)


def what_it_is_doing(pymux: "Pymux") -> str:
    "The whole answer, as text."
    return "\n".join(
        [
            _server(pymux),
            "",
            counters(pymux),
            "",
            _threads(),
            "",
            _tasks(pymux),
            "",
        ]
    )


def counters(pymux: "Pymux") -> str:
    """
    What this server has done, and how often.

    A rate and not only a total: a server that has been up for four days
    has large totals whatever it is doing, and the question is always
    what it is doing **now** compared to what it should be.
    """
    counters = pymux.counters
    seconds = max(1e-9, time.time() - counters.started)

    lines = [
        "--- what it has done, over %s ---" % (_for_how_long(seconds),),
        "",
        "%-46s %10s %9s" % ("", "total", "per second"),
        "%-46s %10d %9.2f" % ("frames out", counters.frames, counters.frames / seconds),
        "%-46s %10d %9.0f"
        % ("characters in them", counters.frame_bytes, counters.frame_bytes / seconds),
        "",
        "%-46s %10s %9s" % ("what asked for a frame", "total", "per second"),
    ]

    for reason, times in counters.invalidates.most_common():
        lines.append("%-46s %10d %9.2f" % (reason[:46], times, times / seconds))

    if not counters.invalidates:
        lines.append("nothing has.")

    return "\n".join(lines)


def _now() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def _server(pymux: "Pymux") -> str:
    "One paragraph: which server this is, and how big it has got."
    windows = list(pymux.arrangement.windows)
    panes = [pane for window in windows for pane in window.panes]

    return "\n".join(
        [
            "pymux server %d, %s" % (os.getpid(), sys.version.split()[0]),
            "up %s, since %s"
            % (
                _for_how_long(time.time() - pymux.created),
                time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(pymux.created)),
            ),
            "%d clients, %d windows, %d panes"
            % (len(pymux.apps), len(windows), len(panes)),
            # And the level, because `set-option log-level debug`
            # reaches a running server and pymux has no `show-options`
            # to read it back with. A person who turned it up and
            # forgot needs somewhere to find out. Lillecarl/pymux#252.
            "log %s, at %s" % (logfile() or "nowhere", level()),
            "a debugger may attach: %s"
            % ("yes" if pymux.allow_remote_debugging else "no"),
        ]
    )


def _for_how_long(seconds: float) -> str:
    days, rest = divmod(int(seconds), 86400)
    hours, rest = divmod(rest, 3600)
    minutes, seconds = divmod(rest, 60)
    if days:
        return "%dd %dh %dm" % (days, hours, minutes)
    if hours:
        return "%dh %dm %ds" % (hours, minutes, seconds)
    return "%dm %ds" % (minutes, seconds)


def _threads() -> str:
    """
    Every thread of this process, and where it is.

    `sys._current_frames` answers for all of them at once, which is what
    makes this one moment rather than several. A thread that changed
    frame while the others were being formatted would otherwise be
    reported from a different instant than its neighbours.
    """
    frames = sys._current_frames()
    named = {thread.ident: thread.name for thread in threading.enumerate()}

    lines = ["--- threads (%d) ---" % (len(frames),)]
    for ident, frame in sorted(frames.items()):
        lines.append("")
        lines.append("thread %s (%s)" % (named.get(ident, "?"), ident))
        lines.extend(
            "  " + line for line in "".join(traceback.format_stack(frame)).splitlines()
        )

    return "\n".join(lines)


def _tasks(pymux: "Pymux") -> str:
    """
    Every asyncio task, and what it waits for.

    **This is the half a thread dump cannot show.** One thread runs the
    event loop, so a thread dump of a server says "the loop is polling"
    and nothing else. The tasks are where the work of a server is: a
    connection reading its client, a packet on its way out, a pane being
    torn down. A task that never finishes is a leak
    (Lillecarl/pymux#226), and one that waits on something that will
    never come is a hang.
    """
    # Here, not at the top: this module is imported by the server, and
    # `asyncio` is the loop underneath anyio rather than what pymux
    # writes against.
    import asyncio

    loop = getattr(pymux, "loop", None)
    if loop is None:
        return "--- asyncio tasks ---\n\nno loop"

    try:
        tasks = sorted(asyncio.all_tasks(loop), key=lambda task: task.get_name())
    except RuntimeError:
        # `all_tasks` of a loop that is not running.
        return "--- asyncio tasks ---\n\nthe loop is not running"

    lines = ["--- asyncio tasks (%d) ---" % (len(tasks),)]
    for task in tasks:
        lines.append("")
        lines.append("task %s: %s" % (task.get_name(), _what_a_task_is_doing(task)))
        for frame in task.get_stack(limit=20):
            lines.extend(
                "  " + line
                for line in "".join(traceback.format_stack(frame, limit=1)).splitlines()
            )

    return "\n".join(lines)


def _what_a_task_is_doing(task) -> str:
    if task.cancelled():
        return "cancelled"
    if task.done():
        problem = task.exception()
        return "raised %r" % (problem,) if problem else "done"

    coroutine = task.get_coro()
    return "pending in %s" % (getattr(coroutine, "__qualname__", coroutine),)
