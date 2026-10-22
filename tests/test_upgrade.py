"""
An upgraded server keeps its pid, its panes and their programs.

A real daemon on a socket, upgraded by `upgrade-server`: the exec is the
subject, and only a process of its own can exec. A new build that fails
its check is refused with the reason, and one that passes it and then
crashes leaves a report that holds enough to fix it.
Lillecarl/pymux#408, Lillecarl/pymux#736.
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
#: upgrade, then it touches a file and waits.
COUNT = 'i=0; while [ $i -lt %d ]; do i=$((i+1)); echo "line $i"; sleep 0.02; done; touch %s; sleep 60'
LINES = 300

#: The file the counted pane touches once it printed every line.
COUNTED = "counted"

#: The server's log, in the test's directory.
LOG = "server.log"

#: A new build that starts, then cannot read the snapshot it was handed:
#: the version it reads is one more than the file's.
CANNOT_RESUME = """#!/bin/sh
exec %(python)s -c '
import sys
from pymux import snapshot
snapshot.SNAPSHOT_VERSION += 1
from pymux.entry_points.run_pymux import run
run()
' "$@"
"""

#: A new build with a module of pymux that raises when it is imported.
CANNOT_IMPORT = """#!/bin/sh
exec %(python)s -c '
import pymux
pymux.__path__.append("%(extra)s")
from pymux.entry_points.run_pymux import run
run()
' "$@"
"""

#: The module `CANNOT_IMPORT` adds.
BROKEN_MODULE = 'raise RuntimeError("broken on purpose, at import")\n'

#: A new build that passes the check, then crashes as it resumes.
CRASHES_AFTER_THE_EXEC = """#!/bin/sh
exec %(python)s -c '
from pymux import snapshot
async def start(pymux):
    raise RuntimeError("broken on purpose, after the exec")
snapshot.start = start
from pymux.entry_points.run_pymux import run
run()
' "$@"
"""


def _new_build(tmp_path, script: str, **values) -> list[str]:
    "A wrapper that starts this build with `script`'s change."
    wrapper = tmp_path / "new-build"
    wrapper.write_text(script % {"python": shlex.quote(sys.executable), **values})
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IXUSR)
    return [str(wrapper)]


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
    """
    The numbers the counting pane printed, once it printed them all.

    The file comes first: each capture is a CLI process, 0.17 CPU
    seconds measured, and the count takes six seconds.
    """
    deadline = time.monotonic() + CLI_TIMEOUT
    while not (sock.parent / COUNTED).exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    counting = _pane(sock, 0)
    while True:
        text = _answer(sock, "capture-pane", "-p", "-S", "-", "-t", counting)
        numbers = [int(line.split()[1]) for line in text.splitlines() if line.startswith("line ")]
        if LINES in numbers or time.monotonic() > deadline:
            return numbers
        time.sleep(0.1)


def _log_of(sock) -> str:
    "What the server logged, for a failure to show."
    path = sock.parent / LOG
    return path.read_text() if path.exists() else "(no log)"


def _server_pid(sock) -> int:
    return int(_once_it_answers(sock, "display-message", "-p", "#{pid}"))


def _what_survives(sock) -> tuple[str, ...]:
    """
    Every pane with the pid of its program, a paste buffer, and the key
    bindings with one bound since the start.
    """
    return (
        _once_it_answers(sock, "list-panes", "-a", "-F", "#{pane_id} #{pane_pid}"),
        _once_it_answers(sock, "show-buffer", "-b", "kept"),
        _once_it_answers(sock, "list-keys"),
        _once_it_answers(sock, "sql", "--list"),
    )


#: Whether a process's parent, environment, fds and state can be read
#: from /proc. Linux keeps them there; macOS keeps them behind libproc,
#: and its run holds what the panes and their programs do instead.
READS_PROC = sys.platform.startswith("linux")


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


def _parent_of(pid: int) -> int:
    with open("/proc/%d/stat" % pid) as f:
        return int(f.read().rsplit(")", 1)[1].split()[1])


def _gone(pid: int) -> bool:
    "Whether `pid` has ended. A zombie its new parent has yet to reap has."
    if not READS_PROC:
        # No /proc to say "zombie"; launchd reaps an orphan at once.
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        return False
    try:
        with open("/proc/%d/stat" % pid) as f:
            return f.read().rsplit(")", 1)[1].split()[0] == "Z"
    except FileNotFoundError:
        return True


def test_kill_server_ends_its_programs(server):
    "A server that ends on purpose takes its programs along. Lillecarl/pymux#736."
    pids = [int(one) for one in _answer(server, "list-panes", "-F", "#{pane_pid}").split()]

    cli(server, "kill-server")

    deadline = time.monotonic() + CLI_TIMEOUT
    while not all(_gone(pid) for pid in pids):
        assert time.monotonic() < deadline, "a program outlived kill-server"
        time.sleep(0.05)


def _leftovers(*pids: int) -> list[str]:
    """
    What these servers' upgrades left. Only theirs: the directory is
    shared, and a test on another worker can be mid-upgrade beside this one.
    """
    return [path for pid in pids for path in glob.glob(os.path.join(socket_directory(), "pymux.upgrade.%d.*" % pid))]


@pytest.fixture
def server(tmp_path):
    "A daemon with a pane that counts, and a shell beside it."
    sock = tmp_path / "upgrade.sock"
    count = tmp_path / "count.sh"
    count.write_text(COUNT % (LINES, shlex.quote(str(tmp_path / COUNTED))))
    # The log names the directory a crash report goes to, and every
    # image of the server writes it.
    started = cli(sock, "--log", tmp_path / LOG, "new-session", "-d", "-x", "80", "-y", "24", "sh %s" % count)
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


def _cmdline(pid: int) -> list[bytes]:
    with open("/proc/%d/cmdline" % pid, "rb") as f:
        return f.read().split(b"\0")


@pytest.mark.parametrize("new_build", ["same", "cannot-resume", "cannot-import"])
def test_an_upgrade_keeps_every_pane_and_its_program(server, tmp_path, new_build):
    before = _what_survives(server)
    old = _server_pid(server)
    command = [sys.executable, "-m", "pymux"]
    if new_build == "cannot-resume":
        command = _new_build(tmp_path, CANNOT_RESUME)
        reason = "snapshot version"
    elif new_build == "cannot-import":
        extra = tmp_path / "extra"
        extra.mkdir()
        (extra / "broken_on_purpose.py").write_text(BROKEN_MODULE)
        command = _new_build(tmp_path, CANNOT_IMPORT, extra=extra)
        reason = "pymux.broken_on_purpose does not import"

    said = cli(server, "upgrade-server", shlex.join(command))
    if new_build == "same":
        # The server answers before the exec. A client that reads no
        # answer says so and fails. Lillecarl/pymux#606.
        assert said.returncode == 0, said.stderr.decode()
    else:
        # Refused, with the reason, and the log holds the whole of it.
        assert said.returncode != 0
        assert reason in said.stderr.decode() + said.stdout.decode()
        logged = (tmp_path / LOG).read_text()
        assert "failed its check, so the upgrade is refused" in logged
        assert reason in logged
        if new_build == "cannot-import":
            assert "broken on purpose, at import" in logged

    assert _what_survives(server) == before
    # The same process serves either way: an exec keeps the pid.
    pid = _server_pid(server)
    assert pid == old
    if READS_PROC:
        assert (b"resume-server" in _cmdline(pid)) == (new_build == "same")
    _answer(server, "split-window", "-d", "sleep 60")
    newest = max(int(one) for one in _answer(server, "list-panes", "-F", "#{pane_pid}").split())
    if READS_PROC:
        # The server forks its programs itself.
        assert _parent_of(newest) == pid
        assert _inheritable(pid) == []
    numbers = _counted(server)
    assert numbers == list(range(1, LINES + 1))
    _shell_answers(server)
    assert _leftovers(old) == []


def test_a_build_that_crashes_after_the_exec_leaves_a_report(server, tmp_path):
    """
    It passed the check, so the server exec'd it, and it crashed loading
    the snapshot. The panes go with it; what is needed to fix it stays.
    """
    old = _server_pid(server)
    said = cli(server, "upgrade-server", shlex.join(_new_build(tmp_path, CRASHES_AFTER_THE_EXEC)))
    assert said.returncode == 0, said.stderr.decode()

    deadline = time.monotonic() + CLI_TIMEOUT
    while not (reports := list(tmp_path.glob("crash-%d-*.log" % old))):
        assert time.monotonic() < deadline, "no crash report: %s" % ((tmp_path / LOG).read_text(),)
        time.sleep(0.1)
    while not _gone(old):
        assert time.monotonic() < deadline, "the crashed server still runs"
        time.sleep(0.1)
    (kept,) = _leftovers(old)
    try:
        (report,) = reports
        text = report.read_text()
        assert "while resuming from the snapshot %s, which is kept" % (kept,) in text
        assert "RuntimeError: broken on purpose, after the exec" in text
        # The chain: the error, and every frame down to the one that raised.
        assert "Traceback (most recent call last)" in text
        assert "in resumed" in text
        assert "Every thread:" in text
        assert "Current thread" in text or "Thread 0x" in text
        assert "argv" in text and "resume-server" in text
        # The log's last line names the report.
        assert str(report) in (tmp_path / LOG).read_text()
    finally:
        os.unlink(kept)


#: A job that counts, then waits for the file `GO` past the upgrade: the
#: check alone can take longer than the count.
JOB_COUNT = (
    'i=0; while [ $i -lt %d ]; do i=$((i+1)); echo "job $i"; sleep 0.02; done; '
    "while [ ! -e %s ]; do sleep 0.05; done; exit 3"
)
GO = "go"


def _job_count(sock) -> str:
    return JOB_COUNT % (LINES, shlex.quote(str(sock.parent / GO)))


@pytest.mark.parametrize("streams", [["--pty"], []], ids=["on a terminal", "on pipes"])
def test_an_upgrade_keeps_a_running_job(server, streams):
    "The exec keeps it a child, so the new image reads it on and hears its end. Lillecarl/pymux#736."
    job = _answer(server, "run", *streams, _job_count(server)).strip()
    said = cli(server, "upgrade-server", shlex.join([sys.executable, "-m", "pymux"]))
    assert said.returncode == 0, said.stderr.decode()
    (server.parent / GO).touch()

    waited = cli(server, "wait-job", job)
    assert "job %s exited 3" % job in waited.stderr.decode(), _log_of(server)
    numbers = [int(line.split()[1]) for line in waited.stdout.decode().splitlines() if line.startswith("job ")]
    assert numbers == list(range(1, LINES + 1))


def _pane_ids(sock) -> set[str]:
    return set(_answer(sock, "list-panes", "-F", "#{pane_id}").split())


def test_an_upgrade_keeps_a_pane_that_views_a_job(server):
    "The new server views the job again, and says once how it ended. Lillecarl/pymux#715."
    job = _answer(server, "run", _job_count(server)).strip()
    split = _answer(server, "split-window", "-d", "-P", "-F", "#{pane_id}").strip()
    before = _pane_ids(server)
    # The viewer takes the place of the split, under an id of its own.
    _answer(server, "view-job", "-k", "-t", split, job)
    (viewer,) = _pane_ids(server) - before
    said = cli(server, "upgrade-server", shlex.join([sys.executable, "-m", "pymux"]))
    assert said.returncode == 0, said.stderr.decode()
    (server.parent / GO).touch()

    ended = "[job %s exited 3]" % job
    deadline = time.monotonic() + CLI_TIMEOUT
    while ended not in (text := _answer(server, "capture-pane", "-p", "-S", "-", "-t", viewer)):
        assert time.monotonic() < deadline, "the viewer never said how the job ended"
        time.sleep(0.1)
    assert text.count(ended) == 1
    numbers = [int(line.split()[1]) for line in text.splitlines() if line.startswith("job ")]
    assert numbers == list(range(1, LINES + 1))


#: Ignores SIGHUP and writes its pid. `exec` keeps the pid, and an
#: ignored signal stays ignored across it.
IGNORES_HUP = "trap '' HUP; echo $$ > %s; exec sleep 600"


def test_an_upgrade_kills_a_program_still_in_its_grace(server, tmp_path):
    "A pane closed just before an upgrade is in no snapshot, so the old server ends its program. Lillecarl/pyterm#1."
    written = tmp_path / "ignores.pid"
    command = "sh -c %s" % shlex.quote(IGNORES_HUP % shlex.quote(str(written)))
    pane = _answer(server, "split-window", "-d", "-P", "-F", "#{pane_id}", command).strip()
    deadline = time.monotonic() + CLI_TIMEOUT
    while not (text := written.read_text().strip() if written.exists() else ""):
        assert time.monotonic() < deadline, "the program never wrote its pid"
        time.sleep(0.05)
    pid = int(text)

    _answer(server, "kill-pane", "-t", pane)
    said = cli(server, "upgrade-server", shlex.join([sys.executable, "-m", "pymux"]))
    assert said.returncode == 0, said.stderr.decode()

    deadline = time.monotonic() + CLI_TIMEOUT
    while not _gone(pid):
        assert time.monotonic() < deadline, "a program that ignores SIGHUP outlived the upgrade"
        time.sleep(0.05)


def test_a_build_that_ends_without_a_word_is_refused(server):
    before = _what_survives(server)
    refused = cli(server, "upgrade-server", shlex.join(["sh", "-c", "exit 3"]))
    assert refused.returncode != 0
    assert "ended with 3" in refused.stderr.decode() + refused.stdout.decode()
    assert _what_survives(server) == before
    _shell_answers(server)
    assert _leftovers(_server_pid(server)) == []
