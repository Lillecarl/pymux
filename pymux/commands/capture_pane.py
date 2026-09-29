import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pyte.html import SCREEN_CLASS, html_of_page

from pymux.commands import CommandException
from pymux.commands import add_command
from pymux.commands.common import find_pane
from pymux.commands.common import show_listing


def _row_of(
    given: str | None, name: str, here: int, far_end: int, line_offset: int
) -> int:
    """
    One end of a range, as a row of the buffer.

    The numbers a caller writes are tmux's: 0 is the first visible row
    and a negative one reaches into the history. "-" is as far as the
    buffer goes, and nothing at all is `here`.
    """
    if given in (None, ""):
        return here
    if given == "-":
        return far_end
    try:
        return line_offset + int(given)
    except ValueError:
        raise CommandException("Invalid %s line: %s" % (name, given))


def _html(screen, args: argparse.Namespace) -> str:
    """
    The rows of a pane as the element `pyte.html` is written for.

    **The range is the visible pane by default**, where the text above
    takes the whole buffer. The caller who asks for HTML is drawing a
    pane a few times a second, and a screenful is what it draws; ten
    thousand rows of history per frame is not a default anybody wants.
    "-S -" still reaches as far back as the buffer goes.
    Lillecarl/pymux#457 holds the same question for the text.

    Nothing here reads a row the buffer does not hold. `html_of_page`
    goes through `.get`, because `data_buffer` is a defaultdict and a
    row made below the floor of the history is a row that came back
    from the dead.
    """
    rows = screen.page.data_buffer.keys()
    line_offset = screen.line_offset
    top = line_offset
    bottom = line_offset + screen.lines - 1

    # **The range covers the screen even where the buffer does not.** A
    # row of the pane that the program never wrote is a blank line and
    # belongs in the answer, so the element a caller draws is always as
    # tall as the pane. A row outside both the buffer and the screen is
    # nothing at all, so "-E 100000" may not answer with a hundred
    # thousand blank lines.
    lowest = min(min(rows, default=top), top)
    highest = max(max(rows, default=bottom), bottom)

    first = max(_row_of(args.start, "start", top, lowest, line_offset), lowest)
    last = min(_row_of(args.end, "end", bottom, highest, line_offset), highest)

    body = ""
    if first <= last:
        body = html_of_page(
            screen.page, first, last, screen.columns, screen.has_reverse_video
        )

    return '<pre class="%s">%s</pre>' % (SCREEN_CLASS, body)


def capture_pane(pymux: "Pymux", args: argparse.Namespace) -> None:
    """
    Capture the content of a pane.

    Line numbers are tmux style: 0 is the first line of the visible pane,
    negative numbers are lines in the history.

    **A line is a row of the pane, and `-J` makes it a line a program
    wrote.** The pane cuts a line to fit its width, so a path or a
    compiler message comes out in pieces without it. With `-J` the
    pieces are joined and the numbers count the joined lines, which is
    a different line 100 in the history. Lillecarl/pymux#135.

    **`-H` answers HTML instead of text**, so that a caller with no
    terminal can draw the pane: the colours, the renditions and the
    hyperlinks that the text drops. `show-html-stylesheet` is the other
    half of it. Lillecarl/pymux#452.
    """
    if args.target_pane:
        pane = find_pane(pymux, args.target_pane)
        if pane is None:
            raise CommandException(
                "Can't find pane: %s" % (args.target_pane,)
            )
    else:
        pane = pymux.arrangement.get_active_pane()

    process = pane.process
    screen = pane.screen
    page = screen.page
    data_buffer = page.data_buffer

    if args.H and args.J:
        raise CommandException(
            "capture-pane: -J joins rows into one line, which -H cannot draw"
        )

    if args.H:
        text = _html(screen, args)
    elif not data_buffer:
        text = ""
    else:
        first_row = min(data_buffer)
        last_row = max(data_buffer)

        if args.J:
            # One entry per line a program wrote, with the rows it was
            # laid out on joined back together.
            lines = page.text_lines(first_row, last_row)
            captured = [line.text for line in lines]

            # Line zero is the line the first visible row falls in. A
            # wrap can carry a line from the history onto the screen,
            # and the whole of that line is line zero.
            visible_top = next(
                (
                    index
                    for index, line in enumerate(lines)
                    if line.last >= screen.line_offset
                ),
                0,
            )
        else:
            captured = [page.text(row, row) for row in range(first_row, last_row + 1)]
            visible_top = screen.line_offset - first_row

        def from_tmux_line_number(line_number: int) -> int:
            "Translate a tmux line number into an index of `captured`."
            return visible_top + line_number

        # Determine the range. (tmux line numbers.)
        start_str = args.start
        end_str = args.end

        if start_str in (None, "", "-"):
            first_index = 0
        else:
            try:
                first_index = from_tmux_line_number(int(start_str))
            except ValueError:
                raise CommandException("Invalid start line: %s" % (start_str,))
            first_index = max(first_index, 0)

        if end_str in (None, "", "-"):
            last_index = len(captured) - 1
        else:
            try:
                last_index = from_tmux_line_number(int(end_str))
            except ValueError:
                raise CommandException("Invalid end line: %s" % (end_str,))
            last_index = min(last_index, len(captured) - 1)

        text = "\n".join(
            line.rstrip() for line in captured[first_index : last_index + 1]
        )

    if args.p:
        pymux.print_command_line(text)
    else:
        show_listing(pymux, "capture-pane", text)


def register(subparsers):
    parser = add_command(subparsers, capture_pane)
    parser.add_argument("-p", dest="p", action="store_true", help="Print to the output of the command line, not a pop-up.")
    parser.add_argument("-J", dest="J", action="store_true", help="Join the pieces a wrapped line was cut into.")
    parser.add_argument("-H", dest="H", action="store_true", help="Answer HTML instead of text. The default range is the visible pane.")
    parser.add_argument("-t", dest="target_pane", metavar="<target-pane>", help="The pane to capture.")
    parser.add_argument("-S", dest="start", metavar="<start>", help="The first line. 0 is the top of the pane, negative is history.")
    parser.add_argument("-E", dest="end", metavar="<end>", help="The last line.")
