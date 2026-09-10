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
from pathlib import Path
from typing import TYPE_CHECKING

from .log import logger, the_logfile

if TYPE_CHECKING:
    from pymux.main import Pymux

__all__ = [
    "a_dump",
    "answer_a_signal",
    "the_stacks_file",
    "where_a_dump_goes",
]

#: The file `faulthandler` writes to, held open for the life of the
#: process. A signal handler cannot open a file: it runs between two
#: bytecodes, or inside a C call, and it may not allocate.
_stacks_file = None


def where_a_dump_goes() -> Path:
    "The directory a dump is written to: the one the log is in."
    return (the_logfile() or Path.home() / ".local/state/pymux/server.log").parent


def the_stacks_file() -> Path:
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

    path = the_stacks_file()
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


def a_dump(pymux: "Pymux") -> Path:
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


def what_it_is_doing(pymux: "Pymux") -> str:
    "The whole answer, as text."
    return "\n".join(
        [
            _the_server(pymux),
            "",
            _the_threads(),
            "",
            _the_tasks(pymux),
            "",
        ]
    )


def _now() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def _the_server(pymux: "Pymux") -> str:
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
            "log %s" % (the_logfile() or "nowhere",),
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


def _the_threads() -> str:
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


def _the_tasks(pymux: "Pymux") -> str:
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
