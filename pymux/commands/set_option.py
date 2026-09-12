import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import CommandException
from pymux.commands import add_command
from pymux.commands.common import answer
from pymux.commands.common import option_as_written
from pymux.options import SetOptionError


def set_option(pymux: "Pymux", args: argparse.Namespace, window: bool = False) -> None:
    """
    Set an option, of the session or of a window.

    -g: for a window option, say what every new window starts with.
        For a session option it changes nothing, because pymux has one
        session and every session option is already global. `set -g`
        is the most common line in a tmux configuration, and `pymux -V`
        says pymux speaks tmux 3.4, so it has to be a line pymux takes.

    With no value, tmux prints what the option holds, and pymux does
    the same. Without it the command line was write-only: a person
    could change `mouse` and had no way to see what it was.
    Lillecarl/pymux#292.
    """
    name = args.option
    value = args.value

    if window:
        option = pymux.window_options.get(name)
    else:
        option = pymux.options.get(name)

    if option is None:
        raise CommandException("Invalid option: %s" % (name,))

    if value is None:
        answer(
            pymux,
            "%s %s" % (name, option_as_written(pymux, option, args, window)),
        )
        return

    try:
        # `-g` says what every new window starts with, and changes
        # no window that is open, which is what it means in tmux.
        # It is the only way a configuration file can set a window
        # option, because that file is read before there is a
        # window. Lillecarl/pymux#199.
        #
        # It means nothing for a session option: pymux has one
        # session, so every session option is already global. That
        # is why the flag is read here and not by the option.
        if window and args.g:
            option.set_default(pymux, value)
        else:
            option.set_value(pymux, value)
            # The colour base of every pane is derived from two of
            # the options: which theme owns the screen, and
            # whether it does. A pane that exists heard the old
            # answer, and hears the new one now.
            # Lillecarl/pymux#283.
            if name in ("theme", "paint-screen"):
                pymux.sync_color_bases()
    except SetOptionError as e:
        raise CommandException(e.message)


def register(subparsers):
    parser = add_command(subparsers, set_option)
    parser.add_argument("-g", dest="g", action="store_true", help="For a window option: what every new window starts with.")
    parser.add_argument("option", metavar="<option>")
    parser.add_argument("value", metavar="<value>", nargs="?")
