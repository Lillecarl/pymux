"""
The commands of pymux: one function each, in one module each.

A module declares what its command takes with argparse in
`register`, and runs as one function that takes the server and the
parsed arguments. argparse parses with the tree; the shell completes
through it with argcomplete, and the command bar of a client
completes through the same tree in process. Lillecarl/pymux#307.
"""

import argparse
import inspect
import shlex
from importlib import import_module
from typing import TYPE_CHECKING, List

from pymux.commands.aliases import ALIASES
from pymux.enums import Woke
from pymux.log import logger

if TYPE_CHECKING:
    from pymux.main import Pymux

__all__ = [
    "CommandException",
    "add_command",
    "add_commands_to",
    "call_command_handler",
    "handle_command",
    "the_parser",
]

#: One module per command, in the order the commands were written.
MODULES = ['break_pane', 'select_pane', 'select_window', 'move_window', 'swap_window', 'rotate_window', 'swap_pane', 'kill_pane', 'kill_window', 'suspend_client', 'clock_mode', 'last_pane', 'next_layout', 'previous_layout', 'new_window', 'split_window', 'new_pane', 'last_window', 'next_window', 'previous_window', 'select_layout', 'switch_column_width', 'move_column', 'consume_or_expel', 'rename_window', 'rename_pane', 'rename_session', 'resize_pane', 'resize_window', 'detach_client', 'confirm_before', 'open_url', 'compose_key', 'command_prompt', 'send_prefix', 'bind_key', 'unbind_key', 'send_keys', 'copy_mode', 'paste_buffer', 'source_file', 'set_option', 'set_window_option', 'set_environment', 'show_options', 'show_window_options', 'list_commands', 'refresh_client', 'show_prompt_history', 'clear_prompt_history', 'respawn_pane', 'respawn_window', 'show_environment', 'display_panes', 'choose_window', 'choose_buffer', 'display_message', 'get', 'clear_history', 'list_keys', 'list_panes', 'list_windows', 'list_sessions', 'has_session', 'new_session', 'kill_session', 'kill_server', 'dump_stacks', 'counters', 'profile', 'display_popup', 'close_popup', 'capture_pane', 'show_buffer', 'set_buffer', 'list_buffers', 'delete_buffer', 'load_buffer', 'save_buffer', 'show_messages', 'attach_session', 'switch_client', 'run_shell', 'if_shell', 'move_pane', 'join_pane', 'link_window', 'unlink_window', 'set_hook', 'show_hooks']


class CommandException(Exception):
    "When raised from a command handler, this message will be shown."

    def __init__(self, message: str) -> None:
        self.message = message


class BadLine(Exception):
    "What a parser says about a line it cannot read."

    def __init__(self, message: str) -> None:
        self.message = message


class CommandParser(argparse.ArgumentParser):
    "An argparse parser that raises instead of exiting."

    def error(self, message: str) -> None:
        raise BadLine(message)


def add_command(subparsers, handler, *, name=None, aliases=()):
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


def add_commands_to(subparsers):
    """
    Mount every command of the tree on a subparsers action.

    The shell completes the command line of pymux through one parser
    that holds the options of the entry point and every command under
    it. This is what fills the tree under it. It parses nothing on
    its own.
    """
    for name in MODULES:
        import_module("." + name, __name__).register(subparsers)


_the_parser = None


def the_parser():
    """
    The parser of the whole command line: every command under one
    subparsers action. Built once, on first use.
    """
    global _the_parser
    if _the_parser is None:
        parser = CommandParser(prog="pymux", add_help=False, allow_abbrev=False)
        subparsers = parser.add_subparsers(metavar="COMMAND", parser_class=CommandParser)
        add_commands_to(subparsers)
        _the_parser = (parser, subparsers)
    return _the_parser


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
            commands: List[List[str]] = [[]]
            for part in parts:
                if part == ";" and not no_semicolon_split:
                    commands.append([])
                else:
                    commands[-1].append(part)

            for args in commands:
                if args:
                    call_command_handler(args[0], pymux, args[1:])


def call_command_handler(command: str, pymux: "Pymux", arguments: List[str]) -> None:
    """
    Execute one command, given its words.
    """
    # Resolve aliases.
    command = ALIASES.get(command, command)

    _parser, subparsers = the_parser()
    parser = subparsers.choices.get(command)
    if parser is None:
        pymux.show_message("Invalid command: %s" % (command,))
        pymux.add_command_error("pymux: invalid command: %s" % (command,))
        return

    try:
        namespace = parser.parse_args(list(arguments))
    except BadLine as e:
        usage = parser.format_usage()[len("usage: "):].rstrip()
        message = "%s (%s)" % (e.message, usage)
        pymux.show_message(message)
        pymux.add_command_error("pymux: %s" % (message,))
        return

    try:
        namespace._handler(pymux, namespace)
    except CommandException as e:
        pymux.show_message(e.message)
        pymux.add_command_error("pymux: %s" % (e.message,))
        return

    pymux.invalidate(Woke.COMMAND_RAN % command)
