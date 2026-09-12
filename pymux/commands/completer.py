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

import argparse
from functools import partial

import argcomplete
from argcomplete.completers import SuppressCompleter
from argcomplete.lexers import split_line
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.document import Document

from pymux.arrangement import LayoutTypes
from pymux.commands import the_parser
from pymux.commands.aliases import ALIASES
from pymux.key_spelling import KeyCompleter

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
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.commenters = ""

    # `lexers.py` binds this same module object, so the change reaches it.
    _shlex.shlex = _Uncommented


stop_shlex_comments()


def _keys(pymux, prefix, **_):
    """
    The key names for `bind-key` and `compose-key`.

    Both read a chord as well as a tmux name, so both offer the chord:
    a list of the older spelling beside a command that takes either is a
    person never finding out about ctrl+Home. Lillecarl/pymux#234.

    Not the prefix. `bind-key` says whether a binding needs it with
    `-n`, and `send-keys` sends to a pane, where `send-prefix` is the
    command that sends the prefix on. Neither takes it as part of a key.
    """
    completer = KeyCompleter(offer_the_prefix=False)
    return [c.text for c in completer.get_completions(Document(prefix), None)]


def _session_option_names(pymux, **_):
    return sorted(pymux.options)


def _window_option_names(pymux, **_):
    return sorted(pymux.window_options)


def _option_values(pymux, parsed_args, **_):
    option = pymux.options.get(parsed_args.option)
    return sorted(option.get_all_values(pymux)) if option else []


def _window_option_values(pymux, parsed_args, **_):
    option = pymux.window_options.get(parsed_args.option)
    return sorted(option.get_all_values(pymux)) if option else []


def _layout_names(pymux, **_):
    return sorted(t.value for t in LayoutTypes)


def _bound_command(pymux, prefix, parsed_args, **_):
    """
    The command a `bind-key` binding runs, and its arguments: the same
    question again, one word further in. The words already given land
    in `parsed_args.arguments`, and the finder reads the line they
    make against the whole tree. It re-enters itself, so the help of
    what it finds lands in the same batch of completions.
    """
    matches = _finder._get_completions(["pymux", *parsed_args.arguments], prefix, "", None)
    meta = _finder.get_display_completions()
    return {m: meta.get(m, "") for m in matches}


def _send_keys_names(pymux, prefix, parsed_args, **_):
    "The keys are names while they are the first thing, and no `-l` says they are text."
    if parsed_args.keys or parsed_args.l:
        return []
    return _keys(pymux, prefix)


#: What completes a value, by the command and the dest of the argument.
_VALUE_COMPLETERS = {
    ("set-option", "option"): _session_option_names,
    ("set-option", "value"): _option_values,
    ("set-window-option", "option"): _window_option_names,
    ("set-window-option", "value"): _window_option_values,
    ("select-layout", "layout_type"): _layout_names,
    ("compose-key", "default"): _keys,
    ("bind-key", "key"): _keys,
    ("bind-key", "arguments"): _bound_command,
    ("send-keys", "keys"): _send_keys_names,
}


def _command_help(name, subparsers):
    "The help of one command, which argparse records on a pseudo action."
    for action in subparsers._choices_actions:
        if action.metavar == name:
            return action.help or ""
    return ""


class CommandCompleter(Completer):
    """
    The completer of the command bar.
    """

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        prequote, prefix, _suffix, words, wordbreak = split_line(text, len(text))

        _finder._display_completions = {}
        matches = _finder._get_completions(["pymux"] + words, prefix, prequote, wordbreak)
        meta = _finder.get_display_completions()

        if not words:
            names = [m for m in matches if not m.startswith("-")]
            if not names:
                # No full name matches: the aliases, spelling the name
                # they run, and what that does.
                _parser, subparsers = the_parser()
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


def matches_loosely(word, candidate) -> bool:
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
    """

    def _matches(self, word, candidate) -> bool:
        return matches_loosely(word, candidate)

    def _flag_matches(self, action, word, option_string) -> bool:
        if self._matches(word, option_string):
            return True
        if word and not word.startswith("-") and action.help:
            return self._matches(word, action.help)
        return False

    def _get_subparser_completions(self, parser, cword_prefix):
        aliases_by_parser: dict = {}
        for key in parser.choices.keys():
            p = parser.choices[key]
            aliases_by_parser.setdefault(p, []).append(key)

        for action in parser._get_subactions():
            for alias in aliases_by_parser[parser.choices[action.dest]]:
                if self._matches(cword_prefix, alias):
                    self._display_completions[alias] = self._get_action_help(action)

        return [
            subcmd
            for subcmd in parser.choices.keys()
            if self._matches(cword_prefix, subcmd)
        ]

    def _get_option_completions(self, parser, cword_prefix):
        for action in parser._actions:
            if action.option_strings:
                for option_string in action.option_strings:
                    if self._matches(cword_prefix, option_string):
                        self._display_completions[option_string] = self._get_action_help(action)

        option_completions = []
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


_finder = None


def create_command_completer(pymux):
    """
    The completer of the command bar, with the completers of the
    values attached to the arguments they complete.
    """
    global _finder
    if _finder is None:
        parser, subparsers = the_parser()
        _finder = FuzzyFinder(
            parser,
            append_space=False,
            default_completer=SuppressCompleter(),
            # argcomplete hands the validator (completion, prefix); the
            # matcher reads (word, candidate), and the word is the
            # prefix the person typed.
            validator=lambda completion, prefix: matches_loosely(prefix, completion),
        )
        for name, command_parser in subparsers.choices.items():
            for action in command_parser._actions:
                fn = _VALUE_COMPLETERS.get((name, action.dest))
                if fn is not None:
                    action.completer = partial(fn, pymux)
    return CommandCompleter()
