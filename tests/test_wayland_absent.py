"""
pymux without pysmithay: no compositor, and nothing a pane can tell.

`checks.pymux-unit` runs without the `wayland` extra, so there this is
the real case. Where pysmithay is installed the module is told it is not.
"""

from __future__ import annotations

import pytest

from pymux import wayland
from pymux.main import Pymux


@pytest.fixture
def no_pysmithay(monkeypatch):
    monkeypatch.setattr(wayland, "pysmithay", None)


async def test_a_server_runs_with_no_compositor(no_pysmithay, tmp_path):
    mux = Pymux()
    mux.socket_name = str(tmp_path / "pymux.sock.test")
    async with mux.running():
        assert mux.options.wayland_server
        assert not wayland.available()
        assert mux.wayland.display is None
        mux.handle_command("set-option wayland-server off")
        mux.handle_command("set-option wayland-server on")
        assert mux.wayland.display is None
        mux.stop()
