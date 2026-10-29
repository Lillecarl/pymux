"""
A real Firefox in a window pane, switched away from and back to.

Carl saw a Firefox pane go dark after a switch away and back, with raw
frames and VP9 alike. The probe window of `test_window_panes.py` draws
every frame it is asked for; Firefox paints only what changed, and a
still page paints nothing at all after the return. Lillecarl/pymux#753.

Runs in `checks.pymux-firefox`, which puts Firefox on the PATH.
"""

from __future__ import annotations

import os
import shutil
from contextlib import asynccontextmanager

import anyio
import pytest
from prompt_toolkit.application.current import set_app
from prompt_toolkit.data_structures import Size
from session import once, over_connection

pytest.importorskip("pysmithay")
FIREFOX = shutil.which("firefox")
pytestmark = pytest.mark.skipif(FIREFOX is None, reason="no firefox on the PATH")

from pymux.commands import handle_command
from pymux.enums import WindowCodec
from pymux.kitty_frames import SHM_PROBE_ID
from pymux.main import Pymux

SIZE = Size(rows=24, columns=80)

#: First-run pages, the default browser question and every request home
#: off: a sandbox has no network, and a dialog covers the page.
PREFS = """
user_pref("browser.shell.checkDefaultBrowser", false);
user_pref("browser.startup.homepage_override.mstone", "ignore");
user_pref("browser.aboutwelcome.enabled", false);
user_pref("datareporting.policy.dataSubmissionEnabled", false);
user_pref("toolkit.telemetry.reportingpolicy.firstRun", false);
user_pref("browser.tabs.warnOnClose", false);
user_pref("app.update.enabled", false);
user_pref("toolkit.startup.max_resumed_crashes", -1);
"""

PAGES = {
    "still": "<body style='margin:0;background:#ff0000'></body>",
    "moving": (
        "<style>@keyframes go{from{background:#ff0000}to{background:#0000ff}}"
        "body{margin:0;animation:go 1s infinite alternate}</style><body></body>"
    ),
}


class Recorder:
    "What a client's terminal is sent, and the window frames it takes as packets."

    def __init__(self) -> None:
        self.out = ""
        self.frames: list[dict] = []

    def __call__(self, packet) -> None:
        if packet.get("cmd") == "out":
            self.out += packet.get("data", "")
        elif packet.get("cmd") == "window-frame":
            self.frames.append(packet)


#: How a client takes a window pane: as kitty commands its terminal reads,
#: or as packets; the second answering each one as a real client does,
#: which paces Firefox, and with VP9 as well.
ROUTES = {
    "kitty commands": {"frames": False},
    # A kitty on the server's machine, which reads frames from shared memory.
    "kitty shared memory": {"frames": False, "shm": True},
    "frame packets": {"frames": True},
    "answered frames": {"frames": True, "shows": True},
    "answered vp9": {"frames": True, "shows": True, "codecs": ("vp9",)},
    # An answer that takes a round trip over SSH to come back.
    "answered late": {"frames": True, "shows": True, "codecs": ("vp9",), "late": 0.15},
}


async def _answer_frames(state, recorder: Recorder, late: float) -> None:
    "`frame-shown` for each frame, after it arrived, as `client/terminal.py` does once its terminal drew it."
    answered = 0
    while True:
        while answered < len(recorder.frames):
            answered += 1
            await anyio.sleep(late)
            state.connection._process({"cmd": "frame-shown"})
        await anyio.sleep(0.01)


@asynccontextmanager
async def firefox_in_a_pane(tmp_path, page: str, route: str):
    pymux = Pymux()
    pymux.socket_name = str(tmp_path / "pymux.sock.test")
    recorder = Recorder()
    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / "user.js").write_text(PREFS)
    url = tmp_path / "page.html"
    url.write_text(PAGES[page])
    runtime = tmp_path / "run"
    runtime.mkdir(mode=0o700)
    client = dict(ROUTES[route])
    late = client.pop("late", 0.0)
    shm = client.pop("shm", False)
    async with over_connection(pymux=pymux, read_packet=recorder) as session, anyio.create_task_group() as tasks:
        state, _size = await session.attach("one", SIZE, **client)
        if client.get("shows"):
            tasks.start_soon(_answer_frames, state, recorder, late)
        if client.get("codecs"):
            state.session.arrangement.get_active_window_for(state.app).options.window_codec = WindowCodec.VP9
        graphics = state.connection.graphics
        graphics.kitty_supported = True
        graphics.detected = True
        graphics.cell_width, graphics.cell_height = (10, 20)
        graphics.cell_size_known = True
        if shm:
            state.connection._process({"cmd": "in", "data": "\x1b_Gi=%d;OK\x1b\\" % SHM_PROBE_ID})
        await once(lambda: pymux.wayland.display, 10.0, "the compositor never started")
        environment = dict(
            os.environ,
            WAYLAND_DISPLAY=pymux.wayland.display,
            XDG_RUNTIME_DIR=str(runtime),
            HOME=str(tmp_path),
            MOZ_ENABLE_WAYLAND="1",
        )
        environment.pop("DISPLAY", None)
        assert FIREFOX is not None
        async with await anyio.open_process(
            [FIREFOX, "--no-remote", "--new-instance", "--profile", str(profile), url.as_uri()], env=environment
        ) as firefox:
            try:
                await once(lambda: pymux.wayland.panes, 60.0, "Firefox never opened a pane")
                (pane,) = pymux.wayland.panes.values()
                yield pymux, state, pane, recorder
            finally:
                if firefox.returncode is None:
                    firefox.kill()
                tasks.cancel_scope.cancel()


def _pictures(recorder: Recorder, frames: bool) -> int:
    "How many whole pictures the client was sent: a kitty transmit, or a whole window frame."
    if frames:
        return sum(1 for frame in recorder.frames if frame.get("whole"))
    return recorder.out.count("\x1b_Ga=t,")


def _shows(state, pane) -> bool:
    "Whether the client's window holds the pane. The active window is kept per application."
    with set_app(state.app):
        return pane in state.session.arrangement.get_active_window().panes


async def _command(pymux, state, command: str) -> None:
    "A command, as the person at this client types it."
    # Awaited here, not spawned as `Pymux.handle_command` does: the
    # server's task does not run under this client's application.
    with set_app(state.app):
        answer = handle_command(pymux, command)
        if answer is not None:
            await answer


@pytest.mark.parametrize("route", sorted(ROUTES))
@pytest.mark.parametrize("page", sorted(PAGES))
async def test_firefox_draws_again_after_a_switch_away_and_back(tmp_path, page, route):
    frames = ROUTES[route]["frames"]
    async with firefox_in_a_pane(tmp_path, page, route) as (pymux, state, pane, recorder):
        await once(lambda: _pictures(recorder, frames), 60.0, "Firefox never drew")
        await anyio.sleep(1.0)
        await _command(pymux, state, "new-window")
        await once(lambda: not _shows(state, pane), 10.0, "never left")

        # Back and forth, away for longer and shorter than a frame takes.
        for away in (0.5, 0.0, 0.05, 0.2, 0.01, 1.0, 0.1, 0.0, 0.3, 0.02):
            before = _pictures(recorder, frames)
            await _command(pymux, state, "last-window")
            await once(lambda: _shows(state, pane), 10.0, "never came back")
            await once(
                lambda before=before: _pictures(recorder, frames) > before,
                10.0,
                lambda before=before, away=away: (
                    "no whole picture after a return from %ss away: %d before, %d after"
                    % (away, before, _pictures(recorder, frames))
                ),
            )
            await _command(pymux, state, "last-window")
            await once(lambda: not _shows(state, pane), 10.0, "never left")
            await anyio.sleep(away)


async def test_a_still_page_is_sent_once_after_a_return(tmp_path):
    "A page that paints nothing new is one whole picture after a return, not a stream of them."
    async with firefox_in_a_pane(tmp_path, "still", "kitty commands") as (pymux, state, pane, recorder):
        await once(lambda: _pictures(recorder, False), 60.0, "Firefox never drew")
        await anyio.sleep(1.0)
        await _command(pymux, state, "new-window")
        await once(lambda: not _shows(state, pane), 10.0, "never left")
        await anyio.sleep(0.5)
        before, written = _pictures(recorder, False), len(recorder.out)
        await _command(pymux, state, "last-window")
        await anyio.sleep(1.5)
        settled = len(recorder.out)
        await anyio.sleep(1.5)
        assert (_pictures(recorder, False) - before, len(recorder.out) - settled) == (1, 0), (
            "after the return: %d pictures, %d bytes in the first 1.5 s, %d in the next"
            % (_pictures(recorder, False) - before, settled - written, len(recorder.out) - settled)
        )
