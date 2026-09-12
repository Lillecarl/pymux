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


def _case_insensitive(completion, prefix):
    """
    The bar completes keys and commands without case (a capital `C`
    offers `ctrl`, the way the key completer always has), so the
    filter that keeps a completion reads both words without case.
    """
    return completion.lower().startswith(prefix.lower())


_finder = None


def create_command_completer(pymux):
    """
    The completer of the command bar, with the completers of the
    values attached to the arguments they complete.
    """
    global _finder
    if _finder is None:
        parser, subparsers = the_parser()
        _finder = argcomplete.CompletionFinder(
            parser,
            append_space=False,
            default_completer=SuppressCompleter(),
            validator=_case_insensitive,
        )
        for name, command_parser in subparsers.choices.items():
            for action in command_parser._actions:
                fn = _VALUE_COMPLETERS.get((name, action.dest))
                if fn is not None:
                    action.completer = partial(fn, pymux)
    return CommandCompleter()
