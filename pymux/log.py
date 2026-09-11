"""
Where the log of pymux goes.

A server logs an exception and keeps going: the read loop of
`pymux/server.py` is written that way on purpose, so one bad packet does
not end a session. That makes logging a normal path and not a crash
path, and it decides where the messages may go.

They may not go to the terminal. In `integrated` and in `standalone` the
server shares one terminal with the client that draws on it, so a
traceback on `sys.stderr` lands on top of the frame. The socket route
does not have the problem, because `daemonize` sends the stderr of the
server to `/dev/null`.

Python writes to `sys.stderr` whenever a record reaches a logger with no
handler. So the answer is to give the logger a handler, always, and
`configure` is what does it.

## A file with an end, and a level that is not DEBUG

A server runs for weeks, so a log that grows is a log that fills a disk.
One left running for four days reached **86 MB**, because the level was
DEBUG and a server writes a line for every frame it draws: eleven lines
a second with nobody typing, on a session where the panes animate.
Lillecarl/pymux#248.

Two things follow, and neither of them is "log less".

**The per-frame lines are DEBUG**, and a server that nobody asked to
debug is at INFO. The lines themselves are good ones -- `Woke` exists so
that a frame drawn for no reason can be traced to what asked for it
(Lillecarl/pymux#180), and it is what made a live server readable. They
are just not what a person wants by default, and `pymux counters` gives
the same finding with nothing written down at all.

**The file has a size.** A person debugging wants the last few minutes,
not the last four days.

## And the level moves while the server runs

`--log-level` is read once, before the server starts. That is the wrong
time: the moment a person wants debug logging is when a server is
already misbehaving, and a restart loses the thing they wanted to look
at. `set-option log-level debug` reaches a running server, and
`level` is what `show-options` reads back. Lillecarl/pymux#252.
"""

import logging
import logging.handlers
import os
from pathlib import Path

__all__ = [
    "logger",
    "configure",
    "default_logfile",
    "logfile",
    "level",
    "set_level",
    "LEVELS",
]

#: The levels a person may name, and what each one means to `logging`.
#: Nothing below INFO and nothing above ERROR: a server that logs
#: nothing at all cannot be debugged, and CRITICAL says nothing that
#: ERROR does not.
LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}


logger = logging.getLogger(__package__)

#: How large the log may get before it starts again, and how many of the
#: old ones to keep.
#:
#: At DEBUG a busy server writes about a kilobyte a second, so this is
#: something like a day of it -- long enough for a fault that happens
#: overnight, and bounded, which is the whole point. At INFO a server
#: writes a line when something happens, and never reaches it.
HOW_BIG = 8 * 1024 * 1024
HOW_MANY = 3

#: The file this process logs to, once `configure` has chosen one.
#: `introspect` writes its dumps into the same directory, because a
#: dump is read with the log around it.
_logfile: Path | None = None


def logfile() -> Path | None:
    "The file this process logs to, or `None` when nothing configured one."
    return _logfile


def level() -> str:
    """
    The level this process logs at, by the name a person writes.

    A level `logging` knows and `LEVELS` does not reads back as the
    number, which is honest: something set it that this did not.
    """
    now = logger.getEffectiveLevel()
    for name, level in LEVELS.items():
        if level == now:
            return name
    return str(now)


def set_level(name: str) -> None:
    "Log at this level from now on. Raises `KeyError` for an unknown name."
    logger.setLevel(LEVELS[name])


def default_logfile() -> Path:
    """
    The file a server logs to when nobody named one.

    Under the state directory of the user, which is where a program puts
    what it wrote itself and nobody has to keep. `XDG_STATE_HOME` names
    it, and the specification says what it is when the variable does
    not.
    """
    state = os.environ.get("XDG_STATE_HOME") or "~/.local/state"
    return Path(os.path.expanduser(state)) / "pymux" / "server.log"


def configure(logfile: str | None = None, level: int = logging.INFO) -> Path | None:
    """
    Send the log of pymux somewhere that is not the terminal.

    `logfile` is the file that `--log` named. Without one the log goes to
    `default_logfile`, and the file is opened on the first message: a run
    that logs nothing leaves nothing behind, and a run that logs
    something leaves it where a person can read it.

    `level` is INFO and not DEBUG, and the file starts again when it
    reaches `HOW_BIG`. The module docstring says what a server left
    running for four days cost without either.

    Returns the file that the log goes to, or `None` when no file could
    be opened. A log that cannot be written is dropped and never falls
    back to the terminal, which is the thing this exists to prevent.
    """
    path = Path(logfile) if logfile else default_logfile()

    handler: logging.Handler
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # `delay` opens the file when the first record arrives.
        handler = logging.handlers.RotatingFileHandler(
            path, maxBytes=HOW_BIG, backupCount=HOW_MANY, delay=True
        )
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        )
    except OSError:
        # A read only home, a full disk, a path that is a directory. The
        # messages are lost, and that is better than painting them over
        # the screen of the person using this.
        logger.addHandler(logging.NullHandler())
        return None

    global _logfile

    logger.addHandler(handler)
    logger.setLevel(level)
    # The root logger reaches `sys.stderr` through `basicConfig`, and
    # nothing here should. The handler above is the whole path.
    logger.propagate = False
    _logfile = path
    return path
