"""
Tests for drawing the unicode placeholders of a pane.

A program writes one character per cell instead of asking for a
placement. The client reads those cells back and draws the matching
piece of the image on the outer terminal.

Each test builds a real `BetterScreen`, feeds it what such a program
sends, and checks the escape sequences that reach the terminal.
"""
import base64
import re

from ptterm.placeholders import DIACRITICS, PLACEHOLDER
from ptterm.screen import BetterScreen
from ptterm.stream import BetterStream

from pymux.graphics import ClientGraphics, PaneView


def make_client():
    written = []
    client = ClientGraphics(written.append, lambda: None)
    client.kitty_supported = True
    return client, written


def make_screen(lines=24, columns=80):
    screen = BetterScreen(lines, columns, write_process_input=lambda data: None)
    stream = BetterStream(screen)
    stream.attach(screen)
    return screen, stream


def rgb_image(width, height):
    return bytes([(x * 7) % 256 for x in range(width * height * 3)])


def transmit_virtual(stream, image_id=5, width=40, height=40):
    "What a program sends before it writes the placeholder cells."
    data = base64.b64encode(rgb_image(width, height)).decode()
    stream.feed(
        "\x1b_Ga=T,U=1,f=24,s=%i,v=%i,i=%i;%s\x1b\\"
        % (width, height, image_id, data)
    )


def mark(number):
    return chr(DIACRITICS[number])


def cells(image_id, row, columns):
    "One line of placeholder cells, in the colour that names the image."
    red, green, blue = (
        (image_id >> 16) & 0xFF,
        (image_id >> 8) & 0xFF,
        image_id & 0xFF,
    )
    text = "\x1b[38;2;%i;%i;%im" % (red, green, blue)
    for column in range(columns):
        text += PLACEHOLDER + mark(row) + mark(column)
    return text + "\x1b[0m"


def view(screen, **kw):
    settings = dict(
        pane_id=1,
        x=0,
        y=0,
        width=80,
        height=24,
        vertical_scroll=0,
        horizontal_scroll=0,
    )
    settings.update(kw)
    return PaneView(graphics=screen.graphics, screen=screen, **settings)


def puts(written):
    "The (row, column, keys) of every put command."
    found = re.findall(
        r"\x1b\[(\d+);(\d+)H\x1b_Ga=p,([^\x1b]*)\x1b\\", "".join(written)
    )
    return [(int(row), int(column), keys) for row, column, keys in found]


def keys_of(put):
    return dict(
        part.split("=", 1) for part in put[2].split(",") if "=" in part
    )


# ----------------------------------------------------------------------


def test_a_screen_of_placeholders_draws_the_image():
    client, written = make_client()
    screen, stream = make_screen()
    transmit_virtual(stream)  # 40x40 pixels: a box of 4 by 2 cells.
    stream.feed(cells(5, 0, 4) + "\r\n" + cells(5, 1, 4))

    client.render([view(screen, x=10, y=5)])

    commands = puts(written)
    assert len(commands) == 1
    row, column, _keys = commands[0]
    assert (row, column) == (6, 11)  # The pane corner, counting from one.
    keys = keys_of(commands[0])
    assert (keys["c"], keys["r"]) == ("4", "2")
    # The whole image: the box is its natural size.
    assert (keys["x"], keys["y"], keys["w"], keys["h"]) == ("0", "0", "40", "40")


def test_the_image_is_transmitted_once_for_every_run():
    client, written = make_client()
    screen, stream = make_screen()
    transmit_virtual(stream)
    stream.feed(cells(5, 0, 4) + "\r\n" + "text" + cells(5, 1, 4))

    client.render([view(screen)])

    assert len(re.findall(r"\x1b_Ga=t,", "".join(written))) == 1
    assert len(puts(written)) == 2  # The text broke the run in two.


def test_one_line_of_the_image_crops_that_line():
    client, written = make_client()
    screen, stream = make_screen()
    transmit_virtual(stream)
    stream.feed(cells(5, 1, 4))  # The lower half only.

    client.render([view(screen)])

    keys = keys_of(puts(written)[0])
    assert (keys["x"], keys["y"], keys["w"], keys["h"]) == ("0", "20", "40", "20")


def test_a_part_of_a_line_crops_sideways():
    client, written = make_client()
    screen, stream = make_screen()
    transmit_virtual(stream)
    stream.feed(cells(5, 0, 2))  # The left half of the top row.

    client.render([view(screen)])

    keys = keys_of(puts(written)[0])
    assert (keys["x"], keys["y"], keys["w"], keys["h"]) == ("0", "0", "20", "20")


def test_the_scroll_of_the_pane_moves_the_image():
    client, written = make_client()
    screen, stream = make_screen()
    transmit_virtual(stream)
    stream.feed("\r\n\r\n" + cells(5, 0, 4))  # On the third row.

    client.render([view(screen, y=0, vertical_scroll=1)])

    row, column, _keys = puts(written)[0]
    assert (row, column) == (2, 1)  # One row scrolled away.


def test_a_run_scrolled_out_of_the_pane_is_dropped():
    client, written = make_client()
    screen, stream = make_screen()
    transmit_virtual(stream)
    stream.feed(cells(5, 0, 4) + "\r\n\r\n")

    client.render([view(screen, vertical_scroll=5)])

    assert puts(written) == []


def test_a_run_past_the_right_edge_is_cut():
    client, written = make_client()
    screen, stream = make_screen(columns=6)
    transmit_virtual(stream)
    stream.feed(cells(5, 0, 4))

    client.render([view(screen, width=2)])

    commands = puts(written)
    keys = keys_of(commands[0])
    assert keys["c"] == "2"
    assert (keys["x"], keys["w"]) == ("0", "20")


def test_a_pane_without_placeholders_draws_nothing():
    client, written = make_client()
    screen, stream = make_screen()
    stream.feed("plain text")

    client.render([view(screen)])

    assert puts(written) == []


def test_a_placeholder_of_an_unknown_image_draws_nothing():
    client, written = make_client()
    screen, stream = make_screen()
    transmit_virtual(stream, image_id=5)
    stream.feed(cells(9, 0, 4))  # Another image id.

    client.render([view(screen)])

    assert puts(written) == []


def test_the_placeholders_do_not_reach_the_terminal_as_text():
    "The character stands for a picture, and must not be drawn."
    from ptterm.terminal import _visible_char

    screen, stream = make_screen()
    transmit_virtual(stream)
    stream.feed(cells(5, 0, 4))
    line = screen.pt_screen.data_buffer[0]
    assert line[0].char.startswith(PLACEHOLDER)
    assert _visible_char(line[0].char) == " "


def test_a_view_without_a_screen_still_renders_the_plain_placements():
    "An embedder that gives no screen loses nothing else."
    from ptterm.graphics import GraphicsPlacement

    client, written = make_client()
    screen, stream = make_screen()
    transmit_virtual(stream)
    screen.graphics.placements.append(
        GraphicsPlacement(5, 0, 0, 0, 4, 2, 0, False)
    )

    client.render(
        [PaneView(1, 0, 0, 80, 24, 0, 0, screen.graphics)]  # No screen.
    )

    assert len(puts(written)) == 1
