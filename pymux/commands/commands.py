import os
import re
import shlex
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Dict,
    List,
    Optional,
    TypeVar,
)

import docopt
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
    an_event_however_it_is_written,
    why_a_pane_cannot_read,
)
from pyte.keys import Unhearable
from pymux.layout import (
    focus_down,
    focus_left,
    focus_right,
    focus_up,
    the_pane_resizes,
)
from pymux.log import logger
from pymux.options import SetOptionError

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
_PymuxDictHandler = Callable[["Pymux", _VariablesDict], None]

# Global mapping of pymux commands to their handlers.
COMMANDS_TO_HANDLERS: Dict[str, _PymuxHandler] = {}

COMMANDS_TO_HELP: Dict[str, str] = {}
COMMANDS_TO_OPTION_FLAGS: Dict[str, List[str]] = {}


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


_F = TypeVar("_F", bound=_PymuxDictHandler)


def _usage_doc(name: str, options: str) -> str:
    """
    Build the docopt document for a command.

    Every `-x <placeholder>` option is also declared in an `Options:` section.
    (docopt-ng only accepts glued options like `-F#{...}` when the option is
    declared to take an argument there.)
    """
    usage = (
        "Usage:\n    %s %s" % (name, options) if options else "Usage:\n    %s" % name
    )

    declarations = []
    for flag, placeholder in re.findall(r"-([a-zA-Z0-9]) (<[^>]+>)", options):
        declarations.append("    -%s %s" % (flag, placeholder))

    if declarations:
        usage += "\n\nOptions:\n" + "\n".join(declarations)

    return usage


def cmd(name: str, options: str = "") -> Callable[[_F], _F]:
    """
    Decorator for all commands.

    Commands will receive (pymux, variables) as input.
    Commands can raise CommandException.
    """
    usage = _usage_doc(name, options)
    value_options = re.findall(r"-([a-zA-Z0-9]) (<[^>]+>)", options)
    value_flags = {flag for flag, _ in value_options}

    # Validate options.
    if options:
        try:
            docopt.docopt(usage, [])
        except SystemExit:
            pass

    @staticmethod
    def _normalize_arguments(arguments: _VariablesList) -> _VariablesList:
        """
        Keep only the last occurrence of every option. (Like tmux, which
        accepts repeated options. libtmux sometimes sends an option twice:
        glued and as a separate argument.)
        """
        result: _VariablesList = []
        i = 0
        count = len(arguments)
        while i < count:
            arg = arguments[i]
            if arg.startswith("-") and len(arg) >= 2 and not arg.startswith("--"):
                flag = arg[1:2]
                rest = arg[2:]
                takes_value = flag in value_flags
                has_inline_value = bool(rest)

                # Is this option repeated later on?
                repeated = any(
                    a.startswith("-") and not a.startswith("--") and a[1:2] == flag
                    for a in arguments[i + 1 :]
                )
                if repeated:
                    # Skip this occurrence, and its separate value if any.
                    i += 1
                    if (
                        takes_value
                        and not has_inline_value
                        and i < count
                        and not arguments[i].startswith("-")
                    ):
                        i += 1
                    continue

            result.append(arg)
            i += 1
        return result

    def decorator(func: _F) -> _F:
        def command_wrapper(pymux: "Pymux", arguments: _VariablesList) -> None:
            arguments = _normalize_arguments(arguments)

            # Hack to make the 'bind-key' option work.
            # (bind-key expects a variable number of arguments.)
            if name == "bind-key" and "--" not in arguments:
                # Insert a double dash after the first non-option.
                for i, p in enumerate(arguments):
                    if not p.startswith("-"):
                        arguments.insert(i + 1, "--")
                        break

            # Parse options.
            try:
                received_options: Dict[str, str] = docopt.docopt(
                    usage,
                    arguments,
                    default_help=False,
                )  # Don't interpret the '-h' option as help.
            except SystemExit:
                raise CommandException("Usage: %s %s" % (name, options))

            # When an option takes an argument, docopt-ng reports it under
            # the short name (e.g. '-t') and omits the `<placeholder>` name.
            # Expose it under both names, so that the handlers can use the
            # `<placeholder>` name, whether or not the option was given.
            for flag, placeholder in value_options:
                received_options[placeholder] = received_options.get("-" + flag)

            # Call handler.
            func(pymux, received_options)

            # Invalidate all clients, not just the current CLI.
            pymux.invalidate(Woke.A_COMMAND_RAN % name)

        COMMANDS_TO_HANDLERS[name] = command_wrapper
        COMMANDS_TO_HELP[name] = options

        # Get list of option flags.
        flags = re.findall(r"-[a-zA-Z0-9]\b", options)
        COMMANDS_TO_OPTION_FLAGS[name] = flags

        return func

    return decorator


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


@cmd("break-pane", options="[-d]")
def break_pane(pymux: "Pymux", variables: _VariablesDict) -> None:
    dont_focus_window = variables["-d"]

    pymux.arrangement.break_pane(set_active=not dont_focus_window)
    pymux.invalidate(Woke.A_PANE_BROKE_OUT)


@cmd("select-pane", options="(-L|-R|-U|-D|-l|-t <pane-id>)")
def select_pane(pymux: "Pymux", variables: _VariablesDict) -> None:
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


@cmd("select-window", options="(-t <target-window>)")
def select_window(pymux: "Pymux", variables: _VariablesDict) -> None:
    """
    Select a window. E.g:  select-window -t :3  or  select-window -t @1001
    """
    window_id = variables["<target-window>"]

    w = _find_window(pymux, window_id)
    if w is None:
        raise CommandException("Can't find window: %s" % (window_id,))

    pymux.arrangement.set_active_window(w)


@cmd("move-window", options="(-t <dst-window>)")
def move_window(pymux: "Pymux", variables: _VariablesDict) -> None:
    """
    Move window to a new index.
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


@cmd("rotate-window", options="[-D|-U]")
def rotate_window(pymux: "Pymux", variables: _VariablesDict) -> None:
    if variables["-D"]:
        pymux.arrangement.rotate_window(count=-1)
    else:
        pymux.arrangement.rotate_window()


@cmd("swap-pane", options="(-D|-U)")
def swap_pane(pymux: "Pymux", variables: _VariablesDict) -> None:
    pymux.arrangement.get_active_window().rotate(with_pane_after_only=variables["-U"])


@cmd("kill-pane", options="[-t <target-pane>]")
def kill_pane(pymux: "Pymux", variables: _VariablesDict) -> None:
    if variables["-t"]:
        pane = _find_pane(pymux, variables["<target-pane>"])
        if pane is None:
            raise CommandException(
                "Can't find pane: %s" % (variables["<target-pane>"],)
            )
    else:
        pane = pymux.arrangement.get_active_pane()
    pymux.kill_pane(pane)


@cmd("kill-window", options="[-t <target-window>]")
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


@cmd("suspend-client")
def suspend_client(pymux: "Pymux", variables: _VariablesDict) -> None:
    connection = pymux.get_connection()

    if connection:
        connection.suspend_client_to_background()


@cmd("clock-mode")
def clock_mode(pymux: "Pymux", variables: _VariablesDict) -> None:
    pane = pymux.arrangement.get_active_pane()
    if pane:
        pane.clock_mode = not pane.clock_mode


@cmd("last-pane")
def last_pane(pymux: "Pymux", variables: _VariablesDict) -> None:
    w = pymux.arrangement.get_active_window()
    prev_active_pane = w.previous_active_pane

    if prev_active_pane:
        w.active_pane = prev_active_pane


@cmd("next-layout")
def next_layout(pymux: "Pymux", variables: _VariablesDict) -> None:
    "Select next layout."
    pane = pymux.arrangement.get_active_window()
    if pane:
        pane.select_next_layout()


@cmd("previous-layout")
def previous_layout(pymux: "Pymux", variables: _VariablesDict) -> None:
    "Select previous layout."
    pane = pymux.arrangement.get_active_window()
    if pane:
        pane.select_previous_layout()


@cmd(
    "new-window",
    options="[-a] [-b] [(-t <target-window>)] [(-n <name>)] "
    "[(-c <start-directory>)] [-d] [-P] [(-F <format>)] [<executable>]",
)
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
    number = _an_index(variables["<target-window>"])

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


def _an_index(target: "str | None") -> int | None:
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


@cmd(
    "split-window",
    options="[-v|-h] [(-t <target-window>)] [(-c <start-directory>)] "
    "[-d] [-P] [(-F <format>)] [<executable>]",
)
def split_window(pymux: "Pymux", variables: _VariablesDict) -> None:
    """
    Split horizontally or vertically.
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


@cmd("last-window")
def _(pymux: "Pymux", variables: _VariablesDict) -> None:
    "Go to previous active window."
    w = pymux.arrangement.get_previous_active_window()

    if w:
        pymux.arrangement.set_active_window(w)


@cmd("next-window")
def next_window(pymux: "Pymux", variables: _VariablesDict) -> None:
    "Focus the next window."
    pymux.arrangement.focus_next_window()


@cmd("previous-window")
def previous_window(pymux: "Pymux", variables: _VariablesDict) -> None:
    "Focus the previous window."
    pymux.arrangement.focus_previous_window()


@cmd("select-layout", options="<layout-type>")
def select_layout(pymux: "Pymux", variables: _VariablesDict) -> None:
    layout_type = variables["<layout-type>"]

    try:
        layout_type_obj: LayoutTypes = LayoutTypes(layout_type)
    except ValueError:
        raise CommandException("Invalid layout type.")
    else:
        pymux.arrangement.get_active_window().select_layout(layout_type_obj)


@cmd("switch-column-width", options="[-p]")
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
    pymux.invalidate(Woke.A_COLUMN_CHANGED_WIDTH)


@cmd("move-column", options="(-L|-R)")
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
        pymux.invalidate(Woke.A_COLUMN_MOVED)


@cmd("consume-or-expel", options="(-L|-R)")
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
        pymux.invalidate(Woke.A_PANE_CHANGED_COLUMN)


@cmd("rename-window", options="<name>")
def rename_window(pymux: "Pymux", variables: _VariablesDict) -> None:
    """
    Rename the active window.
    """
    pymux.arrangement.get_active_window().chosen_name = variables["<name>"]


@cmd("rename-pane", options="<name>")
def rename_pane(pymux: "Pymux", variables: _VariablesDict) -> None:
    """
    Rename the active pane.
    """
    pymux.arrangement.get_active_pane().chosen_name = variables["<name>"]


@cmd("rename-session", options="<name>")
def rename_session(pymux: "Pymux", variables: _VariablesDict) -> None:
    """
    Rename this session.
    """
    pymux.session_name = variables["<name>"]


@cmd(
    "resize-pane", options="[(-L <left>)] [(-U <up>)] [(-D <down>)] [(-R <right>)] [-Z]"
)
def resize_pane(pymux: "Pymux", variables: _VariablesDict) -> None:
    """
    Resize/zoom the active pane.
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
        the_pane_resizes(
            pymux, w, w.active_pane, up=up, right=right, down=down, left=left
        )

        # Zoom in/out.
        if variables["-Z"]:
            w.zoom = not w.zoom


@cmd(
    "resize-window",
    options=(
        "[(-x <columns>)] [(-y <rows>)] "
        "[(-L <left>)] [(-U <up>)] [(-D <down>)] [(-R <right>)]"
    ),
)
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
    now = pymux.the_size_of_the_plane(window)

    def a_number(name, instead):
        given = variables["<%s>" % (name,)]
        if given is None:
            return instead
        try:
            return int(given)
        except ValueError:
            raise CommandException("Expecting an integer.")

    def asked_for(name, then):
        wanted = a_number(name, then)
        if wanted < 1:
            raise CommandException("A window is at least one cell.")
        return wanted

    columns = asked_for("columns", now.columns) + a_number("right", 0)
    rows = asked_for("rows", now.rows) + a_number("down", 0)
    columns -= a_number("left", 0)
    rows -= a_number("up", 0)

    window.manual_size = Size(rows=max(1, rows), columns=max(1, columns))
    window.window_size = WindowSize.MANUAL


@cmd("detach-client")
def detach_client(pymux: "Pymux", variables: _VariablesDict) -> None:
    """
    Detach client.
    """
    pymux.detach_client(get_app())


@cmd("confirm-before", options="[(-p <message>)] <command>")
def confirm_before(pymux: "Pymux", variables: _VariablesDict) -> None:
    client_state = pymux.get_client_state()

    client_state.confirm_text = variables["<message>"] or ""
    client_state.confirm_command = variables["<command>"]


@cmd("open-url", options="[-c] <url>")
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


@cmd(
    "compose-key",
    options="[(-p <message>)] [(-I <default>)]",
)
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


@cmd("command-prompt", options="[(-p <message>)] [(-I <default>)] [<command>]")
def command_prompt(pymux: "Pymux", variables: _VariablesDict) -> None:
    """
    Enter command prompt.
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


@cmd("send-prefix")
def send_prefix(pymux: "Pymux", variables: _VariablesDict) -> None:
    """
    Send prefix to active pane.
    """
    pane = pymux.arrangement.get_active_pane()

    # The prefix is held as prompt_toolkit names, because that is what
    # binds it, and those re-spell as chords: "c-b" is "ctrl+b". So the
    # one command that sends a key pymux keeps for itself goes the same
    # road as `send-keys`, and says the same thing when a pane cannot
    # hear it. Lillecarl/pymux#237.
    for key in pymux.key_bindings_manager.prefix:
        send_a_key(pane, an_event_however_it_is_written(key), key)


@cmd("bind-key", options="[-n] <key> [--] <command> [<arguments>...]")
def bind_key(pymux: "Pymux", variables: _VariablesDict) -> None:
    """
    Bind a key sequence.
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


@cmd("unbind-key", options="[-n] <key>")
def unbind_key(pymux: "Pymux", variables: _VariablesDict) -> None:
    """
    Remove key binding.
    """
    key = variables["<key>"]
    needs_prefix = not variables["-n"]

    try:
        pymux.key_bindings_manager.remove_custom_binding(key, needs_prefix=needs_prefix)
    except ValueError:
        raise CommandException("Invalid key: %r" % (key,))


@cmd("send-keys", options="[-t <target-pane>] [-l] [-R] [<keys>...]")
def send_keys(pymux: "Pymux", variables: _VariablesDict) -> None:
    """
    Send key strokes to the active process.

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
            event = an_event_however_it_is_written(key)
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


@cmd("copy-mode", options="[-u]")
def copy_mode(pymux: "Pymux", variables: _VariablesDict) -> None:
    """
    Enter copy mode.
    """
    # TODO: handle '-u' (go in copy mode and page-up directly).

    pane = pymux.arrangement.get_active_pane()
    pane.enter_copy_mode()


@cmd("paste-buffer")
def paste_buffer(pymux: "Pymux", variables: _VariablesDict) -> None:
    """
    Paste the buffer of the session into the pane.

    The buffer holds what copy mode copied and what a pane wrote to the
    clipboard of the user. It belongs to the session, so the command
    reads it there and not from the application of one client.
    """
    pane = pymux.arrangement.get_active_pane()
    pane.process.write_input(pane.screen.wrap_paste(pymux.clipboard.get_data().text))


@cmd("source-file", options="<filename>")
def source_file(pymux: "Pymux", variables: _VariablesDict) -> None:
    """
    Source configuration file.
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


@cmd("set-option", options="[-g] <option> <value>")
def set_option(pymux: "Pymux", variables: _VariablesDict, window: bool = False) -> None:
    """
    Set an option.

    -g: for a window option, say what every new window starts with.
        For a session option it changes nothing, because pymux has one
        session and every session option is already global. `set -g`
        is the most common line in a tmux configuration, and `pymux -V`
        says pymux speaks tmux 3.4, so it has to be a line pymux takes.
    """
    name = variables["<option>"]
    value = variables["<value>"]

    if window:
        option = pymux.window_options.get(name)
    else:
        option = pymux.options.get(name)

    if option:
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
        except SetOptionError as e:
            raise CommandException(e.message)
    else:
        raise CommandException("Invalid option: %s" % (name,))


@cmd("set-window-option", options="[-g] <option> <value>")
def set_window_option(pymux: "Pymux", variables: _VariablesDict) -> None:
    """
    Set a window option.

    -g: say what every new window starts with, rather than changing
        this one.
    """
    set_option(pymux, variables, window=True)


@cmd("display-panes")
def display_panes(pymux: "Pymux", variables: _VariablesDict) -> None:
    "Display the pane numbers."
    pymux.display_pane_numbers = True


@cmd("display-message", options="<message>")
def display_message(pymux: "Pymux", variables: _VariablesDict) -> None:
    "Display a message."
    message = variables["<message>"]
    client_state = pymux.get_client_state()
    client_state.message = message


@cmd("clear-history")
def clear_history(pymux: "Pymux", variables: _VariablesDict) -> None:
    "Clear scrollback buffer."
    pane = pymux.arrangement.get_active_pane()

    if pane.is_copying:
        raise CommandException("Not available in copy mode")
    else:
        pane.screen.clear_history()


@cmd("list-keys")
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

    # Display help in pane.
    result_str = "\n".join(sorted(result))
    pymux.get_client_state().layout_manager.display_popup("list-keys", result_str)


@cmd("list-panes", options="[-a] [(-t <target-pane>)] [(-F <format>)]")
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

        # Display help in pane.
        result_str = "\n".join(sorted(result))
        pymux.get_client_state().layout_manager.display_popup("list-keys", result_str)


@cmd("list-windows", options="[-a] [(-t <target-window>)] [(-F <format>)]")
def list_windows(pymux: "Pymux", variables: _VariablesDict) -> None:
    """
    Display a list of windows. (With `-F`, the formatted window information
    is printed to the output of the pymux command line. Like tmux.)
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
        result_str = "\n".join(result)
        pymux.get_client_state().layout_manager.display_popup(
            "list-windows", result_str
        )


@cmd("list-sessions", options="[-a] [(-F <format>)]")
def list_sessions(pymux: "Pymux", variables: _VariablesDict) -> None:
    """
    List sessions. (Pymux has one session per server. With `-F`, the
    formatted session information is printed to the output of the pymux
    command line. Like tmux.)
    """
    if variables["-F"]:
        format_str = variables["<format>"]
        window = pymux.arrangement.get_active_window()
        line = format_pymux_string(
            pymux, format_str, window=window, pane=window.active_pane
        )
        pymux.print_command_line(line)
    else:
        # Display as pop-up in the user interface.
        result_str = format_pymux_string(pymux, "#{session_name}")
        pymux.get_client_state().layout_manager.display_popup(
            "list-sessions", result_str
        )


@cmd("has-session", options="[(-t <target-session>)]")
def has_session(pymux: "Pymux", variables: _VariablesDict) -> None:
    """
    Check whether the session exists. Raise a CommandException (which makes
    the pymux command line return a non-zero exit code) when it doesn't.
    """
    target = variables["<target-session>"] or ""
    if not _pane_matches_session_name(pymux, target):
        raise CommandException("can't find session: %s" % (target,))


@cmd("new-session", options="[(-s <session-name>)] [-d] [-P] [(-F <format>)]")
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


@cmd("kill-session")
def kill_session(pymux: "Pymux", variables: _VariablesDict) -> None:
    """
    Kill this session. (This terminates the server, like `tmux kill-session`
    for the last session.)
    """
    pymux.stop()


@cmd("kill-server")
def kill_server(pymux: "Pymux", variables: _VariablesDict) -> None:
    """
    Kill the server. (Pymux has one session per server. Same as
    `kill-session`.)
    """
    pymux.stop()


@cmd("dump-stacks")
def dump_stacks(pymux: "Pymux", variables: _VariablesDict) -> None:
    """
    Write down what this server is doing now, and say where.

    Every thread, every asyncio task and what each one waits for.
    `pymux/introspect.py` says why a server answers for itself, and what
    `SIGUSR1` gives instead when the loop is too wedged to read this.
    """
    path = introspect.a_dump(pymux)
    pymux.print_command_line(str(path))
    pymux.show_message("Wrote a dump to %s" % (path,))


@cmd("counters")
def counters(pymux: "Pymux", variables: _VariablesDict) -> None:
    """
    Say what this server has done, and how often.

    The half a stack cannot give: a stack says where the server is in
    one instant, and this says what it has been doing for an hour.
    """
    said = introspect.the_counters(pymux)
    pymux.print_command_line(said)
    pymux.get_client_state().layout_manager.display_popup("counters", said)


@cmd("profile", options="[<seconds>]")
def profile(pymux: "Pymux", variables: _VariablesDict) -> None:
    """
    Watch this server for a few seconds, and write down where its time
    went.

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


@cmd(
    "display-popup",
    options="[-E] [(-w <width>)] [(-h <height>)] [(-T <title>)] [<executable>]",
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


@cmd("close-popup")
def close_popup(pymux: "Pymux", variables: _VariablesDict) -> None:
    """
    Close the overlay pane, and kill what runs in it.
    """
    pymux.close_overlay()


@cmd(
    "capture-pane",
    options="[-p] [-J] [(-t <target-pane>)] [(-S <start>)] [(-E <end>)]",
)
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
        pymux.get_client_state().layout_manager.display_popup("capture-pane", text)


@cmd("show-buffer")
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


# Check whether all aliases point to real commands.
for k in ALIASES.values():
    assert k in COMMANDS_TO_HANDLERS
