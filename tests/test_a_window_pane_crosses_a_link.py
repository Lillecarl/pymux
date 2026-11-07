"""
A Wayland window pane on one server, shown by another that mirrors its
session: the owner sends the window's frames over the peer link, the
peer draws them, and a person's keys and pointer there reach the window.
Needs the `wayland` extra. Lillecarl/pyterm#890.
"""

from __future__ import annotations

import io
import os
import sys
from contextlib import asynccontextmanager

import anyio
import pytest
from prompt_toolkit.data_structures import Point, Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType, MouseModifier
from prompt_toolkit.output import ColorDepth
from prompt_toolkit.output.vt100 import Vt100_Output
from session import Client, once
from test_window_panes import PROBE, logged

pytest.importorskip("pysmithay")

from pymux.core.main import Pymux
from pymux.enums import WindowCodec
from pymux.graphics.kitty_frames import decoders
from pymux.mirror.window import RemoteWindow
from pymux.wayland.window_pane import is_window_pane


def red_square_in(frame) -> bool:
    "Whether the probe's square shows, near enough for a lossy codec (`test_window_panes.red_square_in`)."
    width, _height, rgba = frame
    start = (6 * width + 11) * 4
    red, green, blue, _alpha = rgba[start : start + 4]
    return red > 200 and green < 60 and blue < 60


STAYS = '%s -c "import time; time.sleep(60)"' % (sys.executable,)


@asynccontextmanager
async def mirrored_window(tmp_path, codec: WindowCodec):
    """
    An owner with the probe window open in a pane, and a peer whose
    client looks at the owner's session. Answers both servers, the
    peer's client, the owner's window pane, its mirror at the peer, and
    the probe's log.
    """
    owner = Pymux()
    owner.options.test_mode = True
    owner.serving.listen_on_socket(str(tmp_path / "owner.sock"))
    peer = Pymux()
    peer.options.test_mode = True
    log = tmp_path / "probe.log"
    log.touch()
    runtime = tmp_path / "run"
    runtime.mkdir(mode=0o700)
    async with owner.running(), peer.running():
        owner.startup_command = STAYS
        await owner.startup()
        # At scale 1 the probe's buffer pixels are the frame's, where `red_square_in` looks.
        owner.arrangement.get_active_window().options.wayland_scale = "1"
        owner.arrangement.get_active_window().options.window_codec = codec
        await peer.panes.create_window(STAYS)
        await once(lambda: owner.wayland.display, 10.0, "the compositor never started")
        output = Vt100_Output(stdout=io.StringIO(), get_size=lambda: Size(rows=24, columns=80))
        with create_pipe_input() as pipe:
            client = peer.add_client(
                output=output,
                input=pipe,
                color_depth=ColorDepth.DEPTH_8_BIT,
                client=Client("here", None, peer, "/dev/pts/8"),
            )
            async with await anyio.open_process(
                [sys.executable, str(PROBE), str(log)],
                env=dict(os.environ, WAYLAND_DISPLAY=owner.wayland.display, XDG_RUNTIME_DIR=str(runtime)),
            ) as probe:
                try:
                    await once(lambda: owner.wayland.panes, 10.0, "the window never opened a pane")
                    (there,) = owner.wayland.panes.values()
                    peer.handle_command("connect %s" % (owner.socket_name,))
                    (link,) = peer.peers.by_address.values()
                    here = await once(lambda: link.tree.panes.get(there.pane_id), 10.0, "the pane never came")
                    (session,) = [session for session in peer.sessions if session.remote is not None]
                    peer.attach_client_to(client, session)
                    peer.invalidate()
                    yield owner, peer, client, there, here, log
                finally:
                    if probe.returncode is None:
                        probe.kill()
                    for mux in (owner, peer):
                        for pane in list(mux.panes_by_id.values()):
                            process = getattr(pane, "process", None)
                            if process is not None and not process.is_terminated:
                                process.kill()
                    peer.stop()
                    owner.stop()


CODECS = [
    WindowCodec.LOSSLESS,
    pytest.param(
        WindowCodec.VP9, marks=pytest.mark.skipif(WindowCodec.VP9 not in decoders(), reason="no VP9 decoder here")
    ),
]


@pytest.mark.parametrize("codec", CODECS)
async def test_the_window_draws_on_at_the_peer(tmp_path, codec):
    """
    The probe draws its square only once its first frame was shown. No
    client of the owner shows it, so the square here proves the peer
    took the first frame, and the owner let the window draw the next.
    """
    async with mirrored_window(tmp_path, codec) as (_owner, _peer, _client, _there, here, _log):
        assert is_window_pane(here)
        host = here.terminal.host
        assert isinstance(host, RemoteWindow)
        await once(lambda: host.frame(here.terminal.toplevel), 10.0, "no frame came")
        await once(
            lambda: red_square_in(host.frame(here.terminal.toplevel)), 10.0, "the window never drew on at the peer"
        )


async def test_a_client_there_draws_the_picture(tmp_path):
    "A client of the peer gets the picture as a local window's: the whole frame, then what changed."
    async with mirrored_window(tmp_path, WindowCodec.LOSSLESS) as (_owner, _peer, _client, _there, here, _log):
        host = here.terminal.host
        await once(lambda: host.frame(here.terminal.toplevel), 10.0, "no frame came")
        serial, whole, width, height, rects = host._since(None)
        assert whole
        assert rects == [(0, 0, width, height, bytes(host.picture))]
        assert host._since(serial)[4] == []


async def test_keys_and_the_pointer_there_reach_the_window(tmp_path):
    async with mirrored_window(tmp_path, WindowCodec.LOSSLESS) as (_owner, _peer, _client, _there, here, log):
        await once(lambda: "drew" in logged(log), 10.0, "the window never drew")
        widget = here.terminal
        widget.press("a")
        await once(lambda: "key 30 0" in logged(log), 10.0, lambda: "no key arrived: %s" % logged(log)[-8:])
        for kind in (MouseEventType.MOUSE_DOWN, MouseEventType.MOUSE_UP):
            widget.mouse(MouseEvent(Point(x=3, y=1), kind, MouseButton.LEFT, frozenset[MouseModifier]()))
        await once(lambda: "button 272 0" in logged(log), 10.0, lambda: "no click arrived: %s" % logged(log)[-8:])
        assert "button 272 1" in logged(log)


async def test_the_owner_stops_sending_when_nobody_there_looks(tmp_path):
    "The client there leaves for its own session: the window pauses, and catches up on its return."
    async with mirrored_window(tmp_path, WindowCodec.LOSSLESS) as (owner, peer, client, there, here, _log):
        (link,) = owner.peer_links()
        await once(lambda: link.windows.get(there.pane_id), 10.0, "the window was never lent")
        window = link.windows[there.pane_id]
        own = next(session for session in peer.sessions if session.remote is None)
        mirrored = next(session for session in peer.sessions if session.remote is not None)
        peer.attach_client_to(client, own)
        peer.invalidate()
        await once(lambda: window.paused, 10.0, "a window nobody sees never paused")
        peer.attach_client_to(client, mirrored)
        peer.invalidate()
        await once(lambda: not window.paused, 10.0, "the window never resumed")
