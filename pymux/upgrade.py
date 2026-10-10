"""
A server that hands itself over to a new build while every pane goes on.

The holder (`ptyhost.holder`) forked every pane and keeps its pty, so a
new server can take the panes over from it: `upgrade-server` starts the
new build beside this one, and this one ends once the new one serves.
What the programs write while nobody reads waits in the kernel.
Lillecarl/pymux#399, Lillecarl/pymux#553.

The order:

1. Write the snapshot (`pymux/snapshot.py`) while the server serves.
   This costs the depth of every history.
2. Tell each client that a server follows on the same socket.
3. The pause: take no client, read no pane, and write again, which
   costs only what the programs wrote since. **No `await` between the
   pause and the write**, so no pane is read after the write that
   describes it.
4. Start the new build with the listening socket and a pipe. It loads
   the file, takes each pane's master from the holder, and says `ok`
   on the pipe before it reads a pane. Then this server ends.

A new build that cannot take over says why on the pipe, or ends without
a word, and this server serves on as it was: it takes clients and reads
its panes again. Nothing went to the new build that this one did not
keep, so there is no dry run before the pause: the new build's own load
is the test, and the cost of a failed one is the length of the pause.

The file holds every screen, so it is 0600 in the per-user socket
directory, and it is deleted once the panes run.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

import anyio
from libpymux.sockets import socket_directory
from ptyhost.held import HeldBackend
from ptyhost.holder import HolderError

from . import log, snapshot
from .commands import CommandException
from .commands.kill_server import RESTART_WAIT
from .log import logger
from .pipes.posix import PosixSocketListener

if TYPE_CHECKING:
    from .main import Pymux

__all__ = ["RESUME", "resume", "upgrade"]

#: The mode word of the new build.
RESUME = "resume-server"

#: How long the new build may take to load, in seconds. It reads every
#: history, which took about 3 seconds for a pane of 50,000 rows.
LOAD_TIMEOUT = 120.0

#: What the new build writes on the pipe once it holds every pane. A
#: build that cannot take over writes `error: ` and the reason instead.
READY = b"ok"
FAILED = b"error: "


def rooted_environment() -> dict[str, str]:
    """
    This environment, naming every path this build runs from.

    Nix's collector treats a store path in any process's environment as
    a root (`findRuntimeRoots` reads `/proc/<pid>/environ`). The holder
    outlives the server that starts it, so it carries the paths of the
    build it runs from this way. Lillecarl/pymux#408.
    """
    # The interpreter alone is not the build: what it imports comes from
    # the other store paths on `sys.path`.
    environment = dict(os.environ)
    environment["PYMUX_BUILD"] = os.pathsep.join([sys.executable, *sys.path])
    return environment


async def upgrade(pymux: Pymux, command: list[str]) -> None:
    "Hand this server over to the build `command` starts."
    executable = shutil.which(command[0])
    if executable is None:
        raise CommandException("%s: not found" % command[0])
    command = [executable, *command[1:]]
    _refuse(pymux)

    path = Path(socket_directory()) / ("pymux.upgrade.%d.sqlite" % os.getpid())
    written = snapshot.Snapshot(path)
    paused = False
    try:
        try:
            written.write(pymux)
            for connection in list(pymux.connections):
                client_state = connection.client_state
                if client_state is not None and not client_state.temporary:
                    await connection.say_restarting(RESTART_WAIT)

            # The pause. Nothing from here to the write awaits.
            _refuse(pymux)
            _pause(pymux)
            paused = True
            written.write(pymux)
        except snapshot.SnapshotError as e:
            raise CommandException(str(e))
        written.finish()

        logger.info("Upgrading to %s.", shlex.join(command))
        await _hand_over(pymux, [*command, *_log_arguments(), RESUME, str(path)])
    except BaseException:
        written.abandon()
        path.unlink(missing_ok=True)
        if paused:
            _resume(pymux)
        raise
    _leave()


def _refuse(pymux: Pymux) -> None:
    "What only an upgrade needs. The snapshot refuses the rest as it writes."
    if not isinstance(pymux.listener, PosixSocketListener) or pymux._serves_one_terminal:
        raise CommandException("this server has no socket for its clients to come back to")
    if pymux.holding is None:
        raise CommandException("this server has no holder to keep its panes")


def _pause(pymux: Pymux) -> None:
    "Take no client and read no pane: what arrives now is the new build's."
    assert isinstance(pymux.listener, PosixSocketListener)
    pymux.listener.pause()
    for pane in snapshot._panes_of(pymux):
        if isinstance(backend := pane.process.backend, HeldBackend):
            backend.pause_reading()


def _resume(pymux: Pymux) -> None:
    "Serve again, as before the pause. Copy mode keeps the panes it stopped."
    if isinstance(pymux.listener, PosixSocketListener):
        pymux.listener.resume()
    for pane in snapshot._panes_of(pymux):
        if isinstance(backend := pane.process.backend, HeldBackend) and not pane.process.suspended:
            backend.resume_reading()


async def _hand_over(pymux: Pymux, argv: list[str]) -> None:
    "Start the new build on this socket, and return once it holds every pane."
    assert isinstance(pymux.listener, PosixSocketListener)
    listening = pymux.listener.socket.fileno()
    ready, told = os.pipe()
    try:
        # A session of its own: this process ends, and the new one goes on.
        process = subprocess.Popen(
            [*argv, "--ready-fd", str(told)],
            pass_fds=(listening, told),
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    finally:
        os.close(told)
    said = b""
    try:
        with anyio.fail_after(LOAD_TIMEOUT):
            # `ok` arrives alone; a reason arrives whole before the end of the file.
            while said != READY and (chunk := await _read(ready)):
                said += chunk
    except TimeoutError:
        process.kill()
    finally:
        os.close(ready)
    if said != READY:
        await anyio.to_thread.run_sync(process.wait)
        reason = said.removeprefix(FAILED).decode(errors="replace").strip()
        raise CommandException(
            "the new build cannot take this server over: %s" % (reason or "it ended with %s" % process.returncode)
        )


async def _read(fd: int) -> bytes:
    await anyio.wait_readable(fd)
    return os.read(fd, 4096)


def _leave() -> NoReturn:
    """
    End this server, and leave the holder, the socket and every pane to
    the new one. Not `stop`: that kills the programs, and the clean-up
    after it takes the socket file the new server listens on.
    """
    _flush_logs()
    os._exit(0)


def _flush_logs() -> None:
    for one in log.LOGGERS:
        for handler in one.handlers:
            handler.flush()


def _log_arguments() -> list[str]:
    "The log file this process writes, for the next one to write too."
    logfile = log._logfile
    return ["--log", str(logfile)] if logfile is not None else []


def resume(path: str, ready: int) -> None:
    """
    Serve, in the new build, from the snapshot the old one wrote.

    The configuration file is read first, for what it says that the
    snapshot does not: its errors, and anything an older snapshot did
    not record. The snapshot then wins for everything it holds -- the
    options, the bindings and the hooks -- because a person may have
    changed one since the file was read.

    Anything that goes wrong before `ok` ends this process at once, with
    the reason on the pipe: the old server serves on, and nothing of this
    one may touch its panes or its socket file on the way out.
    """
    from .main import Pymux

    def give_up(error: BaseException) -> NoReturn:
        logger.exception("Taking the server over from %s failed.", path)
        _flush_logs()
        with open(ready, "wb", closefd=False) as pipe:
            pipe.write(FAILED + ("%s: %s" % (type(error).__name__, error)).encode())
        os._exit(1)

    try:
        pymux = Pymux()
        server = snapshot.read_server(path)
        pymux.source_file = server["source_file"]
        # Before the loop: the holder is beside the socket, and the server
        # connects to it before the snapshot loads.
        pymux.socket_name = server["socket_name"]
    except Exception as error:
        give_up(error)

    async def resumed() -> None:
        try:
            if pymux.holding is None:
                raise HolderError("no holder beside %s" % (pymux.socket_name,))
            answer = pymux.configure()
            if answer is not None:
                await answer
            # A configuration file may make a window, and the snapshot's
            # sessions replace whatever it made.
            strays = list(pymux.panes_by_id.values())
            # Only the masters it names: a copy fetched and not adopted
            # would stay open for the life of this server.
            masters = {}
            for program_id in snapshot.program_ids(path):
                masters[program_id] = await pymux.holding.master(program_id)
            listener = snapshot.load(pymux, path, masters)
            if listener is None:
                raise snapshot.SnapshotError("the snapshot names no listening socket")
            # `pass_fds` made it inheritable, and nothing this server forks may get it.
            os.set_inheritable(listener, False)
            pymux.adopt_listener(listener)
            os.write(ready, READY)
            os.close(ready)
        except Exception as error:
            give_up(error)
        # The old server ends now, and nothing reads a pane until here.
        for pane in strays:
            pane.process.kill()
        pymux.serve_listener()
        await snapshot.start(pymux)
        await pymux.release_orphans()
        Path(path).unlink(missing_ok=True)
        logger.info("Took over %d panes from %s.", len(pymux.panes_by_id), path)

    pymux.run_server(resumed)


def resume_arguments(rest: list[str]) -> tuple[str, int]:
    "The snapshot and the pipe of `resume-server PATH --ready-fd N`."
    match rest:
        case [path, "--ready-fd", number] if number.isdigit():
            return path, int(number)
    raise SystemExit("usage: pymux %s PATH --ready-fd N" % RESUME)
