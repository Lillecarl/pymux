"""
`pymux renice`: raise the priority of pymux's own processes, and keep
it from the programs a person runs.

pymux carries sound and video, which stutter when the machine is busy.
The command asks each server of this user what is its own
(`list-processes`) and sets every thread of each. A client's children
are all pymux's too: its sound player and its browser.

Lowering a niceness needs root. The command sets what the kernel lets
it, and asks `sudo renice` for the rest. It runs as the user and not
under sudo, because root has its own socket directory and finds no
server of anybody else.

A program a person runs takes none of it: `ptyhost.spawn.at_normal_priority`
runs before every exec of a pane, and `as_a_person_runs` wraps every
other start. Lillecarl/pymux#673.

**Light imports.** The server imports `as_a_person_runs`; the command
imports psutil when it runs.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
from collections.abc import Iterable, Sequence
from typing import NamedTuple

__all__ = ["DEFAULT_NICENESS", "as_a_person_runs", "renice"]

#: Under the -11 that rtkit gives a sound server, so PipeWire still
#: comes first.
DEFAULT_NICENESS = -10


def as_a_person_runs(command: str | Sequence[str]) -> str | list[str]:
    """
    `command` for `anyio.open_process` or `anyio.run_process`, started
    at normal priority when this thread runs raised. Unchanged when it
    does not, which costs nothing.

    The wrapper and not a hook between the fork and the exec, because
    anyio takes no `preexec_fn`. A string is a shell command, as anyio
    reads it.
    """
    if os.getpriority(os.PRIO_PROCESS, 0) >= 0:
        return command if isinstance(command, str) else list(command)
    argv = ["/bin/sh", "-c", command] if isinstance(command, str) else list(command)
    return [sys.executable, "-I", "-m", "ptyhost.normal_priority", *argv]


class Target(NamedTuple):
    "One process to renice, and what it is."

    role: str
    pid: int


def targets_of(listing: Iterable[str]) -> list[Target]:
    "The lines of `list-processes`, read. A line it cannot read is skipped."
    found = []
    for line in listing:
        role, _, pid = line.partition(" ")
        if pid.isdigit():
            found.append(Target(role, int(pid)))
    return found


def renice(socket_names: Sequence[str], niceness: int) -> int:
    """
    Set `niceness` on every pymux process of these servers. Prints one
    line for each process, and answers an exit code.
    """
    import anyio
    import psutil
    from libpymux.connection import Connection

    targets: list[Target] = []
    for socket_name in socket_names:
        try:
            listing = anyio.run(Connection(socket_name).run, "list-processes").lines()
        except OSError, RuntimeError:
            print("%s: no answer" % (socket_name,), file=sys.stderr)
            continue
        targets.extend(targets_of(listing))

    threads: dict[int, list[int]] = {}
    for target in _with_children(targets, psutil):
        if target.pid in threads:
            continue  # An integrated server is its own client.
        try:
            process = psutil.Process(target.pid)
            # A client says its pid itself; one that names another
            # user's process gets nothing, and above all no sudo.
            if process.uids().real != os.getuid():
                continue
            threads[target.pid] = _threads_of(process)
            print("%s %d %s" % (target.role, target.pid, process.name()))
        except psutil.Error:
            continue

    if not threads:
        print("No pymux process found.", file=sys.stderr)
        if os.geteuid() == 0:
            print("Run it as the user of the servers: it asks sudo itself.", file=sys.stderr)
        return 1

    refused = []
    for ids in threads.values():
        for thread in ids:
            try:
                os.setpriority(os.PRIO_PROCESS, thread, niceness)
            except PermissionError:
                refused.append(thread)
            except ProcessLookupError:
                pass  # A thread that ended.
    if refused:
        # `renice PRIORITY -p ID...`: an absolute niceness for util-linux
        # and for BSD alike. `-n` is an increment on BSD.
        done = subprocess.run(["sudo", "renice", str(niceness), "-p", *map(str, refused)], stdout=subprocess.DEVNULL)
        if done.returncode != 0:
            return done.returncode
    print("Niceness %d." % (niceness,))
    return 0


def _with_children(targets: list[Target], psutil) -> list[Target]:
    """
    Each target, and every process under a client. Never under a
    server: what it starts is mostly a person's. An integrated server is
    its own client, and counts as a server.
    """
    found = list(targets)
    servers = {target.pid for target in targets if target.role != "client"}
    for target in targets:
        if target.role != "client" or target.pid in servers:
            continue
        with contextlib.suppress(psutil.Error):
            found.extend(Target("client", child.pid) for child in psutil.Process(target.pid).children(recursive=True))
    return found


def _threads_of(process) -> list[int]:
    """
    What `setpriority` takes to reach all of a process. On Linux a
    niceness is a thread's, and a thread that is already running keeps
    its own (setpriority(2), BUGS); elsewhere the pid reaches them all.
    """
    if sys.platform == "linux":
        return [thread.id for thread in process.threads()]
    return [process.pid]
