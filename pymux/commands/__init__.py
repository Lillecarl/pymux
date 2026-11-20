"""
The commands of pymux: one function each, in one module each.

A module declares what its command takes with argparse in
`register`, and runs as one function that takes the server and the
parsed arguments. argparse parses with the tree; the shell completes
through it with argcomplete, and the command bar of a client
completes through the same tree in process. Lillecarl/pyterm#309.
"""

from __future__ import annotations

import argparse
import inspect
from argparse import OPTIONAL
from collections import deque
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from enum import StrEnum
from importlib import import_module
from typing import TYPE_CHECKING, Any, Final, NoReturn, overload, override

from pymux import grammar
from pymux.commands.aliases import ALIASES
from pymux.enums import Woke
from pymux.keys.actions import Kind, arguments_of, note, words_of
from pymux.log import logger

if TYPE_CHECKING:
    from pymux.connection.client_state import ClientState
    from pymux.core.main import Pymux

__all__ = [
    "CommandException",
    "Role",
    "Target",
    "add_command",
    "add_commands_to",
    "add_target",
    "build_parser_tree",
    "call_command_handler",
    "handle_command",
    "not_past_this_client",
    "parser_tree",
    "read_commands",
    "refuse_undeclared_targets",
    "run_arguments",
    "target_of",
    "targets_of",
    "this_client",
]

#: One module per command, in the order the commands were written.
MODULES = [
    "break_pane",
    "select_pane",
    "select_window",
    "move_window",
    "swap_window",
    "rotate_window",
    "swap_pane",
    "kill_pane",
    "kill_window",
    "suspend_client",
    "clock_mode",
    "last_pane",
    "next_layout",
    "previous_layout",
    "new_window",
    "split_window",
    "new_pane",
    "last_window",
    "next_window",
    "previous_window",
    "select_layout",
    "switch_column_width",
    "move_column",
    "consume_or_expel",
    "rename_window",
    "rename_pane",
    "record_pane",
    "record_client",
    "record_export",
    "record_session",
    "record_upload",
    "record_auth",
    "show_urls",
    "rename_session",
    "resize_pane",
    "resize_window",
    "detach_client",
    "confirm_before",
    "open_url",
    "receive",
    "complete_value",
    "compose_key",
    "command_prompt",
    "send_prefix",
    "bind_key",
    "unbind_key",
    "send_keys",
    "type_text",
    "copy_mode",
    "paste_buffer",
    "source_file",
    "set_option",
    "set_window_option",
    "set_client_option",
    "set_environment",
    "set_tag",
    "show_tags",
    "show_options",
    "show_window_options",
    "show_client_options",
    "show_terminal",
    "list_commands",
    "refresh_client",
    "show_prompt_history",
    "clear_prompt_history",
    "respawn_pane",
    "respawn_window",
    "show_environment",
    "display_panes",
    "choose_window",
    "choose_buffer",
    "choose_notifications",
    "choose_job",
    "choose_client",
    "display_message",
    "notify",
    "get",
    "clear_history",
    "unscroll_pane",
    "list_keys",
    "list_panes",
    "list_windows",
    "list_sessions",
    "list_clients",
    "has_session",
    "new_session",
    "kill_session",
    "kill_server",
    "upgrade_server",
    "dump_stacks",
    "counters",
    "profile",
    "display_popup",
    "close_popup",
    "capture_pane",
    "capture_client",
    "capture_window",
    "show_buffer",
    "set_buffer",
    "list_buffers",
    "list_user_vars",
    "info",
    "delete_buffer",
    "load_buffer",
    "save_buffer",
    "show_messages",
    "show_log",
    "show_html_stylesheet",
    "attach_session",
    "switch_client",
    "run_shell",
    "if_shell",
    "move_pane",
    "join_pane",
    "link_window",
    "unlink_window",
    "set_hook",
    "show_hooks",
    "show_plugins",
    "lock_client",
    "lock_session",
    "lock_server",
    "display_menu",
    "customize_mode",
    "wait_for",
    "wait_pane_change",
    "wait_pane",
    "enter_mode",
    "leave_mode",
    "noop",
    "forward_port",
    "unforward_port",
    "list_forwards",
    "run_job",
    "wait_job",
    "list_jobs",
    "list_processes",
    "show_job",
    "kill_job",
    "view_job",
    "tail_job",
    "connect",
    "sql",
    "wl",
    "launch_browser",
]


class CommandException(Exception):
    "When raised from a command handler, this message will be shown."

    def __init__(self, message: str) -> None:
        self.message = message


class BadLine(Exception):
    "What a parser says about a line it cannot read."

    def __init__(self, message: str) -> None:
        self.message = message


def this_client(pymux: Pymux) -> ClientState | None:
    """
    The client that ran this command, or None when no person did.

    A command that arrived over the socket runs under a client that
    draws nothing and goes away with the answer. It stands for nobody,
    so nothing that is about a person applies to it.
    """
    try:
        client_state = pymux.get_client_state()
    except ValueError:
        return None

    return None if client_state.nobody_there else client_state


def not_past_this_client(pymux: Pymux, reaches_another: bool) -> None:
    """
    Refuse what a read-only client aimed at somebody else.

    Two of the commands a watcher may run also reach other clients:
    `detach-client -a` puts every one of them back at a shell prompt,
    and `attach-session -x` closes the terminal each was sitting in.
    `read_only=True` is a yes for the whole command, so a handler asks
    this for the part of it that is not.

    tmux has this guard on `detach-client` and nowhere else
    (`cmd-detach-client.c:73-78`): its `attach-session -x` runs for a
    read-only client and hangs up everybody, which is the harder
    version of the thing the flag exists to stop.
    Lillecarl/pyterm#469.
    """
    if not reaches_another:
        return

    client_state = this_client(pymux)
    if client_state is not None and client_state.read_only:
        raise CommandException("client is read-only")


def _with_value(action: argparse.Action, option: str, words: deque[str], joint: str) -> Iterator[str]:
    """
    An option and the next word, which getopt takes as its value whatever
    it starts with. argparse refuses a separate value that starts with a
    dash, so that one goes joined: `-t-F`, `--name=-x`.

    An optional value is never a word that starts with a dash: `-o --font x`.
    """
    if not words:
        yield option
    elif not words[0].startswith("-"):
        yield option
        yield words.popleft()
    elif action.nargs == OPTIONAL:
        yield option
    else:
        yield option + joint + words.popleft()


class CommandParser(argparse.ArgumentParser):
    "An argparse parser that raises instead of exiting."

    @override
    def error(self, message: str) -> NoReturn:
        raise BadLine(message)

    @override
    def parse_known_args(  # pyrefly: ignore[bad-override]
        self, args: Sequence[str] | None = None, namespace: argparse.Namespace | None = None
    ) -> tuple[argparse.Namespace | None, list[str]]:
        if args is not None:
            args = list(self._as_getopt_reads(args))
        parsed, extras = super().parse_known_args(args, namespace)
        # argparse keeps the `--` that ends the options at the front of a REMAINDER.
        for action in self._actions:
            words = getattr(parsed, action.dest, None) if action.nargs == argparse.REMAINDER else None
            if words and words[0] == "--":
                setattr(parsed, action.dest, words[1:])
        return parsed, extras

    def _as_getopt_reads(self, args: Sequence[str]) -> Iterator[str]:
        """
        Read the words the way tmux's getopt does, for argparse.

        **The options end at the first operand.** `set -g @x '-p 60%'`
        sets @x to `-p 60%`; argparse read the value as `-p` and more.

        **A word of short options splits at its `=`.** tmux reads `-t=`
        as -t with the value `=`, the pane under the mouse. argparse
        splits an option at its `=` and reads `-t=` as -t with an empty
        value, which is the current pane.
        """
        words = deque(args)
        while words:
            word = words.popleft()
            if word == "--":
                yield word
                yield from words
                return
            if word == "-" or not word.startswith("-"):
                rest = [word, *words]
                # The tree's root: its operand is the command, whose own parser reads the rest.
                if self._subparsers is None and any(each.startswith("-") for each in rest):
                    yield "--"
                yield from rest
                return
            if word.startswith("--"):
                action = self._option_string_actions.get(word)
                if action is not None and action.nargs != 0:
                    yield from _with_value(action, word, words, "=")
                else:
                    yield word
                continue
            yield from self._short_options(word, words)

    def _short_options(self, word: str, words: deque[str]) -> Iterator[str]:
        "One word of short options, and the next word too when its last option takes it as a value."
        for i, letter in enumerate(word[1:], 1):
            action = self._option_string_actions.get("-" + letter)
            if action is None:
                yield word
                return
            if action.nargs != 0:
                value = word[i + 1 :]
                if not value:
                    yield from _with_value(action, word, words, "")
                    return
                if "=" not in word:
                    yield word
                    return
                if i > 1:
                    yield word[:i]
                yield "-" + letter
                yield value
                return
        yield word


class Target(StrEnum):
    "What a target argument of a command names (`add_target`)."

    PANE = "pane"
    WINDOW = "window"
    SESSION = "session"
    #: A place in a session, `[session:]index`, which may hold no window.
    SLOT = "slot"
    #: A client of this server. Another server has no name for it.
    CLIENT = "client"
    #: A job of this server, by the id `run-job` answered with. Another
    #: server has no name for it.
    JOB = "job"

    @property
    def crosses(self) -> bool:
        "Whether a peer can name it to the server that owns it (`mirror.forward`)."
        return self not in (Target.CLIENT, Target.JOB)


class Role(StrEnum):
    "Which end of a command a target is: the first word of tmux's metavar."

    TARGET = "target"
    SRC = "src"
    DST = "dst"


def target_metavar(kind: Target, role: Role) -> str:
    "How tmux spells a target of that kind at that end of a command."
    if kind is Target.JOB:
        return "<job>"
    if kind is Target.SLOT:
        return "[<%s-session>:]<%s-index>" % (role, role)
    return "<%s-%s>" % (role, kind)


#: Each metavar `target_metavar` spells. `add_commands_to` refuses one
#: that no `add_target` declared.
TARGET_METAVARS: Final = frozenset(target_metavar(kind, role) for kind in Target for role in Role)


def add_target(
    parser: argparse._ActionsContainer, *flags: str, kind: Target, role: Role = Role.TARGET, **kwargs: Any
) -> argparse.Action:
    """
    An argument that names a target of the command: a pane, a window,
    a session, a place, a client or a job.

    **The kind is said once, here, and everything that reads a target
    reads it from the action**: the metavar the help prints, what Tab
    offers (`completer.TARGET_COMPLETERS`), and how a peer names it to
    the server that owns it (`mirror.forward`). A kind said twice was
    said differently once: `move-window --session` completed as a
    session and went to the owner as the peer spelled it, a name the
    owner has no session by. Lillecarl/pyterm#965.
    """
    action = parser.add_argument(*flags, metavar=target_metavar(kind, role), **kwargs)
    action.target = kind  # pyrefly: ignore[missing-attribute]
    return action


def target_of(action: argparse.Action) -> Target | None:
    "What the argument names, when `add_target` made it."
    return getattr(action, "target", None)


def targets_of(parser: argparse.ArgumentParser) -> dict[str, Target]:
    "The target arguments of one command, by their dest."
    return {action.dest: kind for action in parser._actions if (kind := target_of(action)) is not None}


def add_command(
    subparsers: argparse._SubParsersAction[CommandParser],
    handler: Callable[..., Any],
    *,
    name: str | None = None,
    aliases: tuple[str, ...] | list[str] = (),
    read_only: bool = False,
    forwards: bool = False,
):
    """
    The parser of one command: named after its handler, described by
    the first line of its docstring.

    `read_only` marks a command a client that only watches may still
    run. **The default is no**, because the cost of the two mistakes
    is not the same: a command wrongly refused says so and a person
    runs it from a client of their own, and a command wrongly allowed
    types into somebody else's session. tmux marks five and no more.
    Lillecarl/pyterm#469.

    `forwards` marks a command that changes or reads a session's
    windows and panes. It runs on the server that owns the session it
    acts on: on a session another server owns (`connect`), that server
    runs it, with each target `add_target` declared named in the
    owner's ids. A command with no target acts where its caller is.
    **No, the default, runs here whatever the session**: a command
    about this server, or about what one client looks at.
    Lillecarl/pyterm#761.
    """
    if name is None:
        name = handler.__name__.replace("_", "-")
    described = inspect.getdoc(handler) or ""
    parser = subparsers.add_parser(
        name,
        aliases=list(aliases),
        help=described.partition("\n")[0],
        # The whole docstring, paragraphs kept, for `pymux <command>
        # --help`. Lillecarl/pyterm#847.
        description=described,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        # `-h` is an option of `split-window`, and no command answers
        # a help flag on its own: the help of the command bar and of
        # the shell come from this tree, not from a `-h`.
        add_help=False,
    )
    parser.set_defaults(_handler=handler, _read_only=read_only, _forwards=forwards)
    return parser


@overload
def add_commands_to(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """The commands under the entry point, which exits on a bad line."""


@overload
def add_commands_to(subparsers: argparse._SubParsersAction[CommandParser]) -> None:
    """The commands under the in-process tree, which raises instead."""


def add_commands_to(subparsers: Any) -> None:
    """
    Mount every command of the tree on a subparsers action.

    The shell completes the command line of pymux through one parser
    that holds the options of the entry point and every command under
    it. This is what fills the tree under it. It parses nothing on
    its own.

    Both shapes arrive here: the entry point parses a real command
    line and exits on a bad one, while the in-process tree raises.
    Invariance keeps the two spellings apart, so each has its own
    overload and the body takes what both give it.
    """
    for name in MODULES:
        import_module("." + name, __name__).register(subparsers)
    refuse_undeclared_targets(subparsers.choices)

    import_module("pymux.plugins").register(subparsers)


def refuse_undeclared_targets(parsers: Mapping[str, argparse.ArgumentParser]) -> None:
    "A target spelled with a plain `add_argument` completes as nothing and forwards as typed."
    undeclared = sorted(
        "%s %s" % (name, "/".join(action.option_strings) or action.dest)
        for name, parser in parsers.items()
        for action in parser._actions
        if action.metavar in TARGET_METAVARS and target_of(action) is None
    )
    if undeclared:
        raise TypeError("a target needs add_target, not add_argument: %s" % (", ".join(undeclared),))


_parser_tree = None


def build_parser_tree() -> tuple[CommandParser, argparse._SubParsersAction[CommandParser]]:
    "A new parser of the whole command line: every command under one subparsers action."
    parser = CommandParser(prog="pymux", add_help=False, allow_abbrev=False)
    subparsers = parser.add_subparsers(metavar="COMMAND", parser_class=CommandParser)
    add_commands_to(subparsers)
    return parser, subparsers


def parser_tree():
    """
    The tree that runs commands. Built once, on first use.

    **Completion never touches it.** argcomplete swaps the class of
    every parser it walks for its own, whose copy of argparse's
    `_parse_known_args` keeps a `--` that Python 3.14 drops, and it
    never swaps back. A `bind-key x -- next-window` after any completion
    then bound the key `['x', '--']`. The completer builds a tree of its
    own. Lillecarl/pyterm#737.
    """
    global _parser_tree
    if _parser_tree is None:
        _parser_tree = build_parser_tree()
    return _parser_tree


def handle_command(pymux: Pymux, input_string: str, targets: Mapping[str, str] | None = None):
    """
    Handle command.

    Like tmux, several commands can be given at once, separated by an
    unquoted semicolon. E.g. `send-keys -t %5 -R ; clear-history -t %5`.

    `targets` stand in for the targets the words name, by argparse
    dest: a peer that forwards a command resolved them in this
    server's ids (`mirror.forward`).

    **The answer is `None`, or the rest of the work.** A handler that
    only reads the server is a plain function and everything it does
    is done when it returns. A handler that has to wait -- for a
    program to exit, for a channel to be woken -- is a coroutine, and
    then this answers with one that finishes the whole line.
    Lillecarl/pyterm#89.
    """
    logger.debug("handle command: %s", input_string)
    try:
        commands = read_commands(pymux, input_string)
    except grammar.ParseError as e:
        pymux.show_message("Invalid command %s: %s" % (input_string.strip(), e.message))
        pymux.add_command_error("pymux: %s" % (e.message,))
        return None
    return _run_in_order(pymux, [command.strings() for command in commands], targets)


def read_commands(pymux: Pymux, text: str, line: int = 1) -> list[grammar.Command]:
    """
    The commands of `text`, read with tmux's grammar against this server.

    `$NAME` reads the global environment over the server's own, as in
    tmux. `NAME=value` sets it there. `%hidden NAME=value` is seen by
    the rest of this text only: pymux has no hidden scope for it to
    live in. A `%if` asks a tmux format.
    """
    from pymux.core.environment import global_environment
    from pymux.format import Language, format_pymux_string

    def condition(text: str) -> bool:
        # A configuration is read before the first window: tmux formats
        # with what there is.
        return format_pymux_string(pymux, text, language=Language.TMUX, windowless=True) not in ("", "0")

    def assign(name: str, value: str, hidden: bool) -> None:
        if not hidden:
            pymux.global_environment[name] = value

    commands = grammar.parse(text, environment=global_environment(pymux), condition=condition, assign=assign, line=line)
    _known_in_blocks(commands)
    return commands


def _known_in_blocks(commands: list[grammar.Command]) -> None:
    """
    Refuse a block whose command does not exist, as tmux does while it
    reads one. `-t {end}` unquoted is a block that runs `end`, and it
    would otherwise reach the command as the word `end`, in silence.
    """
    _parser, subparsers = parser_tree()
    for command in commands:
        for word in command.words:
            if not isinstance(word, grammar.Block):
                continue
            for inner in word.commands:
                name = inner.words[0]
                if isinstance(name, str) and ALIASES.get(name, name) not in subparsers.choices:
                    raise grammar.ParseError(inner.line, "unknown command: %s" % (name,))
            _known_in_blocks(word.commands)


def run_arguments(pymux: Pymux, words: list[str], targets: Mapping[str, str] | None = None):
    "Run words split as tmux's command line splits them: a word ending in `;` ends a command."
    return _run_in_order(pymux, grammar.from_arguments(words), targets)


def _run_in_order(pymux: Pymux, commands: list[list[str]], targets: Mapping[str, str] | None = None):
    """
    Run the commands of one line, left to right.

    **The order holds across a wait.** `wait-for done ; kill-pane`
    means the pane dies after the wait, so the first handler that
    answers with a coroutine takes the commands after it with it.
    """
    for index, args in enumerate(commands):
        if not args:
            continue

        answer = call_command_handler(args[0], pymux, args[1:], targets)
        if answer is None:
            continue

        rest = commands[index + 1 :]
        if not any(rest):
            return answer  # Nothing follows it, so nothing wraps it.
        return _then_the_rest(pymux, answer, rest, targets)

    return None


async def _then_the_rest(
    pymux: Pymux, answer: Awaitable[Any], rest: list[list[str]], targets: Mapping[str, str] | None
) -> None:
    await answer

    more = _run_in_order(pymux, rest, targets)
    if more is not None:
        await more


def call_command_handler(command: str, pymux: Pymux, arguments: list[str], targets: Mapping[str, str] | None = None):
    """
    Execute one command, given its words, and the `targets` that stand
    in for what they name (`handle_command`).

    Answers with `None`, or with what the handler left to await.
    """
    # Resolve aliases.
    command = ALIASES.get(command, command)

    _parser, subparsers = parser_tree()
    parser = subparsers.choices.get(command)
    if parser is None:
        pymux.show_message("Invalid command: %s" % (command,))
        pymux.add_command_error("pymux: invalid command: %s" % (command,))
        if pymux.options.action_log:
            note(pymux, Kind.COMMAND, command, words_of(command, arguments), "invalid command")
        return None

    try:
        namespace = parser.parse_args(list(arguments))
    except BadLine as e:
        usage = parser.format_usage()[len("usage: ") :].rstrip()
        message = "%s (%s)" % (e.message, usage)
        pymux.show_message(message)
        pymux.add_command_error("pymux: %s" % (message,))
        if pymux.options.action_log:
            note(pymux, Kind.COMMAND, command, words_of(command, arguments), e.message)
        return None

    # **Every route to a command comes through here**: a key binding,
    # the command bar, a configuration file and the socket. So this is
    # the one place the read-only answer has to be given, and a second
    # one would be a second thing to keep in step.
    #
    # One command at a time, where tmux refuses a whole `;` line before
    # any of it runs (`cmd_list_all_have`, `key-bindings.c:769`). A
    # binding here holds one command, so the difference shows only for
    # a line somebody types, and a read-only client cannot open the
    # command bar: `command-prompt` is not a read-only command.
    if pymux.refusing_commands:
        _failed(pymux, command, namespace, CommandException("the server is upgrading"))
        return None

    client_state = this_client(pymux)
    if client_state is not None and client_state.read_only and not namespace._read_only:
        _failed(pymux, command, namespace, CommandException("client is read-only"))
        return None

    try:
        if targets:
            _take_targets(command, parser, namespace, targets)
            answer = namespace._handler(pymux, namespace)
        elif namespace._forwards and pymux.peers.by_address:
            # A command on a session another server owns runs there.
            from pymux.mirror.forward import forwarded

            answer = forwarded(pymux, command, arguments, namespace, targets_of(parser))
            if answer is None:
                answer = namespace._handler(pymux, namespace)
        else:
            answer = namespace._handler(pymux, namespace)
    except CommandException as e:
        _failed(pymux, command, namespace, e)
        return None

    if answer is not None:
        # A coroutine: the command is not done, so neither the message
        # it may fail with nor the redraw it earns can happen yet.
        return _finish(pymux, command, namespace, answer)

    _ran(pymux, command, namespace)
    return None


def _take_targets(
    command: str, parser: argparse.ArgumentParser, namespace: argparse.Namespace, targets: Mapping[str, str]
) -> None:
    "Put the targets a peer resolved in place of what the words named."
    declared: dict[str, Target] = targets_of(parser) if namespace._forwards else {}
    for dest, target in targets.items():
        if dest not in declared:
            raise CommandException("%s takes no target %s" % (command, dest))
        setattr(namespace, dest, target)


async def _finish(pymux: Pymux, command: str, namespace: argparse.Namespace, answer: Awaitable[Any]) -> None:
    "Wait for a handler that answers later, and end it the same way."
    try:
        await answer
    except CommandException as e:
        _failed(pymux, command, namespace, e)
        return
    except Exception as e:
        # Up from here only the log hears it, and the caller hears that
        # the command ran: `upgrade-server` answered 0 to every upgrade
        # a snapshot bug refused.
        logger.error("%s failed.", command, exc_info=e)
        _failed(pymux, command, namespace, CommandException("%s failed: %r" % (command, e)))
        return

    _ran(pymux, command, namespace)


def _ran(pymux: Pymux, command: str, namespace: argparse.Namespace) -> None:
    pymux.invalidate(Woke.COMMAND_RAN % command)
    if pymux.options.action_log:
        note(pymux, Kind.COMMAND, command, arguments_of(command, namespace))


def _failed(pymux: Pymux, command: str, namespace: argparse.Namespace, error: CommandException) -> None:
    pymux.show_message(error.message)
    pymux.add_command_error("pymux: %s" % (error.message,))
    if pymux.options.action_log:
        note(pymux, Kind.COMMAND, command, arguments_of(command, namespace), error.message)
