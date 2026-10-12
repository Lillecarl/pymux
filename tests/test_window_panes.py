"""
A Wayland window in a pane, end to end: the window opens a pane, a kitty
client is sent the whole frame and then only the damage, the keyboard
and the mouse reach the window, and the pane goes when the window does.
Needs the `wayland` extra. Lillecarl/pymux#620.
"""

from __future__ import annotations

import os
import shlex
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import anyio
import pytest
from prompt_toolkit.data_structures import Size
from session import once, over_connection

pytest.importorskip("pysmithay")

from pymux.enums import Woke
from pymux.main import Pymux
from pymux.window_pane import is_window_pane

PROBE = Path(__file__).with_name("wayland_probe.py")
SIZE = Size(rows=24, columns=80)


class Recorder:
    "Everything a client's terminal is sent."

    def __init__(self) -> None:
        self.out = ""

    def __call__(self, packet) -> None:
        message = packet
        if message.get("cmd") == "out":
            self.out += message.get("data", "")


@asynccontextmanager
async def with_a_window(tmp_path, kitty: bool = True, animate: bool = False):
    "A server, a client at a kitty terminal, and the probe window open in a pane."
    pymux = Pymux()
    pymux.socket_name = str(tmp_path / "pymux.sock.test")
    recorder = Recorder()
    log = tmp_path / "probe.log"
    log.touch()
    async with over_connection(pymux=pymux, read_packet=recorder) as session:
        state, _size = await session.attach("one", SIZE)
        state.connection.graphics.kitty_supported = kitty
        state.connection.graphics.detected = True
        await once(lambda: pymux.wayland.display, 10.0, "the compositor never started")
        # pywayland makes its shm pools in XDG_RUNTIME_DIR, which a build
        # sandbox does not set.
        runtime = tmp_path / "run"
        runtime.mkdir(mode=0o700)
        async with await anyio.open_process(
            [sys.executable, str(PROBE), str(log), *(["--animate"] if animate else [])],
            env=dict(os.environ, WAYLAND_DISPLAY=pymux.wayland.display, XDG_RUNTIME_DIR=str(runtime)),
        ) as probe:
            try:
                await once(lambda: pymux.wayland.panes, 10.0, "the window never opened a pane")
                (pane,) = pymux.wayland.panes.values()
                yield pymux, state, pane, recorder, log, probe
            finally:
                if probe.returncode is None:
                    probe.kill()


def logged(log: Path) -> list[str]:
    return log.read_text().splitlines()


async def test_a_window_opens_a_pane_beside_the_active_one(tmp_path):
    async with with_a_window(tmp_path) as (pymux, state, pane, _recorder, _log, _probe):
        window = state.session.arrangement.get_active_window()
        assert pane in window.panes
        assert len(window.panes) == 2
        assert window.active_pane is pane
        assert is_window_pane(pane)
        await once(lambda: pane.screen.titles.window == "probe", 10.0, "no title")
        assert pane.name == "pymux.probe"


async def test_a_kitty_client_gets_the_frame_then_only_the_damage(tmp_path):
    async with with_a_window(tmp_path) as (_pymux, _state, _pane, recorder, log, _probe):
        await once(lambda: "drew" in logged(log), 10.0, "the window never drew its second frame")
        # The square, and nothing around it, as an edit of the frame the
        # client holds: the command that carries it is a frame edit.
        await once(lambda: "x=10,y=5,s=4,v=4" in recorder.out, 10.0, "the square's edit never reached the client")
        square = recorder.out.index("x=10,y=5,s=4,v=4")
        command = recorder.out.rindex("\x1b_G", 0, square)
        assert recorder.out.startswith("\x1b_Ga=f,", command)
        # Placed at the pane's cells.
        assert "\x1b_Ga=p," in recorder.out


async def test_a_client_without_kitty_is_sent_no_pixels(tmp_path):
    async with with_a_window(tmp_path, kitty=False) as (_pymux, _state, _pane, recorder, _log, _probe):
        await anyio.sleep(0.5)
        assert "\x1b_Ga=t," not in recorder.out


async def test_a_window_draws_no_faster_than_the_pymux_window_holding_it(tmp_path):
    async with with_a_window(tmp_path, animate=True) as (pymux, state, _pane, _recorder, log, _probe):
        state.session.arrangement.get_active_window().frame_rate = 10
        pymux.invalidate(Woke.WINDOW_PANE_CHANGED)
        await once(lambda: logged(log).count("frame") >= 3, 10.0, "the window never drew again")
        before = logged(log).count("frame")
        await anyio.sleep(1.0)
        drawn = logged(log).count("frame") - before
        # Ten a second, and one for where the second started.
        assert 2 <= drawn <= 11


@asynccontextmanager
async def with_a_shell(tmp_path, monkeypatch):
    "A server, a client at a kitty terminal, and the one pane it started with."
    # pywayland makes its shm pools in XDG_RUNTIME_DIR, and a program
    # `wl` starts takes the caller's environment: this process's here.
    runtime = tmp_path / "run"
    runtime.mkdir(mode=0o700)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    pymux = Pymux()
    pymux.socket_name = str(tmp_path / "pymux.sock.test")
    async with over_connection(pymux=pymux, read_packet=Recorder()) as session:
        state, _size = await session.attach("one", SIZE)
        state.connection.graphics.kitty_supported = True
        state.connection.graphics.detected = True
        await once(lambda: pymux.wayland.display, 10.0, "the compositor never started")
        window = state.session.arrangement.get_active_window()
        (shell,) = window.panes
        yield pymux, session, window, shell


def answered(got: list) -> dict:
    "What a command connection was told: its output, its errors and its exit code."
    told = {"out": "", "err": "", "exit": None}
    for packet in got:
        message = packet
        if message.get("cmd") in ("out", "err"):
            told[message["cmd"]] += message.get("data", "")
        elif message.get("cmd") == "exit":
            told["exit"] = message.get("code")
    return told


async def test_wl_turns_the_pane_it_ran_in_into_the_window(tmp_path, monkeypatch):
    log = tmp_path / "probe.log"
    log.touch()
    async with with_a_shell(tmp_path, monkeypatch) as (pymux, session, window, shell):
        got = await session.command(
            "wl -- %s %s %s" % (sys.executable, PROBE, log), pane_id=shell.pane_id, environment=dict(os.environ)
        )
        await once(lambda: pymux.wayland.panes, 10.0, "the window never opened")
        (pane,) = pymux.wayland.panes.values()
        # In the shell's place, and the shell is gone: one pane, not two.
        assert window.panes == [pane]
        assert window.active_pane is pane
        await once(lambda: "drew" in logged(log), 10.0, "the window never drew")
        # Its window opened, so `wl` has nothing to say.
        await once(lambda: answered(got)["exit"] is not None, 10.0, "wl never answered")
        assert answered(got) == {"out": "", "err": "", "exit": 0}


async def test_a_later_window_of_a_wl_program_goes_beside_its_first(tmp_path, monkeypatch):
    first, second = tmp_path / "first.log", tmp_path / "second.log"
    first.touch()
    second.touch()
    # One program, two connections: the second takes WAYLAND_DISPLAY.
    probe = "%s %s" % (sys.executable, PROBE)
    both = "env -u WAYLAND_SOCKET %s %s & exec %s %s" % (probe, second, probe, first)
    async with with_a_shell(tmp_path, monkeypatch) as (pymux, session, window, shell):
        await session.command("wl -- sh -c %s" % shlex.quote(both), pane_id=shell.pane_id, environment=dict(os.environ))
        await once(lambda: len(pymux.wayland.panes) == 2, 10.0, "two windows never opened")
        assert shell not in window.panes
        assert len(window.panes) == 2


async def test_wl_tells_why_a_program_opened_no_window(tmp_path, monkeypatch):
    async with with_a_shell(tmp_path, monkeypatch) as (_pymux, session, window, shell):
        got = await session.command("wl -- sh -c 'echo no display here; exit 3'", pane_id=shell.pane_id)
        await once(lambda: answered(got)["exit"] is not None, 10.0, "wl never answered")
        assert answered(got) == {"out": "no display here\n", "err": "", "exit": 3}
        assert window.panes == [shell]

        got = await session.command("wl -- /no/such/program", pane_id=shell.pane_id)
        await once(lambda: answered(got)["exit"] is not None, 10.0, "wl never answered")
        assert "/no/such/program" in answered(got)["err"]
        assert answered(got)["exit"] == 1


async def test_keys_reach_the_window(tmp_path):
    async with with_a_window(tmp_path) as (pymux, state, pane, _recorder, log, _probe):
        # The window's seat is set up before it draws.
        await once(lambda: "drew" in logged(log), 10.0, "the window never drew")
        widget = pane.terminal
        widget.press("a")
        widget.press("A")
        await once(lambda: logged(log).count("key 30 0") >= 2, 10.0, "keys never arrived: %s" % logged(log))
        lines = logged(log)
        # "A" is shift (42) around a.
        assert "key 42 1" in lines
        assert "key 42 0" in lines


async def test_the_pane_goes_when_the_window_closes(tmp_path):
    async with with_a_window(tmp_path) as (pymux, state, pane, _recorder, log, probe):
        await once(lambda: "drew" in logged(log), 10.0, "the window never drew")
        pymux.kill_pane(pane)
        await once(lambda: "closed" in logged(log), 10.0, "the window was never asked to close")
        with anyio.fail_after(10):
            await probe.wait()
        await once(lambda: not pymux.wayland.panes, 10.0, "the compositor still holds the window")
        window = state.session.arrangement.get_active_window()
        assert pane not in window.panes
        assert window.active_pane in window.panes
