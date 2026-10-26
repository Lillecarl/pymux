"""
What completes a command as it is typed: argcomplete, in process.

argcomplete completes a command line for a shell by parsing the line
against the parser tree. The same move works in process: the bar
hands over the line and the cursor, argcomplete hands back the words
and their help, and nothing leaves the process. The values that only
the running server knows -- the options, the words an option takes,
the layout names, the keys -- are attached to their arguments below,
by command and dest. Lillecarl/pymux#307.

One finder owns the tree for the life of the process: the first
completion patches the parsers with hooks that hold the finder, so a
second finder would find them already bound to the first. The same
finder serves every client, and it re-enters itself for the command
a `bind-key` binding runs.
"""

from __future__ import annotations

import argparse
from functools import partial
from typing import TYPE_CHECKING, Any, Final, override

import argcomplete
from argcomplete.completers import SuppressCompleter
from argcomplete.lexers import split_line
from prompt_toolkit.completion import CompleteEvent, Completer, Completion
from prompt_toolkit.document import Document

from pymux.arrangement import LayoutTypes
from pymux.commands import CommandException, CommandParser, build_parser_tree, parser_tree
from pymux.commands.aliases import ALIASES
from pymux.format import Language, format_pymux_string, tmux_pane_id_prefix
from pymux.forwarding import Direction
from pymux.key_spelling import KeyCompleter

if TYPE_CHECKING:
    from pymux.main import Pymux

__all__ = ["create_command_completer", "stop_shlex_comments"]


def stop_shlex_comments() -> None:
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
    this correction for itself. The command bar lexes through
    `split_line` too, so the completer module installs it on import.
    """
    from argcomplete.packages import _shlex

    class _Uncommented(_shlex.shlex):
        def __init__(self, *args: Any, **kwargs: Any):
            super().__init__(*args, **kwargs)
            self.commenters = ""

    # `lexers.py` binds this same module object, so the change reaches it.
    _shlex.shlex = _Uncommented


stop_shlex_comments()


def _keys(pymux: Pymux, prefix: str, **_) -> list[str]:
    """
    The key names for `bind-key` and `compose-key`.

    Both read a chord as well as a tmux name, so both offer the chord:
    a list of the older spelling beside a command that takes either is a
    person never finding out about ctrl+Home. Lillecarl/pymux#234.

    Not the prefix. `bind-key` says whether a binding needs it with
    `-n`, and `send-keys` sends to a pane, where `send-prefix` is the
    command that sends the prefix on. Neither takes it as part of a key.
    """
    completer = KeyCompleter(offer_prefix=False)
    return [c.text for c in completer.get_completions(Document(prefix), CompleteEvent())]


def _session_option_names(pymux: Pymux, **_) -> list[str]:
    return sorted(pymux.options)


def _window_option_names(pymux: Pymux, **_) -> list[str]:
    return sorted(pymux.window_options)


def _option_values(pymux: Pymux, parsed_args: argparse.Namespace, **_) -> list[str]:
    option = pymux.options.get(parsed_args.option)
    return sorted(option.get_all_values(pymux)) if option else []


def _window_option_values(pymux: Pymux, parsed_args: argparse.Namespace, **_) -> list[str]:
    option = pymux.window_options.get(parsed_args.option)
    return sorted(option.get_all_values(pymux)) if option else []


def _client_option_names(pymux: Pymux, **_) -> list[str]:
    return sorted(pymux.client_options)


def _client_option_values(pymux: Pymux, parsed_args: argparse.Namespace, **_) -> list[str]:
    option = pymux.client_options.get(parsed_args.option)
    return sorted(option.get_all_values(pymux)) if option else []


def _layout_names(pymux: Pymux, **_) -> list[str]:
    return sorted(t.value for t in LayoutTypes)


def _bound_command(pymux: Pymux, prefix: str, parsed_args: argparse.Namespace, **_) -> dict[str, str]:
    """
    The command a `bind-key` binding runs, and its arguments: the same
    question again, one word further in. The words already given land
    in `parsed_args.arguments`, and the finder reads the line they
    make against the whole tree. It re-enters itself, so the help of
    what it finds lands in the same batch of completions.
    """
    finder = _finder
    if finder is None:
        return {}
    matches = finder._get_completions(["pymux", *parsed_args.arguments], prefix, "", None)
    meta = finder.get_display_completions()
    return {m: meta.get(m, "") for m in matches}


def _send_keys_names(pymux: Pymux, prefix: str, parsed_args: argparse.Namespace, **_) -> list[str]:
    "The keys are names while they are the first thing, and no `-l` says they are text."
    if parsed_args.keys or parsed_args.l:
        return []
    return _keys(pymux, prefix)


def _forward_listenings(pymux: Pymux, parsed_args: argparse.Namespace, direction: Direction, **_) -> dict[str, str]:
    """
    The listening ends `unforward-port` can name.

    **The number offered is the number shown.** `list-forwards` draws
    the port really bound, so a forward that asked for any free port is
    reachable only by the number a person read there. Offering that same
    spelling is what makes the offer removable. Lillecarl/pymux#441.
    """
    try:
        connection = pymux.forwarding_client().client_state.connection
    except CommandException, ValueError:
        return {}

    return {
        "%s:%s" % (one.get("listen_host", ""), one.get("port", "")): one.get("dest", "")
        for one in connection.forwards
        if one.get("direction") == direction.value
    }


#: What each kind of target says about itself beside its spelling, as
#: tmux formats. A person finds a thing by these words as well as by
#: its spelling: `vim` reaches the pane that runs it.
SESSION_SAYS: Final = "#{session_windows} windows#{?session_attached, (attached),}"
WINDOW_SAYS: Final = "#{window_name}, #{window_panes} panes"
PANE_SAYS: Final = "#{session_name}:#{window_index}.#{pane_index} #{pane_current_command} #{pane_title}"


def _offer(found: dict[str, str]) -> dict[str, str]:
    "Answer with these, and let the words beside each match as well."
    if _finder is not None:
        _finder.descriptions.update(found)
    return found


def _says(pymux: Pymux, template: str, **things) -> str:
    try:
        return format_pymux_string(pymux, template, language=Language.TMUX, **things).strip()
    except CommandException:
        # A session with no window yet has nothing to format over.
        return ""


def _sessions(pymux: Pymux, **_) -> dict[str, str]:
    return _offer({session.name: _says(pymux, SESSION_SAYS, session=session) for session in pymux.sessions})


def _session_ends(pymux: Pymux, **_) -> dict[str, str]:
    "A session alone, which puts a window after the last one there."
    return _offer({"%s:" % session.name: "after the last window" for session in pymux.sessions})


def _windows(pymux: Pymux, **_) -> dict[str, str]:
    return _offer(
        {
            "%s:%s" % (session.name, window.index): _says(pymux, WINDOW_SAYS, session=session, window=window)
            for session in pymux.sessions
            for window in session.arrangement.windows
        }
    )


def _panes(pymux: Pymux, **_) -> dict[str, str]:
    return _offer(
        {
            "%s%s" % (tmux_pane_id_prefix(), pane.pane_id): _says(
                pymux, PANE_SAYS, session=session, window=window, pane=pane
            )
            for session in pymux.sessions
            for window in session.arrangement.windows
            for pane in window.panes
        }
    )


def _clients(pymux: Pymux, **_) -> dict[str, str]:
    return _offer(
        {
            getattr(client_state, "name", "") or client_state.connection.name: client_state.session.name
            for client_state in pymux.clients
        }
    )


def _jobs(pymux: Pymux, **_) -> dict[str, str]:
    return _offer({str(job.job_id): job.command for job in pymux.jobs.listing()})


def _tagged(pymux: Pymux, parsed_args: argparse.Namespace, **_) -> dict[str, str]:
    "`set-tag -t` names the kind its flag says, and a pane without one."
    if parsed_args.j:
        return _jobs(pymux)
    if parsed_args.s:
        return _sessions(pymux)
    if parsed_args.w:
        return _windows(pymux)
    return _panes(pymux)


#: What completes a target, by the metavar that spells its kind. The
#: metavars are tmux's own, and `test_every_target_completes` holds
#: that each one a command spells is here.
TARGET_COMPLETERS: Final = {
    "<target-session>": _sessions,
    "<target-window>": _windows,
    "<src-window>": _windows,
    "<dst-window>": _windows,
    "<target-pane>": _panes,
    "<src-pane>": _panes,
    "<target-client>": _clients,
    "<job>": _jobs,
    "[<dst-session>:]<dst-index>": _session_ends,
}


#: What completes a value, by the command and the dest of the argument.
_VALUE_COMPLETERS = {
    ("set-option", "option"): _session_option_names,
    ("set-option", "value"): _option_values,
    ("set-window-option", "option"): _window_option_names,
    ("set-window-option", "value"): _window_option_values,
    ("set-client-option", "option"): _client_option_names,
    ("set-client-option", "value"): _client_option_values,
    ("select-layout", "layout_type"): _layout_names,
    ("compose-key", "default"): _keys,
    ("bind-key", "key"): _keys,
    ("bind-key", "arguments"): _bound_command,
    ("send-keys", "keys"): _send_keys_names,
    ("unforward-port", "local"): partial(_forward_listenings, direction=Direction.LOCAL),
    ("unforward-port", "remote"): partial(_forward_listenings, direction=Direction.REMOTE),
    ("set-tag", "target"): _tagged,
    ("show-tags", "target"): _tagged,
}


def _command_help(name: str, subparsers: argparse._SubParsersAction[CommandParser]):
    "The help of one command, which argparse records on a pseudo action."
    for action in subparsers._choices_actions:
        if action.metavar == name:
            return action.help or ""
    return ""


class CommandCompleter(Completer):
    """
    The completer of the command bar.
    """

    @override
    def get_completions(self, document: Document, complete_event: CompleteEvent):
        text = document.text_before_cursor
        prequote, prefix, _suffix, words, wordbreak = split_line(text, len(text))

        finder = _finder
        if finder is None:
            return
        finder._display_completions = {}
        finder.descriptions = {}
        matches = finder._get_completions(["pymux", *words], prefix, prequote, wordbreak)
        meta = finder.get_display_completions()

        if not words:
            names = [m for m in matches if not m.startswith("-")]
            if not names:
                # No full name matches: the aliases, spelling the name
                # they run, and what that does.
                _parser, subparsers = parser_tree()
                for alias in ALIASES:
                    if alias.startswith(prefix):
                        full = ALIASES[alias]
                        yield Completion(
                            full,
                            start_position=-len(prefix),
                            display="%s (%s)" % (alias, full),
                            display_meta=_command_help(full, subparsers),
                        )
                return

        for m in matches:
            # The match carries the prefix; it replaces the word being
            # typed whole.
            yield Completion(m, start_position=-len(prefix), display_meta=meta.get(m, ""))


def matches_loosely(word: str, candidate: str) -> bool:
    """
    The word is in the candidate, without case, anywhere in it.

    The command bar is where the tmux spellings live, and
    remembering them exactly is the failure mode: `option` reaches
    `set-option` without the set, `vert` reaches `even-vertical`
    without the even. Lillecarl/pymux#269.
    """
    return word.lower() in candidate.lower()


class FuzzyFinder(argcomplete.CompletionFinder):
    """
    A finder that keeps what the word matches loosely, not only what
    it starts.

    argcomplete filters three of its four candidates by a prefix --
    the subcommands, the flags and the words a completer answered;
    the validator is the fourth. The first three are "exposed for
    overriding", and this is the override: the same walks, with
    `matches_loosely` where they said `startswith`.

    A flag also matches what its help says, when the word does not
    start with a dash: the ask of Lillecarl/pymux#148 was that a
    flag lists without the dash at all, and "-d" says nothing about
    itself, while "Leave the new window unfocused" says plenty.

    A target matches what it says about itself for the same reason:
    `%12` says nothing, and `vim` is how a person remembers the pane.
    argcomplete hands the validator only the candidate, so a target
    completer leaves its words in `descriptions` for it.
    """

    def __init__(self, parser: argparse.ArgumentParser, **kwargs: Any):
        super().__init__(parser, validator=self._valid, **kwargs)
        self.descriptions: dict[str, str] = {}

    def _valid(self, completion: str, prefix: str) -> bool:
        # argcomplete hands (completion, prefix); the matcher reads
        # (word, candidate), and the word is the prefix the person typed.
        return matches_loosely(prefix, completion) or matches_loosely(prefix, self.descriptions.get(completion, ""))

    def _matches(self, word: str, candidate: str) -> bool:
        return matches_loosely(word, candidate)

    def _flag_matches(self, action: argparse.Action, word: str, option_string: str) -> bool:
        if self._matches(word, option_string):
            return True
        if word and not word.startswith("-") and action.help:
            return self._matches(word, action.help)
        return False

    @override
    def _get_subparser_completions(self, parser: argparse._SubParsersAction[CommandParser], cword_prefix: str):
        aliases_by_parser: dict[argparse.ArgumentParser, list[str]] = {}
        for key in parser.choices.keys():
            p = parser.choices[key]
            aliases_by_parser.setdefault(p, []).append(key)

        for action in parser._get_subactions():
            for alias in aliases_by_parser[parser.choices[action.dest]]:
                if self._matches(cword_prefix, alias):
                    self._display_completions[alias] = self._get_action_help(action)

        return [subcmd for subcmd in parser.choices.keys() if self._matches(cword_prefix, subcmd)]

    @override
    def _get_option_completions(self, parser: argparse.ArgumentParser, cword_prefix: str):
        for action in parser._actions:
            if action.option_strings:
                for option_string in action.option_strings:
                    if self._matches(cword_prefix, option_string):
                        self._display_completions[option_string] = self._get_action_help(action)

        option_completions: list[str] = []
        for action in parser._actions:
            if not self.print_suppressed:
                completer = getattr(action, "completer", None)
                if isinstance(completer, SuppressCompleter) and completer.suppress():
                    continue
                if action.help == argparse.SUPPRESS:
                    continue
            if not self._action_allowed(action, parser):
                continue
            if not isinstance(action, argparse._SubParsersAction):
                option_completions += [
                    option_string
                    for option_string in action.option_strings
                    if self._flag_matches(action, cword_prefix, option_string)
                ]
        return option_completions


_finder: FuzzyFinder | None = None
_completion_tree: tuple[CommandParser, argparse._SubParsersAction[CommandParser]] | None = None


def create_command_completer(pymux: Pymux):
    """
    The completer of the command bar, with the completers of the
    values attached to the arguments they complete.

    **The finder is made once and the values are bound every time.** The
    finder owns the tree for the life of the process, which is what the
    module docstring says and is right. What a value completer reads is
    the server, and binding that once meant the second server in a
    process completed against the first: `set-option <TAB>` offered the
    options of a session nobody was looking at. One process serves one
    server, so nothing a person does reaches it; a suite that makes a
    `Pymux` per test does, and the answer it got depended on test order.
    """
    global _finder, _completion_tree
    # Not `parser_tree()`: argcomplete patches what it walks for good.
    if _completion_tree is None:
        _completion_tree = build_parser_tree()
    parser, subparsers = _completion_tree
    if _finder is None:
        _finder = FuzzyFinder(
            parser,
            append_space=False,
            default_completer=SuppressCompleter(),
        )
    for name, command_parser in subparsers.choices.items():
        for action in command_parser._actions:
            fn = _VALUE_COMPLETERS.get((name, action.dest)) or TARGET_COMPLETERS.get(str(action.metavar))
            if fn is not None:
                # A custom completer, which is argcomplete's own
                # extension point: it reads `action.completer` when one
                # is set, and argparse never declares it.
                action.completer = partial(fn, pymux)  # pyrefly: ignore[missing-attribute]
    return CommandCompleter()
