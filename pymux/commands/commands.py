import argparse
import inspect
import os
import shlex
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Dict,
    List,
    Optional,
)
from prompt_toolkit.application.current import get_app
from prompt_toolkit.data_structures import Size
from prompt_toolkit.document import Document
from prompt_toolkit.key_binding.vi_state import InputMode

from pymux import introspect
from pymux.arrangement import LayoutTypes
from pymux.commands.aliases import ALIASES
from pymux.commands.utils import wrap_argument
from pymux.enums import WindowSize, Woke
from pymux.format import format_pymux_string
from pymux.key_spelling import (
    KeyCompleter,
    event_however_it_is_written,
    why_a_pane_cannot_read,
)
from pyte.keys import Unhearable
from pymux.layout import (
    focus_down,
    focus_left,
    focus_right,
    focus_up,
    change_pane_size,
)
from pymux.log import logger
from pymux.options import ALL_OPTIONS, ALL_WINDOW_OPTIONS, SetOptionError

if TYPE_CHECKING:
    from pymux.arrangement import Pane, Window
    from pymux.main import Pymux

__all__ = [
    "call_command_handler",
    "get_documentation_for_command",
    "get_option_flags_for_command",
    "handle_command",
    "has_command_handler",
]

_VariablesList = List[str]
_VariablesDict = Dict[str, Any]
_PymuxHandler = Callable[["Pymux", _VariablesList], None]

# Global mapping of pymux commands to their handlers.
COMMANDS_TO_HANDLERS: Dict[str, _PymuxHandler] = {}

COMMANDS_TO_HELP: Dict[str, str] = {}
COMMANDS_TO_OPTION_FLAGS: Dict[str, List[str]] = {}

#: The argparse parser of each command. The completers read it: the shell
#: through argcomplete, and the command bar through
#: `pymux/commands/completer.py`. Lillecarl/pymux#48.
COMMANDS_TO_PARSERS: Dict[str, argparse.ArgumentParser] = {}

#: The first line of the docstring of each command. A completion shows it
#: beside the name, and so does `help` of the command line.
COMMANDS_TO_DESCRIPTIONS: Dict[str, str] = {}


def has_command_handler(command: str) -> bool:
    return command in COMMANDS_TO_HANDLERS


def get_documentation_for_command(command: str) -> str | None:
    """
    Return the help text for this command, or None if the command is not known.
    """
    if command in COMMANDS_TO_HELP:
        return "Usage: %s %s" % (command, COMMANDS_TO_HELP.get(command, ""))

    return None


def get_option_flags_for_command(command: str) -> List[str]:
    "Return a list of options (-x flags) for this command."
    return COMMANDS_TO_OPTION_FLAGS.get(command, [])


def handle_command(pymux: "Pymux", input_string: str) -> None:
    """
    Handle command.

    Like tmux, several commands can be given at once, separated by an
    unquoted semicolon. E.g. `send-keys -t %5 -R ; clear-history -t %5`.
    """
    input_string = input_string.strip()
    logger.debug("handle command: %s", input_string)

    if input_string and not input_string.startswith("#"):  # Ignore comments.
        try:
            parts = shlex.split(input_string)
        except ValueError as e:
            # E.g. missing closing quote.
            pymux.show_message("Invalid command %s: %s" % (input_string, e))
        else:
            # Split into separate commands on bare ';' tokens.
            # (Exception: for bind-key/unbind-key, a ';' can be the name of
            # the key that is bound. Like tmux, we don't split there.)
            no_semicolon_split = parts[0] in ("bind-key", "unbind-key")
            commands: List[_VariablesList] = [[]]
            for part in parts:
                if part == ";" and not no_semicolon_split:
                    commands.append([])
                else:
                    commands[-1].append(part)

            for args in commands:
                if args:
                    call_command_handler(args[0], pymux, args[1:])


def call_command_handler(
    command: str, pymux: "Pymux", arguments: _VariablesList
) -> None:
    """
    Execute command.

    :param arguments: List of options.
    """
    # Resolve aliases.
    command = ALIASES.get(command, command)

    try:
        handler = COMMANDS_TO_HANDLERS[command]
    except KeyError:
        pymux.show_message("Invalid command: %s" % (command,))
        pymux.add_command_error("pymux: invalid command: %s" % (command,))
    else:
        try:
            handler(pymux, arguments)
        except CommandException as e:
            pymux.show_message(e.message)
            pymux.add_command_error("pymux: %s" % (e.message,))


#
# The parser tree.
#
# Every command declares what it takes with argparse's own API, in a
# declarer next to its handler. argparse parses with the tree, the
# shell completes through it, and the command bar of a client reads
# it. One description of a command, and three readers of it.
# Lillecarl/pymux#48.
#

#: The declarers, in the order their commands were written.
DECLARERS: List[Callable[[Any], None]] = []


def declarer(func: Callable[[Any], None]) -> Callable[[Any], None]:
    "Collect the declarer of a command."
    DECLARERS.append(func)
    return func


def _command(
    subparsers: Any, handler: Any, *, name: str | None = None, aliases: tuple = ()
) -> Any:
    """
    The parser of one command: named after its handler, described by
    the first line of its docstring.
    """
    if name is None:
        name = handler.__name__.replace("_", "-")
    parser = subparsers.add_parser(
        name,
        aliases=list(aliases),
        help=(inspect.getdoc(handler) or "").partition("\n")[0],
        # `-h` is an option of `split-window`, and no command answers
        # a help flag on its own: the help of the command bar and of
        # the shell come from this tree, not from a `-h`.
        add_help=False,
    )
    parser.set_defaults(_handler=handler)
    return parser


class _BadLine(Exception):
    "What a parser says about a line it cannot read."

    def __init__(self, message: str) -> None:
        self.message = message


class _Parser(argparse.ArgumentParser):
    "An argparse parser that raises instead of exiting."

    def error(self, message: str) -> None:
        raise _BadLine(message)


def add_commands_to(subparsers: Any) -> None:
    """
    Mount every command of the server on a subparsers action.

    The shell completes the command line of pymux through one parser
    that holds the options of the entry point and every command under
    it. This is what fills the tree under it. It parses nothing on its
    own.
    """
    for declare in DECLARERS:
        declare(subparsers)


def list_commands(pymux: "Pymux", variables: _VariablesDict) -> None:
    """
    One line per command: the name, and what it does.
    """
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    add_commands_to(subparsers)

    # argparse records the help of each command on a pseudo action of
    # the subparsers action, not on the parser it built.
    lines = [
        "%-24s %s" % (action.metavar, action.help or "")
        for action in sorted(subparsers._choices_actions, key=lambda a: a.metavar)
    ]
    answer(pymux, "\n".join(lines))


def _variables_of(parser: argparse.ArgumentParser, namespace: argparse.Namespace) -> _VariablesDict:
    """
    What the handlers read, in the shape docopt used to give.

    A value option is there under both spellings, `-t` and
    `<target-pane>`, so a handler that asks whether `-t` was given and
    a handler that reads the value ask the same dictionary. A
    positional is there as `<name>`. The names argparse gives, the
    dests, are there as themselves: that is the spelling a handler
    that is written against argparse reads.
    """
    variables: _VariablesDict = {}
    for action in parser._actions:
        if action.dest in ("help",):
            continue
        value = getattr(namespace, action.dest, None)
        variables[action.dest] = value
        if action.metavar:
            spelled = str(action.metavar).strip("<>")
            variables["<%s>" % (spelled,)] = value
        for option in action.option_strings:
            if len(option) == 2:
                variables[option] = bool(value) if action.nargs == 0 else value
    return variables


def _shlex_that_keeps_a_hash() -> None:
    """
    Stop argcomplete reading a `#` as the start of a comment.

    A command line is not a script, and no part of one is a comment.
    argcomplete lexes the line with a vendored `shlex` whose
    `commenters` is `#`, so everything from the first `#` is dropped,
    and `pymux list-panes -F "#{pane_id}"<TAB>` completed an empty
    word. No shell reads it that way: bash treats `#` as a comment
    only at the start of a word, and `#` is in no `COMP_WORDBREAKS`.

    The fix upstream is one line in `split_line`, and until it is
    there, every program that completes a format string has to install
    this correction for itself.
    """
    from argcomplete.packages import _shlex

    class _Uncommented(_shlex.shlex):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self.commenters = ""

    # `lexers.py` binds this same module object, so the change reaches it.
    _shlex.shlex = _Uncommented


class CommandException(Exception):
    "When raised from a command handler, this message will be shown."

    def __init__(self, message: str) -> None:
        self.message = message


#
# Target parsing. (tmux style targets: `%<pane-id>`, `@<window-id>`,
# `$<session-id>`, `<index>`, `<session>:<window>.<pane>`.)
#


def _find_window(pymux: "Pymux", target: str | None) -> Optional["Window"]:
    """
    Find a window for a tmux-style target.

    Supported targets: `@<window-id>`, `%<pane-id>` (the window that owns
    this pane), `<window-index>`, `:<window-index>`, and the window part of
    `session:window.pane`.
    """
    if target is None or target == "":
        return pymux.arrangement.get_active_window()

    # Strip the session part. (Pymux has one session per server.)
    if ":" in target:
        target = target.rsplit(":", 1)[1]

    # A pane ID target: `%<id>`. (Find the window that owns this pane.)
    if target.startswith("%"):
        pane = _find_pane(pymux, target)
        if pane is not None:
            for w in pymux.arrangement.windows:
                if pane in w.panes:
                    return w
        return None

    if target.startswith("@"):
        window_id = target[1:]
        if window_id.isdigit():
            for w in pymux.arrangement.windows:
                if w.window_id == int(window_id):
                    return w

    if target.isdigit():
        return pymux.arrangement.get_window_by_index(int(target))

    return None


def _find_pane(pymux: "Pymux", target: str | None) -> Optional["Pane"]:
    """
    Find a pane for a tmux-style target.

    Supported targets: `%<pane-id>`, `@<window-id>.<pane-index>`,
    `<window-index>.<pane-index>`, `.<pane-index>`, and the pane part of
    `session:window.pane`.
    """
    if target is None or target == "":
        return pymux.arrangement.get_active_pane()

    # A pane ID target: `%<id>`. (Look it up in all windows.)
    if target.startswith("%"):
        pane_id = target[1:]
        if pane_id.isdigit():
            pane_id_int = int(pane_id)
            for w in pymux.arrangement.windows:
                for p in w.panes:
                    if p.pane_id == pane_id_int:
                        return p
        return None

    window = pymux.arrangement.get_active_window()

    # Strip the session part.
    if ":" in target:
        _, _, target = target.rpartition(":")

    # Split off the pane part.
    pane_part: str | None = None
    if "." in target:
        target, _, pane_part = target.partition(".")

    if target:
        window = _find_window(pymux, target)
        if window is None:
            return None

    if pane_part is None or pane_part == "":
        return window.active_pane

    if pane_part.isdigit():
        index = int(pane_part)
        if 0 <= index < len(window.panes):
            return window.panes[index]

    return None


def _pane_matches_session_name(pymux: "Pymux", target: str) -> bool:
    "Whether the given target matches the session. (For has-session.)"
    # No target asks whether the server has a session at all, which is
    # what `has-session` with no `-t` means in tmux. A server always has
    # one, so the answer is yes.
    if not target:
        return True

    # Accept an exact match ('=name' syntax as used by tmux/libtmux) or a
    # plain name. Only one session exists on a server.
    name = target[1:] if target.startswith("=") else target
    return name == pymux.session_name


#
# The actual commands.
#


def break_pane(pymux: "Pymux", variables: _VariablesDict) -> None:
    """
    Take the active pane out of its window, into one of its own.
    """
    dont_focus_window = variables["-d"]

    pymux.arrangement.break_pane(set_active=not dont_focus_window)
    pymux.invalidate(Woke.PANE_BROKE_OUT)


def select_pane(pymux: "Pymux", variables: _VariablesDict) -> None:
    """
    Focus a pane beside this one, or rotate the panes of the window.
    """
    if variables["-t"]:
        pane_id = variables["<pane-id>"]
        w = pymux.arrangement.get_active_window()

        if pane_id == ":.+":
            w.focus_next()
        elif pane_id == ":.-":
            w.focus_previous()
        else:
            pane = _find_pane(pymux, pane_id)
            if pane is None:
                raise CommandException("Can't find pane: %s" % (pane_id,))
            w.active_pane = pane

    elif variables["-l"]:
        pymux.arrangement.get_active_window().rotate(with_pane_after_only=True)

    else:
        if variables["-L"]:
            h = focus_left
        if variables["-U"]:
            h = focus_up
        if variables["-D"]:
            h = focus_down
        if variables["-R"]:
            h = focus_right

        h(pymux)


def select_window(pymux: "Pymux", variables: _VariablesDict) -> None:
    """
    Focus a window by index, by id, or by the pane that holds it.
    E.g:  select-window -t :3  or  select-window -t @1001
    """
    window_id = variables["<target-window>"]

    w = _find_window(pymux, window_id)
    if w is None:
        raise CommandException("Can't find window: %s" % (window_id,))

    pymux.arrangement.set_active_window(w)


def swap_window(pymux: "Pymux", variables: _VariablesDict) -> None:
    """
    Swap the active window with the window a target names.

    The windows trade indexes, and without `-d` the window swapped
    into the active one's place takes the focus, the way tmux's does.
    A relative target, `+1` or `-1`, counts from the active window:
    the tmux spell is `swap-window -t -1`. Lillecarl/pymux#296.
    """
    dst = variables["<dst-window>"]
    active = pymux.arrangement.get_active_window()

    if dst.startswith(("+", "-")):
        dst_window = pymux.arrangement.get_window_by_index(active.index + int(dst))
    else:
        dst_window = _find_window(pymux, dst)

    if dst_window is None or dst_window is active:
        return

    pymux.arrangement.swap_window(active, dst_window)

    if not variables["-d"]:
        pymux.arrangement.set_active_window(dst_window)


def move_window(pymux: "Pymux", variables: _VariablesDict) -> None:
    """
    Move this window to another index.
    """
    dst_window = variables["<dst-window>"]
    try:
        new_index = int(dst_window)
    except ValueError:
        raise CommandException("Invalid window index: %r" % (dst_window,))

    # Check first whether the index was not yet taken.
    if pymux.arrangement.get_window_by_index(new_index):
        raise CommandException("Can't move window: index in use.")

    # Save index.
    w = pymux.arrangement.get_active_window()
    pymux.arrangement.move_window(w, new_index)


def rotate_window(pymux: "Pymux", variables: _VariablesDict) -> None:
    """
    Rotate the panes of the window.
    """
    if variables["-D"]:
        pymux.arrangement.rotate_window(count=-1)
    else:
        pymux.arrangement.rotate_window()


def swap_pane(pymux: "Pymux", variables: _VariablesDict) -> None:
    """
    Swap the active pane with the one above or below.
    """
    pymux.arrangement.get_active_window().rotate(with_pane_after_only=variables["-U"])


def kill_pane(pymux: "Pymux", variables: _VariablesDict) -> None:
    """
    Kill a pane, or the active one.
    """
    if variables["-t"]:
        pane = _find_pane(pymux, variables["<target-pane>"])
        if pane is None:
            raise CommandException(
                "Can't find pane: %s" % (variables["<target-pane>"],)
            )
    else:
        pane = pymux.arrangement.get_active_pane()
    pymux.kill_pane(pane)


def kill_window(pymux: "Pymux", variables: _VariablesDict) -> None:
    "Kill all panes in the current window."
    if variables["-t"]:
        w = _find_window(pymux, variables["<target-window>"])
        if w is None:
            raise CommandException(
                "Can't find window: %s" % (variables["<target-window>"],)
            )
    else:
        w = pymux.arrangement.get_active_window()

    for pane in w.panes:
        pymux.kill_pane(pane)


def suspend_client(pymux: "Pymux", variables: _VariablesDict) -> None:
    """
    Suspend this client, the way ctrl+z suspends a program in a shell.
    """
    connection = pymux.get_connection()

    if connection:
        connection.suspend_client_to_background()


def clock_mode(pymux: "Pymux", variables: _VariablesDict) -> None:
    """
    Show a clock in the active pane, or put the program back.
    """
    pane = pymux.arrangement.get_active_pane()
    if pane:
        pane.clock_mode = not pane.clock_mode


def last_pane(pymux: "Pymux", variables: _VariablesDict) -> None:
    """
    Focus the pane that was active before this one.
    """
    w = pymux.arrangement.get_active_window()
    prev_active_pane = w.previous_active_pane

    if prev_active_pane:
        w.active_pane = prev_active_pane


def next_layout(pymux: "Pymux", variables: _VariablesDict) -> None:
    "Select next layout."
    pane = pymux.arrangement.get_active_window()
    if pane:
        pane.select_next_layout()


def previous_layout(pymux: "Pymux", variables: _VariablesDict) -> None:
    "Select previous layout."
    pane = pymux.arrangement.get_active_window()
    if pane:
        pane.select_previous_layout()


def new_window(pymux: "Pymux", variables: _VariablesDict) -> None:
    """
    Open a window, next to the one a person is on.

    **Next to it, and not in the lowest gap.** A new window took the
    first free index from `base-index` up, whatever window a person
    was looking at: on window five of one, two, five, `ctrl+b c` gave
    window three. A person who works across windows builds a map of
    what is where, and a new window arriving at the far end of it is a
    window they then have to go and find. Lillecarl/pymux#191.

    With no gaps the two rules agree, which is why this went so long
    without being noticed: the lowest free index is the one after the
    last window.

    `-a` and `-b` are tmux's, and name the side. `-t` names the window
    to sit next to, and the active one is the default. tmux reads a
    bare `-t` as the index to create at instead, and so does this.
    """
    executable = variables["<executable>"]
    start_directory = variables["<start-directory>"]
    name = variables["<name>"]
    dont_select = variables["-d"]

    window = pymux.arrangement.get_active_window()
    pymux.create_window(
        executable,
        start_directory=start_directory,
        name=name,
        index=_where_a_new_window_goes(pymux, variables),
    )

    # **The one that is active, and not the last of the list.** A new
    # window went at the end while it always took the highest index,
    # and it can go anywhere now. `create_window` focuses it, which is
    # the only thing that says which one it is.
    new_window = pymux.arrangement.get_active_window()

    if dont_select:
        # Don't make the new window active.
        pymux.arrangement.set_active_window(window)

    if variables["-P"]:
        _print_object_format(
            pymux,
            variables["<format>"],
            window=new_window,
            pane=new_window.active_pane,
        )


def _where_a_new_window_goes(pymux: "Pymux", variables: _VariablesDict) -> int | None:
    """
    The index a new window takes, from the options it was given.

    Four answers, and the first that applies wins:

    - `-b`, before the target: the target's own index. What is there
      moves up.
    - `-a`, after the target: one past it.
    - `-t` on its own: the index to create at, which is how tmux reads
      a bare target for this command.
    - Nothing: after the active window, which is the one a person is
      looking at.

    `None` means the lowest free index, and nothing returns it any
    more. It is still what `Arrangement.create_window` does without an
    index, because a session that is restored builds its windows by
    number and asks for none.

    A target nobody can find is the active window. tmux errors there,
    and a person who mistypes a window number while opening one does
    not want the window not to open.
    """
    number = _index(variables["<target-window>"])

    where = None
    if number is not None:
        where = pymux.arrangement.get_window_by_index(number)
    if where is None:
        where = pymux.arrangement.get_active_window()

    if variables["-b"]:
        return where.index
    if variables["-a"]:
        return where.index + 1
    if number is not None:
        return number
    return where.index + 1


def _index(target: "str | None") -> int | None:
    """
    The window number a target names, or None for one that is a name.

    tmux takes a window by number, by name, or by one of its own
    shorthands. Only the number places a window, so the rest read as
    "no number" and leave the placement to the active window.
    """
    try:
        return int(target)
    except (TypeError, ValueError):
        return None


def split_window(pymux: "Pymux", variables: _VariablesDict) -> None:
    """
    Split this window into two panes, side by side or stacked.
    """
    executable = variables["<executable>"]
    start_directory = variables["<start-directory>"]

    # Split in the target window. (libtmux targets the pane of the window.)
    target_window = _find_window(pymux, variables["<target-window>"])

    # The tmux definition of horizontal is the opposite of prompt_toolkit.
    pymux.add_process(
        executable,
        vsplit=variables["-h"],
        start_directory=start_directory,
        window=target_window,
    )

    if variables["-P"]:
        window = pymux.arrangement.get_active_window()
        if target_window is not None:
            window = target_window
        _print_object_format(
            pymux, variables["<format>"], window=window, pane=window.active_pane
        )


def _(pymux: "Pymux", variables: _VariablesDict) -> None:
    "Go to previous active window."
    w = pymux.arrangement.get_previous_active_window()

    if w:
        pymux.arrangement.set_active_window(w)


def next_window(pymux: "Pymux", variables: _VariablesDict) -> None:
    "Focus the next window."
    pymux.arrangement.focus_next_window()


def previous_window(pymux: "Pymux", variables: _VariablesDict) -> None:
    "Focus the previous window."
    pymux.arrangement.focus_previous_window()


def select_layout(pymux: "Pymux", variables: _VariablesDict) -> None:
    """
    Arrange the panes of the window in a named layout.
    """
    layout_type = variables["<layout-type>"]

    try:
        layout_type_obj: LayoutTypes = LayoutTypes(layout_type)
    except ValueError:
        raise CommandException("Invalid layout type.")
    else:
        pymux.arrangement.get_active_window().select_layout(layout_type_obj)


def switch_column_width(pymux: "Pymux", variables: _VariablesDict) -> None:
    """
    Give this column of the strip the next preset width.

    -p: the previous one instead.

    The presets are a third, a half and two thirds of the window, which
    are niri's own. This is the strip's answer to `resize-pane`: a
    person picks between a few widths that fit together rather than
    nudging a border. Lillecarl/pymux#198.

    No key is bound to it. tmux has no equivalent command, so there is
    nothing to keep for muscle memory and nothing to collide with, and
    which key it should be is a choice rather than a default.
    """
    window = pymux.arrangement.get_active_window()

    if not window.strip:
        raise CommandException(
            "This window is not a strip. `set-window-option strip on` first."
        )

    pane = window.active_pane
    if pane is None:
        raise CommandException("There is no pane here.")

    window.switch_column_width(pane, back=variables["-p"])
    pymux.invalidate(Woke.COLUMN_CHANGED_WIDTH)


def move_column(pymux: "Pymux", variables: _VariablesDict) -> None:
    """
    Move this column of the strip one place along the row.

    -L: to the left. -R: to the right.

    niri binds this beside the movement keys, and it is half of why
    the model works: a person puts related columns next to each other
    without renegotiating a layout. Without it, getting two columns
    adjacent means closing one and opening it again in the right
    place, which is the renegotiation a strip exists to avoid.
    Lillecarl/pymux#202.

    **The whole column moves**, panes and width and all. Taking the
    focused pane out of a stack is a different move, and `break-pane`
    is the command that does that kind of thing.

    A column at the end of the row stays where it is. That is not an
    error: a key held down at the edge of the row does nothing, the
    way it does nothing in niri.

    No key is bound to it, for the same reason as
    `switch-column-width`: tmux has no equivalent command, so there is
    nothing to keep for muscle memory and which key it should be is a
    choice rather than a default.
    """
    window = pymux.arrangement.get_active_window()

    if not window.strip:
        raise CommandException(
            "This window is not a strip. `set-window-option strip on` first."
        )

    pane = window.active_pane
    if pane is None:
        raise CommandException("There is no pane here.")

    if window.move_column(pane, -1 if variables["-L"] else 1):
        pymux.invalidate(Woke.COLUMN_MOVED)


def consume_or_expel(pymux: "Pymux", variables: _VariablesDict) -> None:
    """
    Move this pane into the next column of the strip, or out of its own.

    -L: to the left. -R: to the right.

    **One key does both jobs.** A pane that shares its column leaves
    it, into a new column of its own on that side. A pane that is
    alone in its column joins the next column, at the bottom. niri
    binds this to one key each way, and the "or" is the point: a
    person holds the key and the pane walks in and out of the columns
    without deciding which move they wanted. Lillecarl/pymux#213.

    `move-column` moves a whole column along the row, and `break-pane`
    takes a pane out to a window of its own. This is the move between
    them, and it is the one a strip had no way to make: putting a pane
    in another column meant closing it and opening it again there.

    A pane that is alone in the column at the end of the row stays
    where it is. That is not an error.

    No key is bound to it. niri uses `Mod+BracketLeft` and
    `Mod+BracketRight`, and `{` and `}` are the same two keys behind
    the prefix, but tmux already binds those to `swap-pane -U` and
    `-D` and pymux keeps tmux's keys. Which keys the three commands of
    the strip take is Lillecarl/pymux#212.
    """
    window = pymux.arrangement.get_active_window()

    if not window.strip:
        raise CommandException(
            "This window is not a strip. `set-window-option strip on` first."
        )

    pane = window.active_pane
    if pane is None:
        raise CommandException("There is no pane here.")

    if window.consume_or_expel(pane, -1 if variables["-L"] else 1):
        pymux.invalidate(Woke.PANE_CHANGED_COLUMN)


def rename_window(pymux: "Pymux", variables: _VariablesDict) -> None:
    """
    Rename the active window.
    """
    pymux.arrangement.get_active_window().chosen_name = variables["<name>"]


def rename_pane(pymux: "Pymux", variables: _VariablesDict) -> None:
    """
    Rename the active pane.
    """
    pymux.arrangement.get_active_pane().chosen_name = variables["<name>"]


def rename_session(pymux: "Pymux", variables: _VariablesDict) -> None:
    """
    Rename this session.
    """
    pymux.session_name = variables["<name>"]


def resize_pane(pymux: "Pymux", variables: _VariablesDict) -> None:
    """
    Resize the active pane, or zoom it.
    """
    try:
        left = int(variables["<left>"] or 0)
        right = int(variables["<right>"] or 0)
        up = int(variables["<up>"] or 0)
        down = int(variables["<down>"] or 0)
    except ValueError:
        raise CommandException("Expecting an integer.")

    w = pymux.arrangement.get_active_window()

    if w and w.active_pane is not None:
        change_pane_size(
            pymux, w, w.active_pane, up=up, right=right, down=down, left=left
        )

        # Zoom in/out.
        if variables["-Z"]:
            w.zoom = not w.zoom


def resize_window(pymux: "Pymux", variables: _VariablesDict) -> None:
    """
    Say how big this window is, and stop following the clients.

    -x: how many columns. -y: how many rows.

    -L, -R: that many columns narrower or wider. -U, -D: that many
    rows shorter or taller.

    **This is the `manual` half of `window-size`**, and running it
    turns that option on: a person who names a size means it to stay.
    Decision 11 of `docs/layout-engine-plan.md`.

    A client bigger than the window draws background around it, and a
    client smaller than it moves its view over it, which is what every
    policy does. So a window may be made bigger than any terminal
    watching, and every pane of it is still reachable.

    **The size is the window's own**, so nothing comes off it for the
    status line: `-x 100 -y 40` is a hundred cells by forty. An axis
    that is not given keeps what it has.

    **A nudge counts from the size the window has now**, which is why
    it is the one a person binds to a key: naming an absolute size
    means knowing what the size is, so "a little wider" would be a
    look at the status line and then a command.

    An absolute and a nudge together are read in that order, so
    `-x 80 -R 10` is ninety columns. tmux takes one nudge at a time;
    several here cost nothing and say more.

    **A nudge stops at one cell and does not complain.** A key held
    down at the edge does nothing, the way it does nothing in
    `move-column`. An absolute size below one is a person asking for
    something that cannot exist, and that raises.

    tmux also takes `-A` and `-a` for the largest and smallest client.
    The four policies of `window-size` already say that and keep
    saying it, so whether those are worth having at all is
    Lillecarl/pymux#225.
    """
    window = pymux.arrangement.get_active_window()
    now = pymux.size_of_the_plane(window)

    def number(name, instead):
        given = variables["<%s>" % (name,)]
        if given is None:
            return instead
        try:
            return int(given)
        except ValueError:
            raise CommandException("Expecting an integer.")

    def asked_for(name, then):
        wanted = number(name, then)
        if wanted < 1:
            raise CommandException("A window is at least one cell.")
        return wanted

    columns = asked_for("columns", now.columns) + number("right", 0)
    rows = asked_for("rows", now.rows) + number("down", 0)
    columns -= number("left", 0)
    rows -= number("up", 0)

    window.manual_size = Size(rows=max(1, rows), columns=max(1, columns))
    window.window_size = WindowSize.MANUAL


def detach_client(pymux: "Pymux", variables: _VariablesDict) -> None:
    """
    Detach this client from the session.

    The session and its panes stay with the server.
    """
    pymux.detach_client(get_app())


def confirm_before(pymux: "Pymux", variables: _VariablesDict) -> None:
    """
    Ask on the command line before running a command.
    """
    client_state = pymux.get_client_state()

    client_state.confirm_text = variables["<message>"] or ""
    client_state.confirm_command = variables["<command>"]


def open_url(pymux: "Pymux", variables: _VariablesDict) -> None:
    """
    Open a URL in the browser of a client.

    The client that gets it follows `open-url-target`, and whether it
    asks first follows `open-url-mode`. "-c" is the answer of a
    confirmation: it opens without asking again.
    """
    pymux.open_url(variables["<url>"], confirmed=bool(variables["-c"]))


def ask_the_person(
    pymux: "Pymux",
    message: str,
    command: str,
    default: str = "",
    completer=None,
) -> None:
    """
    Ask a question on the prompt, and run `command` with the answer in
    place of "%%".

    `completer` is what completes the answer, when the question knows
    what the answers are. A question that has one draws in a box, and
    the completions fill it.
    """
    client_state = pymux.get_client_state()

    client_state.prompt_text = message
    client_state.prompt_command = command
    client_state.prompt_completer = completer

    client_state.prompt_mode = True
    client_state.prompt_buffer.reset(Document(format_pymux_string(pymux, default)))

    get_app().layout.focus(client_state.prompt_buffer)
    get_app().vi_state.input_mode = InputMode.INSERT


def compose_key(pymux: "Pymux", variables: _VariablesDict) -> None:
    """
    Compose a key this keyboard cannot type, and send it to the pane.

    A laptop with no Home key, no Insert and no function row cannot
    answer a program that asks for one, and the fn chords differ per
    machine and per external keyboard. pymux is the layer in the
    middle, and it can send the key.

    The box completes the key names, so a person reads the list rather
    than remembering the spelling. "ctrl+home" and "C-Home" both read,
    and so does a sequence: "escape a" is two presses.
    Lillecarl/pymux#220.
    """
    ask_the_person(
        pymux,
        variables["<message>"] or "Send key",
        "send-keys %%",
        variables["<default>"] or "",
        # Not the prefix. It is a step of the grammar, and it is the
        # one key pymux keeps for itself; `send-prefix` sends it on.
        KeyCompleter(offer_the_prefix=False),
    )


def command_prompt(pymux: "Pymux", variables: _VariablesDict) -> None:
    """
    Open the command line, or ask a question with a command behind it.
    """
    client_state = pymux.get_client_state()

    if variables["<command>"]:
        # When a 'command' has been given.
        ask_the_person(
            pymux,
            variables["<message>"] or "(%s)" % variables["<command>"].split()[0],
            variables["<command>"],
            variables["<default>"] or "",
        )
        return

    # Show the ':' prompt.
    client_state.prompt_text = ""
    client_state.prompt_command = ""

    get_app().layout.focus(client_state.command_buffer)
    get_app().vi_state.input_mode = InputMode.INSERT


def send_prefix(pymux: "Pymux", variables: _VariablesDict) -> None:
    """
    Send the prefix on to the pane, so a program can read it.
    """
    pane = pymux.arrangement.get_active_pane()

    # The prefix is held as prompt_toolkit names, because that is what
    # binds it, and those re-spell as chords: "c-b" is "ctrl+b". So the
    # one command that sends a key pymux keeps for itself goes the same
    # road as `send-keys`, and says the same thing when a pane cannot
    # hear it. Lillecarl/pymux#237.
    for key in pymux.key_bindings_manager.prefix:
        send_a_key(pane, event_however_it_is_written(key), key)


def bind_key(pymux: "Pymux", variables: _VariablesDict) -> None:
    """
    Bind a key sequence to a command.
    -n: Not necessary to use the prefix.
    """
    key = variables["<key>"]
    command = variables["<command>"]
    arguments = variables["<arguments>"]
    needs_prefix = not variables["-n"]

    try:
        pymux.key_bindings_manager.add_custom_binding(
            key, command, arguments, needs_prefix=needs_prefix
        )
    except ValueError:
        raise CommandException("Invalid key: %r" % (key,))


def unbind_key(pymux: "Pymux", variables: _VariablesDict) -> None:
    """
    Remove a key binding.
    """
    key = variables["<key>"]
    needs_prefix = not variables["-n"]

    try:
        pymux.key_bindings_manager.remove_custom_binding(key, needs_prefix=needs_prefix)
    except ValueError:
        raise CommandException("Invalid key: %r" % (key,))


def send_keys(pymux: "Pymux", variables: _VariablesDict) -> None:
    """
    Send keys to a pane, as key names or as text.

    **The keys are optional, because `-R` needs none.** `send-keys -R`
    puts a pane back when a program has left it in a state a person
    cannot type out of, and asking for a key as well means sending one
    to a terminal that is being reset. tmux takes it on its own.
    Lillecarl/pymux#118.
    """
    if variables["-t"]:
        pane = _find_pane(pymux, variables["<target-pane>"])
        if pane is None:
            raise CommandException(
                "Can't find pane: %s" % (variables["<target-pane>"],)
            )
    else:
        pane = pymux.arrangement.get_active_pane()

    if pane.is_copying:
        raise CommandException("Cannot send keys. Pane is in copy mode.")

    if variables["-R"]:
        # Reset the terminal of this pane. (Like `reset`.)
        #
        # `screen.reset()` makes a new page, so the cells go with it.
        # There was a call to `pt_screen.reset()` before this one, and
        # prompt_toolkit's `Screen` has no such method, so `send-keys
        # -R` raised `AttributeError` and reset nothing.
        # Lillecarl/pymux#118.
        pane.screen.reset()

    keys = variables["<keys>"]

    if variables["-l"]:
        # Send keys literally. (Don't interpret key names like 'Enter'.)
        pane.process.write_input(" ".join(keys))
        return

    for key in keys:
        # Read the name into a key, in either spelling.
        try:
            event = event_however_it_is_written(key)
        except ValueError:
            # Not a known key name. Like tmux, send this argument as
            # literal text.
            pane.process.write_input(key)
            continue

        send_a_key(pane, event, key)


def send_a_key(pane, event, written: str) -> None:
    """
    Write one key to a pane, or say that the pane cannot read it.

    **The pane decides what it can read, so the pane is asked.** A
    legacy pane turns super+a into "a" and ctrl+shift+a into ctrl+a,
    and used to be sent those in silence. Nobody typing `send-keys
    super+a` means "type an a". Lillecarl/pymux#237.
    """
    try:
        pane.process.write_input(pane.screen.encode_key_event(event, exactly=True))
    except Unhearable as cannot:
        raise CommandException(_why_not(written, cannot))


def _why_not(written: str, cannot: Unhearable) -> str:
    "Why a pane could not read a key, naming what a person wrote."
    return "%s: %s" % (
        written,
        why_a_pane_cannot_read(cannot.event, cannot.lost, cannot.encoded),
    )


def copy_mode(pymux: "Pymux", variables: _VariablesDict) -> None:
    """
    Enter copy mode.
    """
    # TODO: handle '-u' (go in copy mode and page-up directly).

    pane = pymux.arrangement.get_active_pane()
    pane.enter_copy_mode()


def paste_buffer(pymux: "Pymux", variables: _VariablesDict) -> None:
    """
    Paste the buffer of the session into the pane.

    The buffer holds what copy mode copied and what a pane wrote to the
    clipboard of the user. It belongs to the session, so the command
    reads it there and not from the application of one client.
    """
    pane = pymux.arrangement.get_active_pane()
    pane.process.write_input(pane.screen.wrap_paste(pymux.clipboard.get_data().text))


def source_file(pymux: "Pymux", variables: _VariablesDict) -> None:
    """
    Read a configuration file.
    """
    filename = os.path.expanduser(variables["<filename>"])
    try:
        with open(filename, "r") as f:
            lines = list(f)
    except IOError as e:
        raise CommandException("IOError: %s" % (e,))

    # A line that fails names the file and the line it is on. Without
    # that a person reads "Invalid option: -g" and has to find which of
    # forty lines said it.
    for number, line in enumerate(lines, start=1):
        pymux.sourcing = "%s line %i" % (filename, number)
        try:
            handle_command(pymux, line)
        finally:
            pymux.sourcing = None


def set_option(pymux: "Pymux", variables: _VariablesDict, window: bool = False) -> None:
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
    name = variables["<option>"]
    value = variables["<value>"]

    if window:
        option = pymux.window_options.get(name)
    else:
        option = pymux.options.get(name)

    if option is None:
        raise CommandException("Invalid option: %s" % (name,))

    if value is None:
        answer(
            pymux,
            "%s %s" % (name, option_as_written(pymux, option, variables, window)),
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
        if window and variables.get("-g"):
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


def option_as_written(
    pymux: "Pymux", option, variables: _VariablesDict, window: bool
) -> str:
    """
    What an option holds, as a person wrote it.

    The on/off options hold booleans and a person writes on and off;
    the rest hold what they were given. A window option reads the
    window that is active, or, with `-g`, the default every new
    window starts with -- which is recorded only when somebody set
    it, so one that was never set reads as not set. `-g` says nothing
    for a session option, on the read as on the write. An option that
    holds its state somewhere else than one attribute -- the prefix
    key lives in the binding manager -- reads as not set too.
    """
    if option.attribute_name is None:
        return "not set"
    if window and variables.get("-g"):
        value = pymux.arrangement.window_defaults.get(option.attribute_name)
    else:
        holder = pymux.arrangement.get_active_window() if window else pymux
        value = getattr(holder, option.attribute_name, None)
    if value is None:
        return "not set"
    if isinstance(value, bool):
        return "on" if value else "off"
    return str(value)


def set_window_option(pymux: "Pymux", variables: _VariablesDict) -> None:
    """
    Set a window option.

    -g: say what every new window starts with, rather than changing
        this one.
    """
    set_option(pymux, variables, window=True)


def show_options(pymux: "Pymux", variables: _VariablesDict) -> None:
    """
    Read a session option, or list the ones there are.

    A name says what that one holds, as a person writes it: the same
    wording `set-option` without a value answers with. Without a
    name, one `name value` line per option, sorted. `-g` says
    nothing here, on the read as on the write -- there is one
    session per server. Lillecarl/pymux#298.
    """
    name = variables["<option>"]
    if name:
        option = ALL_OPTIONS.get(name)
        if option is None:
            raise CommandException("Unknown option: %s" % (name,))
        answer(pymux, option_as_written(pymux, option, variables, window=False))
        return

    lines = [
        "%s %s" % (key, option_as_written(pymux, option, variables, window=False))
        for key, option in sorted(ALL_OPTIONS.items())
    ]
    answer(pymux, "\n".join(lines))


def show_window_options(pymux: "Pymux", variables: _VariablesDict) -> None:
    """
    Read a window option, or list the ones there are.

    A name says what the active window holds; `-g` reads what every
    new window starts with, where a default nobody set reads as not
    set and shows as such. Without a name, one `name value` line per
    option, sorted. Lillecarl/pymux#298.
    """
    name = variables["<option>"]
    if name:
        option = ALL_WINDOW_OPTIONS.get(name)
        if option is None:
            raise CommandException("Unknown option: %s" % (name,))
        answer(pymux, option_as_written(pymux, option, variables, window=True))
        return

    lines = [
        "%s %s" % (key, option_as_written(pymux, option, variables, window=True))
        for key, option in sorted(ALL_WINDOW_OPTIONS.items())
    ]
    answer(pymux, "\n".join(lines))


def set_environment(pymux: "Pymux", variables: _VariablesDict) -> None:
    """
    Put a variable in the environment a new pane runs under.

    -g fills the global scope, which is the fallback a session unset
    of a name falls through to. A name with no value, or with `-u`,
    is an unset: the name leaves the environment of a new pane, and
    with `-g` it leaves the global scope itself. A program that
    already runs never sees any of this -- the environment is read
    once, at exec, so only panes spawned after the set carry it.
    Lillecarl/pymux#270.
    """
    name, value = variables["<name>"], variables["<value>"]
    if not name or "=" in name:
        raise CommandException("Invalid variable name: %r" % (name,))

    scope = pymux.global_environment if variables["-g"] else pymux.session_environment
    if variables["-u"] or value is None:
        scope[name] = None
    else:
        scope[name] = value


def show_environment(pymux: "Pymux", variables: _VariablesDict) -> None:
    """
    Read the environment a new pane runs under.

    Without a name, one `NAME=value` line per variable, the form
    `eval $(pymux show-environment -s)` wants; `-s` escapes the
    values for the shell. A name that no scope set and the server
    does not hold reads as an error. With `-g`, only what the global
    scope holds: an unset prints as `-NAME`, and nothing that the
    server holds on its own shows. Lillecarl/pymux#270.
    """
    name = variables["<name>"]
    escaped = variables["-s"]

    if variables["-g"]:
        scope = dict(pymux.global_environment)
        if name:
            value = scope.get(name)
            if value is None and name not in scope:
                raise CommandException("Can't find variable: %s" % (name,))
            answer(
                pymux,
                "-%s" % (name,)
                if value is None
                else "%s=%s" % (name, shlex.quote(value) if escaped else value),
            )
            return
        lines = [
            "-%s" % (key,)
            if value is None
            else "%s=%s" % (key, shlex.quote(value) if escaped else value)
            for key, value in sorted(scope.items())
        ]
        answer(pymux, "\n".join(lines))
        return

    merged = pymux.pane_environment()
    if name:
        if name not in merged:
            raise CommandException("Can't find variable: %s" % (name,))
        answer(
            pymux,
            "%s=%s" % (name, shlex.quote(merged[name]) if escaped else merged[name]),
        )
        return

    lines = [
        "%s=%s" % (key, shlex.quote(value) if escaped else value)
        for key, value in sorted(merged.items())
    ]
    answer(pymux, "\n".join(lines))


def display_panes(pymux: "Pymux", variables: _VariablesDict) -> None:
    "Display the pane numbers."
    pymux.display_pane_numbers = True


def choose_window(pymux: "Pymux", variables: _VariablesDict) -> None:
    """
    Show the windows of the session, to choose from.

    tmux spells the view choose-tree, and prefix w opens it there. A
    pymux server holds one session, so the tree has one root: the
    chooser lists the windows, `/` searches the names, and Enter
    switches to one. With a command as its argument, the chooser
    runs that command on the chosen window instead of switching to
    it, with `%%` in the command standing for the target of the
    window, the way the command-prompt does. Lillecarl/pymux#295.

    The command line has no view to open a chooser on, so an asker
    that reads stdout gets nothing -- the same shape as the pop-ups.
    Lillecarl/pymux#272.
    """
    if pymux.command_output is not None:
        return
    pymux.get_client_state().layout_manager.display_chooser(
        template=variables["<command>"]
    )


def display_message(pymux: "Pymux", variables: _VariablesDict) -> None:
    '''
    Show a message on the status line.

    With `-p`, print the message, formatted, instead: the way a script
    asks the session a question and reads the answer. tmux spells it
    the same. Lillecarl/pymux#289.
    '''
    message = variables["<message>"]
    if variables["-p"]:
        answer(pymux, format_pymux_string(pymux, message))
        return

    client_state = pymux.get_client_state()
    client_state.message = message


def clear_history(pymux: "Pymux", variables: _VariablesDict) -> None:
    "Clear the scrollback of the pane."
    pane = pymux.arrangement.get_active_pane()

    if pane.is_copying:
        raise CommandException("Not available in copy mode")
    else:
        pane.screen.clear_history()


def answer(pymux: "Pymux", text: str) -> None:
    """
    Answer a question on the channel the asker reads.

    A command that arrives over the command line gets stdout, which
    is what `command_output` says. A person typing at the prompt of a
    pane gets the message line, because there is no stdout to write
    to. Lillecarl/pymux#289.
    """
    if pymux.command_output is not None:
        pymux.print_command_line(text)
    else:
        pymux.get_client_state().message = text


def show_listing(pymux: "Pymux", title: str, text: str) -> None:
    """
    A listing, to the person who asked for it.

    A person in a pane asked for a view and gets the popup. The
    command line has no view to show a listing in, and used to lose
    it there: the popup went to the client that ran the command, and
    there is no client to ask for when the command came over the
    command line -- `get_client_state` has no app to answer with,
    which is one reason the listing was silence and not a fault. That
    asker reads stdout, so the listing is printed there, which is
    what `command_output` says: the same question
    `print_command_line` asks. tmux prints a listing to stdout,
    always. Lillecarl/pymux#288.
    """
    if pymux.command_output is not None:
        pymux.print_command_line(text)
    else:
        pymux.get_client_state().layout_manager.display_popup(title, text)


def list_keys(pymux: "Pymux", variables: _VariablesDict) -> None:
    """
    Display all configured key bindings.
    """
    # Create help string.
    result = []

    for k, custom_binding in pymux.key_bindings_manager.custom_bindings.items():
        needs_prefix, _keys = k

        result.append(
            "bind-key %3s %-10s %s %s"
            % (
                ("-n" if needs_prefix else ""),
                custom_binding.written,
                custom_binding.command,
                " ".join(map(wrap_argument, custom_binding.arguments)),
            )
        )

    show_listing(pymux, "list-keys", "\n".join(sorted(result)))


def list_panes(pymux: "Pymux", variables: _VariablesDict) -> None:
    """
    Display a list of all the panes.

    Without `-F`, this displays the pane overview as a pop-up in the user
    interface. With `-F`, the formatted pane information is printed to the
    output of the pymux command line. (Like tmux.)
    """
    if variables["-t"]:
        window = _find_window(pymux, variables["<target-pane>"].rsplit(".", 1)[0])
        if window is None:
            raise CommandException(
                "Can't find window: %s" % (variables["<target-pane>"],)
            )
        windows: List["Window"] = [window]
    elif variables["-a"]:
        windows = list(pymux.arrangement.windows)
    else:
        windows = [pymux.arrangement.get_active_window()]

    active_pane = windows[0].active_pane

    if variables["-F"]:
        # Print one line for every pane.
        format_str = variables["<format>"] or "#{pane_id}"
        lines = [
            format_pymux_string(pymux, format_str, window=w, pane=p)
            for w in windows
            for p in w.panes
        ]
        pymux.print_command_line("\n".join(lines))
    else:
        result = []

        for i, p in enumerate(windows[0].panes):
            process = p.process

            result.append(
                "%i: [%sx%s] [history %s/%s] %s"
                % (
                    i,
                    process.sx,
                    process.sy,
                    min(pymux.history_limit, p.screen.line_offset + process.sy),
                    pymux.history_limit,
                    ("(active)" if p == active_pane else ""),
                )
            )

        # The list-keys title rode along when this branch was
        # written, and the overview of panes said list-keys.
        # Lillecarl/pymux#288.
        show_listing(pymux, "list-panes", "\n".join(sorted(result)))


def list_windows(pymux: "Pymux", variables: _VariablesDict) -> None:
    """
    List the windows of the session.

    With `-F`, the formatted window information is printed to the
    output of the pymux command line. (Like tmux.)
    """
    if variables["-F"]:
        format_str = variables["<format>"] or "#{window_id}"
        lines = [
            format_pymux_string(pymux, format_str, window=w, pane=w.active_pane)
            for w in pymux.arrangement.windows
        ]
        pymux.print_command_line("\n".join(lines))
    else:
        w = pymux.arrangement.get_active_window()
        result = []
        for i, window in enumerate(pymux.arrangement.windows):
            result.append(
                "%i %s%s [%sx%s]"
                % (
                    window.index,
                    window.name,
                    " (active)" if window == w else "",
                    w.active_pane.process.sx,
                    w.active_pane.process.sy,
                )
            )
        show_listing(pymux, "list-windows", "\n".join(result))


def list_sessions(pymux: "Pymux", variables: _VariablesDict) -> None:
    """
    List the session of this server.

    With `-F`, the formatted session information is printed to the
    output of the pymux command line. (Like tmux.)
    """
    if variables["-F"]:
        format_str = variables["<format>"]
        window = pymux.arrangement.get_active_window()
        line = format_pymux_string(
            pymux, format_str, window=window, pane=window.active_pane
        )
        pymux.print_command_line(line)
    else:
        show_listing(
            pymux, "list-sessions", format_pymux_string(pymux, "#{session_name}")
        )


def has_session(pymux: "Pymux", variables: _VariablesDict) -> None:
    """
    Check whether the session exists.

    A session that is not there answers a non-zero exit code on the
    command line, which is what a script reads.
    """
    target = variables["<target-session>"] or ""
    if not _pane_matches_session_name(pymux, target):
        raise CommandException("can't find session: %s" % (target,))


def new_session(pymux: "Pymux", variables: _VariablesDict) -> None:
    """
    Create a new session.

    Pymux has one session per server. The session is created when the server
    starts, so this command can only fail with a duplicate session error,
    like tmux does when the session already exists.
    """
    session_name = variables["<session-name>"]

    if session_name and session_name != pymux.session_name:
        raise CommandException("duplicate session: %s" % (session_name,))

    if variables["-P"]:
        window = pymux.arrangement.get_active_window()
        _print_object_format(
            pymux, variables["<format>"], window=window, pane=window.active_pane
        )


def kill_session(pymux: "Pymux", variables: _VariablesDict) -> None:
    """
    Kill this session, and the server that runs it.

    This is the way the last `tmux kill-session` ends its server.
    """
    pymux.stop()


def kill_server(pymux: "Pymux", variables: _VariablesDict) -> None:
    """
    Kill the server, and the session that runs in it.

    Pymux has one session per server, so this is the same as
    `kill-session`.
    """
    pymux.stop()


def dump_stacks(pymux: "Pymux", variables: _VariablesDict) -> None:
    """
    Write down what this server is doing now, and say where.

    Every thread, every asyncio task and what each one waits for.
    `pymux/introspect.py` says why a server answers for itself, and what
    `SIGUSR1` gives instead when the loop is too wedged to read this.
    """
    path = introspect.write_dump(pymux)
    pymux.print_command_line(str(path))
    pymux.show_message("Wrote a dump to %s" % (path,))


def counters(pymux: "Pymux", variables: _VariablesDict) -> None:
    """
    Say what this server has done, and how often.

    The half a stack cannot give: a stack says where the server is in
    one instant, and this says what it has been doing for an hour.
    """
    show_listing(pymux, "counters", introspect.counters(pymux))


def profile(pymux: "Pymux", variables: _VariablesDict) -> None:
    """
    Watch this server for a few seconds, and write down where its time went.

    **Where the time of an await goes, and not only of a call.**
    pyinstrument attributes the time of a coroutine that is waiting to
    the frame that awaits, so a server that spends its life in
    `epoll_wait` still says which work the waiting was for.
    """
    try:
        seconds = float(variables["<seconds>"] or introspect.HOW_LONG_TO_WATCH)
    except ValueError:
        raise CommandException(
            "Not a number of seconds: %r" % (variables["<seconds>"],)
        )

    try:
        path = introspect.start_watching(pymux, seconds)
    except ImportError:
        raise CommandException(
            "pyinstrument is not installed, so this server cannot profile itself."
        )

    pymux.print_command_line(str(path))
    pymux.show_message(
        "Watching for %.1f seconds. It lands in %s" % (seconds, path.name)
    )


def display_popup(pymux: "Pymux", variables: _VariablesDict) -> None:
    """
    Open an overlay pane in the middle of the screen.

    It runs the given program, or the default shell, and closes itself
    when that finishes. `-w` and `-h` take a number of cells or a share
    of the screen, like "80" or "60%". `-T` names the title bar.

    A session has one overlay at a time, so a second call replaces the
    first. `-E` is accepted for the tmux command line and changes
    nothing: an overlay of pymux always closes when its program ends.
    """
    pymux.display_overlay(
        command=variables["<executable>"],
        width=variables["<width>"],
        height=variables["<height>"],
        title=variables["<title>"],
    )


def close_popup(pymux: "Pymux", variables: _VariablesDict) -> None:
    """
    Close the overlay pane, and kill what runs in it.
    """
    pymux.close_overlay()


def capture_pane(pymux: "Pymux", variables: _VariablesDict) -> None:
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
    if variables["-t"]:
        pane = _find_pane(pymux, variables["<target-pane>"])
        if pane is None:
            raise CommandException(
                "Can't find pane: %s" % (variables["<target-pane>"],)
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

        if variables["-J"]:
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
        start_str = variables["<start>"]
        end_str = variables["<end>"]

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

    if variables["-p"]:
        pymux.print_command_line(text)
    else:
        show_listing(pymux, "capture-pane", text)


def show_buffer(pymux: "Pymux", variables: _VariablesDict) -> None:
    """
    Display the clipboard content.
    """
    text = get_app().clipboard.get_data().text
    pymux.get_client_state().layout_manager.display_popup("show-buffer", text)


def _print_object_format(
    pymux: "Pymux",
    format_str: str | None,
    window: "Window",
    pane: "Pane",
) -> None:
    """
    Print the information of a newly created object. (Like `tmux
    new-window -P`.)
    """
    if format_str is None:
        format_str = "#{session_name}:#{window_index}.#{pane_index}"
    pymux.print_command_line(
        format_pymux_string(pymux, format_str, window=window, pane=pane)
    )


#
# What each command takes.
#
# One declarer per command, with argparse's own API. The tree is built
# from them once, below, and it is what argparse parses with, what the
# shell completes through, and what the command bar of a client reads.
#

@declarer
def _declare_break_pane(subparsers: Any) -> None:
    parser = _command(subparsers, break_pane)
    parser.add_argument("-d", action="store_true", help="Leave the new window unfocused.")


@declarer
def _declare_select_pane(subparsers: Any) -> None:
    parser = _command(subparsers, select_pane)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("-L", action="store_true", help="Focus the pane to the left.")
    group.add_argument("-R", action="store_true", help="Focus the pane to the right.")
    group.add_argument("-U", action="store_true", help="Focus the pane above.")
    group.add_argument("-D", action="store_true", help="Focus the pane below.")
    group.add_argument("-l", action="store_true", help="Rotate the panes of the window once.")
    group.add_argument("-t", metavar="<pane-id>", help="The pane to focus.")


@declarer
def _declare_select_window(subparsers: Any) -> None:
    parser = _command(subparsers, select_window)
    parser.add_argument("-t", metavar="<target-window>", required=True, help="The window to focus.")


@declarer
def _declare_move_window(subparsers: Any) -> None:
    parser = _command(subparsers, move_window)
    parser.add_argument("-t", metavar="<dst-window>", required=True, help="The index to move to.")


@declarer
def _declare_swap_window(subparsers: Any) -> None:
    parser = _command(subparsers, swap_window)
    parser.add_argument(
        "-d",
        action="store_true",
        help="Keep the active window active. The windows trade places either way.",
    )
    parser.add_argument("-t", metavar="<dst-window>", required=True, help="The index to swap with. `+1` and `-1` count from the active window.")


@declarer
def _declare_rotate_window(subparsers: Any) -> None:
    parser = _command(subparsers, rotate_window)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("-D", action="store_true", help="Rotate the other way.")
    group.add_argument("-U", action="store_true")


@declarer
def _declare_swap_pane(subparsers: Any) -> None:
    parser = _command(subparsers, swap_pane)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("-D", action="store_true")
    group.add_argument("-U", action="store_true", help="Swap with the pane below.")


@declarer
def _declare_kill_pane(subparsers: Any) -> None:
    parser = _command(subparsers, kill_pane)
    parser.add_argument("-t", metavar="<target-pane>", help="The pane to kill.")


@declarer
def _declare_kill_window(subparsers: Any) -> None:
    parser = _command(subparsers, kill_window)
    parser.add_argument("-t", metavar="<target-window>", help="The window to kill.")


@declarer
def _declare_suspend_client(subparsers: Any) -> None:
    _command(subparsers, suspend_client)


@declarer
def _declare_clock_mode(subparsers: Any) -> None:
    _command(subparsers, clock_mode)


@declarer
def _declare_last_pane(subparsers: Any) -> None:
    _command(subparsers, last_pane)


@declarer
def _declare_next_layout(subparsers: Any) -> None:
    _command(subparsers, next_layout)


@declarer
def _declare_previous_layout(subparsers: Any) -> None:
    _command(subparsers, previous_layout)


@declarer
def _declare_new_window(subparsers: Any) -> None:
    parser = _command(subparsers, new_window)
    parser.add_argument("-a", action="store_true", help="After the target window.")
    parser.add_argument("-b", action="store_true", help="Before the target window.")
    parser.add_argument("-t", metavar="<target-window>", help="The window to sit next to, or the index to create at.")
    parser.add_argument("-n", metavar="<name>", help="The name of the window.")
    parser.add_argument("-c", metavar="<start-directory>", help="Where the program starts.")
    parser.add_argument("-d", action="store_true", help="Leave the new window unfocused.")
    parser.add_argument("-P", action="store_true", help="Print information about the new window.")
    parser.add_argument("-F", metavar="<format>", help="The format to print with -P.")
    parser.add_argument("executable", nargs="?", metavar="<executable>")


@declarer
def _declare_split_window(subparsers: Any) -> None:
    parser = _command(subparsers, split_window)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("-v", action="store_true", help="Split top over bottom.")
    group.add_argument("-h", action="store_true", help="Split side by side.")
    parser.add_argument("-t", metavar="<target-window>", help="The window to split.")
    parser.add_argument("-c", metavar="<start-directory>", help="Where the program starts.")
    parser.add_argument("-d", action="store_true", help="Leave the new pane unfocused.")
    parser.add_argument("-P", action="store_true", help="Print information about the new pane.")
    parser.add_argument("-F", metavar="<format>", help="The format to print with -P.")
    parser.add_argument("executable", nargs="?", metavar="<executable>")


@declarer
def _declare_last_window(subparsers: Any) -> None:
    _command(subparsers, _, name="last-window")


@declarer
def _declare_next_window(subparsers: Any) -> None:
    _command(subparsers, next_window)


@declarer
def _declare_previous_window(subparsers: Any) -> None:
    _command(subparsers, previous_window)


@declarer
def _declare_select_layout(subparsers: Any) -> None:
    parser = _command(subparsers, select_layout)
    parser.add_argument("layout_type", metavar="<layout-type>", help="The layout to arrange the panes in.")


@declarer
def _declare_switch_column_width(subparsers: Any) -> None:
    parser = _command(subparsers, switch_column_width)
    parser.add_argument("-p", action="store_true", help="The previous width instead.")


@declarer
def _declare_move_column(subparsers: Any) -> None:
    parser = _command(subparsers, move_column)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("-L", action="store_true", help="Move the column one place to the left.")
    group.add_argument("-R", action="store_true", help="Move the column one place to the right.")


@declarer
def _declare_consume_or_expel(subparsers: Any) -> None:
    parser = _command(subparsers, consume_or_expel)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("-L", action="store_true", help="Move the pane into the column to the left.")
    group.add_argument("-R", action="store_true", help="Move the pane into the column to the right.")


@declarer
def _declare_rename_window(subparsers: Any) -> None:
    parser = _command(subparsers, rename_window)
    parser.add_argument("name", metavar="<name>", help="The new name of the window.")


@declarer
def _declare_rename_pane(subparsers: Any) -> None:
    parser = _command(subparsers, rename_pane)
    parser.add_argument("name", metavar="<name>", help="The new name of the pane.")


@declarer
def _declare_rename_session(subparsers: Any) -> None:
    parser = _command(subparsers, rename_session)
    parser.add_argument("name", metavar="<name>", help="The new name of the session.")


@declarer
def _declare_resize_pane(subparsers: Any) -> None:
    parser = _command(subparsers, resize_pane)
    parser.add_argument("-L", metavar="<left>", help="That many columns narrower.")
    parser.add_argument("-U", metavar="<up>", help="That many rows shorter.")
    parser.add_argument("-D", metavar="<down>", help="That many rows taller.")
    parser.add_argument("-R", metavar="<right>", help="That many columns wider.")
    parser.add_argument("-Z", action="store_true", help="Zoom the pane in or out.")


@declarer
def _declare_resize_window(subparsers: Any) -> None:
    parser = _command(subparsers, resize_window)
    parser.add_argument("-x", metavar="<columns>", help="How many columns the window is.")
    parser.add_argument("-y", metavar="<rows>", help="How many rows the window is.")
    parser.add_argument("-L", metavar="<left>", help="That many columns narrower.")
    parser.add_argument("-U", metavar="<up>", help="That many rows shorter.")
    parser.add_argument("-D", metavar="<down>", help="That many rows taller.")
    parser.add_argument("-R", metavar="<right>", help="That many columns wider.")


@declarer
def _declare_detach_client(subparsers: Any) -> None:
    _command(subparsers, detach_client)


@declarer
def _declare_confirm_before(subparsers: Any) -> None:
    parser = _command(subparsers, confirm_before)
    parser.add_argument("-p", metavar="<message>", help="The question to ask.")
    parser.add_argument("command", metavar="<command>", help="The command to run when the answer is yes.")


@declarer
def _declare_open_url(subparsers: Any) -> None:
    parser = _command(subparsers, open_url)
    parser.add_argument("-c", action="store_true", help="Open without asking again.")
    parser.add_argument("url", metavar="<url>")


@declarer
def _declare_compose_key(subparsers: Any) -> None:
    parser = _command(subparsers, compose_key)
    parser.add_argument("-p", metavar="<message>", help="The question to ask.")
    parser.add_argument("-I", metavar="<default>", help="What the answer starts with.")


@declarer
def _declare_command_prompt(subparsers: Any) -> None:
    parser = _command(subparsers, command_prompt)
    parser.add_argument("-p", metavar="<message>", help="The question to ask.")
    parser.add_argument("-I", metavar="<default>", help="What the answer starts with.")
    parser.add_argument("command", nargs="?", metavar="<command>")


@declarer
def _declare_send_prefix(subparsers: Any) -> None:
    _command(subparsers, send_prefix)


@declarer
def _declare_bind_key(subparsers: Any) -> None:
    parser = _command(subparsers, bind_key)
    parser.add_argument("-n", action="store_true", help="Bind without the prefix.")
    parser.add_argument("key", metavar="<key>", help="The key to bind.")
    # Everything from the bound command on is a remainder, so an
    # option of the bound command is never read as an option of
    # bind-key. The wrapper splits it into the command and its
    # arguments; the metavar only says what it is in a usage line.
    parser.add_argument("arguments", nargs=argparse.REMAINDER, metavar="<arguments>")


@declarer
def _declare_unbind_key(subparsers: Any) -> None:
    parser = _command(subparsers, unbind_key)
    parser.add_argument("-n", action="store_true", help="Remove a binding that needs no prefix.")
    parser.add_argument("key", metavar="<key>")


@declarer
def _declare_send_keys(subparsers: Any) -> None:
    parser = _command(subparsers, send_keys)
    parser.add_argument("-t", metavar="<target-pane>", help="The pane to send to.")
    parser.add_argument("-l", action="store_true", help="Send the keys as text, not key names.")
    parser.add_argument("-R", action="store_true", help="Reset the terminal of the pane first.")
    parser.add_argument("keys", nargs=argparse.REMAINDER, metavar="<keys>")


@declarer
def _declare_copy_mode(subparsers: Any) -> None:
    parser = _command(subparsers, copy_mode)
    parser.add_argument("-u", action="store_true", help="Accepted for tmux. Pymux does not page up yet.")


@declarer
def _declare_paste_buffer(subparsers: Any) -> None:
    _command(subparsers, paste_buffer)


@declarer
def _declare_source_file(subparsers: Any) -> None:
    parser = _command(subparsers, source_file)
    parser.add_argument("filename", metavar="<filename>", help="The configuration file to read.")


@declarer
def _declare_set_option(subparsers: Any) -> None:
    parser = _command(subparsers, set_option)
    parser.add_argument("-g", action="store_true", help="For a window option: what every new window starts with.")
    parser.add_argument("option", metavar="<option>")
    parser.add_argument("value", metavar="<value>", nargs="?")


@declarer
def _declare_set_window_option(subparsers: Any) -> None:
    parser = _command(subparsers, set_window_option)
    parser.add_argument("-g", action="store_true", help="What every new window starts with.")
    parser.add_argument("option", metavar="<option>")
    parser.add_argument("value", metavar="<value>", nargs="?")


@declarer
def _declare_set_environment(subparsers: Any) -> None:
    parser = _command(subparsers, set_environment)
    parser.add_argument("-g", action="store_true", help="Fill the global scope, which new sessions start from.")
    parser.add_argument("-u", action="store_true", help="Remove the variable from the scope.")
    parser.add_argument("name", metavar="<name>")
    parser.add_argument("value", metavar="<value>", nargs="?")


@declarer
def _declare_show_options(subparsers: Any) -> None:
    parser = _command(subparsers, show_options)
    parser.add_argument("-g", action="store_true", help="Accepted for tmux and changes nothing: there is one session per server.")
    parser.add_argument("option", metavar="<option>", nargs="?")


@declarer
def _declare_show_window_options(subparsers: Any) -> None:
    parser = _command(subparsers, show_window_options)
    parser.add_argument("-g", action="store_true", help="Read what every new window starts with.")
    parser.add_argument("option", metavar="<option>", nargs="?")


@declarer
def _declare_list_commands(subparsers: Any) -> None:
    _command(subparsers, list_commands)


@declarer
def _declare_show_environment(subparsers: Any) -> None:
    parser = _command(subparsers, show_environment)
    parser.add_argument("-g", action="store_true", help="Read the global scope rather than what a new pane runs under.")
    parser.add_argument("-s", action="store_true", help="Escape the values for the shell.")
    parser.add_argument("name", metavar="<name>", nargs="?")


@declarer
def _declare_display_panes(subparsers: Any) -> None:
    _command(subparsers, display_panes)


@declarer
def _declare_choose_window(subparsers: Any) -> None:
    parser = _command(subparsers, choose_window)
    parser.add_argument(
        "command",
        metavar="<command>",
        nargs="?",
        help="Run this command on the chosen window instead of switching to it. `%%` stands for the target of the window.",
    )


@declarer
def _declare_display_message(subparsers: Any) -> None:
    parser = _command(subparsers, display_message)
    parser.add_argument("-p", action="store_true", help="Print the message instead of showing it.")
    parser.add_argument("message", metavar="<message>")


def get(pymux: "Pymux", variables: _VariablesDict) -> None:
    """
    Read an id, by the thing it names.

    `get paneid` says the id of the pane a target names, or of the
    active one. The id is the handle every targeted command accepts
    as `%<id>`, and unlike the window and pane indexes it does not
    move when panes open and close. Lillecarl/pymux#291.
    """
    what = variables["<what>"]
    if what != "paneid":
        raise CommandException("Unknown thing to get: %s" % (what,))

    if variables["-t"]:
        pane = _find_pane(pymux, variables["<target-pane>"])
        if pane is None:
            raise CommandException(
                "Can't find pane: %s" % (variables["<target-pane>"],)
            )
    else:
        pane = pymux.arrangement.get_active_pane()

    answer(pymux, str(pane.pane_id))


@declarer
def _declare_get(subparsers: Any) -> None:
    parser = _command(subparsers, get)
    parser.add_argument(
        "-t",
        metavar="<target-pane>",
        help="The pane to read, rather than the active one.",
    )
    parser.add_argument("what", metavar="<what>")


@declarer
def _declare_clear_history(subparsers: Any) -> None:
    _command(subparsers, clear_history)


@declarer
def _declare_list_keys(subparsers: Any) -> None:
    _command(subparsers, list_keys)


@declarer
def _declare_list_panes(subparsers: Any) -> None:
    parser = _command(subparsers, list_panes)
    parser.add_argument("-a", action="store_true", help="The panes of every window, not of the active one.")
    parser.add_argument("-t", metavar="<target-pane>", help="The pane whose window to list.")
    parser.add_argument("-F", metavar="<format>", help="Print this format for every pane.")


@declarer
def _declare_list_windows(subparsers: Any) -> None:
    parser = _command(subparsers, list_windows)
    parser.add_argument("-a", action="store_true", help="Every window, not only of the session.")
    parser.add_argument("-t", metavar="<target-window>", help="The window to list.")
    parser.add_argument("-F", metavar="<format>", help="Print this format for every window.")


@declarer
def _declare_list_sessions(subparsers: Any) -> None:
    parser = _command(subparsers, list_sessions, aliases=("ls",))
    parser.add_argument("-a", action="store_true", help="Accepted for tmux. Pymux has one session per server.")
    parser.add_argument("-F", metavar="<format>", help="Print this format for the session.")


@declarer
def _declare_has_session(subparsers: Any) -> None:
    parser = _command(subparsers, has_session)
    parser.add_argument("-t", metavar="<target-session>", help="The session to look for.")


@declarer
def _declare_new_session(subparsers: Any) -> None:
    parser = _command(subparsers, new_session)
    parser.add_argument("-s", metavar="<session-name>", help="The name of the session.")
    parser.add_argument("-d", action="store_true", help="Do not attach.")
    parser.add_argument("-P", action="store_true", help="Print information about the session.")
    parser.add_argument("-F", metavar="<format>", help="The format to print with -P.")


@declarer
def _declare_kill_session(subparsers: Any) -> None:
    _command(subparsers, kill_session)


@declarer
def _declare_kill_server(subparsers: Any) -> None:
    _command(subparsers, kill_server)


@declarer
def _declare_dump_stacks(subparsers: Any) -> None:
    _command(subparsers, dump_stacks)


@declarer
def _declare_counters(subparsers: Any) -> None:
    _command(subparsers, counters)


@declarer
def _declare_profile(subparsers: Any) -> None:
    parser = _command(subparsers, profile)
    parser.add_argument("seconds", nargs="?", metavar="<seconds>", help="How long to watch, in seconds.")


@declarer
def _declare_display_popup(subparsers: Any) -> None:
    parser = _command(subparsers, display_popup)
    parser.add_argument("-E", action="store_true", help="Accepted for tmux. The overlay always closes when its program ends.")
    parser.add_argument("-w", metavar="<width>", help="How many cells wide, or a share like '60%%'.")
    parser.add_argument("-h", metavar="<height>", help="How many cells high, or a share like '60%%'.")
    parser.add_argument("-T", metavar="<title>", help="The name on the title bar.")
    parser.add_argument("executable", nargs="?", metavar="<executable>", help="The program to run.")


@declarer
def _declare_close_popup(subparsers: Any) -> None:
    _command(subparsers, close_popup)


@declarer
def _declare_capture_pane(subparsers: Any) -> None:
    parser = _command(subparsers, capture_pane)
    parser.add_argument("-p", action="store_true", help="Print to the output of the command line, not a pop-up.")
    parser.add_argument("-J", action="store_true", help="Join the pieces a wrapped line was cut into.")
    parser.add_argument("-t", metavar="<target-pane>", help="The pane to capture.")
    parser.add_argument("-S", metavar="<start>", help="The first line. 0 is the top of the pane, negative is history.")
    parser.add_argument("-E", metavar="<end>", help="The last line.")


@declarer
def _declare_show_buffer(subparsers: Any) -> None:
    _command(subparsers, show_buffer)


#
# The tree, and the registries read from it.
#


def _build_the_tree() -> Any:
    "Every command of the server, under one root."
    parser = argparse.ArgumentParser(prog="pymux", add_help=False, allow_abbrev=False)
    subparsers = parser.add_subparsers(metavar="COMMAND", parser_class=_Parser)
    add_commands_to(subparsers)
    return parser, subparsers


_TREE, _SUBPARSERS = _build_the_tree()


def _options(parser: argparse.ArgumentParser) -> str:
    "What the usage line says after the name of the command."
    text = parser.format_usage()
    prefix = "usage: %s " % (parser.prog,)
    return text[len(prefix) :].strip() if text.startswith(prefix) else text.strip()


def _wrapper(name: str, parser: argparse.ArgumentParser) -> _PymuxHandler:
    def command_wrapper(pymux: "Pymux", arguments: _VariablesList) -> None:
        try:
            namespace = parser.parse_args(list(arguments))
        except _BadLine as e:
            # The complaint names the part that is wrong, and the
            # usage line beside it names the command: a line of a
            # configuration file that fails is read far from here.
            raise CommandException(
                "%s (%s)" % (e.message, parser.format_usage().strip())
            )

        variables = _variables_of(parser, namespace)

        if name == "bind-key":
            # The command that is bound, and everything it takes. A
            # `--` the caller wrote is dropped: it separates, and
            # neither side is an option of bind-key.
            rest = variables["arguments"] or []
            if rest and rest[0] == "--":
                rest = rest[1:]
            variables["<command>"] = rest[0] if rest else None
            variables["<arguments>"] = rest[1:] if rest else []

        # Call handler.
        namespace._handler(pymux, variables)

        # Invalidate all clients, not just the current CLI.
        pymux.invalidate(Woke.COMMAND_RAN % name)

    return command_wrapper


for _name, _parser in _SUBPARSERS.choices.items():
    COMMANDS_TO_HANDLERS[_name] = _wrapper(_name, _parser)
    COMMANDS_TO_PARSERS[_name] = _parser
    _handler = _parser.get_default("_handler")
    COMMANDS_TO_DESCRIPTIONS[_name] = (inspect.getdoc(_handler) or "").partition("\n")[0]
    COMMANDS_TO_HELP[_name] = _options(_parser)
    COMMANDS_TO_OPTION_FLAGS[_name] = [
        option
        for action in _parser._actions
        for option in action.option_strings
        if len(option) == 2
    ]


# Check whether all aliases point to real commands.
for k in ALIASES.values():
    assert k in COMMANDS_TO_HANDLERS
