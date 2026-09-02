"""
Drive pymux through libtmux, like it's tmux.

libtmux controls tmux through the `tmux` command line. Pymux implements
that command line, so libtmux can drive pymux as well. This test uses a
small `tmux` shim that runs `python -m pymux`.

Run with:

    nix develop --file . shell --command python3 tests/drive_with_libtmux.py
"""
import stat
import sys
import tempfile
import time
from pathlib import Path

from libtmux import Server
from libtmux.constants import PaneDirection


def make_tmux_shim(directory: Path) -> Path:
    """
    Create a `tmux` executable that runs pymux.
    """
    shim = directory / "tmux"
    shim.write_text(
        "#!/bin/sh\nexec %s -m pymux \"$@\"\n" % (sys.executable,)
    )
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR)
    return shim


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="pymux-libtmux-test-"))
    tmux_bin = make_tmux_shim(tmp)
    socket_path = tmp / "test.sock"

    server = Server(tmux_bin=tmux_bin, socket_path=socket_path)
    assert not server.is_alive(), "Server should not be running yet."

    # new-session: starts the server (daemonized) and creates the session.
    session = server.new_session(session_name="test", attach=False)
    assert session.session_id == "$0", session.session_id
    assert session.session_name == "test"
    assert server.is_alive()

    # One window is created with the session.
    windows = list(session.windows)
    assert len(windows) == 1, windows
    initial_window = windows[0]
    assert initial_window.window_index == "0"

    # new-window
    window = session.new_window(window_name="editor")
    assert window.window_name == "editor"
    assert window.window_index == "1"
    assert list(session.windows)[1].window_id == window.window_id

    # split-window
    pane = window.split(direction=PaneDirection.Right)
    panes = list(window.panes)
    assert len(panes) == 2, panes
    assert pane.pane_id in [p.pane_id for p in panes]

    # Let the shell in the pane start.
    time.sleep(1.0)

    # send-keys + capture-pane
    pane.send_keys("echo hello-from-libtmux", enter=True)
    time.sleep(1.0)
    captured = pane.capture_pane()
    captured_text = "\n".join(captured)
    assert "hello-from-libtmux" in captured_text, captured_text

    # select-pane
    panes[1].select()
    session.refresh()
    active = [p for p in window.panes if p.pane_active == "1"]
    assert [p.pane_id for p in active] == [panes[1].pane_id], active

    # kill-pane
    panes[0].kill()
    time.sleep(0.3)
    remaining = [p.pane_id for p in window.panes]
    assert panes[0].pane_id not in remaining, remaining

    # kill-window
    window.kill()
    time.sleep(0.3)
    assert [w.window_id for w in session.windows] == [initial_window.window_id]

    # kill-session: stops the server.
    server.kill()
    time.sleep(0.5)
    assert not server.is_alive()

    print("All libtmux checks passed.")


if __name__ == "__main__":
    main()
