"""
What one viewer of one pane has been sent, and what to send it next.

**A frame carries the rows that changed and nothing else.** A screen of
200x50 is about forty kilobytes of markup, and a viewer wants five a
second, so sending the screen is the thing to avoid. pyte counts writes
per row and the count only goes up, so the whole diff is a comparison of
numbers -- and the state of that comparison belongs to the viewer, not to
the screen, because one pane has several viewers and none of them may
empty a set the others read. Lillecarl/pymux#126.

**A cell's spelling is sent once, not once per run.** A frame holds a
style table: the first time a way of drawing appears, its CSS
declarations go out with a number, and every run after that is the
number. So a client renders without knowing what bold is, `pyte.html`
stays the only place a cell's CSS is written, and a row of a hundred
styled runs costs a hundred small integers.

The table is keyed by **the declaration string**, never by the
`Appearance`. `pyte.cells.appearance_of` is a bounded cache, so an
appearance that falls out of it can be collected and a later one land on
the same address: a table keyed by `id()` would hand a viewer a style
that belongs to something else. Keying by the string also means two
appearances that spell alike share one number.

Nothing here does any I/O, imports no transport and holds no socket.
Lillecarl/pymux#461.
"""

from __future__ import annotations

from typing import Any, NamedTuple

from pyte.html import CSS, href_of, runs_of_row, style_of, theme_css

__all__ = [
    "FRAME",
    "PLAIN_STYLE",
    "WELCOME",
    "PaneView",
    "Typed",
    "typed_of",
]

#: What a frame of rows is called on the wire.
FRAME = "frame"

#: The first message of a stream: what a client needs before any frame
#: means anything. The stylesheet is in it because a client that fetched
#: it separately would need a second route to pymux, and a relay would
#: have to carry that too.
WELCOME = "welcome"

#: **There is no message for "the pane ended".** The server closes the
#: connection, and a client reads that as the end. A frame saying it as
#: well would be a path that cannot be tested: the last pane of a server
#: takes the server down with it, so whatever was going to say so is
#: cancelled before it can. A client has to handle the close in any case,
#: so the close is the whole contract and a second answer would only be
#: the one nobody exercises.

#: The number of the way of drawing that needs no declarations at all.
#: It is never in the table, so a client starts knowing it.
PLAIN_STYLE = 0


class PaneView:
    """
    One viewer's idea of one pane.

    A viewer is anything that draws: a browser over a websocket, a relay,
    a test. Each one holds its own instance and asks `frame` for what to
    send next; two viewers of one pane share nothing at all.
    """

    def __init__(self) -> None:
        #: The write count this viewer has drawn, per screen row. A row
        #: that is not in here has never been drawn.
        self._drawn_at: dict[int, int] = {}

        #: The declarations this viewer has been given, and the number
        #: each one went out as.
        self._styles: dict[tuple[str, str, str], int] = {}
        self._next_style = PLAIN_STYLE + 1

        #: The screen-wide answers a row does not carry. Each one changes
        #: how every row draws, and pyte's per-row count does not move for
        #: any of them -- measured. So they are compared here, the way
        #: `ptterm` compares reverse video.
        self._size: tuple[int, int] | None = None
        self._reverse_video: bool | None = None
        self._palette: str | None = None

        self._cursor: tuple[int, int] | None = None
        self._welcomed = False

    # -- what to send --------------------------------------------------

    def welcome(self, screen, revision: int, writable: bool) -> dict[str, Any]:
        """
        The message that goes before any frame.

        It carries the whole stylesheet, because the custom properties the
        runs name have to be answered by something and a client that
        fetched them separately would need a second route to the server.

        **`css` here is everything; `palette` on a frame is only the
        sixteen colours and the two defaults.** They are named apart for
        that reason: a client that treated them as one field put both into
        one stylesheet and the first frame threw the rules away.
        """
        self._welcomed = True
        return {
            "type": WELCOME,
            "revision": revision,
            "writable": writable,
            "css": CSS + "\n" + theme_css(screen.colors),
            "size": {"columns": screen.columns, "rows": screen.lines},
        }

    def frame(self, screen, revision: int) -> dict[str, Any] | None:
        """
        What this viewer has not seen, or None when it has seen it all.

        **None is the answer that makes a stream cheap.** A pane whose
        revision moved for bytes that drew nothing gives every viewer
        None, so an animation that repaints one cell costs one row and a
        program writing invisible sequences costs nothing.
        """
        columns, lines = screen.columns, screen.lines
        size = (columns, lines)
        reverse_video = screen.has_reverse_video
        palette = theme_css(screen.colors)

        # Any of these changes how every row draws, so a viewer holding
        # rows under the old answer holds nothing worth keeping.
        whole = size != self._size or reverse_video != self._reverse_video or palette != self._palette
        if whole:
            self._drawn_at.clear()
            self._size = size
            self._reverse_video = reverse_video
            self._palette = palette

        data_buffer = screen.page.data_buffer
        written_at = screen.written_at
        # A row with no count of its own carries the one that
        # `touch_everything` last set, so a page swap or a reflow moves
        # every row at once and costs one number to say.
        everything_at = screen.everything_at
        top = screen.line_offset

        styles: dict[str, dict[str, str]] = {}
        rows: dict[str, list[Any]] = {}

        for index in range(lines):
            number = top + index
            at = written_at.get(number, everything_at)
            if not whole and self._drawn_at.get(index) == at:
                continue
            self._drawn_at[index] = at
            rows[str(index)] = self._runs(data_buffer.get(number), columns, reverse_video, styles)

        cursor = self._cursor_of(screen)

        if not rows and cursor == self._cursor and not whole:
            return None

        self._cursor = cursor

        answer: dict[str, Any] = {
            "type": FRAME,
            "revision": revision,
            "rows": rows,
            "cursor": {"row": cursor[0], "column": cursor[1]},
        }
        if styles:
            answer["styles"] = styles
        if whole:
            # A client that holds rows drawn under another size, another
            # reverse video or another palette throws them away: every
            # row is in this frame, and the ones outside it are gone.
            answer["whole"] = True
            answer["size"] = {"columns": columns, "rows": lines}
            answer["reverse"] = reverse_video
            # **`palette` and not `css`, because it is not the
            # stylesheet.** It was called `css` and the welcome's whole
            # stylesheet was called `css` too, so the first client put
            # both into one sheet: the first frame replaced every rule
            # with the palette block, and the screen lost its background,
            # `white-space: pre`, its font, its link rule and its blink.
            # A browser found it, and one field meaning two things was the
            # fault rather than anything the client did with it. Two
            # names, two sheets, and neither can eat the other.
            answer["palette"] = palette
        return answer

    # -- the pieces ----------------------------------------------------

    def _runs(
        self,
        row,
        columns: int,
        reverse_video: bool,
        styles: dict[str, dict[str, str]],
    ) -> list[Any]:
        """
        One row as `[[style, text], ...]`, filling `styles` on the way.

        An absent row is an empty list. `runs_of_row` is what decides
        where a row stops and what a blank is worth, and it is the same
        function the markup uses, so a browser and a document agree.
        """
        if row is None:
            return []

        answer: list[Any] = []
        for appearance, text in runs_of_row(row, columns, reverse_video):
            answer.append([self._style_number(appearance, reverse_video, styles), text])
        return answer

    def _style_number(self, appearance, reverse_video: bool, styles: dict[str, dict[str, str]]) -> int:
        "The number this way of drawing goes out as, naming it if it is new."
        drawn = style_of(appearance, reverse_video)
        key = (drawn.classes, drawn.style, href_of(appearance.hyperlink))
        if key == ("", "", ""):
            return PLAIN_STYLE

        held = self._styles.get(key)
        if held is not None:
            return held

        held = self._next_style
        self._next_style += 1
        self._styles[key] = held

        # **The classes go out as a name and not as their rules.** Most
        # of a rendition carries no value, and the stylesheet the client
        # already fetched answers every one of those, so the wire holds
        # a word rather than the declarations again.
        # Lillecarl/pymux#460.
        entry: dict[str, str] = {}
        if key[0]:
            entry["c"] = key[0]
        if key[1]:
            entry["s"] = key[1]
        if key[2]:
            entry["h"] = key[2]
        styles[str(held)] = entry
        return held

    def _cursor_of(self, screen) -> tuple[int, int]:
        """
        Where the cursor is, in rows of the screen.

        A buffer row is not a screen row, and a client draws a screen.
        A cursor above or below the screen is a cursor a client does not
        draw, and `-1` says so rather than a row that is not there.
        """
        position = screen.pt_cursor_position
        row = position.y - screen.line_offset
        if not 0 <= row < screen.lines:
            return (-1, -1)
        return (row, position.x)


#: What a client may say. Anything else is refused by name, so a typo in
#: a client is an error and not silence.
_INPUT = "input"
_TEXT = "text"
_PASTE = "paste"


class Typed(NamedTuple):
    "What one message from a viewer asks to put into the pane."

    text: str

    #: Whether the words are the **names** of keys for the server to
    #: spell: `C-c`, `Escape`, `Enter`.
    named: bool

    #: Whether to mark it as a paste, for a program that asked to know.
    bracketed: bool


def typed_of(message: dict[str, Any]) -> Typed:
    """
    What a viewer's message asks to put into the pane.

    Three kinds, and the reason there are three is composition.

    - `input` carries the **names** of keys. **A client never spells
      bytes**: the server owns the three keyboard modes and the
      translation table, so a browser that sent an escape sequence would
      have to know which mode the program asked for and would be wrong
      the moment it changed.
    - `text` carries characters a viewer has finished composing, written
      as typed. A browser draws a composition itself -- the preedit and
      the candidate window belong in the element, and a real terminal
      shows nothing until the commit either -- so the intermediate state
      never reaches the wire.
    - `paste` carries text that becomes a bracketed paste when the
      program asked for one. Not the same as `text`: a shell that reads
      a paste as keys runs the lines in it, and a typed character must
      not be wrapped in markers a program would draw.

    **`text` is not only for CJK.** The first caller measured this and it
    is the reason the message exists: a dead key on a European layout
    fires the same composition events, and so do an emoji picker and
    dictation. A design with keys alone breaks on a Latin keyboard.

    Raises `ValueError` with the reason, so a transport answers rather
    than dropping it.
    """
    kind = message.get("type")
    if kind == _INPUT:
        keys = message.get("keys")
        if not isinstance(keys, str) or not keys:
            raise ValueError("an input message needs a keys string")
        return Typed(keys, named=True, bracketed=False)
    if kind == _TEXT:
        text = message.get("text")
        if not isinstance(text, str):
            raise ValueError("a text message needs a text string")
        return Typed(text, named=False, bracketed=False)
    if kind == _PASTE:
        text = message.get("text")
        if not isinstance(text, str):
            raise ValueError("a paste message needs a text string")
        return Typed(text, named=False, bracketed=True)
    raise ValueError("%r is not something a client may say" % (kind,))
