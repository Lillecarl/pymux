import argparse
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from pymux.main import Pymux


from pyte.html import CSS, theme_css
from pyte.osc import ColorOverrides

from pymux.commands import add_command
from pymux.commands.common import the_pane
from pymux.commands.common import show_listing


def show_html_stylesheet(pymux: "Pymux", args: argparse.Namespace) -> None:
    """
    The stylesheet that `capture-pane -H` is written against.

    **A caller has no other way to get it.** `libpymux` takes one
    dependency and pyte is not it, so the string cannot be a constant
    there, and a copy of it would drift from the one the spans are
    written against. So the server answers with it.

    With `-t` the answer carries that pane's own colours as well: the
    theme the terminal of a person is showing, and whatever the program
    in the pane changed with "OSC 4" or "OSC 10". Without `-t` it is
    the conventional palette, which is the right answer for a caller
    that serves one stylesheet for every pane.

    Lillecarl/pymux#452.
    """
    if not args.target_pane:
        show_listing(pymux, "show-html-stylesheet", CSS)
        return

    pane = the_pane(pymux, args.target_pane)

    # The screen belongs to the installed pyte that ptterm was built
    # against, and `theme_css` to the one this package imports; pyrefly
    # sees two classes with the same name where there is one class.
    colors = cast(ColorOverrides, pane.screen.colors)
    show_listing(pymux, "show-html-stylesheet", CSS + "\n" + theme_css(colors))


def register(subparsers):
    parser = add_command(subparsers, show_html_stylesheet)
    parser.add_argument(
        "-t",
        dest="target_pane",
        metavar="<target-pane>",
        help="Answer with this pane's own colours over the conventional ones.",
    )
