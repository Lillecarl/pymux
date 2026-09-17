"""
What `pymux diagnose` reports.

The report is the answer to a support conversation, so the tests read
it back as a person and as a machine: the human lines name the facts,
the JSON parses, and the socket facts say what is really there -- a
server that answers, and a path where none is.
"""

import json
import os
import socket
import subprocess
import sys
import time

from pymux.diagnose import diagnose, human, as_json


def test_a_report_without_a_server(tmp_path):
    "The honest answer when nothing is running, in both shapes."
    missing = str(tmp_path / "nothing.sock")

    report = diagnose(socket_name=missing, config_file=None)

    assert report["socket"]["exists"] is False
    assert report["socket"]["connectable"] is False
    assert "no server running" in report["socket"]["error"]
    assert report["configuration"]["file"] is None
    # A run in a test has no terminal of its own to ask.
    assert report["outer_terminal"]["asked"] is False

    json.loads(as_json(report))
    assert "TERM" in human(report)


def test_a_report_of_a_running_server(tmp_path):
    "A server that is there is told apart from one that is not."
    sock = str(tmp_path / "pymux.sock.diagnose-test.0")
    env = dict(os.environ)
    env.pop("PYMUX", None)
    server = subprocess.Popen(
        [sys.executable, "-m", "pymux", "start-server", "-S", sock],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.time() + 10
        while not os.path.exists(sock) and time.time() < deadline:
            time.sleep(0.05)
        assert os.path.exists(sock), "the test server never came up"

        report = diagnose(socket_name=sock, config_file=None)

        assert report["socket"]["connectable"] is True
        assert report["socket"]["sessions"], "the server listed no session"
        assert report["pymux"]["version"]

        # The human shape names the session, since that is what a
        # person pastes it for.
        text = human(report)
        assert "sessions" in text
    finally:
        subprocess.run(
            [sys.executable, "-m", "pymux", "-S", sock, "kill-server"],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
        server.wait(timeout=30)


def test_a_socket_that_refuses_is_an_error_and_not_a_crash(tmp_path):
    "Something listens and says nothing: the report says so."
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        sock = str(tmp_path / "mute.sock")
        listener.bind(sock)
        listener.listen(1)

        report = diagnose(socket_name=sock, config_file=None)

        assert report["socket"]["connectable"] is True
