"""
`pymux new-session` with no server running starts one, and the flags
mean what they mean against a running server.
"""

from __future__ import annotations

import subprocess
import sys

from test_jobs_over_the_socket import CLI_TIMEOUT, _wait_for_server, cli


def test_the_name_names_the_window_and_stays_out_of_the_command(tmp_path):
    "`-n named` joined the command, so the pane ran `named sleep 60` and the server ended with it."
    sock_path = tmp_path / "named.sock"
    started = subprocess.run(
        [sys.executable, "-m", "pymux", "-S", str(sock_path), "new-session", "-d", "-n", "named", "sleep 60"],
        capture_output=True,
        timeout=CLI_TIMEOUT,
        check=False,
    )
    assert started.returncode == 0, started.stderr.decode()
    _wait_for_server(sock_path)
    try:
        listed = cli(sock_path, "list-windows", "-F", "#{window_name}|#{pane_current_command}")
        assert listed.returncode == 0, listed.stderr.decode()
        assert listed.stdout.decode().strip() == "named|sleep"
    finally:
        cli(sock_path, "kill-server")
