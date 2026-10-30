"""
A Wayland window in a pane, end to end: the window opens a pane, a kitty
client is sent the whole frame and then only the damage, the keyboard
and the mouse reach the window, and the pane goes when the window does.
Needs the `wayland` extra. Lillecarl/pymux#620.
"""

from __future__ import annotations

import base64
import io
import os
import shlex
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import anyio
import pytest
from PIL import Image
from prompt_toolkit.data_structures import Size
from session import once, over_connection

pytest.importorskip("pysmithay")

from pymux.enums import WindowCodec, WindowScroll, Woke
from pymux.graphics import WINDOW_Z
from pymux.main import Pymux
from pymux.window_options import DEFAULT_FRAME_RATE
from pymux.window_pane import is_window_pane

PROBE = Path(__file__).with_name("wayland_probe.py")
SIZE = Size(rows=24, columns=80)
#: A face for the pictures pymux draws itself; `checks.pymux-wayland` names one.
FONT = os.environ.get("PYMUX_TEST_FONT", "/nonexistent")


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
    shows: bool = False,
    text: bool = False,
):
    """
    A server, a client at a kitty terminal, and the probe window open in a pane.

    The window draws at `scale`: at 1 the probe's buffer pixels are the
    canvas's. `cell` is the client's cell, width and height, when it
    answered one. `codecs` are the ones the client decodes, and `codec`
    the window's `window-codec`. `shows` says the client answers each
    frame, which a test then does itself.
    """
    pymux = Pymux()
    pymux.socket_name = str(tmp_path / "pymux.sock.test")
    recorder = Recorder()
    log = tmp_path / "probe.log"
    log.touch()
    async with over_connection(pymux=pymux, read_packet=recorder) as session:
        state, _size = await session.attach("one", SIZE, frames=frames, codecs=codecs, shows=shows)
        graphics = state.connection.graphics
        graphics.kitty_supported = kitty
        graphics.detected = True
        if cell is not None:
            graphics.cell_width, graphics.cell_height = cell
            graphics.cell_size_known = True
        state.session.arrangement.get_active_window().options.wayland_scale = scale
        state.session.arrangement.get_active_window().options.window_codec = codec
        await once(lambda: pymux.wayland.display, 10.0, "the compositor never started")
        # pywayland makes its shm pools in XDG_RUNTIME_DIR, which a build
        # sandbox does not set.
        runtime = tmp_path / "run"
        runtime.mkdir(mode=0o700)
        async with await anyio.open_process(
            [
                sys.executable,
                str(PROBE),
                str(log),
                *(["--animate"] if animate else []),
                *(["--idle"] if idle else []),
                *(["--text"] if text else []),
            ],
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


@pytest.mark.parametrize("frames", [False, True], ids=["kitty", "client-encodes"])
async def test_a_redraw_places_the_window_again(tmp_path, frames):
    """
    A redraw erases the terminal, and an erase takes kitty's placements
    with it. So the window goes again whole, and is placed again: else
    its pane shows the background. `record-client` redraws.
    """
    async with with_a_window(tmp_path, frames=frames) as (pymux, state, _pane, recorder, log, _probe):

        def placed() -> bool:
            return "\x1b_Ga=p," in recorder.out or any(
                "\x1b_Ga=p," in frame.get("after", "") for frame in recorder.frames
            )

        await once(lambda: "drew" in logged(log), 10.0, "the window never drew its second frame")
        await once(placed, 10.0, "the window was never placed")
        await anyio.sleep(0.3)
        recorder.out, recorder.frames = "", []
        pymux.redraw(state)
        await once(placed, 10.0, "the redraw left the window unplaced")


async def test_the_picture_goes_under_what_pymux_draws_over_the_pane(tmp_path):
    "Under every cell with a colour, so a prompt or a message covers it. Lillecarl/pymux#647."
    async with with_a_window(tmp_path) as (_pymux, _state, pane, recorder, _log, _probe):
        await once(lambda: "\x1b_Ga=p," in recorder.out, 10.0, "the window was never placed")
        put = recorder.out[recorder.out.index("\x1b_Ga=p,") :].split("\x1b\\", 1)[0]
        assert ",z=%d" % WINDOW_Z in put
        # And the pane's own cells keep the default background, which
        # the picture shows through.
        assert pane.terminal.window.style == "bg:default"


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
        decoder = pymux_rs.Vp9Decoder()
        decoded: list = []
        red = bytes([0xFF, 0, 0])
        at = (5 * 16 + 10) * 3

        def square_is_red() -> bool:
            "Decode what came since the last look; whether the square's pixel is red."
            for frame in frames_of(recorder)[len(decoded) :]:
                rects = [tuple(rect) for rect in frame[-1]["rects"]]
                decoded.append(decoder.decode(b"".join(part["video"] for part in frame), rects))
            last = decoded[-1][2] if decoded else []
            if [rect[:4] for rect in last] != [(0, 0, 16, 16)]:
                return False
            # Lossy: a saturated red in a small square came back 11 off.
            return max(abs(a - b) for a, b in zip(last[0][4][at : at + 3], red, strict=True)) <= 16

        # The one pixel frame before the square damages the same block:
        # only the decoded pixels tell the two apart.
        await once(square_is_red, 10.0, lambda: "rects %s" % [frame[-1]["rects"] for frame in frames_of(recorder)])
        frames = frames_of(recorder)
        assert all(part["codec"] == "vp9" and "video" in part for frame in frames for part in frame)
        assert frames[0][0]["whole"]
        assert "\x1b_Ga=p," in frames[0][-1]["after"]


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


async def test_a_client_that_answers_frames_has_one_unanswered_at_most(tmp_path):
    """
    The next frame waits for the client to say it wrote the last one: a
    socket that drained says only that the kernel took it, and over SSH
    the frames piled up past it. Lillecarl/pymux#641.
    """
    async with with_a_window(tmp_path, animate=True, frames=True, shows=True) as (
        _pymux,
        state,
        _pane,
        recorder,
        log,
        _probe,
    ):
        await once(lambda: frames_of(recorder), 10.0, "no frame came")
        drawn = logged(log).count("frame")
        await anyio.sleep(0.5)
        assert len(frames_of(recorder)) == 1, "a second frame went before the first was answered"
        # The window is held too: it was not told to draw on.
        assert logged(log).count("frame") <= drawn + 1
        for answered in range(1, 4):
            state.connection._process({"cmd": "frame-shown"})
            await once(
                lambda answered=answered: len(frames_of(recorder)) > answered,
                10.0,
                "no frame after the answer",
            )
            await anyio.sleep(0.2)
            assert len(frames_of(recorder)) == answered + 1


async def test_the_terminals_answer_lets_the_next_frame_go(tmp_path):
    """
    The last part asks the client to let its terminal answer, and that
    answer, arriving as terminal input, counts as the frame shown.
    Lillecarl/pymux#643.
    """
    async with with_a_window(tmp_path, animate=True, frames=True, shows=True) as (
        _pymux,
        state,
        _pane,
        recorder,
        _log,
        _probe,
    ):
        await once(lambda: frames_of(recorder), 10.0, "no frame came")
        first = frames_of(recorder)[0]
        assert first[-1]["answer"]
        image = first[-1]["image"]
        for answered in range(1, 3):
            state.connection._process({"cmd": "in", "data": "\x1b_Gi=%d;OK\x1b\\" % image})
            await once(
                lambda answered=answered: len(frames_of(recorder)) > answered,
                10.0,
                "no frame after the terminal answered",
            )


async def test_a_terminal_that_never_answers_costs_frames_not_the_window(tmp_path, monkeypatch):
    "Lillecarl/pymux#643."
    monkeypatch.setattr("pymux.server.FRAME_ANSWER_SECONDS", 0.3)
    async with with_a_window(tmp_path, animate=True, frames=True, shows=True) as (
        _pymux,
        _state,
        _pane,
        recorder,
        _log,
        _probe,
    ):
        await once(lambda: len(frames_of(recorder)) >= 3, 10.0, "the window stopped at an unanswered frame")


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
            # A frame sent before the switch may still arrive after it.
            switched = await once(
                lambda codec=codec: next(
                    (
                        index
                        for index, frame in enumerate(frames_of(recorder)[before:], before)
                        if frame[0].get("codec") == codec and frame[0]["whole"]
                    ),
                    None,
                ),
                10.0,
                lambda value=value: "no whole frame after switching to %s" % value,
            )
            assert all(frame[0].get("codec") == codec for frame in frames_of(recorder)[switched:])


async def test_a_client_without_kitty_is_sent_no_pixels(tmp_path):
    async with with_a_window(tmp_path, kitty=False) as (_pymux, _state, _pane, recorder, _log, _probe):
        await anyio.sleep(0.5)
        assert "\x1b_Ga=t," not in recorder.out


async def test_a_window_draws_no_faster_than_a_frame_rate_chosen_for_it(tmp_path):
    async with with_a_window(tmp_path, animate=True) as (pymux, state, _pane, _recorder, log, _probe):
        window = state.session.arrangement.get_active_window()
        # What `set-window-option frame-rate 10` does.
        window.options.frame_rate = 10
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
        # More than the thirty that caps the cells. The link's pace is 37
        # to 39 a second on a machine at load 10 to 16, where 40 failed.
        assert logged(log).count("frame") - before > DEFAULT_FRAME_RATE + 3


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
        shell.tags["browser"] = "probe"
        got = await session.command(
            "wl -- %s %s %s" % (sys.executable, PROBE, log), pane_id=shell.pane_id, environment=dict(os.environ)
        )
        await once(lambda: pymux.wayland.panes, 10.0, "the window never opened")
        (pane,) = pymux.wayland.panes.values()
        # In the shell's place, and the shell is gone: one pane, not two.
        assert window.panes == [pane]
        assert window.active_pane is pane
        # A caller that tagged the pane finds the window by the same tags.
        assert pane.tags == {"browser": "probe"}
        await once(lambda: "drew" in logged(log), 10.0, "the window never drew")
        # Its window opened, so `wl` has nothing to say.
        await once(lambda: answered(got)["exit"] is not None, 10.0, "wl never answered")
        assert answered(got) == {"out": "", "err": "", "exit": 0}


def red_square_in(frame) -> bool:
    "Whether the probe's frame holds the square it draws once its first frame was shown."
    width, _height, rgba = frame
    start = (6 * width + 11) * 4
    return rgba[start : start + 4] == bytes([0xFF, 0x00, 0x00, 0xFF])


async def test_a_capture_lets_a_window_nobody_shows_draw_what_it_waits_to(tmp_path):
    """
    A client with no kitty graphics shows no window, so the probe waits
    after its first frame. What a capture asks for first lets it draw
    on to the square. Lillecarl/pymux#822.
    """
    async with with_a_window(tmp_path, kitty=False) as (pymux, _state, pane, _recorder, log, _probe):
        toplevel = pane.terminal.toplevel
        await once(lambda: pymux.wayland.frame(toplevel), 10.0, "the window never drew")
        await anyio.sleep(0.3)
        assert not red_square_in(pymux.wayland.frame(toplevel))
        await pymux.wayland.drawn_again([toplevel], 10.0)
        assert red_square_in(pymux.wayland.frame(toplevel))
        assert "drew" in logged(log)


async def test_a_window_that_never_stops_drawing_costs_a_capture_its_timeout(tmp_path):
    async with with_a_window(tmp_path, kitty=False, animate=True) as (pymux, _state, pane, _recorder, log, _probe):
        toplevel = pane.terminal.toplevel
        await once(lambda: pymux.wayland.frame(toplevel), 10.0, "the window never drew")
        with anyio.fail_after(5.0):
            await pymux.wayland.drawn_again([toplevel], 0.5)
        assert "frame" in logged(log)


async def test_capture_pane_png_of_a_window_pane_is_the_windows_frame(tmp_path, monkeypatch):
    "Lillecarl/pymux#809."
    log = tmp_path / "probe.log"
    log.touch()
    async with with_a_shell(tmp_path, monkeypatch) as (pymux, session, _window, shell):
        await session.command(
            "wl -- %s %s %s" % (sys.executable, PROBE, log), pane_id=shell.pane_id, environment=dict(os.environ)
        )
        await once(lambda: pymux.wayland.panes, 10.0, "the window never opened")
        (pane,) = pymux.wayland.panes.values()
        await once(lambda: "drew" in logged(log), 10.0, "the window never drew")

        got = await session.command("capture-pane -p --png -t %%%d" % pane.pane_id)
        await once(lambda: answered(got)["exit"] is not None, 10.0, "capture-pane never answered")
        told = answered(got)
        assert (told["err"], told["exit"]) == ("", 0)
        picture = Image.open(io.BytesIO(base64.b64decode(told["out"])))
        width, height, rgba = pymux.wayland.frame(pane.terminal.toplevel)
        assert picture.size == (width, height)
        assert picture.convert("RGBA").tobytes() == rgba
        # The probe's fill, in RGBA: it wrote B, G, R, A.
        assert picture.convert("RGBA").getpixel((30, 20)) == (0x33, 0x66, 0x99, 0xFF)

        got = await session.command("capture-pane -p --png --fit 16 -t %%%d" % pane.pane_id)
        await once(lambda: answered(got)["exit"] is not None, 10.0, "capture-pane --fit never answered")
        assert max(Image.open(io.BytesIO(base64.b64decode(answered(got)["out"]))).size) == 16

        got = await session.command("capture-pane -p -t %%%d" % pane.pane_id)
        await once(lambda: answered(got)["exit"] is not None, 10.0, "capture-pane -p never answered")
        assert "shows a Wayland window" in answered(got)["err"]


@pytest.mark.parametrize("command", ["capture-client", "capture-window"])
async def test_a_picture_of_the_whole_shows_the_windows_frame_in_its_pane(tmp_path, monkeypatch, command):
    "Lillecarl/pymux#814."
    log = tmp_path / "probe.log"
    log.touch()
    async with with_a_shell(tmp_path, monkeypatch) as (pymux, session, _window, shell):
        await session.command(
            "wl -- %s %s %s" % (sys.executable, PROBE, log), pane_id=shell.pane_id, environment=dict(os.environ)
        )
        await once(lambda: pymux.wayland.panes, 10.0, "the window never opened")
        await once(lambda: "drew" in logged(log), 10.0, "the window never drew")
        # capture-client reads the client's last frame, which may not show the window yet.
        await once(
            lambda: any(client.window_panes() for client in pymux.clients),
            10.0,
            "the client never drew the window pane",
        )

        (pane,) = pymux.wayland.panes.values()
        got = await session.command(
            "%s -p --png --font %s --cell 4x8" % (command, shlex.quote(FONT)), pane_id=pane.pane_id
        )
        await once(lambda: answered(got)["exit"] is not None, 10.0, "%s never answered" % command)
        told = answered(got)
        assert (told["err"], told["exit"]) == ("", 0)
        picture = Image.open(io.BytesIO(base64.b64decode(told["out"]))).convert("RGB")
        # The one pane is the frame scaled to its cells. A client's status
        # line may sit above it; a window has none.
        width, height, rgba = pymux.wayland.frame(pane.terminal.toplevel)
        size = (pane.terminal.columns * 4, pane.terminal.rows * 8)
        frame = Image.frombytes("RGBA", (width, height), rgba).resize(size, Image.Resampling.LANCZOS)
        top = 0
        if command == "capture-client":
            (client,) = pymux.clients
            ((_pane, _widget, place),) = client.window_panes()
            top = place.ypos * 8
        assert picture.crop((0, top, size[0], top + size[1])).tobytes() == frame.convert("RGB").tobytes()


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


async def test_wl_says_the_session_is_wayland_and_to_draw_in_software(tmp_path, monkeypatch):
    "Chromium picks X11 without the first, and the compositor has no GPU buffers for the rest."
    show = "wl -- sh -c 'echo $XDG_SESSION_TYPE $LIBGL_ALWAYS_SOFTWARE $QT_QUICK_BACKEND; exit 3'"
    async with with_a_shell(tmp_path, monkeypatch) as (_pymux, session, _window, shell):
        environment = dict(os.environ, XDG_SESSION_TYPE="tty")
        environment.pop("QT_QUICK_BACKEND", None)
        environment.pop("LIBGL_ALWAYS_SOFTWARE", None)
        got = await session.command(show, pane_id=shell.pane_id, environment=environment)
        await once(lambda: answered(got)["exit"] is not None, 10.0, "wl never answered")
        assert answered(got)["out"] == "wayland 1 software\n"

        # What the person chose stays.
        chose = dict(environment, LIBGL_ALWAYS_SOFTWARE="0", QT_QUICK_BACKEND="rhi")
        got = await session.command(show, pane_id=shell.pane_id, environment=chose)
        await once(lambda: answered(got)["exit"] is not None, 10.0, "wl never answered")
        assert answered(got)["out"] == "wayland 0 rhi\n"


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


async def test_a_key_pymux_cannot_name_reaches_the_window(tmp_path):
    "ctrl+=, foot's zoom, as the client's terminal sends it. Lillecarl/pymux#627."
    async with with_a_window(tmp_path) as (_pymux, state, _pane, _recorder, log, _probe):
        await once(lambda: "drew" in logged(log), 10.0, "the window never drew")
        state.connection._process({"cmd": "in", "data": "\x1b[61;5u"})
        await once(lambda: "key 13 0" in logged(log), 10.0, lambda: "ctrl+= never arrived: %s" % logged(log)[-8:])
        lines = logged(log)
        assert lines.index("key 29 1") < lines.index("key 13 1")


async def test_text_no_key_types_reaches_the_window(tmp_path):
    "é and an emoji, which no key of the US keymap types, arrive as text. Lillecarl/pymux#624."
    async with with_a_window(tmp_path, text=True) as (_pymux, state, _pane, _recorder, log, _probe):
        await once(lambda: "drew" in logged(log), 10.0, "the window never drew")
        state.connection._process({"cmd": "in", "data": "é"})
        state.connection._process({"cmd": "in", "data": "😀"})
        await once(lambda: "text 😀" in logged(log), 10.0, lambda: "the text never arrived: %s" % logged(log)[-8:])
        assert [line for line in logged(log) if line.startswith("text")] == ["text é", "text 😀"]


async def test_kill_server_stops_with_a_window_open(tmp_path):
    "A window pane has no reap to wait for. Lillecarl/pymux#679."
    async with with_a_window(tmp_path) as (pymux, _state, _pane, _recorder, _log, _probe):
        with anyio.fail_after(10):
            await pymux.hang_up_and_stop()
        assert pymux.done.is_set()


async def test_a_click_from_the_terminal_reaches_the_window(tmp_path):
    "A mouse report the client's terminal sends, on the window pane's cells."
    async with with_a_window(tmp_path) as (pymux, state, pane, _recorder, log, _probe):
        await once(lambda: "drew" in logged(log), 10.0, "the window never drew")
        pymux.session_defaults.mouse = True
        where = await once(
            lambda: state.layout_manager.pane_write_positions.get(pane), 10.0, "the pane was never drawn"
        )
        # Not the pane's first cell, where every click once landed, and
        # inside the probe's 64x32 buffer, which is all the window
        # takes pointer events on. Lillecarl/pymux#645.
        column, row = 3, 1
        x, y = where.xpos + column, where.ypos + row
        for end in ("M", "m"):
            state.connection._process({"cmd": "in", "data": "\x1b[<0;%d;%d%s" % (x + 1, y + 1, end)})
        await once(
            lambda: "button 272 0" in logged(log),
            10.0,
            lambda: "no click arrived at %s in %s: %s" % ((x, y), where, logged(log)[-8:]),
        )
        assert "button 272 1" in logged(log)
        width, height = pymux.terminals.cell_size()
        motion = [line for line in logged(log) if line.startswith("motion")][-1]
        assert motion == "motion %d %d" % ((column + 0.5) * width, (row + 0.5) * height)


async def test_a_terminal_that_reports_pixels_puts_the_pointer_on_the_pixel(tmp_path):
    "SGR pixel mouse: the cell, and where in it. Lillecarl/pymux#648."
    async with with_a_window(tmp_path, cell=(10, 20)) as (pymux, state, pane, recorder, log, _probe):
        await once(lambda: "drew" in logged(log), 10.0, "the window never drew")
        # kitty, which numbers its first pixel 0.
        state.connection.graphics.terminal_version = "kitty(0.49.2)"
        # What a client sends after it queried its terminal.
        state.connection._process({"cmd": "kitty-detect"})
        await once(lambda: "\x1b[?1016$p" in recorder.out, 10.0, "the terminal was never asked")
        state.connection._process({"cmd": "in", "data": "\x1b[?1016;2$y"})
        await once(lambda: "\x1b[?1016h" in recorder.out, 10.0, "pixel mode was never turned on")
        where = await once(
            lambda: state.layout_manager.pane_write_positions.get(pane), 10.0, "the pane was never drawn"
        )
        # Cell (3, 0) of the pane, 7 and 13 pixels into it: inside the
        # probe's 64x32 buffer, which is all the window takes events on.
        x, y = (where.xpos + 3) * 10 + 7, where.ypos * 20 + 13
        for end in ("M", "m"):
            state.connection._process({"cmd": "in", "data": "\x1b[<0;%d;%d%s" % (x, y, end)})
        await once(lambda: "button 272 0" in logged(log), 10.0, lambda: "no click: %s" % logged(log)[-6:])
        motion = [line for line in logged(log) if line.startswith("motion")][-1]
        width, height = pymux.terminals.cell_size()
        assert motion == "motion %d %d" % (3.75 * width, 0.675 * height)


async def test_a_terminal_that_can_never_report_pixels_keeps_the_mouse_in_cells(tmp_path):
    "iTerm2 3.7 answers 1016 with 4, permanently reset: it knows the mode and never does it."
    async with with_a_window(tmp_path, cell=(10, 20)) as (_pymux, state, _pane, _recorder, log, _probe):
        await once(lambda: "drew" in logged(log), 10.0, "the window never drew")
        state.connection.graphics.terminal_version = "iTerm2 3.7.6"
        state.connection._process({"cmd": "kitty-detect"})
        state.connection._process({"cmd": "in", "data": "\x1b[?1016;4$y"})
        state.connection._process({"cmd": "in", "data": "\x1b[0n"})
        assert not state.connection.pixel_mouse


async def test_a_terminal_that_keeps_its_cell_size_keeps_the_mouse_in_cells(tmp_path):
    "xterm answers 1016 and not the cell size query: a pixel would be read as a cell. Lillecarl/pymux#651."
    async with with_a_window(tmp_path) as (pymux, state, pane, _recorder, log, _probe):
        await once(lambda: "drew" in logged(log), 10.0, "the window never drew")
        pymux.session_defaults.mouse = True
        state.connection._process({"cmd": "kitty-detect"})
        state.connection._process({"cmd": "in", "data": "\x1b[?1016;2$y"})
        where = await once(
            lambda: state.layout_manager.pane_write_positions.get(pane), 10.0, "the pane was never drawn"
        )
        # A cell report: read as cells, the click lands on cell (3, 1)
        # of the pane. Read as pixels, it would land on the pane's first.
        x, y = where.xpos + 3, where.ypos + 1
        for end in ("M", "m"):
            state.connection._process({"cmd": "in", "data": "\x1b[<0;%d;%d%s" % (x + 1, y + 1, end)})
        await once(lambda: "button 272 0" in logged(log), 10.0, lambda: "no click: %s" % logged(log)[-6:])
        assert not state.connection.pixel_mouse
        width, height = pymux.terminals.cell_size()
        motion = [line for line in logged(log) if line.startswith("motion")][-1]
        assert motion == "motion %d %d" % (3.5 * width, 1.5 * height)


async def test_the_terminal_shows_the_pointer_the_window_asks_for(tmp_path):
    "Lillecarl/pymux#650."
    async with with_a_window(tmp_path) as (pymux, state, pane, recorder, log, _probe):
        await once(lambda: "drew" in logged(log), 10.0, "the window never drew")
        pymux.session_defaults.mouse = True
        where = await once(
            lambda: state.layout_manager.pane_write_positions.get(pane), 10.0, "the pane was never drawn"
        )
        state.connection._process({"cmd": "in", "data": "\x1b[<35;%d;%dM" % (where.xpos + 4, where.ypos + 2)})
        await once(lambda: "\x1b]22;text\x1b\\" in recorder.out, 10.0, "the pointer never changed")
        assert pane.screen.pointer_shapes.shape == "text"


@pytest.mark.parametrize("scroll", list(WindowScroll))
async def test_a_scroll_reaches_the_window_as_the_client_says(tmp_path, scroll):
    "One cell of pixels for `smooth`, one wheel notch for `notch`. Lillecarl/pymux#652."
    async with with_a_window(tmp_path) as (pymux, state, pane, _recorder, log, _probe):
        await once(lambda: "drew" in logged(log), 10.0, "the window never drew")
        pymux.session_defaults.mouse = True
        state.options.window_scroll = scroll
        where = await once(
            lambda: state.layout_manager.pane_write_positions.get(pane), 10.0, "the pane was never drawn"
        )
        state.connection._process({"cmd": "in", "data": "\x1b[<65;%d;%dM" % (where.xpos + 4, where.ypos + 2)})
        line = await once(
            lambda: next((one for one in logged(log) if one.startswith("axis")), None), 10.0, "no scroll arrived"
        )
        _width, height = pymux.terminals.cell_size()
        # 0 is `wl_pointer.axis.vertical_scroll`; a notch is 15 units.
        assert line == "axis 0 %d" % (height if scroll == WindowScroll.SMOOTH else 15)


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
