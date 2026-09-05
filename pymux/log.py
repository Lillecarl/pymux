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
"""
import logging
import os
from pathlib import Path

__all__ = ["logger", "configure", "default_logfile"]


logger = logging.getLogger(__package__)


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


def configure(logfile: str | None = None, level: int = logging.DEBUG) -> Path | None:
    """
    Send the log of pymux somewhere that is not the terminal.

    `logfile` is the file that `--log` named. Without one the log goes to
    `default_logfile`, and the file is opened on the first message: a run
    that logs nothing leaves nothing behind, and a run that logs
    something leaves it where a person can read it.

    Returns the file that the log goes to, or `None` when no file could
    be opened. A log that cannot be written is dropped and never falls
    back to the terminal, which is the thing this exists to prevent.
    """
    path = Path(logfile) if logfile else default_logfile()

    handler: logging.Handler
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # `delay` opens the file when the first record arrives.
        handler = logging.FileHandler(path, delay=True)
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        )
    except OSError:
        # A read only home, a full disk, a path that is a directory. The
        # messages are lost, and that is better than painting them over
        # the screen of the person using this.
        logger.addHandler(logging.NullHandler())
        return None

    logger.addHandler(handler)
    logger.setLevel(level)
    # The root logger reaches `sys.stderr` through `basicConfig`, and
    # nothing here should. The handler above is the whole path.
    logger.propagate = False
    return path
