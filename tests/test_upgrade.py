"""
A server upgraded in place keeps its pid, its panes and their programs.

A real daemon on a socket, upgraded to this same build by
`upgrade-server`: the exec is the subject, and only a process of its own
can exec. Lillecarl/pymux#408.
"""

from __future__ import annotations

import glob
import os
import shlex
import stat
import sys
import time

import pytest
from libpymux.sockets import socket_directory
from test_jobs_over_the_socket import CLI_TIMEOUT, _wait_for_server, cli

#: What the counted pane prints: one numbered line every 20 ms, past the
#: upgrade, then it waits.
COUNT = 'i=0; while [ $i -lt %d ]; do i=$((i+1)); echo "line $i"; sleep 0.02; done; sleep 60'
LINES = 300

#: A new build that passes the dry run, then cannot read the snapshot
#: it was handed: the version it reads is one more than the file's.
CANNOT_RESUME = """#!/bin/sh
case " $* " in
  *" check-snapshot "*) exec %(python)s -m pymux "$@" ;;
esac
exec %(python)s -c '
import sys
from pymux import snapshot
snapshot.SNAPSHOT_VERSION += 1
from pymux.entry_points.run_pymux import run
run()
' "$@"
"""


def _answer(sock, *args) -> str:
    done = cli(sock, *args)
    assert done.returncode == 0, done.stderr.decode()
    return done.stdout.decode()


def _once_it_answers(sock, *args) -> str:
    "The answer of a server that may be between two builds."
    deadline = time.monotonic() + CLI_TIMEOUT
    while True:
        done = cli(sock, *args)
        if done.returncode == 0:
            return done.stdout.decode()
        if time.monotonic() > deadline:
            pytest.fail("no answer to %s: %s" % (args, done.stderr.decode()))
        time.sleep(0.1)


def _counted(sock) -> list[int]:
    "The numbers the counting pane printed, once it printed them all."
    deadline = time.monotonic() + CLI_TIMEOUT
    counting = _pane(sock, 0)
    while True:
        text = _answer(sock, "capture-pane", "-p", "-S", "-", "-t", counting)
        numbers = [int(line.split()[1]) for line in text.splitlines() if line.startswith("line ")]
        if LINES in numbers or time.monotonic() > deadline:
            return numbers
        time.sleep(0.1)


def _what_survives(sock) -> tuple[str, ...]:
    """
    The server's pid, every pane with the pid of its program, a paste
    buffer, and the key bindings with one bound since the start.
    """
    return (
        _once_it_answers(sock, "display-message", "-p", "#{pid}"),
        _once_it_answers(sock, "list-panes", "-a", "-F", "#{pane_id} #{pane_pid}"),
        _once_it_answers(sock, "show-buffer", "-b", "kept"),
        _once_it_answers(sock, "list-keys"),
        _once_it_answers(sock, "sql", "--list"),
    )


def _inheritable(pid: int) -> list[str]:
    """
    Every fd above 2 of `pid` that an exec would carry. The resumed
    server forks panes for weeks, and none of them may get a pty.
    """
    found = []
    for fd in os.listdir("/proc/%d/fd" % pid):
        if int(fd) <= 2:
            continue
        with open("/proc/%d/fdinfo/%s" % (pid, fd)) as info:
            flags = next(int(line.split()[1], 8) for line in info if line.startswith("flags:"))
        if not flags & os.O_CLOEXEC:
            found.append("%s -> %s" % (fd, os.readlink("/proc/%d/fd/%s" % (pid, fd))))
    return found


def _leftovers() -> list[str]:
    return glob.glob(os.path.join(socket_directory(), "pymux.upgrade.*"))


@pytest.fixture
def server(tmp_path):
    "A daemon with a pane that counts, and a shell beside it."
    sock = tmp_path / "upgrade.sock"
    count = tmp_path / "count.sh"
    count.write_text(COUNT % LINES)
    started = cli(sock, "new-session", "-d", "-x", "80", "-y", "24", "sh %s" % count)
    assert started.returncode == 0, started.stderr.decode()
    _wait_for_server(sock)
    try:
        _answer(sock, "split-window", "-d", "sh")
        _answer(sock, "set-buffer", "-b", "kept", "pasted after the upgrade")
        _answer(sock, "bind-key", "-n", "F5", "display-message", "bound at run time")
        _answer(sock, "sql", "--save", "kept", "SELECT id FROM jobs")
        yield sock
    finally:
        cli(sock, "kill-server")


def _pane(sock, which: int) -> str:
    "The id of the counting pane (0) or the shell (1)."
    return _answer(sock, "list-panes", "-F", "#{pane_id}").split()[which]


def _shell_answers(sock) -> None:
    "Type into the shell pane, and read what it printed back."
    shell = _pane(sock, 1)
    _answer(sock, "send-keys", "-t", shell, "echo mar''ker", "Enter")
    deadline = time.monotonic() + CLI_TIMEOUT
    while "marker" not in _answer(sock, "capture-pane", "-p", "-t", shell):
        assert time.monotonic() < deadline, "the shell never answered"
        time.sleep(0.1)


@pytest.mark.parametrize("new_build", ["same", "cannot-resume"])
def test_an_upgrade_keeps_every_pane_and_its_program(server, tmp_path, new_build):
    before = _what_survives(server)
    command = [sys.executable, "-m", "pymux"]
    if new_build == "cannot-resume":
        wrapper = tmp_path / "new-build"
        wrapper.write_text(CANNOT_RESUME % {"python": shlex.quote(sys.executable)})
        wrapper.chmod(wrapper.stat().st_mode | stat.S_IXUSR)
        command = [str(wrapper)]

    # The command's own connection goes with the exec, so its answer is
    # not the verdict; what the server answers afterwards is.
    cli(server, "upgrade-server", shlex.join(command))

    assert _what_survives(server) == before
    # The same pid runs another command line: the exec happened. Only
    # the first exec names a build to fall back to, so a server that
    # names none is the old build, taken back.
    pid = int(before[0])
    with open("/proc/%d/cmdline" % pid, "rb") as f:
        words = f.read().split(b"\0")
    assert b"resume-server" in words
    assert (b"--fall-back" in words) == (new_build == "same")
    assert _inheritable(pid) == []
    numbers = _counted(server)
    assert numbers == list(range(1, LINES + 1))
    _shell_answers(server)
    assert _leftovers() == []


def test_a_build_that_cannot_load_the_snapshot_is_refused(server):
    before = _what_survives(server)
    refused = cli(server, "upgrade-server", shlex.join(["sh", "-c", "echo cannot read it >&2; exit 3"]))
    assert refused.returncode != 0
    assert "cannot read it" in refused.stderr.decode() + refused.stdout.decode()
    assert _what_survives(server) == before
    _shell_answers(server)
    assert _leftovers() == []
