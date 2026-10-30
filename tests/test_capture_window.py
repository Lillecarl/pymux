"""
`capture-window` draws a window as its layout lays it out, with no
client: every pane at its place and the border lines between them. An
agent working in a session of its own reads its work this way.
Lillecarl/pymux#818.
"""

from __future__ import annotations

import base64
import io
import sys

from PIL import Image
from session import once
from test_capture_client import pictures  # noqa: F401
from test_capture_pane_as_png import FALLBACK, FONTS, pytestmark  # noqa: F401

from pymux.answer import Answer
from pymux.commands import call_command_handler
from pymux.main import Pymux

CELL = (9, 18)
SAYS = "%s -c \"import time; print('%%s', flush=True); time.sleep(60)\"" % (sys.executable,)
FONT_FLAGS = [word for path in FONTS for word in ("--font", path)] + ["--fallback", FALLBACK]


async def run(mux, command: str, *arguments) -> tuple[str, list[str]]:
    "What the command printed, and the errors it gave."
    errors: list[str] = []
    mux.add_command_error = errors.append
    mux.show_message = lambda _message: None
    mux.answer = Answer()
    try:
        answer = call_command_handler(command, mux, list(arguments))
        if answer is not None:
            await answer
        return "\n".join(mux.answer.output), errors
    finally:
        mux.answer = None


async def a_split_window(mux):
    "A detached session whose window holds two panes side by side, each saying a word."
    await run(mux, "new-session", "-d", "-s", "work", "-x", "60", "-y", "10", SAYS % "LEFT")
    (session,) = [one for one in mux.sessions if one.name == "work"]
    window = session.arrangement.windows[0]
    await mux.add_process(SAYS % "RIGHT", vsplit=True, window=window)
    for pane in window.panes:
        await once(lambda pane=pane: _first_row(pane).strip(), 10.0, "a pane never spoke")
    return window


def _first_row(pane) -> str:
    screen = pane.screen
    return screen.page.text(screen.line_offset, screen.line_offset)


def kill_all(mux) -> None:
    for pane in list(mux.panes_by_id.values()):
        if not pane.process.is_terminated:
            pane.process.kill()


async def test_the_text_holds_both_panes_and_the_line_between():
    mux = Pymux()
    try:
        async with mux.running():
            window = await a_split_window(mux)
            printed, errors = await run(mux, "capture-window", "-p", "-t", "@%d" % window.window_id)
    finally:
        kill_all(mux)
    assert not errors, errors
    first = printed.split("\n")[0]
    assert "LEFT" in first and "RIGHT" in first, printed
    assert first.index("LEFT") < first.index("│") < first.index("RIGHT"), printed


async def test_the_picture_is_the_window_at_the_cell_it_was_given():
    mux = Pymux()
    try:
        async with mux.running():
            window = await a_split_window(mux)
            printed, errors = await run(
                mux,
                "capture-window",
                "-p",
                "--png",
                *FONT_FLAGS,
                "--cell",
                "%dx%d" % CELL,
                "-t",
                "@%d" % window.window_id,
            )
            plane = mux.plane_size(window)
    finally:
        kill_all(mux)
    assert not errors, errors
    image = Image.open(io.BytesIO(base64.b64decode(printed)))
    assert image.width == plane.columns * CELL[0]
    assert image.height % CELL[1] == 0


async def test_o_saves_the_window_in_the_pictures_directory(pictures):
    mux = Pymux()
    try:
        async with mux.running():
            window = await a_split_window(mux)
            printed, errors = await run(
                mux,
                "capture-window",
                "-o",
                "work.png",
                *FONT_FLAGS,
                "--cell",
                "%dx%d" % CELL,
                "-t",
                "@%d" % window.window_id,
            )
    finally:
        kill_all(mux)
    assert not errors, errors
    assert printed == str(pictures / "work.png")
    assert (pictures / "work.png").read_bytes().startswith(b"\x89PNG")


async def test_without_print_it_says_so():
    mux = Pymux()
    async with mux.running():
        _printed, errors = await run(mux, "capture-window")
    assert errors and "needs -p" in errors[0], errors
