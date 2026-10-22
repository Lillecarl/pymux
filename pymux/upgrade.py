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

import contextlib
import importlib
import os
import pkgutil
import shlex
import shutil
import signal
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

import anyio
from libpymux.sockets import HOLDER_SUFFIX, socket_directory
from ptyhost.backends.posix import PosixBackend
from ptyhost.held import Holding

from . import introspect, log, snapshot
from .commands import CommandException
from .commands.kill_server import RESTART_WAIT
from .log import logger
from .pipes.posix import PosixSocketListener

if TYPE_CHECKING:
    from .main import Pymux

__all__ = ["CHECK", "RESUME", "check", "resume", "upgrade"]

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
        pymux.audio.pause()
        try:
            written.write(pymux)
            await _check(command, written.partial)
            for connection in list(pymux.connections):
                client_state = connection.client_state
                if client_state is not None and not client_state.temporary:
                    await connection.say_restarting(RESTART_WAIT)
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
        if paused:
            _resume(pymux)
        pymux.audio.resume()
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
    "The log file this process writes, for the next image -- or the holder -- to write too."
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


def resume(path: str, ready: int | None) -> None:
    """
    Serve again, in the new build, from the snapshot the old one wrote.

    The configuration file is read first, for what it says that the
    snapshot does not: its errors, and anything an older snapshot did
    not record. The snapshot then wins for everything it holds -- the
    options, the bindings and the hooks -- because a person may have
    changed one since the file was read.

    `ready` is the pipe of a server from before Lillecarl/pymux#736,
    which starts this build beside itself (`_take_over_from_the_holder`).
    """
    if ready is not None:
        _take_over_from_the_holder(path, ready)
        return
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


#: What the new build writes on the pipe of a server from before
#: Lillecarl/pymux#736 once it holds every pane, or `error: ` and why.
READY = b"ok"
FAILED = b"error: "


def _take_over_from_the_holder(path: str, ready: int) -> None:
    """
    **The one move off the holder; delete it with the holder (stage E
    of Lillecarl/pymux#736).**

    A server from before this build forked nothing itself: the holder
    did. It starts this build beside itself, and ends once this one says
    `ok`. So this one takes a copy of every master and pipe from the
    holder, says `ok`, and ends the holder with SIGTERM -- not `quit`,
    which hangs every program up. The programs run on, children of the
    person's service manager now, and this server waits for them as
    strangers.

    Anything that goes wrong before `ok` ends this process at once, with
    the reason on the pipe: the old server serves on, and nothing of this
    one may touch its panes or its socket file on the way out.
    """
    from .main import Pymux

    introspect.note_for_a_crash("taking the server over from the holder, by the snapshot %s" % (path,))
    started = time.monotonic()

    def give_up(error: BaseException) -> NoReturn:
        _crashed(error, started)
        with open(ready, "wb", closefd=False) as pipe:
            pipe.write(FAILED + ("%s: %s" % (type(error).__name__, error)).encode())
        os._exit(1)

    try:
        pymux = Pymux()
        server = snapshot.read_server(path)
        pymux.source_file = server["source_file"]
        pymux.socket_name = server["socket_name"]
    except Exception as error:
        give_up(error)

    async def resumed() -> None:
        try:
            answer = pymux.configure()
            if answer is not None:
                await answer
            strays = list(pymux.panes_by_id.values())
            takeover = await _from_the_holder(pymux.socket_name, path)
            listener = snapshot.load(pymux, path, takeover)
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
        if takeover.holder_pid is not None:
            with contextlib.suppress(ProcessLookupError):
                os.kill(takeover.holder_pid, signal.SIGTERM)
        Path(path).unlink(missing_ok=True)
        introspect.note_for_a_crash(None)
        logger.info("Took %d panes over from the holder, which ends.", len(pymux.panes_by_id))

    pymux.run_server(resumed)


async def _from_the_holder(socket_name: str | None, path: str) -> snapshot.FromTheHolder:
    "Copies of what the holder beside `socket_name` keeps of each program the snapshot names."
    holding = await Holding.connect("%s%s" % (socket_name, HOLDER_SUFFIX))
    try:
        async with anyio.create_task_group() as reading:
            reading.start_soon(holding.run)
            takeover = await snapshot.FromTheHolder.handed_over(holding, path)
            reading.cancel_scope.cancel()
    finally:
        holding.close()
    return takeover


def resume_arguments(rest: list[str]) -> tuple[str, int | None]:
    "The snapshot, and the pipe of a server from before Lillecarl/pymux#736: `resume-server PATH [--ready-fd N]`."
    match rest:
        case [path]:
            return path, None
        case [path, "--ready-fd", number] if number.isdigit():
            return path, int(number)
    raise SystemExit("usage: pymux %s PATH [--ready-fd N]" % RESUME)
