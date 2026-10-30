"""
Start the server in a systemd scope of its own, apart from the login
that started it. Lillecarl/pymux#736.

A daemonised server leaves its process group and session, and stays in
the cgroup of the ssh login that started it. Where logind kills a
session's processes at logout (`KillUserProcesses=yes`, systemd's
upstream default) that logout ends the server and every pane.

**Started in the scope, not moved there.** A login's scope belongs to
the system manager, and the person's own manager cannot take a process
out of it: `StartTransientUnit` with the server's pid answers yes and
moves nothing (measured). `systemd-run --user --scope` starts a command
inside the person's manager instead, keeping the terminal and the
environment, so the command that is about to start a server runs again
there, and the server it forks lives in that scope.

Best effort: only from a login's scope, so a server that a service of
the person's starts stays in that service; and on a machine with no
user systemd -- macOS among them -- nothing changes.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
from pathlib import Path

__all__ = ["SCOPED", "rerun_in_a_scope"]

#: Set on the command that runs again in a scope, so it does not again.
SCOPED = "PYMUX_SCOPED"

#: The cgroup of a login session, as /proc/self/cgroup names it.
_SESSION_SCOPE = re.compile(r"/session-[^/]+\.scope$")


def _cgroup() -> str:
    try:
        lines = Path("/proc/self/cgroup").read_text().splitlines()
    except OSError:
        return ""
    # cgroup v2: one line, "0::/user.slice/.../session-263.scope".
    for line in lines:
        if line.startswith("0::"):
            return line[3:]
    return ""


def rerun_in_a_scope() -> None:
    """
    Run this command again inside a scope of the person's systemd, when
    it is about to start a server from a login's scope. Returns when it
    stays where it is; otherwise this process becomes that command.
    """
    if os.environ.get(SCOPED) or not _SESSION_SCOPE.search(_cgroup()):
        return
    systemd_run = shutil.which("systemd-run")
    if systemd_run is None:
        return
    os.environ[SCOPED] = "1"
    try:
        os.execv(
            systemd_run,
            [systemd_run, "--user", "--scope", "--quiet", "--", sys.executable, *sys.orig_argv[1:]],
        )
    except OSError:
        # No user manager to ask, or none that answers: start here.
        return
