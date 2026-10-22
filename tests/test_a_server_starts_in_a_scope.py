"""
A server about to start from a login's scope runs again inside a scope
of the person's systemd, so a logout cannot end it. Lillecarl/pymux#736.

The build sandbox has no systemd, so these hold the decision; the move
itself was measured on a host: the server and its pane both landed in
`user@1000.service/app.slice/run-*.scope`.
"""

from __future__ import annotations

import sys

import pytest

from pymux import scope


@pytest.fixture
def ran(monkeypatch):
    "What `rerun_in_a_scope` would exec, instead of exec'ing it."
    calls = []
    monkeypatch.setattr(scope.shutil, "which", lambda name: "/run/current-system/sw/bin/%s" % name)
    monkeypatch.setattr(scope.os, "execv", lambda path, argv: calls.append(argv))
    monkeypatch.delenv(scope.SCOPED, raising=False)
    return calls


def test_from_a_login_it_runs_again_in_a_scope(monkeypatch, ran):
    monkeypatch.setattr(scope, "_cgroup", lambda: "/user.slice/user-1000.slice/session-263.scope")

    scope.rerun_in_a_scope()

    (argv,) = ran
    assert argv[1:5] == ["--user", "--scope", "--quiet", "--"]
    assert argv[5] == sys.executable
    assert scope.os.environ[scope.SCOPED] == "1"


def test_once_in_a_scope_it_stays(monkeypatch, ran):
    monkeypatch.setattr(scope, "_cgroup", lambda: "/user.slice/user-1000.slice/session-263.scope")
    monkeypatch.setenv(scope.SCOPED, "1")

    scope.rerun_in_a_scope()

    assert ran == []


def test_a_server_a_service_starts_stays_in_that_service(monkeypatch, ran):
    service = "/user.slice/user-1000.slice/user@1000.service/app.slice/pymux.service"
    monkeypatch.setattr(scope, "_cgroup", lambda: service)

    scope.rerun_in_a_scope()

    assert ran == []


def test_with_no_systemd_nothing_changes(monkeypatch, ran):
    monkeypatch.setattr(scope, "_cgroup", lambda: "/user.slice/user-1000.slice/session-263.scope")
    monkeypatch.setattr(scope.shutil, "which", lambda name: None)

    scope.rerun_in_a_scope()

    assert ran == []
