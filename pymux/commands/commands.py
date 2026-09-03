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
from prompt_toolkit.document import Document
from prompt_toolkit.key_binding.vi_state import InputMode

from pymux.arrangement import LayoutTypes
from pymux.commands.aliases import ALIASES
from pymux.commands.utils import wrap_argument
from pymux.format import format_pymux_string
from pymux.key_mappings import (
    prompt_toolkit_key_to_vt100_key,
    pymux_key_to_prompt_toolkit_key_sequence,
)
from pymux.layout import focus_down, focus_left, focus_right, focus_up
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


def get_documentation_for_command(command: str) -> Optional[str]:
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
    logger.info("handle command: %s %s.", input_string, type(input_string))

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
    usage = "Usage:\n    %s %s" % (name, options) if options else "Usage:\n    %s" % name

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
            if (
                arg.startswith("-")
                and len(arg) >= 2
                and not arg.startswith("--")
            ):
                flag = arg[1:2]
                rest = arg[2:]
                takes_value = flag in value_flags
                has_inline_value = bool(rest)

                # Is this option repeated later on?
                repeated = any(
                    a.startswith("-")
                    and not a.startswith("--")
                    and a[1:2] == flag
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
            pymux.invalidate()

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


def _find_window(pymux: "Pymux", target: Optional[str]) -> Optional["Window"]:
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


def _find_pane(pymux: "Pymux", target: Optional[str]) -> Optional["Pane"]:
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
    pane_part: Optional[str] = None
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
    pymux.invalidate()


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
        pymux.arrangement.get_active_window().rotate(
            with_pane_after_only=True
        )

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
            raise CommandException("Can't find pane: %s" % (variables["<target-pane>"],))
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
    options="[(-t <target-window>)] [(-n <name>)] [(-c <start-directory>)] "
    "[-d] [-P] [(-F <format>)] [<executable>]",
)
def new_window(pymux: "Pymux", variables: _VariablesDict) -> None:
    executable = variables["<executable>"]
    start_directory = variables["<start-directory>"]
    name = variables["<name>"]
    dont_select = variables["-d"]

    window = pymux.arrangement.get_active_window()
    pymux.create_window(executable, start_directory=start_directory, name=name)

    # The newly created window is the last one in the list.
    new_window = pymux.arrangement.windows[-1]

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

    if w:
        w.change_size_for_active_pane(up=up, right=right, down=down, left=left)

        # Zoom in/out.
        if variables["-Z"]:
            w.zoom = not w.zoom


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


@cmd("command-prompt", options="[(-p <message>)] [(-I <default>)] [<command>]")
def command_prompt(pymux: "Pymux", variables: _VariablesDict) -> None:
    """
    Enter command prompt.
    """
    client_state = pymux.get_client_state()

    if variables["<command>"]:
        # When a 'command' has been given.
        client_state.prompt_text = (
            variables["<message>"] or "(%s)" % variables["<command>"].split()[0]
        )
        client_state.prompt_command = variables["<command>"]

        client_state.prompt_mode = True
        client_state.prompt_buffer.reset(
            Document(format_pymux_string(pymux, variables["<default>"] or ""))
        )

        get_app().layout.focus(client_state.prompt_buffer)
    else:
        # Show the ':' prompt.
        client_state.prompt_text = ""
        client_state.prompt_command = ""

        get_app().layout.focus(client_state.command_buffer)

    # Go to insert mode.
    get_app().vi_state.input_mode = InputMode.INSERT


@cmd("send-prefix")
def send_prefix(pymux: "Pymux", variables: _VariablesDict) -> None:
    """
    Send prefix to active pane.
    """
    process = pymux.arrangement.get_active_pane().process

    for k in pymux.key_bindings_manager.prefix:
        vt100_data = prompt_toolkit_key_to_vt100_key(k)
        process.write_input(vt100_data)


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

    pymux.key_bindings_manager.remove_custom_binding(key, needs_prefix=needs_prefix)


@cmd("send-keys", options="[-t <target-pane>] [-l] [-R] <keys>...")
def send_keys(pymux: "Pymux", variables: _VariablesDict) -> None:
    """
    Send key strokes to the active process.
    """
    if variables["-t"]:
        pane = _find_pane(pymux, variables["<target-pane>"])
        if pane is None:
            raise CommandException("Can't find pane: %s" % (variables["<target-pane>"],))
    else:
        pane = pymux.arrangement.get_active_pane()

    if pane.display_scroll_buffer:
        raise CommandException("Cannot send keys. Pane is in copy mode.")

    if variables["-R"]:
        # Reset the terminal of this pane. (Like `reset`.)
        pane.process.screen.pt_screen.reset()
        pane.process.screen.reset()

    keys = variables["<keys>"]

    if variables["-l"]:
        # Send keys literally. (Don't interpret key names like 'Enter'.)
        pane.process.write_input(" ".join(keys))
        return

    for key in keys:
        # Translate key from pymux key to prompt_toolkit key.
        try:
            keys_sequence = pymux_key_to_prompt_toolkit_key_sequence(key)
        except ValueError:
            # Not a known key name. Like tmux, send this argument as
            # literal text.
            pane.process.write_input(key)
            continue

        # Translate prompt_toolkit key to VT100 key.
        for k in keys_sequence:
            pane.process.write_key(k)


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
    Paste clipboard content into buffer.
    """
    pane = pymux.arrangement.get_active_pane()
    pane.process.write_input(get_app().clipboard.get_data().text, paste=True)


@cmd("source-file", options="<filename>")
def source_file(pymux: "Pymux", variables: _VariablesDict) -> None:
    """
    Source configuration file.
    """
    filename = os.path.expanduser(variables["<filename>"])
    try:
        with open(filename, "r") as f:
            for line in f:
                handle_command(pymux, line)
    except IOError as e:
        raise CommandException("IOError: %s" % (e,))


@cmd("set-option", options="<option> <value>")
def set_option(pymux: "Pymux", variables: _VariablesDict, window: bool = False) -> None:
    name = variables["<option>"]
    value = variables["<value>"]

    if window:
        option = pymux.window_options.get(name)
    else:
        option = pymux.options.get(name)

    if option:
        try:
            option.set_value(pymux, value)
        except SetOptionError as e:
            raise CommandException(e.message)
    else:
        raise CommandException("Invalid option: %s" % (name,))


@cmd("set-window-option", options="<option> <value>")
def set_window_option(pymux: "Pymux", variables: _VariablesDict) -> None:
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

    if pane.display_scroll_buffer:
        raise CommandException("Not available in copy mode")
    else:
        pane.process.screen.clear_history()


@cmd("list-keys")
def list_keys(pymux: "Pymux", variables: _VariablesDict) -> None:
    """
    Display all configured key bindings.
    """
    # Create help string.
    result = []

    for k, custom_binding in pymux.key_bindings_manager.custom_bindings.items():
        needs_prefix, key = k

        result.append(
            "bind-key %3s %-10s %s %s"
            % (
                ("-n" if needs_prefix else ""),
                key,
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
                    min(pymux.history_limit, process.screen.line_offset + process.sy),
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


@cmd("capture-pane", options="[-p] [(-t <target-pane>)] [(-S <start>)] [(-E <end>)]")
def capture_pane(pymux: "Pymux", variables: _VariablesDict) -> None:
    """
    Capture the content of a pane.

    Line numbers are tmux style: 0 is the first line of the visible pane,
    negative numbers are lines in the history.
    """
    if variables["-t"]:
        pane = _find_pane(pymux, variables["<target-pane>"])
        if pane is None:
            raise CommandException("Can't find pane: %s" % (variables["<target-pane>"],))
    else:
        pane = pymux.arrangement.get_active_pane()

    process = pane.process
    screen = process.screen
    pt_screen = screen.pt_screen
    data_buffer = pt_screen.data_buffer

    lines_count = screen.lines

    if not data_buffer:
        text = ""
    else:
        # Collect all lines, from the top of the buffer until the bottom.
        all_keys = sorted(data_buffer.keys())
        total = max(all_keys) + 1

        # Index 0 of `all_lines` is the first line in the buffer. The visible
        # pane are the last `lines_count` lines.
        visible_top = total - lines_count

        def to_tmux_line_number(buffer_index: int) -> int:
            "Translate a buffer index into a tmux line number."
            return buffer_index - visible_top

        def from_tmux_line_number(line_number: int) -> int:
            "Translate a tmux line number into a buffer index."
            return visible_top + line_number

        # Determine the range. (tmux line numbers.)
        start_str = variables["<start>"]
        end_str = variables["<end>"]

        if start_str in (None, "", "-"):
            first_buffer_index = all_keys[0]
        else:
            try:
                first_buffer_index = from_tmux_line_number(int(start_str))
            except ValueError:
                raise CommandException("Invalid start line: %s" % (start_str,))
            first_buffer_index = max(first_buffer_index, all_keys[0])

        if end_str in (None, "", "-"):
            last_buffer_index = all_keys[-1]
        else:
            try:
                last_buffer_index = from_tmux_line_number(int(end_str))
            except ValueError:
                raise CommandException("Invalid end line: %s" % (end_str,))
            last_buffer_index = min(last_buffer_index, all_keys[-1])

        lines = []
        for buffer_index in range(first_buffer_index, last_buffer_index + 1):
            line = data_buffer.get(buffer_index, {})
            chars = [line[x].char for x in sorted(line.keys()) if x in line]
            lines.append("".join(chars).rstrip())

        text = "\n".join(lines)

    if variables["-p"]:
        pymux.print_command_line(text)
    else:
        pymux.get_client_state().layout_manager.display_popup(
            "capture-pane", text
        )


@cmd("show-buffer")
def show_buffer(pymux: "Pymux", variables: _VariablesDict) -> None:
    """
    Display the clipboard content.
    """
    text = get_app().clipboard.get_data().text
    pymux.get_client_state().layout_manager.display_popup("show-buffer", text)


def _print_object_format(
    pymux: "Pymux",
    format_str: Optional[str],
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
