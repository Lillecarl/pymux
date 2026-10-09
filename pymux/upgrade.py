"""
A server that replaces its own build while every pane goes on.

`upgrade-server` writes a snapshot (`pymux/snapshot.py`) and `execve`s
the new build into this same process. The pane programs are children of
this pid and their ptys are fds of it, so neither notices: what they
write while no build reads waits in the kernel. Lillecarl/pymux#399,
Lillecarl/pymux#408.

The order:

1. Write the snapshot while the server serves. This costs the depth of
   every history.
2. The dry run: the new build loads that file in a process of its own
   (`check-snapshot`). A build that cannot read it is refused here,
   while nothing is lost yet.
3. Tell each client that a server follows on the same socket.
4. The pause: write again, which costs only what the programs wrote
   since, then exec. **No `await` between the two**, so no pane is read
   after the write that describes it.

Only the fds `carried` names cross the exec: each pane's pty master, the
slave copy this process keeps, and the listening socket. They are marked
inheritable just before the exec, and the new build marks them back once
it has started the panes. The new build forks no pane before that, and
every fork closes its fds above 2 anyway (`PosixBackend._in_child`).

A new build that cannot resume execs the old one, which loads the same
file. The old one is this process's own interpreter, so its store path
is the one that is running, whatever `PATH` says by then.

The file holds every screen, so it is 0600 in the per-user socket
directory, and it is deleted once the panes run.
"""

from __future__ import annotations

import os
import shlex
import shutil
import sys
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

import anyio
from libpymux.sockets import socket_directory
from ptyhost.backends.posix import PosixBackend

from . import log, snapshot
from .commands import CommandException
from .commands.kill_server import RESTART_WAIT
from .log import logger
from .pipes.posix import PosixSocketListener

if TYPE_CHECKING:
    from .main import Pymux

__all__ = ["CHECK", "RESUME", "check", "resume", "upgrade"]

#: The mode words of the new build: the dry run, and the start after the exec.
CHECK = "check-snapshot"
RESUME = "resume-server"

#: How long the dry run may take, in seconds. It reads every history,
#: which took about 3 seconds for a pane of 50,000 rows.
CHECK_TIMEOUT = 120.0


def this_build() -> list[str]:
    "The command that starts the build this process runs."
    return [sys.executable, "-m", "pymux"]


async def upgrade(pymux: Pymux, command: list[str]) -> None:
    "Replace this server's build with the one `command` starts."
    executable = shutil.which(command[0])
    if executable is None:
        raise CommandException("%s: not found" % command[0])
    command = [executable, *command[1:]]
    _refuse(pymux)

    path = Path(socket_directory()) / ("pymux.upgrade.%d.sqlite" % os.getpid())
    written = snapshot.Snapshot(path)
    try:
        try:
            written.write(pymux)
            await _check(command, written.partial)
            for connection in list(pymux.connections):
                client_state = connection.client_state
                if client_state is not None and not client_state.temporary:
                    await connection.say_restarting(RESTART_WAIT)

            # The pause. Nothing from here on awaits.
            _refuse(pymux)
            written.write(pymux)
        except snapshot.SnapshotError as e:
            raise CommandException(str(e))
        written.finish()
    except BaseException:
        written.abandon()
        raise

    logger.info("Upgrading to %s.", shlex.join(command))
    try:
        _exec([*command, *_log_arguments(), RESUME, str(path), "--fall-back", shlex.join(this_build())], carried(pymux))
    except OSError as e:
        path.unlink(missing_ok=True)
        raise CommandException("%s: %s" % (command[0], e.strerror))


def _refuse(pymux: Pymux) -> None:
    "Refuse a server whose state no snapshot holds yet."
    if pymux._runs_standalone or pymux._serves_one_terminal:
        raise CommandException("this server draws on the terminal it runs in")
    if pymux.listener is None:
        raise CommandException("this server has no socket for its clients to come back to")


async def _check(command: list[str], path: Path) -> None:
    "The dry run: the new build loads the snapshot, in a process of its own."
    with anyio.fail_after(CHECK_TIMEOUT):
        result = await anyio.run_process([*command, CHECK, str(path)], check=False)
    if result.returncode != 0:
        said = result.stderr.decode(errors="replace").strip().splitlines()
        raise CommandException("the new build cannot load this server: %s" % (said[-1] if said else result.returncode))


def carried(pymux: Pymux) -> list[int]:
    "Every fd that crosses the exec. Nothing else may be inheritable."
    fds = []
    for pane in snapshot._panes_of(pymux):
        backend = pane.process.backend
        if isinstance(backend, PosixBackend):  # a snapshot refuses any other
            fds.extend(fd for fd in (backend.master, backend.slave) if fd is not None)
    listener = pymux.listener
    if isinstance(listener, PosixSocketListener):
        fds.append(listener.socket.fileno())
    return fds


def _log_arguments() -> list[str]:
    "The log file this process writes, for the next one to write too."
    logfile = log._logfile
    return ["--log", str(logfile)] if logfile is not None else []


def _exec(argv: list[str], fds: list[int]) -> None:
    "Become `argv`, with `fds` open in it. Returns only by raising."
    for one in log.LOGGERS:
        for handler in one.handlers:
            handler.flush()
    for fd in fds:
        os.set_inheritable(fd, True)
    try:
        os.execv(argv[0], argv)
    except OSError:
        for fd in fds:
            os.set_inheritable(fd, False)
        raise


def check(path: str) -> None:
    "The dry run, in the new build: raises when it cannot load `path`."
    from .main import Pymux

    async def load() -> None:
        # In a loop, because a job's `done` is an event.
        pymux = Pymux()
        snapshot.load(pymux, path, snapshot.checking(pymux))

    anyio.run(load)


def resume(path: str, fall_back: list[str] | None) -> None:
    """
    Serve again, in the new build, from the snapshot the old one wrote.

    The configuration file is read first, for what no snapshot holds
    yet: key bindings and hooks. The snapshot then wins for everything
    it does hold, the options included, because a person may have
    changed one since the file was read.

    Anything that goes wrong before the panes run execs `fall_back`
    over the same file. That one has no `fall_back` of its own, so two
    builds never hand the server back and forth.
    """
    from .main import Pymux

    def give_up() -> NoReturn:
        logger.exception("Resuming from %s failed.", path)
        if fall_back is None:
            Path(path).unlink(missing_ok=True)
            raise
        logger.info("Going back to %s.", shlex.join(fall_back))
        os.execv(fall_back[0], [*fall_back, *_log_arguments(), RESUME, path])

    try:
        pymux = Pymux()
        server = snapshot.read_server(path)
        pymux.source_file = server["source_file"]
    except Exception:
        give_up()

    async def resumed() -> None:
        try:
            answer = pymux.configure()
            if answer is not None:
                await answer
            # A configuration file may make a window, and the snapshot's
            # sessions replace whatever it made.
            strays = list(pymux.panes_by_id.values())
            listener = snapshot.load(pymux, path)
            if listener is not None:
                pymux.adopt_listener(listener)
                pymux.serve_listener()
            for pane in strays:
                pane.process.kill()
            await snapshot.start(pymux)
        except Exception:
            give_up()
        for fd in carried(pymux):
            os.set_inheritable(fd, False)
        Path(path).unlink(missing_ok=True)
        logger.info("Resumed %d panes from %s.", len(pymux.panes_by_id), path)

    pymux.run_server(resumed)


def resume_arguments(rest: list[str]) -> tuple[str, list[str] | None]:
    "The snapshot and the fall back of `resume-server PATH [--fall-back COMMAND]`."
    match rest:
        case [path]:
            return path, None
        case [path, "--fall-back", command]:
            return path, shlex.split(command)
    raise SystemExit("usage: pymux %s PATH [--fall-back COMMAND]" % RESUME)
