"""
Jobs through a real client and a real server.

The suites beside this judge each half with the other half faked:
the packet tests read what a stub socket was sent, and the job tests
set the caller context by hand. This judges the seam they meet at: a
real `pymux` CLI process with an agent session in its environment, a
real server daemon on a socket, and the stamp, lookup, directory and
variables that come out.

No pty and no attach: detached commands are the whole subject, so a
daemon and `run_cli` are the whole fixture. `drive_with_pty.py` owns
the shape; this borrows nothing but the idea.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time

import pytest

from pymux.agentic import AGENT_SESSION_VARS

#: How long one CLI call may take. A detached command answers at
#: once; anything slower is the server not answering at all.
CLI_TIMEOUT = 20


def _wait_for_server(sock_path, timeout: float = 10.0) -> None:
    "Hold until the daemon answers on its socket, or fail the test."
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                sock.connect(str(sock_path))
                return
            finally:
                sock.close()
        except OSError:
            time.sleep(0.05)
    pytest.fail("no server answered on %s" % (sock_path,))


@pytest.fixture
def server(tmp_path):
    """
    A server daemon with nothing in it, and its socket.

    `new-session -d` starts the daemon without attaching, the way a
    person starts one, and `kill-server` ends it. The pane sleeps so
    the server outlives every command of the test.
    """
    sock_path = tmp_path / "jobs.sock"
    started = subprocess.run(
        [sys.executable, "-m", "pymux", "-S", str(sock_path), "new-session", "-d", "-s", "test", "sleep 60"],
        capture_output=True,
        timeout=CLI_TIMEOUT,
        check=False,
    )
    assert started.returncode == 0, started.stderr.decode()
    _wait_for_server(sock_path)
    try:
        yield sock_path
    finally:
        subprocess.run(
            [sys.executable, "-m", "pymux", "-S", str(sock_path), "kill-server"],
            capture_output=True,
            timeout=CLI_TIMEOUT,
            check=False,
        )


def cli(sock_path, *args, env=None, cwd=None):
    """
    One detached command, as a real CLI process.

    The environment is the test process's own with every agent
    variable scrubbed first, so a session id the developer happens to
    carry never leaks into an "anonymous" call; what `env` names is
    then the whole agent environment of the call. The directory
    defaults to the test process's own.
    """
    full_env = {key: value for key, value in os.environ.items() if key not in AGENT_SESSION_VARS}
    full_env.update(env or {})
    return subprocess.run(
        [sys.executable, "-m", "pymux", "-S", str(sock_path), *[str(a) for a in args]],
        capture_output=True,
        timeout=CLI_TIMEOUT,
        check=False,
        env=full_env,
        cwd=str(cwd) if cwd is not None else None,
    )


def test_a_cli_session_stamps_and_finds_its_job(server):
    first = cli(server, "run", "--tag", "e2e", "-w", "echo one", env={"OPENCODE_SESSION_ID": "e2e-1"})
    assert first.returncode == 0, first.stderr
    second = cli(server, "run", "--tag", "e2e", "-w", "echo two", env={"OPENCODE_SESSION_ID": "e2e-2"})
    assert second.returncode == 0, second.stderr

    # The caller's own job answers, though another session's job is
    # newer-or-older beside it; naming that session scopes to it; and
    # with no caller at all the lookup still reaches a job rather than
    # nothing.
    shown = cli(server, "show-job", "--tag", "e2e", env={"OPENCODE_SESSION_ID": "e2e-1"})
    assert shown.returncode == 0, shown.stderr
    assert "one" in shown.stdout.decode() and "two" not in shown.stdout.decode()

    scoped = cli(server, "show-job", "--tag", "e2e", "--session", "e2e-2")
    assert scoped.returncode == 0, scoped.stderr
    assert "two" in scoped.stdout.decode() and "one" not in scoped.stdout.decode()

    listed = cli(server, "list-jobs", env={"OPENCODE_SESSION_ID": "e2e-1"})
    assert listed.returncode == 0, listed.stderr
    lines = listed.stdout.decode().splitlines()
    assert lines[0].startswith("1 ") and "[e2e,session=e2e-1]" in lines[0]
    assert lines[1].startswith("2 ") and "[e2e,session=e2e-2]" in lines[1]


def test_a_job_inherits_the_callers_directory_and_environment(server, tmp_path):
    run = cli(
        server,
        "run",
        "--tag",
        "e2e-env",
        "-w",
        "sh",
        "-c",
        "echo $E2E_MARKER; pwd",
        env={"E2E_MARKER": "from-the-caller"},
        cwd=tmp_path,
    )
    assert run.returncode == 0, run.stderr
    # `-w` answers with the output: the marker proves the variables,
    # and the directory proves the working directory.
    assert "from-the-caller" in run.stdout.decode()
    assert str(tmp_path) in run.stdout.decode()

    shown = cli(server, "show-job", "--tag", "e2e-env")
    assert shown.returncode == 0, shown.stderr
    assert "from-the-caller" in shown.stdout.decode()


def test_saved_queries_answer_over_the_wire(server):
    cli(server, "run", "--tag", "e2e", "-w", "echo one", env={"OPENCODE_SESSION_ID": "e2e-1"})
    answered = cli(server, "sql", "--json", "--run", "by_tag", "--param", "tag=session")
    assert answered.returncode == 0, answered.stderr
    rows = json.loads(answered.stdout.decode())["rows"]
    assert rows == [{"id": 1, "command": "echo one"}]
