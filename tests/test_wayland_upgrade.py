"""
A Wayland window survives `upgrade-server`: its compositor's process and
its program are children of the server's pid, which the exec keeps, and
the new image adopts them. A real daemon, because only a process of its
own can exec. Needs the `wayland` extra. Lillecarl/pyterm#1004.
"""

from __future__ import annotations

import base64
import json
import os
import shlex
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("pysmithay")

from test_jobs_over_the_socket import _wait_for_server, cli
from test_upgrade import (
    CANNOT_IMPORT,
    LOG,
    _answer,
    _deadline,
    _gone,
    _new_build,
    _once_it_answers,
    _parent_of,
    _server_pid,
    _server_side,
)

PROBE = Path(__file__).with_name("wayland_probe.py")
NEW_BUILD = [sys.executable, "-m", "pymux"]
PNG = b"\x89PNG"

#: A new build that speaks no protocol a compositor child may speak.
CANNOT_ADOPT = """#!/bin/sh
exec %(python)s -c '
from pymux.wayland import wayland
wayland.speaks = lambda version: False
from pymux.entry_points.run_pymux import run
run()
' "$@"
"""


@pytest.fixture
def server(tmp_path, monkeypatch):
    "A daemon with the probe's window in a pane, as `pymux wl` opens one."
    # pywayland makes its shm pools in XDG_RUNTIME_DIR, which a build
    # sandbox does not set.
    runtime = tmp_path / "run"
    runtime.mkdir(mode=0o700)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    sock = tmp_path / "wayland.sock"
    started = cli(sock, "--log", tmp_path / LOG, "new-session", "-d", "-x", "80", "-y", "24", "sh")
    assert started.returncode == 0, started.stderr.decode()
    _wait_for_server(sock)
    try:
        log = tmp_path / "probe.log"
        log.touch()
        wl = shlex.join([sys.executable, "-m", "pymux", "wl", "--", sys.executable, str(PROBE), str(log)])
        _answer(sock, "split-window", "-d", "--tag", "probe", wl)
        # No client shows it, so it waits for a capture to draw more.
        _until(sock, lambda: _window_pane(sock) is not None, "the window never opened a pane")
        yield sock
    finally:
        cli(sock, "kill-server")


def _until(sock, holds, complaint: str) -> None:
    deadline = _deadline(sock)
    while not holds():
        if deadline.passed():
            pytest.fail("%s in %s\n%s" % (complaint, deadline, _server_side(sock)))
        time.sleep(0.1)


def _window_pane(sock) -> str | None:
    "The id of the pane that shows the probe's window."
    said = _once_it_answers(sock, "sql", "--json", "SELECT pane_id FROM panes WHERE window_pane = 1")
    rows = json.loads(said)["rows"]
    return None if not rows else "%%%d" % rows[0]["pane_id"]


def _compositor(sock) -> int:
    "The compositor's pid, once one runs: the new image adopts it after it answers."
    found: list[int] = []

    def runs() -> bool:
        lines = _once_it_answers(sock, "list-processes").splitlines()
        found[:] = [int(line.split()[1]) for line in lines if line.startswith("wayland ")]
        return bool(found)

    _until(sock, runs, "no compositor runs")
    return found[0]


def _probe_pid(sock, log: Path) -> int:
    "The probe that writes to `log`: another test's runs beside it."
    wanted = [sys.executable.encode(), str(PROBE).encode(), str(log).encode()]
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            argv = (Path("/proc") / pid / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        if argv[:3] == wanted:
            return int(pid)
    raise AssertionError("no probe runs\n%s" % _server_side(sock))


def _draws(sock, pane: str) -> None:
    "The window's frame comes back as a picture, through the compositor this image holds."
    said = base64.b64decode(_once_it_answers(sock, "capture-pane", "-p", "--png", "-t", pane))
    assert said.startswith(PNG)


def test_an_upgrade_keeps_a_window_and_its_program(server, tmp_path):
    log = tmp_path / "probe.log"
    pane, compositor, probe, old = (
        _window_pane(server),
        _compositor(server),
        _probe_pid(server, log),
        _server_pid(server),
    )
    tags = _answer(server, "list-panes", "-a", "--tag", "probe", "-F", "#{pane_id}").split()

    for _ in range(2):
        said = cli(server, "upgrade-server", shlex.join(NEW_BUILD))
        assert said.returncode == 0, said.stderr.decode()
        assert _server_pid(server) == old
        assert _window_pane(server) == pane
        assert _compositor(server) == compositor
        assert _probe_pid(server, log) == probe
        # Still the server's own child: the exec kept the pid.
        assert _parent_of(probe) == old
        assert _answer(server, "list-panes", "-a", "--tag", "probe", "-F", "#{pane_id}").split() == tags
        _draws(server, pane)
    assert "closed" not in log.read_text()


def test_a_refused_upgrade_keeps_the_window_drawing(server, tmp_path):
    pane, compositor = _window_pane(server), _compositor(server)
    extra = tmp_path / "extra"
    extra.mkdir()
    (extra / "broken_on_purpose.py").write_text('raise RuntimeError("broken on purpose, at import")\n')

    said = cli(server, "upgrade-server", shlex.join(_new_build(tmp_path, CANNOT_IMPORT, extra=extra)))

    assert said.returncode != 0
    assert _window_pane(server) == pane
    assert _compositor(server) == compositor
    _draws(server, pane)


def test_close_windows_starts_a_new_compositor(server, tmp_path):
    log = tmp_path / "probe.log"
    compositor, probe = _compositor(server), _probe_pid(server, log)

    said = cli(server, "upgrade-server", "-W", shlex.join(NEW_BUILD))

    assert said.returncode == 0, said.stderr.decode()
    _until(server, lambda: _window_pane(server) is None, "the window pane stayed")
    _until(server, lambda: _gone(probe), "the probe outlived its window")
    _until(server, lambda: _compositor(server) != compositor, "no new compositor started")


def test_an_upgrade_right_after_one_keeps_the_window(server):
    "The second lands while the first's new image adopts the compositor: refused, or carried."
    pane, compositor = _window_pane(server), _compositor(server)

    first = cli(server, "upgrade-server", shlex.join(NEW_BUILD))
    second = cli(server, "upgrade-server", shlex.join(NEW_BUILD))

    assert first.returncode == 0, first.stderr.decode()
    assert second.returncode == 0 or b"still being adopted" in second.stderr, second.stderr.decode()
    assert _compositor(server) == compositor
    assert _window_pane(server) == pane
    _draws(server, pane)


def test_a_build_that_cannot_adopt_the_compositor_is_refused_unless_told(server, tmp_path):
    "Carl's rule: the windows close only on purpose, with -W."
    pane, compositor = _window_pane(server), _compositor(server)
    build = shlex.join(_new_build(tmp_path, CANNOT_ADOPT))

    refused = cli(server, "upgrade-server", build)

    assert refused.returncode != 0
    assert b"upgrade-server -W closes them" in refused.stderr + refused.stdout
    assert _window_pane(server) == pane
    assert _compositor(server) == compositor
    _draws(server, pane)

    told = cli(server, "upgrade-server", "-W", build)

    assert told.returncode == 0, told.stderr.decode()
    _until(server, lambda: _window_pane(server) is None, "the window pane stayed")
