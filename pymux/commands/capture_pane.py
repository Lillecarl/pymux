import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import CommandException
from pymux.commands import add_command
from pymux.commands.common import find_pane
from pymux.commands.common import show_listing


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

    if not data_buffer:
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
    parser.add_argument("-t", dest="target_pane", metavar="<target-pane>", help="The pane to capture.")
    parser.add_argument("-S", dest="start", metavar="<start>", help="The first line. 0 is the top of the pane, negative is history.")
    parser.add_argument("-E", dest="end", metavar="<end>", help="The last line.")
