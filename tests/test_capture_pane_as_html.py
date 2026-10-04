"""
`capture-pane -H` draws a pane for a caller that has no terminal.

The text capture drops everything but the characters. A web UI wants
the colours, the renditions and the hyperlinks, and the server is the
only place that holds them: it owns the `Page`, and a caller that rebuilt
one from the text would have thrown them away first.

**Why the server renders and not the caller.** The other way round is
`capture-pane` with escape sequences and pyte in the caller, and that
puts the renderer in the wrong process: a pymux webserver would need it
again. So `pyte.html` runs here, and a caller needs no pyte at all.

`show-html-stylesheet` is the other half. The spans name custom
properties, so a page that defines none of them draws nothing.
Lillecarl/pymux#452.
"""

from __future__ import annotations

import sys
from html.parser import HTMLParser

import pytest

from pymux.commands import call_command_handler
from pymux.main import Pymux

COLUMNS = 20
LINES = 5


@pytest.fixture
def pymux():
    "A server with one window, whose program ends at once."
    mux = Pymux()
    mux.create_window("%s -c pass" % (sys.executable,))
    try:
        yield mux
    finally:
        for window in list(mux.arrangement.windows):
            for pane in list(window.panes):
                process = getattr(pane, "process", None)
                if process is not None and not process.is_terminated:
                    process.kill()


def create_pane(mux, data: str):
    "The active pane, sized, with `data` written on it."
    pane = mux.arrangement.get_active_window().active_pane
    pane.screen.resize(LINES, COLUMNS)
    pane.terminal.terminal_control.stream.feed(data)
    return pane


def run(mux, command: str, *arguments) -> str:
    """
    What a command answers the client that asked.

    `command_output` and not a patched `print_command_line`, because
    `show_listing` reads that field to know whether anybody is waiting
    for text: with no field it draws a popup, and a popup needs a client.
    The server sets it the same way and joins it the same way.
    """
    mux.command_output = []
    try:
        call_command_handler(command, mux, list(arguments))
        return "\n".join(mux.command_output)
    finally:
        mux.command_output = None


def capture(mux, *arguments) -> str:
    return run(mux, "capture-pane", "-p", "-H", *arguments)


def text(mux, *arguments) -> str:
    return run(mux, "capture-pane", "-p", *arguments)


class _Read(HTMLParser):
    """
    What a browser would have: the text, and how each piece is drawn.

    The test reads the markup back rather than comparing strings,
    because a string comparison would fail on a change that no reader
    can see.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        #: One entry per piece of text, as (text, classes, style, href).
        self.pieces: list[tuple[str, str, str, str]] = []
        self._classes = ""
        self._style = ""
        self._href = ""
        self.element = ""
        self.screen_class = ""

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "pre":
            self.element = tag
            self.screen_class = values.get("class") or ""
        else:
            self._classes = values.get("class") or ""
            self._style = values.get("style") or ""
            self._href = values.get("href") or ""

    def handle_endtag(self, tag):
        if tag != "pre":
            self._classes = ""
            self._style = ""
            self._href = ""

    def handle_data(self, data):
        self.pieces.append((data, self._classes, self._style, self._href))

    @property
    def text(self) -> str:
        return "".join(piece for piece, _c, _s, _h in self.pieces)

    def drawn_by(self, wanted: str) -> str:
        """
        Everything that draws the piece holding `wanted`.

        The classes and the attribute together, because a reader should
        not have to know which half a rendition took: most of them are
        a class of the stylesheet now, and a colour with a value in it
        is not. Lillecarl/pymux#460.
        """
        for piece, classes, style, _href in self.pieces:
            if wanted in piece:
                return classes + " " + style
        raise AssertionError("no piece holds %r: %r" % (wanted, self.pieces))


def read(markup: str) -> _Read:
    reader = _Read()
    reader.feed(markup)
    return reader


NINE_LINES = "".join("line %d\r\n" % number for number in range(9))


def test_the_answer_is_one_element_a_page_can_hold(pymux):
    create_pane(pymux, "hello")
    reader = read(capture(pymux))

    assert reader.element == "pre"
    # The class every rule of the stylesheet is written under.
    assert reader.screen_class == "pyte-screen"


def test_the_characters_are_the_ones_the_text_capture_gives(pymux):
    "The spelling is new; what is on the screen is not."
    create_pane(pymux, "one\r\ntwo")

    assert read(capture(pymux)).text.rstrip("\n") == "one\ntwo"


def test_a_colour_reaches_the_drawing(pymux):
    "The whole reason for this command: the text drops it."
    create_pane(pymux, "\x1b[31mred\x1b[0m")

    assert "pyte-fg-1" in read(capture(pymux)).drawn_by("red")


def test_a_rendition_reaches_the_drawing(pymux):
    create_pane(pymux, "\x1b[1mbold\x1b[0m")

    assert "pyte-bold" in read(capture(pymux)).drawn_by("bold")


def test_a_colour_a_program_named_carries_its_value(pymux):
    "No rule of a stylesheet can answer one, so it stays an attribute."
    create_pane(pymux, "\x1b[38;2;30;170;90mgreen\x1b[0m")

    assert "color:#1eaa5a" in read(capture(pymux)).drawn_by("green")


def test_a_hyperlink_becomes_an_anchor(pymux):
    'What "OSC 8" opened, which no text capture can carry.'
    create_pane(pymux, "\x1b]8;;https://example.com/\x1b\\link\x1b]8;;\x1b\\")
    reader = read(capture(pymux))

    assert [href for _piece, _c, _s, href in reader.pieces if href] == ["https://example.com/"]


def test_what_a_program_writes_cannot_become_markup(pymux):
    "A program in the pane writes every character of this document."
    create_pane(pymux, "<b>&")

    assert read(capture(pymux)).text.startswith("<b>&")


# ----------------------------------------------------------------------
# The range.


def test_the_default_range_is_the_visible_pane(pymux):
    """
    A caller that draws a pane a few times a second wants the screen,
    not ten thousand rows of history.

    The text capture took the whole buffer when this was written, and
    the two agree now: Lillecarl/pymux#457 gave it the same default,
    and `-S -` is what still reaches the history on both sides.
    """
    create_pane(pymux, NINE_LINES)
    drawn = read(capture(pymux)).text

    assert drawn.splitlines() == ["line 5", "line 6", "line 7", "line 8"]
    # The fifth row is the one the cursor stands on, and a blank row is
    # a line: `html_of_page` joins the rows, so it is the last newline.
    assert drawn.endswith("line 8\n")

    # The two spellings of a capture answer the same rows.
    assert text(pymux).splitlines() == drawn.splitlines()


def test_a_dash_reaches_as_far_back_as_the_buffer_goes(pymux):
    create_pane(pymux, NINE_LINES)

    assert read(capture(pymux, "-S", "-")).text.splitlines()[0] == "line 0"


def test_a_negative_line_reaches_into_the_history(pymux):
    create_pane(pymux, NINE_LINES)

    assert read(capture(pymux, "-S", "-2", "-E", "-1")).text == "line 3\nline 4"


def test_a_pane_is_as_tall_as_the_pane(pymux):
    """
    A row the program never wrote is a blank line, so a caller's box
    does not change height with what is on the screen.
    """
    create_pane(pymux, "one")

    assert read(capture(pymux)).text == "one" + "\n" * (LINES - 1)


def test_a_range_outside_the_buffer_is_empty(pymux):
    create_pane(pymux, "one")

    assert read(capture(pymux, "-S", "40", "-E", "50")).text == ""


def test_a_line_number_that_is_not_a_number_is_an_error(pymux):
    create_pane(pymux, "one")
    errors = []
    pymux.add_command_error = errors.append
    pymux.show_message = lambda message: None

    call_command_handler("capture-pane", pymux, ["-p", "-H", "-S", "nope"])

    assert errors == ["pymux: Invalid start line: nope"]


def test_joining_the_rows_is_refused(pymux):
    """
    `-J` answers the lines a program wrote, which have no width. HTML
    draws a screen, so the two ask for different things.
    """
    create_pane(pymux, "one")
    errors = []
    pymux.add_command_error = errors.append
    pymux.show_message = lambda message: None

    call_command_handler("capture-pane", pymux, ["-p", "-H", "-J"])

    assert errors == ["pymux: capture-pane: -J joins rows into one line, which -H cannot draw"]


# ----------------------------------------------------------------------
# The stylesheet.


def test_the_stylesheet_answers_the_properties_the_spans_name(pymux):
    "Without these the page draws nothing at all."
    create_pane(pymux, "one")
    stylesheet = run(pymux, "show-html-stylesheet")

    assert ".pyte-screen {" in stylesheet
    assert "--pyte-1:" in stylesheet
    assert "--pyte-fg:" in stylesheet


def test_a_pane_carries_the_colours_a_program_set(pymux):
    '"OSC 4" from inside the pane reaches the browser.'
    pane = create_pane(pymux, "\x1b]4;1;rgb:12/34/56\x1b\\")
    stylesheet = run(pymux, "show-html-stylesheet", "-t", "%%%i" % pane.pane_id)

    # Twice: the conventional answer first, then the pane's own over it.
    assert stylesheet.count(".pyte-screen {") == 2
    assert "--pyte-1: #123456;" in stylesheet


def test_the_stylesheet_of_no_pane_holds_one_rule(pymux):
    "One stylesheet for every pane is what a caller should serve."
    create_pane(pymux, "one")

    assert run(pymux, "show-html-stylesheet").count(".pyte-screen {") == 1
