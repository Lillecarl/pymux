"""
A server that replaces its own build while every pane goes on.

`upgrade-server` execs the new build into this same process. The pane
programs and the jobs are children of this pid and their ptys and pipes
are fds of it, so none of them notices: what they write while no build
reads waits in the kernel. Lillecarl/pymux#399, Lillecarl/pymux#736.

The order:

1. Write the snapshot (`pymux/snapshot.py`) while the server serves.
   This costs the depth of every history.
2. The check: the new build, in a process of its own (`check-snapshot`),
   imports every module of pymux and loads that file into a server that
   starts nothing. A build that fails is refused here, while nothing is
   lost, and the reason goes to the log and to the caller.
3. Tell each client that a server follows on the same socket.
4. The pause: take no client, read no pane, and write again, which
   costs only what the programs wrote since. **No `await` between the
   pause and the write**, so no pane is read after the write that
   describes it.
5. Answer the caller, make every carried fd inheritable, and exec.

Only the fds `carried` names cross the exec: each pane's master, each
running job's master or pipes, and the listening socket. The new image
adopts them, makes them close-on-exec again, and reaps each program
that ended during the exec before it waits for the rest.

**A crash after the exec costs the panes**, and that is accepted. It
must leave what is needed to fix it: the resume writes a crash report
that names the snapshot it was loading, and keeps that file.

The file holds every screen, so it is 0600 in the per-user socket
directory, and it is deleted once the panes run.
"""

from __future__ import annotations

import importlib
import os
import pkgutil
import shlex
import shutil
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

import anyio
from libpymux.sockets import socket_directory
from ptyhost.backends.posix import PosixBackend

from . import introspect, log, snapshot
from .commands import CommandException
from .commands.kill_server import RESTART_WAIT
from .log import logger
from .pipes.posix import PosixSocketListener

if TYPE_CHECKING:
    from .main import Pymux

__all__ = ["CHECK", "RESUME", "check", "resume", "upgrade"]

#: What a client shows while the new build starts.
UPGRADING = "pymux is being upgraded."

#: The mode words of the new build: the check, and the start after the exec.
CHECK = "check-snapshot"
RESUME = "resume-server"

#: How long the check may take, in seconds. It reads every history,
#: which took about 3 seconds for a pane of 50,000 rows.
CHECK_TIMEOUT = 120.0

#: How long a program closed just before the upgrade has to go once
#: killed, in seconds. It is in no snapshot, so the new image would
#: never reap it.
KILL_TIMEOUT = 2.0

#: The packages a check imports must all import, as one build of them.
#: A missing module outside them is an optional dependency this machine
#: lacks, such as pysmithay on macOS: absent, not broken.
OURS = ("pymux", "libpymux", "ptterm", "ptyhost", "pyte", "prompt_toolkit")


async def upgrade(pymux: Pymux, command: list[str]) -> None:
    "Replace this server's build with the one `command` starts."
    executable = shutil.which(command[0])
    if executable is None:
        raise CommandException("%s: not found" % command[0])
    command = [executable, *command[1:]]
    _refuse(pymux)
    # A recording holds Python objects of this build, so it ends here.
    pymux.finish_recordings()
    # A Wayland window lives on this server's compositor, which ends with
    # the exec, so its pane goes now and not into the snapshot.
    # Lillecarl/pymux#620.
    pymux.close_window_panes()

    path = Path(socket_directory()) / ("pymux.upgrade.%d.sqlite" % os.getpid())
    written = snapshot.Snapshot(path)
    paused = False
    try:
        # Sound stops too: the PipeWire is this server's child, and the
        # new image would not know it. Lillecarl/pymux#657.
        await pymux.audio.pause()
        # And the portal's private bus, for the same reason.
        await pymux.portal.pause()
        try:
            written.write(pymux)
            await _check(command, written.partial)
            for connection in list(pymux.connections):
                client_state = connection.client_state
                if client_state is not None and not client_state.temporary:
                    await connection.say_restarting(RESTART_WAIT, UPGRADING)
            # From the sweep on, a command that ends a pane would leave a
            # program neither image ends or reaps. Lillecarl/pymux#740.
            pymux.refusing_commands = True
            await _end_the_hung_up(pymux)

            # The pause. Nothing from here to the write awaits.
            _refuse(pymux)
            _pause(pymux)
            paused = True
            written.write(pymux)
        except snapshot.SnapshotError as e:
            raise CommandException(str(e))
        written.finish()

        logger.info("Upgrading to %s.", shlex.join(command))
        # Before the exec, which closes the connection: a client that
        # reads no answer says so and fails. Lillecarl/pymux#606.
        await _answer_the_caller(pymux)
        _exec([*command, *log_arguments(), RESUME, str(path)], carried(pymux))
    except BaseException:
        written.abandon()
        path.unlink(missing_ok=True)
        pymux.refusing_commands = False
        if paused:
            _resume(pymux)
        pymux.audio.resume()
        pymux.portal.resume()
        raise


async def _check(command: list[str], path: Path) -> None:
    "Run the new build's check on `path`, and refuse the upgrade with its reason when it fails."
    try:
        with anyio.fail_after(CHECK_TIMEOUT):
            result = await anyio.run_process([*command, CHECK, str(path)], check=False)
    except TimeoutError:
        logger.warning("The new build %s took longer than %ds to check.", command[0], CHECK_TIMEOUT)
        raise CommandException("the new build took longer than %ds to check this server" % CHECK_TIMEOUT)
    if result.returncode == 0:
        return
    said = result.stderr.decode(errors="replace").strip()
    logger.warning("The new build %s failed its check, so the upgrade is refused:\n%s", command[0], said)
    lines = said.splitlines()
    raise CommandException(
        "the new build failed its check: %s" % (lines[-1] if lines else "it ended with %d" % result.returncode)
    )


async def _end_the_hung_up(pymux: Pymux) -> None:
    """
    Kill a program still in its grace after a SIGHUP, and let it be
    reaped here. Its pane is in no snapshot, so the new image would
    neither send its SIGKILL nor reap it. Lillecarl/pyterm#1.
    """
    backends = list(pymux.hanging_up)
    for backend in backends:
        backend.kill()
    with anyio.move_on_after(KILL_TIMEOUT):
        for backend in backends:
            await backend.ready_f.wait()


async def _answer_the_caller(pymux: Pymux) -> None:
    """
    Tell the client that ran `upgrade-server` that it worked. The exec
    closes its connection before the command returns, so the answer that
    every command sends at its end never goes. Lillecarl/pymux#606.
    """
    try:
        connection = pymux.get_client_state().connection
    except ValueError:
        return
    if connection is not None:
        await connection.answer(0)


def _refuse(pymux: Pymux) -> None:
    "What only an upgrade needs. The snapshot refuses the rest as it writes."
    if not isinstance(pymux.listener, PosixSocketListener) or pymux._serves_one_terminal:
        raise CommandException("this server has no socket for its clients to come back to")


def _pause(pymux: Pymux) -> None:
    "Take no client and read no pane: what arrives now is the new image's."
    assert isinstance(pymux.listener, PosixSocketListener)
    pymux.listener.pause()
    for pane in snapshot._panes_of(pymux):
        if isinstance(backend := pane.process.backend, PosixBackend):
            backend.pause_reading()
    pymux.jobs.pause_reading()


def _resume(pymux: Pymux) -> None:
    "Serve again, as before the pause. Copy mode keeps the panes it stopped."
    if isinstance(pymux.listener, PosixSocketListener):
        pymux.listener.resume()
    for pane in snapshot._panes_of(pymux):
        if isinstance(backend := pane.process.backend, PosixBackend) and not pane.process.suspended:
            backend.resume_reading()
    pymux.jobs.resume_reading()


def carried(pymux: Pymux) -> list[int]:
    "Every fd that crosses the exec. Nothing else may be inheritable."
    fds = []
    for pane in snapshot._panes_of(pymux):
        backend = getattr(pane.process, "backend", None)
        if isinstance(backend, PosixBackend) and backend.master is not None:
            fds.append(backend.master)
    for job in pymux.jobs.listing():
        if not job.is_done and job.backend is not None:
            fds.extend(snapshot._fds_of(job))
    listener = pymux.listener
    if isinstance(listener, PosixSocketListener):
        fds.append(listener.socket.fileno())
    return fds


def _exec(argv: list[str], fds: list[int]) -> None:
    "Become `argv`, with `fds` open in it. Returns only by raising."
    logger.info("The exec carries fds %s.", " ".join(map(str, sorted(fds))))
    _flush_logs()
    for fd in fds:
        os.set_inheritable(fd, True)
    try:
        os.execv(argv[0], argv)
    except OSError as error:
        for fd in fds:
            os.set_inheritable(fd, False)
        logger.exception("The exec of %s failed after the caller was told it worked.", argv[0])
        raise CommandException("%s: %s" % (argv[0], error.strerror))


def _flush_logs() -> None:
    for one in log.LOGGERS:
        for handler in one.handlers:
            handler.flush()


def log_arguments() -> list[str]:
    "The log file this process writes, for the next image to write too."
    logfile = log._logfile
    return ["--log", str(logfile)] if logfile is not None else []


def check(path: str) -> None:
    """
    The check, in the new build: raise when it cannot run the server
    `path` describes. Every module of pymux imports, and the snapshot
    loads into a server that adopts nothing and starts nothing.
    """
    _import_everything()
    from .main import Pymux

    async def load() -> None:
        # In a loop, because a job's `done` is an event.
        snapshot.load(Pymux(), path)

    anyio.run(load)


def _import_everything() -> None:
    "Import every module of pymux, so a build that cannot is refused before the exec."
    import pymux

    def failed(name: str) -> None:
        error = sys.exc_info()[1]
        if not _optional(error):
            raise RuntimeError("%s does not import" % (name,)) from error

    for module in pkgutil.walk_packages(pymux.__path__, "pymux.", onerror=failed):
        if module.name.rsplit(".", 1)[-1] == "__main__":
            continue
        try:
            importlib.import_module(module.name)
        except Exception as error:
            if not _optional(error):
                raise RuntimeError("%s does not import" % (module.name,)) from error


def _optional(error: BaseException | None) -> bool:
    "A module missing that is no part of this build."
    return isinstance(error, ModuleNotFoundError) and error.name is not None and error.name.split(".")[0] not in OURS


def resume(path: str) -> None:
    """
    Serve again, in the new build, from the snapshot the old one wrote.

    The configuration file is read first, for what it says that the
    snapshot does not: its errors, and anything an older snapshot did
    not record. The snapshot then wins for everything it holds -- the
    options, the bindings and the hooks -- because a person may have
    changed one since the file was read.
    """
    from .main import Pymux

    introspect.note_for_a_crash("resuming from the snapshot %s, which is kept" % (path,))
    started = time.monotonic()
    try:
        pymux = Pymux()
        server = snapshot.read_server(path)
        pymux.source_file = server["source_file"]
        pymux.socket_name = server["socket_name"]
    except Exception as error:
        _crashed(error, started)
        raise SystemExit(1)

    async def resumed() -> None:
        answer = pymux.configure()
        if answer is not None:
            await answer
        # A configuration file may make a window, and the snapshot's
        # sessions replace whatever it made.
        strays = list(pymux.panes_by_id.values())
        listener = snapshot.load(pymux, path, snapshot.Inherited())
        if listener is None:
            raise snapshot.SnapshotError("the snapshot names no listening socket")
        pymux.adopt_listener(listener)
        # Nothing this server forks from here on may get one.
        for fd in carried(pymux):
            os.set_inheritable(fd, False)
        pymux.serve_listener()
        for pane in strays:
            pane.process.kill()
        await snapshot.start(pymux)
        Path(path).unlink(missing_ok=True)
        introspect.note_for_a_crash(None)
        logger.info("Resumed %d panes from %s.", len(pymux.panes_by_id), path)

    # A failure in `resumed` ends the server, and `run_server` writes
    # the crash report, which names the snapshot.
    pymux.run_server(resumed)


def _crashed(error: BaseException, started: float) -> None:
    "A crash before `run_server` could write its own report."
    report = introspect.write_crash_report(error, started)
    logger.critical("Resuming failed: %r. The report is %s.", error, report, exc_info=error)
    _flush_logs()
