"""
The frames a viewer of a pane is sent, and what it may send back.

**The diff is the whole point**, so most of this asks what a frame does
*not* hold. A screen of 200x50 is forty kilobytes of markup and a viewer
wants five a second; sending the screen is the thing being avoided.

`pymux.web.protocol` does no I/O and holds no socket, so none of this
needs a server, a websocket or a pty. That is the reason the protocol is
a module of its own: the transport carries frames and decides nothing,
and it is the deciding that is worth testing. Lillecarl/pymux#461.
"""

from __future__ import annotations

import pytest
from pyte.screen import Screen
from pyte.streams import Stream

from pymux.web.protocol import FRAME, PLAIN_STYLE, WELCOME, PaneView, typed_of

LINES = 5
COLUMNS = 20


def a_screen(lines: int = LINES, columns: int = COLUMNS) -> Screen:
    return Screen(lines, columns, write_process_input=lambda _data: None)


def feed(screen, text: str) -> None:
    Stream(screen).feed(text)


def drawn(frame) -> dict:
    "The text of each row a frame holds, by row number."
    return {number: "".join(text for _style, text in runs) for number, runs in frame["rows"].items()}


# ----------------------------------------------------------------------
# The first frame.


def test_the_first_frame_holds_every_row_that_draws():
    screen = a_screen()
    feed(screen, "one\r\ntwo")
    view = PaneView()

    frame = view.frame(screen, 1)

    assert frame["type"] == FRAME
    assert drawn(frame) == {"0": "one", "1": "two", "2": "", "3": "", "4": ""}


def test_the_first_frame_says_it_is_the_whole_screen():
    "So a client throws away anything it held."
    view = PaneView()

    frame = view.frame(a_screen(), 1)

    assert frame["whole"] is True
    assert frame["size"] == {"columns": COLUMNS, "rows": LINES}


def test_a_welcome_carries_the_whole_stylesheet():
    """
    A client has no second route to the server, and the runs name custom
    properties that something has to answer.

    **Everything, not only the properties.** `white-space: pre` is what
    makes a column land, so a client that lost it lost the layout. That is
    what happened when a frame's `palette` and this shared a field name.
    """
    screen = a_screen()
    message = PaneView().welcome(screen, 1, writable=True)

    assert message["type"] == WELCOME
    assert ".pyte-screen {" in message["css"]
    assert "--pyte-1:" in message["css"]
    assert "white-space: pre" in message["css"]
    assert "@keyframes pyte-blink" in message["css"]
    assert message["writable"] is True


# ----------------------------------------------------------------------
# What a later frame leaves out.


def test_nothing_changed_is_no_frame_at_all():
    """
    The answer that makes a stream cheap. A pane whose revision moved for
    bytes that drew nothing costs a viewer nothing.
    """
    screen = a_screen()
    feed(screen, "hello")
    view = PaneView()
    view.frame(screen, 1)

    assert view.frame(screen, 2) is None


def test_only_the_row_that_changed_is_sent():
    screen = a_screen()
    feed(screen, "one\r\ntwo")
    view = PaneView()
    view.frame(screen, 1)

    feed(screen, "\x1b[1;1Hnew")

    frame = view.frame(screen, 2)
    assert list(frame["rows"]) == ["0"]
    assert drawn(frame) == {"0": "new"}
    assert "whole" not in frame


def test_a_later_frame_names_no_size():
    "The size is in a frame only when it changed."
    screen = a_screen()
    view = PaneView()
    view.frame(screen, 1)
    feed(screen, "x")

    assert "size" not in view.frame(screen, 2)


def test_two_viewers_of_one_pane_share_nothing():
    """
    One pane has several viewers and no reader may empty a set another
    one reads, which is why the state is the viewer's.
    Lillecarl/pymux#126.
    """
    screen = a_screen()
    feed(screen, "hello")
    first = PaneView()
    first.frame(screen, 1)

    second = PaneView()

    # The one that has seen nothing gets everything, and the one that has
    # seen it all gets nothing.
    assert drawn(second.frame(screen, 1))["0"] == "hello"
    assert first.frame(screen, 1) is None


# ----------------------------------------------------------------------
# The style table.


def test_a_spelling_is_sent_once_and_numbered():
    screen = a_screen()
    feed(screen, "\x1b[1mbold\x1b[0m")
    view = PaneView()

    frame = view.frame(screen, 1)
    numbers = [style for style, _text in frame["rows"]["0"]]
    entry = frame["styles"][str(numbers[0])]

    # A class of the stylesheet the client already has, and not the
    # declaration again. Lillecarl/pymux#460.
    assert "pyte-bold" in entry["c"]
    assert "s" not in entry
    assert numbers[0] != PLAIN_STYLE


def test_a_spelling_with_a_value_in_it_still_carries_the_value():
    "A colour a program named itself answers to no rule of a stylesheet."
    screen = a_screen()
    feed(screen, "\x1b[38;2;30;170;90mgreen\x1b[0m")
    view = PaneView()

    frame = view.frame(screen, 1)
    numbers = [style for style, _text in frame["rows"]["0"]]
    entry = frame["styles"][str(numbers[0])]

    assert entry["s"] == "color:#1eaa5a"


def test_a_spelling_already_sent_is_only_a_number():
    "The reason a row of a hundred styled runs is a hundred integers."
    screen = a_screen()
    feed(screen, "\x1b[1mbold\x1b[0m")
    view = PaneView()
    first = view.frame(screen, 1)
    was = [style for style, _text in first["rows"]["0"]][0]

    feed(screen, "\x1b[2;1H\x1b[1mmore\x1b[0m")
    second = view.frame(screen, 2)

    assert "styles" not in second
    assert [style for style, _text in second["rows"]["1"]][0] == was


def test_plain_cells_need_no_entry():
    screen = a_screen()
    feed(screen, "plain")
    view = PaneView()

    frame = view.frame(screen, 1)

    assert "styles" not in frame
    assert [style for style, _text in frame["rows"]["0"]] == [PLAIN_STYLE]


def test_two_appearances_that_spell_alike_share_a_number():
    """
    The table is keyed by the declarations, so this falls out. It is also
    why it may not be keyed by the `Appearance`: `appearance_of` is a
    bounded cache, so one that falls out of it can be collected and a
    later one land on the same address.
    """
    screen = a_screen()
    # Bold, then bold again through a different route: a reset and a
    # second "SGR 1" makes another `Appearance` object with one spelling.
    feed(screen, "\x1b[1ma\x1b[0m\x1b[1mb\x1b[0m")
    view = PaneView()

    frame = view.frame(screen, 1)
    numbers = {style for style, _text in frame["rows"]["0"]} - {PLAIN_STYLE}

    assert len(numbers) == 1
    assert len(frame["styles"]) == 1


def test_a_hyperlink_travels_with_its_spelling():
    screen = a_screen()
    feed(screen, "\x1b]8;;https://example.com/\x1b\\link\x1b]8;;\x1b\\")
    view = PaneView()

    frame = view.frame(screen, 1)
    entry = next(iter(frame["styles"].values()))

    assert entry["h"] == "https://example.com/"


def test_a_link_a_program_must_not_choose_never_reaches_a_client():
    "The allowlist is `pyte.html.href_of`, and this is a caller of it."
    screen = a_screen()
    feed(screen, "\x1b]8;;javascript:alert(1)\x1b\\x\x1b]8;;\x1b\\")
    view = PaneView()

    frame = view.frame(screen, 1)

    for entry in frame.get("styles", {}).values():
        assert "h" not in entry


# ----------------------------------------------------------------------
# What a row does not carry.
#
# Each of these changes how every row draws while pyte's per-row write
# count stands still, so a diff that trusted the count alone would show a
# viewer the wrong screen until something else moved.


def test_reverse_video_redraws_everything():
    screen = a_screen()
    feed(screen, "hello")
    view = PaneView()
    view.frame(screen, 1)

    writes = screen.writes
    feed(screen, "\x1b[?5h")
    assert screen.writes == writes, "the row counter is expected not to see this"

    frame = view.frame(screen, 2)
    assert frame is not None
    assert frame["whole"] is True
    assert frame["reverse"] is True


def test_a_palette_change_redraws_everything_and_resends_the_stylesheet():
    """
    A program that changes colour one changes every cell that uses it,
    and the client's stylesheet is what says what colour one is.
    """
    screen = a_screen()
    feed(screen, "\x1b[31mred\x1b[0m")
    view = PaneView()
    view.frame(screen, 1)

    writes = screen.writes
    feed(screen, "\x1b]4;1;rgb:ff/55/55\x1b\\")
    assert screen.writes == writes, "the row counter is expected not to see this"

    frame = view.frame(screen, 2)
    assert frame is not None
    assert frame["whole"] is True
    assert "--pyte-1: #ff5555;" in frame["palette"]

    # **`palette`, and never `css`.** The welcome's whole stylesheet is
    # `css`; this is the sixteen colours and the two defaults. One field
    # for both put a client's first frame over the rules the welcome sent,
    # so the screen lost its background, its font, `white-space: pre`, the
    # link rule and the blink. A browser found it, and the field name is
    # the fix.
    assert "css" not in frame
    assert "white-space" not in frame["palette"]


def test_a_resize_redraws_everything():
    screen = a_screen()
    feed(screen, "hello")
    view = PaneView()
    view.frame(screen, 1)

    screen.resize(3, 10)

    frame = view.frame(screen, 2)
    assert frame["whole"] is True
    assert frame["size"] == {"columns": 10, "rows": 3}
    assert len(frame["rows"]) == 3


# ----------------------------------------------------------------------
# The cursor.


def test_the_cursor_is_a_row_of_the_screen():
    screen = a_screen()
    feed(screen, "one\r\ntwo")
    view = PaneView()

    assert view.frame(screen, 1)["cursor"] == {"row": 1, "column": 3}


def test_a_cursor_that_moved_alone_is_a_frame_with_no_rows():
    "Moving the cursor writes no cell, and a client draws a cursor."
    screen = a_screen()
    feed(screen, "hello")
    view = PaneView()
    view.frame(screen, 1)

    feed(screen, "\x1b[3;5H")

    frame = view.frame(screen, 2)
    assert frame is not None
    assert frame["rows"] == {}
    assert frame["cursor"] == {"row": 2, "column": 4}


def test_a_cursor_off_the_screen_says_so():
    """
    A buffer row is not a screen row. Rather than a row a client cannot
    draw, the answer is -1.
    """
    screen = a_screen()
    feed(screen, "\r\n" * (LINES + 3))
    view = PaneView()
    view.frame(screen, 1)
    screen.line_offset  # the screen has scrolled; the cursor is on it

    # Put the cursor into the history, where no client draws it.
    screen.pt_cursor_position.y = 0
    assert view.frame(screen, 2)["cursor"] == {"row": -1, "column": -1}


# ----------------------------------------------------------------------
# What a client may say.


def test_keys_are_names_and_not_bytes():
    """
    The server owns the three keyboard modes and the translation. A
    client that spelled an escape sequence would have to know which mode
    the program asked for, and would be wrong the moment it changed.
    """
    assert typed_of({"type": "input", "keys": "C-c"}) == ("C-c", True, False)


def test_composed_text_goes_as_it_stands_and_is_not_a_paste():
    """
    What a viewer finished composing. Not bracketed: a program that asked
    to know about pastes would draw the markers around a character
    somebody typed.
    """
    assert typed_of({"type": "text", "text": "C-c"}) == ("C-c", False, False)


def test_a_paste_is_marked_as_one():
    assert typed_of({"type": "paste", "text": "C-c"}) == ("C-c", False, True)


def test_the_three_kinds_are_three_because_of_composition():
    """
    A design with keys alone breaks on a Latin keyboard, not only on a
    CJK one: a dead key fires the same composition events a browser uses
    for an IME, and so do an emoji picker and dictation. The first caller
    measured that; this says the three kinds stay distinguishable.
    """
    named = {typed_of({"type": kind, "text": "e"}).named for kind in ("text", "paste")}
    assert named == {False}
    assert typed_of({"type": "input", "keys": "e"}).named is True


@pytest.mark.parametrize(
    ("message", "reason"),
    [
        ({"type": "input"}, "keys string"),
        ({"type": "input", "keys": ""}, "keys string"),
        ({"type": "input", "keys": 3}, "keys string"),
        ({"type": "text"}, "text string"),
        ({"type": "text", "text": 3}, "text string"),
        ({"type": "paste"}, "text string"),
        ({"type": "resize", "columns": 10}, "not something a client may say"),
        ({}, "not something a client may say"),
    ],
)
def test_what_a_client_may_not_say_is_refused_by_name(message, reason):
    "A typo in a client is an error and not silence."
    with pytest.raises(ValueError, match=reason):
        typed_of(message)


def test_empty_composed_text_is_allowed():
    """
    A composition a viewer cancelled commits nothing, and a client that
    sent it should not get an error for being honest.
    """
    assert typed_of({"type": "text", "text": ""}).text == ""
