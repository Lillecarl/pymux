"""
`capture-client` reads back what a client is shown: the whole of
pymux, status line, borders, every pane and what is drawn over them.

The client here is a real one, rendering into a pyte screen, so the
frame it holds is the one a terminal would have been sent.
"""

from __future__ import annotations

import base64
import io

import pytest
from PIL import Image
from prompt_toolkit.application.current import set_app
from test_a_session_on_a_pyte_screen import COLUMNS, ROWS, attached, pymux, settled, shows  # noqa: F401
from test_capture_pane_as_png import FALLBACK, FONTS, KITTY_RED, RED, pytestmark  # noqa: F401

from pymux.answer import Answer
from pymux.commands import call_command_handler

CELL = (9, 18)


async def run(mux, *arguments) -> tuple[str, list[str]]:
    "What capture-client printed, and the errors it gave."
    errors: list[str] = []
    mux.add_command_error = errors.append
    mux.show_message = lambda _message: None
    mux.answer = Answer()
    try:
        answer = call_command_handler("capture-client", mux, ["-p", *arguments])
        if answer is not None:
            await answer
        return "\n".join(mux.answer.output), errors
    finally:
        mux.answer = None


async def picture(mux, *arguments) -> Image.Image:
    fonts = [word for path in FONTS for word in ("--font", path)]
    printed, errors = await run(mux, "--png", *fonts, "--fallback", FALLBACK, "--cell", "%dx%d" % CELL, *arguments)
    assert not errors, errors
    return Image.open(io.BytesIO(base64.b64decode(printed))).convert("RGB")


def _colours_of_row(image: Image.Image, row: int) -> set:
    width, height = CELL
    box = image.crop((0, row * height, image.width, (row + 1) * height))
    return {color for _count, color in box.getcolors(box.width * box.height)}


async def test_the_text_is_the_whole_screen_the_client_is_shown(pymux):
    async with pymux.running(), attached(pymux) as session:
        await shows(session, "$")
        printed, errors = await run(pymux)
    assert not errors, errors
    lines = printed.split("\n")
    assert len(lines) == ROWS, lines
    assert any(line.endswith("$") for line in lines), lines
    # The status line is pymux's own, beside the pane: it names the window.
    assert "bash" in lines[0] + lines[-1], lines


async def test_the_picture_is_the_client_at_the_cell_it_was_given(pymux):
    async with pymux.running(), attached(pymux) as session:
        await shows(session, "$")
        image = await picture(pymux)
    assert image.size == (COLUMNS * CELL[0], ROWS * CELL[1])
    # The status line is painted, where an empty pane row in the middle is not.
    assert len(_colours_of_row(image, 0) | _colours_of_row(image, ROWS - 1)) > 2
    assert len(_colours_of_row(image, ROWS // 2)) == 1


async def test_a_panes_image_is_where_the_client_shows_it(pymux):
    async with pymux.running(), attached(pymux) as session:
        await shows(session, "$")
        (pane,) = pymux.panes_by_id.values()
        pane.terminal.terminal_control.stream.feed("\x1b[H\x1b[2J\x1b[?25l" + KITTY_RED % "")
        await settled(session)
        image = await picture(pymux)
    width, height = CELL
    corner = image.crop((0, 0, width, height))
    assert {color for _count, color in corner.getcolors(width * height)} == {RED}


async def test_a_capture_from_the_command_line_shows_it_closed(pymux):
    """
    The key that runs a command also closes the command line, and the
    frame without it comes after: the capture waits for that frame.
    Lillecarl/pymux#820.
    """
    async with pymux.running(), attached(pymux) as session:
        await shows(session, "$")
        (client,) = pymux.clients
        typed = "capture-client -p"
        with set_app(client.app):
            call_command_handler("command-prompt", pymux, [])
        client.command_buffer.text = typed
        await shows(session, typed)
        printed, errors = await run(pymux)
        assert not errors, errors
        assert typed in printed, "the open command line is not in the capture"

        pymux.answer = Answer()
        try:
            with set_app(client.app):
                answer = call_command_handler("capture-client", pymux, ["-p"])
                pymux.leave_command_mode()
            assert answer is not None
            await answer
            printed = "\n".join(pymux.answer.output)
        finally:
            pymux.answer = None
    assert typed not in printed, printed


async def test_an_image_goes_under_a_menu_drawn_over_it(pymux):
    """
    A red image over the whole pane, and a menu over that: the cells of
    the menu show the menu, and the image keeps the cells around it.
    Lillecarl/pymux#816.
    """
    async with pymux.running(), attached(pymux) as session:
        await shows(session, "$")
        (pane,) = pymux.panes_by_id.values()
        whole = "\x1b_Ga=T,f=24,s=2,v=2,c=%d,r=%d;" % (COLUMNS, ROWS) + base64.b64encode(bytes(RED) * 4).decode()
        pane.terminal.terminal_control.stream.feed("\x1b[H\x1b[2J\x1b[?25l" + whole + "\x1b\\")
        await settled(session)
        (client,) = pymux.clients
        # The client that asks is the one the menu shows on.
        with set_app(client.app):
            call_command_handler("display-menu", pymux, ["-T", "Do", "again", "a", "display-message again"])
        client.app.invalidate()
        await settled(session)

        covers = client.layout_manager.pane_covers[pane]
        assert covers, "the menu covers nothing of the pane"
        at = client.layout_manager.pane_write_positions[pane]
        image = await picture(pymux)

    width, height = CELL
    under = covers[0]
    beside = next(
        (x, y)
        for y in range(at.ypos, at.ypos + at.height)
        for x in range(at.xpos, at.xpos + at.width)
        if not any(cover.x <= x < cover.right and cover.y <= y < cover.bottom for cover in covers)
    )

    def colours(x, y):
        cell = image.crop((x * width, y * height, (x + 1) * width, (y + 1) * height))
        return {color for _count, color in cell.getcolors(width * height)}

    assert colours(*beside) == {RED}
    assert colours(under.x, under.y) != {RED}


async def test_a_client_nobody_has_is_refused(pymux):
    async with pymux.running(), attached(pymux) as session:
        await shows(session, "$")
        _printed, errors = await run(pymux, "-t", "nobody")
    assert errors == ["pymux: can't find client: nobody"], errors


async def test_without_print_it_says_so(pymux):
    async with pymux.running(), attached(pymux) as session:
        await shows(session, "$")
        errors: list[str] = []
        pymux.add_command_error = errors.append
        pymux.show_message = lambda _message: None
        call_command_handler("capture-client", pymux, [])
    assert errors and "needs -p" in errors[0], errors


@pytest.fixture
def pictures(tmp_path, monkeypatch):
    "A pictures directory that xdg-user-dirs names, as a desktop sets it up."
    shots = tmp_path / "shots"
    (tmp_path / "user-dirs.dirs").write_text('XDG_PICTURES_DIR="%s"\n' % (shots,))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    return shots


async def test_o_saves_a_new_picture_in_the_pictures_directory(pymux, pictures):
    "Lillecarl/pymux#813."
    fonts = [word for path in FONTS for word in ("--font", path)]
    async with pymux.running(), attached(pymux) as session:
        await shows(session, "$")
        printed, errors = await run(pymux, "-o", *fonts, "--cell", "%dx%d" % CELL)
    assert not errors, errors
    (saved,) = pictures.iterdir()
    assert printed == str(saved)
    assert saved.name.startswith("pymux-") and saved.suffix == ".png"
    assert Image.open(saved).size == (COLUMNS * CELL[0], ROWS * CELL[1])


async def test_o_takes_a_name_in_the_pictures_directory(pymux, pictures):
    fonts = [word for path in FONTS for word in ("--font", path)]
    async with pymux.running(), attached(pymux) as session:
        await shows(session, "$")
        printed, errors = await run(pymux, "-o", "desk.png", *fonts, "--cell", "%dx%d" % CELL)
    assert not errors, errors
    assert printed == str(pictures / "desk.png")
    assert (pictures / "desk.png").read_bytes().startswith(b"\x89PNG")
