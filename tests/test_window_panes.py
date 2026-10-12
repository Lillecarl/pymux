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

from pymux.enums import WindowCodec, Woke
from pymux.main import Pymux
from pymux.window_pane import is_window_pane

PROBE = Path(__file__).with_name("wayland_probe.py")
SIZE = Size(rows=24, columns=80)


class Recorder:
    "Everything a client's terminal is sent."

    def __init__(self) -> None:
        self.out = ""
        #: The `window-frame` packets, for a client that takes them.
        self.frames: list[dict] = []

    def __call__(self, packet) -> None:
        message = packet
        if message.get("cmd") == "out":
            self.out += message.get("data", "")
        elif message.get("cmd") == "window-frame":
            self.frames.append(message)


@asynccontextmanager
async def with_a_window(
    tmp_path,
    kitty: bool = True,
    animate: bool = False,
    scale: str = "1",
    cell: tuple[int, int] | None = None,
    idle: bool = False,
    frames: bool = False,
    codecs: tuple[str, ...] = (),
    codec: WindowCodec = WindowCodec.LOSSLESS,
):
    """
    A server, a client at a kitty terminal, and the probe window open in a pane.

    The window draws at `scale`: at 1 the probe's buffer pixels are the
    canvas's. `cell` is the client's cell, width and height, when it
    answered one. `codecs` are the ones the client decodes, and `codec`
    the window's `window-codec`.
    """
    pymux = Pymux()
    pymux.socket_name = str(tmp_path / "pymux.sock.test")
    recorder = Recorder()
    log = tmp_path / "probe.log"
    log.touch()
    async with over_connection(pymux=pymux, read_packet=recorder) as session:
        state, _size = await session.attach("one", SIZE, frames=frames, codecs=codecs)
        graphics = state.connection.graphics
        graphics.kitty_supported = kitty
        graphics.detected = True
        if cell is not None:
            graphics.cell_width, graphics.cell_height = cell
            graphics.cell_size_known = True
        state.session.arrangement.get_active_window().wayland_scale = scale
        state.session.arrangement.get_active_window().window_codec = codec
        await once(lambda: pymux.wayland.display, 10.0, "the compositor never started")
        # pywayland makes its shm pools in XDG_RUNTIME_DIR, which a build
        # sandbox does not set.
        runtime = tmp_path / "run"
        runtime.mkdir(mode=0o700)
        async with await anyio.open_process(
            [sys.executable, str(PROBE), str(log), *(["--animate"] if animate else []), *(["--idle"] if idle else [])],
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


def frames_of(recorder: Recorder) -> list[list[dict]]:
    "The `window-frame` packets, gathered into frames at each last part."
    frames: list[list[dict]] = [[]]
    for part in recorder.frames:
        frames[-1].append(part)
        if part.get("last"):
            frames.append([])
    return frames[:-1]


async def test_a_client_that_encodes_frames_gets_raw_pixels(tmp_path):
    "The whole frame, then the square alone, as RGB bands. Lillecarl/pymux#634."
    async with with_a_window(tmp_path, frames=True) as (_pymux, _state, _pane, recorder, log, _probe):
        await once(lambda: "drew" in logged(log), 10.0, "the window never drew its second frame")
        square = [[10, 5, 4, 4, bytes([0xFF, 0, 0]) * 16]]
        await once(
            lambda: any(frame[-1]["rects"] == square for frame in frames_of(recorder)),
            10.0,
            lambda: (
                "frames %s" % [[part["rects"][:1] and part["rects"][0][:4] for part in f] for f in frames_of(recorder)]
            ),
        )
        assert "\x1b_Ga=t," not in recorder.out, "the frame went as kitty commands too"
        first = frames_of(recorder)[0]
        assert first[0]["whole"]
        width, height = first[0]["width"], first[0]["height"]
        assert sum(len(band[4]) for part in first for band in part["rects"]) == width * height * 3
        # Placed at the pane's cells, after the pixels.
        assert "\x1b_Ga=p," in first[-1]["after"]
        assert all(len(band[4]) <= 64 * 1024 for part in first for band in part["rects"])


async def test_a_client_that_decodes_vp9_gets_the_window_in_it(tmp_path):
    "A keyframe, then the block around the square. Lillecarl/pymux#639."
    pymux_rs = pytest.importorskip("pymux_rs")
    async with with_a_window(tmp_path, frames=True, codecs=("vp9",), codec=WindowCodec.VP9) as (
        _pymux,
        _state,
        _pane,
        recorder,
        log,
        _probe,
    ):
        await once(lambda: "drew" in logged(log), 10.0, "the window never drew its second frame")
        block = [[0, 0, 16, 16]]
        await once(
            lambda: any(frame[-1]["rects"] == block for frame in frames_of(recorder)),
            10.0,
            lambda: "frames %s" % [frame[-1]["rects"] for frame in frames_of(recorder)],
        )
        frames = frames_of(recorder)
        assert all(part["codec"] == "vp9" and "video" in part for frame in frames for part in frame)
        assert frames[0][0]["whole"]
        assert "\x1b_Ga=p," in frames[0][-1]["after"]
        decoder = pymux_rs.Vp9Decoder()
        for frame in frames:
            _width, _height, rects = decoder.decode(
                b"".join(part["video"] for part in frame), [tuple(rect) for rect in frame[-1]["rects"]]
            )
        ((x, y, w, h, rgb),) = rects
        assert (x, y, w, h) == (0, 0, 16, 16)
        at = (5 * 16 + 10) * 3
        red = bytes([0xFF, 0, 0])
        # Lossy: a saturated red in a small square came back 11 off.
        assert max(abs(a - b) for a, b in zip(rgb[at : at + 3], red, strict=True)) <= 16


async def test_a_client_that_decodes_no_vp9_gets_raw_pixels_all_the_same(tmp_path):
    async with with_a_window(tmp_path, frames=True, codec=WindowCodec.VP9) as (
        _pymux,
        _state,
        _pane,
        recorder,
        _log,
        _probe,
    ):
        await once(lambda: frames_of(recorder), 10.0, "no frame came")
        assert "codec" not in frames_of(recorder)[0][0]


async def test_the_codec_switches_while_the_window_shows(tmp_path):
    "The next frame goes the new way, whole, though the window draws nothing new."
    async with with_a_window(tmp_path, frames=True, codecs=("vp9",)) as (
        pymux,
        _state,
        _pane,
        recorder,
        log,
        _probe,
    ):
        await once(lambda: "drew" in logged(log), 10.0, "the window never drew its second frame")
        await once(lambda: len(frames_of(recorder)) >= 2, 10.0, "no raw frames came")
        assert "codec" not in frames_of(recorder)[-1][0]

        for value, codec in (("vp9", "vp9"), ("lossless", None)):
            before = len(frames_of(recorder))
            answer = pymux.handle_command("set-window-option window-codec %s" % value)
            if answer is not None:
                await answer
            await once(
                lambda: len(frames_of(recorder)) > before,
                10.0,
                lambda value=value: "no frame after switching to %s" % value,
            )
            first = frames_of(recorder)[before][0]
            assert (first.get("codec"), first["whole"]) == (codec, True)


async def test_a_client_without_kitty_is_sent_no_pixels(tmp_path):
    async with with_a_window(tmp_path, kitty=False) as (_pymux, _state, _pane, recorder, _log, _probe):
        await anyio.sleep(0.5)
        assert "\x1b_Ga=t," not in recorder.out


async def test_a_window_draws_no_faster_than_a_frame_rate_chosen_for_it(tmp_path):
    async with with_a_window(tmp_path, animate=True) as (pymux, state, _pane, _recorder, log, _probe):
        window = state.session.arrangement.get_active_window()
        # What `set-window-option frame-rate 10` does.
        window.frame_rate = 10
        window.frame_rate_set = True
        pymux.invalidate(Woke.WINDOW_PANE_CHANGED)
        await once(lambda: logged(log).count("frame") >= 3, 10.0, "the window never drew again")
        before = logged(log).count("frame")
        await anyio.sleep(1.0)
        drawn = logged(log).count("frame") - before
        # Ten a second, and one for where the second started.
        assert 2 <= drawn <= 11


async def test_a_window_with_no_frame_rate_chosen_goes_at_the_links_pace(tmp_path):
    "The default thirty paces the cells, not a window. Lillecarl/pymux#636."
    async with with_a_window(tmp_path, animate=True) as (pymux, state, _pane, _recorder, log, _probe):
        assert not state.session.arrangement.get_active_window().frame_rate_set
        await once(lambda: logged(log).count("frame") >= 3, 10.0, "the window never drew again")
        before = logged(log).count("frame")
        await anyio.sleep(1.0)
        assert logged(log).count("frame") - before > 40


async def test_a_commit_that_draws_nothing_is_answered_at_the_outputs_refresh(tmp_path):
    """
    Firefox commits a frame callback with no buffer and paints nothing
    until it is answered: the window stood still, and so did every
    click. With nothing sent, no link paces it, so the 60 Hz the output
    says it has does. Lillecarl/pymux#638.
    """
    async with with_a_window(tmp_path, idle=True) as (_pymux, _state, _pane, _recorder, log, _probe):
        await once(lambda: logged(log).count("idle") >= 3, 10.0, lambda: "answered %d" % logged(log).count("idle"))
        before = logged(log).count("idle")
        await anyio.sleep(1.0)
        assert logged(log).count("idle") - before <= 62


def last_size(log: Path) -> tuple[int, int] | None:
    "The window size the probe was last configured with, in its own pixels."
    sizes = [line.split()[1:] for line in logged(log) if line.startswith("size ")]
    return (int(sizes[-1][0]), int(sizes[-1][1])) if sizes else None


async def test_auto_scale_reads_the_clients_cell(tmp_path):
    "A cell twice the nominal height is scale 2: the window lays out in half the pixels."
    async with with_a_window(tmp_path, scale="auto", cell=(16, 32)) as (_pymux, _state, pane, _recorder, log, _probe):
        widget = pane.terminal
        await once(lambda: widget.columns, 10.0, "the pane was never sized")
        expected = (widget.columns * 16 // 2, widget.rows * 32 // 2)
        await once(lambda: last_size(log) == expected, 10.0, lambda: "sized %s" % (last_size(log),))


async def test_wayland_scale_overrides_auto_at_once(tmp_path):
    async with with_a_window(tmp_path, scale="auto", cell=(16, 32)) as (pymux, _state, pane, _recorder, log, _probe):
        widget = pane.terminal
        await once(lambda: widget.columns, 10.0, "the pane was never sized")
        answer = pymux.handle_command("set-window-option wayland-scale auto-1")
        if answer is not None:
            await answer
        expected = (widget.columns * 16, widget.rows * 32)
        await once(lambda: last_size(log) == expected, 10.0, lambda: "sized %s" % (last_size(log),))


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
