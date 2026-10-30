"""
Tests for drawing the unicode placeholders of a pane.

A program writes one character per cell instead of asking for a
placement. The client reads those cells back and draws the matching
piece of the image on the outer terminal.

Each test builds a real `Screen`, feeds it what such a program
sends, and checks the escape sequences that reach the terminal.
"""

from __future__ import annotations

import base64
import re

from pyte import escape
from pyte.placeholders import DIACRITICS, PLACEHOLDER
from pyte.screen import Screen
from pyte.sequences import csi
from pyte.streams import Stream

from pymux.graphics import ClientGraphics, PaneView


def make_client():
    written = []
    client = ClientGraphics(written.append, lambda: None)
    client.kitty_supported = True
    return client, written


def make_screen(lines=24, columns=80):
    screen = Screen(lines, columns, write_process_input=lambda data: None)
    stream = Stream(screen)
    return screen, stream


def rgb_image(width, height):
    return bytes([(x * 7) % 256 for x in range(width * height * 3)])


def transmit_virtual(stream, image_id=5, width=40, height=40):
    "What a program sends before it writes the placeholder cells."
    data = base64.b64encode(rgb_image(width, height)).decode()
    stream.feed("\x1b_Ga=T,U=1,f=24,s=%i,v=%i,i=%i;%s\x1b\\" % (width, height, image_id, data))


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
    return text + csi(escape.SGR, 0)


def view(screen, **kw):
    settings = {
        "pane_id": 1,
        "x": 0,
        "y": 0,
        "width": 80,
        "height": 24,
        "vertical_scroll": 0,
        "horizontal_scroll": 0,
    }
    settings.update(kw)
    return PaneView(graphics=screen.graphics, screen=screen, **settings)


def puts(written):
    "The (row, column, keys) of every put command."
    found = re.findall(r"\x1b\[(\d+);(\d+)H\x1b_Ga=p,([^\x1b]*)\x1b\\", "".join(written))
    return [(int(row), int(column), keys) for row, column, keys in found]


def keys_of(put):
    return dict(part.split("=", 1) for part in put[2].split(",") if "=" in part)


# ----------------------------------------------------------------------


def test_screen_of_placeholders_draws_image():
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


def test_image_is_transmitted_once_for_every_run():
    client, written = make_client()
    screen, stream = make_screen()
    transmit_virtual(stream)
    stream.feed(cells(5, 0, 4) + "\r\n" + "text" + cells(5, 1, 4))

    client.render([view(screen)])

    assert len(re.findall(r"\x1b_Ga=t,", "".join(written))) == 1
    assert len(puts(written)) == 2  # The text broke the run in two.


def test_one_line_of_image_crops_that_line():
    client, written = make_client()
    screen, stream = make_screen()
    transmit_virtual(stream)
    stream.feed(cells(5, 1, 4))  # The lower half only.

    client.render([view(screen)])

    keys = keys_of(puts(written)[0])
    assert (keys["x"], keys["y"], keys["w"], keys["h"]) == ("0", "20", "40", "20")


def test_part_of_line_crops_sideways():
    client, written = make_client()
    screen, stream = make_screen()
    transmit_virtual(stream)
    stream.feed(cells(5, 0, 2))  # The left half of the top row.

    client.render([view(screen)])

    keys = keys_of(puts(written)[0])
    assert (keys["x"], keys["y"], keys["w"], keys["h"]) == ("0", "0", "20", "20")


def test_scroll_of_pane_moves_image():
    client, written = make_client()
    screen, stream = make_screen()
    transmit_virtual(stream)
    stream.feed("\r\n\r\n" + cells(5, 0, 4))  # On the third row.

    client.render([view(screen, y=0, vertical_scroll=1)])

    row, column, _keys = puts(written)[0]
    assert (row, column) == (2, 1)  # One row scrolled away.


def test_run_scrolled_out_of_pane_is_dropped():
    client, written = make_client()
    screen, stream = make_screen()
    transmit_virtual(stream)
    stream.feed(cells(5, 0, 4) + "\r\n\r\n")

    client.render([view(screen, vertical_scroll=5)])

    assert puts(written) == []


def test_run_past_right_edge_is_cut():
    client, written = make_client()
    screen, stream = make_screen(columns=6)
    transmit_virtual(stream)
    stream.feed(cells(5, 0, 4))

    client.render([view(screen, width=2)])

    commands = puts(written)
    keys = keys_of(commands[0])
    assert keys["c"] == "2"
    assert (keys["x"], keys["w"]) == ("0", "20")


def test_pane_without_placeholders_draws_nothing():
    client, written = make_client()
    screen, stream = make_screen()
    stream.feed("plain text")

    client.render([view(screen)])

    assert puts(written) == []


def test_placeholder_of_unknown_image_draws_nothing():
    client, written = make_client()
    screen, stream = make_screen()
    transmit_virtual(stream, image_id=5)
    stream.feed(cells(9, 0, 4))  # Another image id.

    client.render([view(screen)])

    assert puts(written) == []


def test_placeholders_do_not_reach_terminal_as_text():
    "The character stands for a picture, and must not be drawn."
    from ptterm.style import visible_char

    screen, stream = make_screen()
    transmit_virtual(stream)
    stream.feed(cells(5, 0, 4))
    line = screen.page.data_buffer[0]
    assert line[0].char.startswith(PLACEHOLDER)
    assert visible_char(line[0].char) == " "


# ----------------------------------------------------------------------
# A terminal that reads the placeholders itself. Lillecarl/pymux#600.


def make_kitty(image_ids=None):
    written = []
    client = ClientGraphics(written.append, lambda: None, image_ids=image_ids, reads_placeholders=lambda: True)
    client.kitty_supported = True
    return client, written


def test_a_terminal_that_reads_placeholders_gets_the_box_and_no_put():
    "It fits the image to the cells the text carries, so pymux puts nothing."
    client, written = make_kitty()
    screen, stream = make_screen()
    stream.feed("\x1b_Ga=T,U=1,f=24,s=40,v=40,i=5,c=7,r=3;%s\x1b\\" % base64.b64encode(rgb_image(40, 40)).decode())
    stream.feed(cells(5, 0, 4) + "\r\n" + cells(5, 1, 4))

    client.render([view(screen, x=10, y=5)])

    assert puts(written) == []
    virtual = re.findall(r"\x1b_Ga=p,U=1,([^\x1b]*)\x1b\\", "".join(written))
    assert len(virtual) == 1
    keys = dict(part.split("=", 1) for part in virtual[0].split(","))
    assert (keys["c"], keys["r"]) == ("7", "3")
    sent = re.search(r"\x1b_Ga=t,i=(\d+),", "".join(written))
    assert sent is not None and keys["i"] == sent.group(1)


def test_a_terminal_that_takes_fewer_colours_gets_the_puts():
    "The id travels in the colour, and 256 colours would change it."
    from prompt_toolkit.output import ColorDepth

    written = []
    client = ClientGraphics(
        written.append,
        lambda: None,
        color_depth=lambda: ColorDepth.DEPTH_8_BIT,
        reads_placeholders=lambda: True,
    )
    client.kitty_supported = True
    screen, stream = make_screen()
    transmit_virtual(stream)
    stream.feed(cells(5, 0, 4))

    client.render([view(screen)])

    assert len(puts(written)) == 1


def test_every_client_knows_an_image_by_one_id():
    "A pane builds the text of its image cells once for all of them."
    from pymux.graphics import OuterImageIds

    ids = OuterImageIds()
    sent = []
    for _ in range(2):
        client, written = make_kitty(ids)
        screen, stream = make_screen()
        transmit_virtual(stream)
        stream.feed(cells(5, 0, 4))
        client.render([view(screen)])
        sent.append(re.search(r"\x1b_Ga=t,i=(\d+),", "".join(written)).group(1))

    assert sent[0] == sent[1] == str(ids.id_of(1, 5))
    assert ids.id_of(1, 5) < 1 << 24
    assert ids.id_of(2, 5) != ids.id_of(1, 5)


def test_a_replaced_image_is_put_again_under_its_id():
    "The delete took its placements, and the id stays the same."
    client, written = make_kitty()
    screen, stream = make_screen()
    transmit_virtual(stream)
    stream.feed(cells(5, 0, 4))
    client.render([view(screen)])
    # New pixels, which take the placements with them, and the same box.
    data = base64.b64encode(rgb_image(20, 20)).decode()
    stream.feed("\x1b_Ga=t,f=24,s=20,v=20,i=5;%s\x1b\\" % data)
    stream.feed("\x1b_Ga=p,U=1,i=5,c=4,r=2\x1b\\")
    written.clear()

    client.render([view(screen)])

    assert len(re.findall(r"\x1b_Ga=p,U=1,", "".join(written))) == 1


def test_a_frame_for_a_terminal_that_draws_no_placeholder_has_spaces():
    import io

    from prompt_toolkit.data_structures import Size

    from pymux.client_io import FrameOutput

    for keeps in (False, True):
        stdout = io.StringIO()
        output = FrameOutput(stdout, lambda: Size(24, 80), term="xterm")
        output.keeps_placeholders = lambda keeps=keeps: keeps
        output.write("a" + PLACEHOLDER + mark(0) + mark(3) + "b")
        output.flush()
        expected = "a" + PLACEHOLDER + mark(0) + mark(3) + "b" if keeps else "a b"
        assert stdout.getvalue() == expected


def test_two_clients_of_one_pane_each_get_their_own_way():
    "kitty reads the placeholders, and foot gets the image as a sixel."
    from pymux.graphics import OuterImageIds

    ids = OuterImageIds()
    kitty, kitty_wrote = make_kitty(ids)
    foot_wrote = []
    foot = ClientGraphics(foot_wrote.append, lambda: None, image_ids=ids, reads_placeholders=lambda: False)
    foot.sixel_supported = True
    screen, stream = make_screen()
    transmit_virtual(stream)
    stream.feed(cells(5, 0, 4))

    for client in (kitty, foot):
        client.render([view(screen)])

    assert re.findall(r"\x1b_Ga=p,U=1,", "".join(kitty_wrote)) and not puts(kitty_wrote)
    assert "\x1bP" in "".join(foot_wrote) and "\x1b_G" not in "".join(foot_wrote)


# ----------------------------------------------------------------------
# Which terminals read them. Lillecarl/pymux#607.


def test_the_terminal_names_itself_in_the_detection():
    from pymux.client.terminal import DETECTION_QUERIES
    from pymux.graphics import VERSION_QUERY

    queries = DETECTION_QUERIES.decode("ascii")
    assert VERSION_QUERY == "\x1b[>0q"
    # Before the device attributes, which end the detection.
    assert queries.index(VERSION_QUERY) < queries.index("\x1b[c")


def test_the_version_reply_is_kept():
    client, _written = make_client()
    client.handle_reply("\x1bP>|ghostty 1.3.1\x1b\\")
    assert client.terminal_version == "ghostty 1.3.1"


def test_view_without_screen_still_renders_plain_placements():
    "An embedder that gives no screen loses nothing else."
    from pyte.images import GraphicsPlacement

    client, written = make_client()
    screen, stream = make_screen()
    transmit_virtual(stream)
    screen.graphics.placements.append(GraphicsPlacement(5, 0, 0, 0, 4, 2, 0, False))

    client.render(
        [PaneView(1, 0, 0, 80, 24, 0, 0, screen.graphics)]  # No screen.
    )

    assert len(puts(written)) == 1
