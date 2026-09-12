"""
What the program in the pane paints (`tests/demo_application.py`).

It asks its pane for its colours the way a program does, and paints
with what it is told. The pane answers what it holds, which is the
theme's palette when the theme owns the screen -- so the pictures of
a theme show the theme's colours inside the pane too.
"""

from demo_application import CONVENTIONAL, Painter, parse_replies, ASKS


def test_the_asks_name_the_sixteen_and_the_two_defaults():
    assert ASKS.count("\x1b]4;") == 16
    assert ASKS.endswith("\x1b]10;?\x1b\\\x1b]11;?\x1b\\")


def test_a_palette_reply_is_read_by_index():
    colours = parse_replies(b"\x1b]4;1;rgb:ffff/0000/0000\x1b\\")
    assert colours == {1: (0xFF, 0x00, 0x00)}


def test_the_default_replies_are_read_by_name():
    colours = parse_replies(
        b"\x1b]10;#aabbcc\x1b\\\x1b]11;rgb:1111/2222/3333\x1b\\"
    )
    assert colours == {"foreground": (0xAA, 0xBB, 0xCC), "background": (0x11, 0x22, 0x33)}


def test_a_reply_split_across_reads_is_read_when_it_is_whole():
    first, second = b"\x1b]4;2;rgb:00", b"00/ffff/0000\x1b\\"
    assert parse_replies(first) == {}
    assert parse_replies(first + second) == {2: (0x00, 0xFF, 0x00)}


def test_the_replies_of_other_things_are_ignored():
    garbage = (
        b"typed keys \r\n"
        b"\x1b]4;20;rgb:ffff/0000/0000\x07"  # Beyond the sixteen.
        b"\x1b]4;aubergine;rgb:ffff/0000/0000\x07"  # No index.
        b"\x1b]4;3;aubergine\x07"  # No colour.
        b"\x1b]52;c;\x07"  # Another code entirely.
        b"\x1b]4;4;rgb:5c5c/5c5c/ffff\x1b\\"  # The one that answers.
    )
    assert parse_replies(garbage) == {4: (0x5C, 0x5C, 0xFF)}


def test_the_painter_paints_what_it_was_told():
    painter = Painter({1: (0xFF, 0x00, 0x00)})

    assert painter.fg(1) == "\x1b[38;2;255;0;0m"
    assert painter.bg(1) == "\x1b[48;2;255;0;0m"


def test_the_painter_falls_back_to_the_convention():
    painter = Painter({})

    assert painter.fg(2) == "\x1b[38;2;0;205;0m"
    assert painter.rgb(2) == (0x00, 0xCD, 0x00)
    assert len(CONVENTIONAL) == 16
