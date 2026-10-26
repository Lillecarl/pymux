"""
What completes a command as it is typed: argcomplete, in process.

argcomplete completes a command line for a shell by parsing the line
against the parser tree. The same move works in process: the bar
hands over the line and the cursor, argcomplete hands back the words
and their help, and nothing leaves the process. Lillecarl/pymux#307.

**The commands and their flags come from the parser tree, and every
value comes from SQL.** A value -- a session, a pane, an option, a
word an option takes, a layout, a key -- is what a saved question
answers over the tables `sql` reads (`Questions`). So SQL holds
everything Tab can offer, and a person changes an offer by saving a
question under its name.

One finder owns the tree for the life of the process: the first
completion patches the parsers with hooks that hold the finder, so a
second finder would find them already bound to the first. The same
finder serves every client, and it re-enters itself for the command
a `bind-key` binding runs.
"""

from __future__ import annotations

import argparse
import time
from functools import partial
from typing import TYPE_CHECKING, Any, Final, override

import apsw
import argcomplete
from argcomplete.completers import SuppressCompleter
from argcomplete.lexers import split_line
from prompt_toolkit.completion import CompleteEvent, Completer, Completion
from prompt_toolkit.document import Document

from pymux.commands import CommandException, CommandParser, build_parser_tree, parser_tree
from pymux.commands.aliases import ALIASES
from pymux.forwarding import Direction
from pymux.key_spelling import key_completions
from pymux.query import register, tables_of

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


def _keys(questions: Questions, prefix: str, **_) -> dict[str, str]:
    """
    The key names for `bind-key` and `compose-key`.

    Both read a chord as well as a tmux name, so both offer the chord:
    a list of the older spelling beside a command that takes either is a
    person never finding out about ctrl+Home. Lillecarl/pymux#234.

    Not the prefix. `bind-key` says whether a binding needs it with
    `-n`, and `send-keys` sends to a pane, where `send-prefix` is the
    command that sends the prefix on. Neither takes it as part of a key.
    """
    words = [(str(row[0]), str(row[1] or ""), str(row[2])) for row in questions.rows("complete-key") if len(row) > 2]
    # The grammar completes the last segment; argcomplete replaces the
    # whole word, so each answer carries the segments before it.
    return {
        prefix[: len(prefix) + c.start_position] + c.text: c.display_meta_text
        for c in key_completions(prefix, words, offer_prefix=False)
    }


def _scopes_of(scope: str, parsed_args: argparse.Namespace) -> tuple[str, ...]:
    "`set-option` takes the server's names too, and `-s` takes only those."
    if scope != "session":
        return (scope,)
    if getattr(parsed_args, "s", False):
        return ("server",)
    return ("session", "server")


def _option_names(scope: str, questions: Questions, parsed_args: argparse.Namespace, **_) -> dict[str, str]:
    found: dict[str, str] = {}
    for each in _scopes_of(scope, parsed_args):
        found |= questions.ask("complete-option", scope=each)
    return found


def _option_values(scope: str, questions: Questions, parsed_args: argparse.Namespace, **_) -> dict[str, str]:
    found: dict[str, str] = {}
    for each in _scopes_of(scope, parsed_args):
        found |= questions.ask("complete-option-value", scope=each, option=parsed_args.option)
    return found


def _bound_command(questions: Questions, prefix: str, parsed_args: argparse.Namespace, **_) -> dict[str, str]:
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


def _send_keys_names(questions: Questions, prefix: str, parsed_args: argparse.Namespace, **_) -> dict[str, str]:
    "The keys are names while they are the first thing, and no `-l` says they are text."
    if parsed_args.keys or parsed_args.l:
        return {}
    return _keys(questions, prefix)


def _forward_listenings(direction: Direction, questions: Questions, **_) -> dict[str, str]:
    "The listening ends `unforward-port` can name, of the client it forwards for."
    try:
        connection = questions.pymux.forwarding_client().client_state.connection
    except CommandException, ValueError:
        return {}
    return questions.ask("complete-forward", client=connection.client_id or "", direction=direction.value)


#: How long one completion question may run. It runs on the loop, and a
#: question a person saved over a seed can be as slow as they wrote it.
#: Not less: the values of `theme` take 65 ms to list, measured.
QUESTION_BUDGET: Final = 0.25


class Questions:
    """
    The completion questions of one server, on a connection of its own.

    A value is what a saved question answers, as `word` and `says`: the
    seeds `complete-session`, `complete-pane` and the rest in `SEEDS`,
    or whatever a person saved under that name instead. So the values
    come from the same tables `sql` reads, and a value that cannot be
    asked for there cannot be offered here.

    **The connection runs on the loop, not on a worker.** argcomplete
    asks without awaiting, and the tables read the server's objects as
    the question reaches them, which only the loop may do.
    """

    def __init__(self, pymux: Pymux) -> None:
        self.pymux = pymux
        self._db: apsw.Connection | None = None

    def rows(self, name: str, **params: str) -> list[tuple]:
        "What the question of that name answers, or nothing when it fails or outruns its budget."
        saved = self.pymux.jobs.queries.get(name)
        if saved is None:
            return []
        if self._db is None:
            self._db = apsw.Connection(":memory:")
            register(self._db, tables_of(self.pymux), on_loop=False)
        deadline = time.monotonic() + QUESTION_BUDGET
        self._db.set_progress_handler(lambda: time.monotonic() > deadline, 1000)
        try:
            return [tuple(row) for row in self._db.execute(saved[0], params)]
        except apsw.Error, KeyError:
            # A KeyError is a `:name` the question reads and nothing fills.
            return []

    def ask(self, name: str, **params: str) -> dict[str, str]:
        "The words a question answers, each with what it says beside it."
        found = {
            str(row[0]): str(row[1]) if len(row) > 1 and row[1] is not None else "" for row in self.rows(name, **params)
        }
        if _finder is not None:
            # The words beside each match as well.
            _finder.descriptions.update(found)
        return found


def _asked(name: str, questions: Questions, **_) -> dict[str, str]:
    return questions.ask(name)


def _tagged(questions: Questions, parsed_args: argparse.Namespace, **_) -> dict[str, str]:
    "`set-tag -t` names the kind its flag says, and a pane without one."
    kind = "job" if parsed_args.j else "session" if parsed_args.s else "window" if parsed_args.w else "pane"
    return questions.ask("complete-%s" % kind)


#: What completes a target, by the metavar that spells its kind. The
#: metavars are tmux's own, and `test_every_target_a_command_spells_completes`
#: holds that each one a command spells is here.
TARGET_COMPLETERS: Final = {
    "<target-session>": partial(_asked, "complete-session"),
    "<target-window>": partial(_asked, "complete-window"),
    "<src-window>": partial(_asked, "complete-window"),
    "<dst-window>": partial(_asked, "complete-window"),
    "<target-pane>": partial(_asked, "complete-pane"),
    "<src-pane>": partial(_asked, "complete-pane"),
    "<target-client>": partial(_asked, "complete-client"),
    "<job>": partial(_asked, "complete-job"),
    "[<dst-session>:]<dst-index>": partial(_asked, "complete-session-end"),
}

#: What completes a value, by the command and the dest of the argument.
#: Every one but `bind-key`'s command asks a question; that one is the
#: command line again, which the parser tree completes.
_VALUE_COMPLETERS = {
    ("set-tag", "target"): _tagged,
    ("show-tags", "target"): _tagged,
    ("set-option", "option"): partial(_option_names, "session"),
    ("set-option", "value"): partial(_option_values, "session"),
    ("set-window-option", "option"): partial(_option_names, "window"),
    ("set-window-option", "value"): partial(_option_values, "window"),
    ("set-client-option", "option"): partial(_option_names, "client"),
    ("set-client-option", "value"): partial(_option_values, "client"),
    ("show-options", "option"): partial(_option_names, "session"),
    ("show-window-options", "option"): partial(_option_names, "window"),
    ("show-client-options", "option"): partial(_option_names, "client"),
    ("select-layout", "layout_type"): partial(_asked, "complete-layout"),
    ("compose-key", "default"): _keys,
    ("bind-key", "key"): _keys,
    ("bind-key", "arguments"): _bound_command,
    ("send-keys", "keys"): _send_keys_names,
    ("unforward-port", "local"): partial(_forward_listenings, Direction.LOCAL),
    ("unforward-port", "remote"): partial(_forward_listenings, Direction.REMOTE),
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

        # What starts with the word before what only holds it, so `sta`
        # puts `status` in front of `pane-border-status`. A word with no
        # dash is a value first: a flag it matches through its help comes
        # after, or `-g` ("what a session starts with") stands in front.
        matches = sorted(
            matches,
            key=lambda m: (not prefix.startswith("-") and m.startswith("-"), not m.startswith(prefix)),
        )
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


class FuzzyFinder(argcomplete.ExclusiveCompletionFinder):
    """
    A finder that keeps what the word matches loosely, not only what
    it starts.

    Exclusive: a flag already on the line is not offered again, unless
    it is one that repeats (`--tag`).

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
    questions = Questions(pymux)
    for name, command_parser in subparsers.choices.items():
        for action in command_parser._actions:
            # A custom completer, which is argcomplete's own extension
            # point: it reads `action.completer` when one is set, and
            # argparse never declares it.
            fn = _VALUE_COMPLETERS.get((name, action.dest)) or TARGET_COMPLETERS.get(str(action.metavar))
            if fn is not None:
                action.completer = partial(fn, questions)  # pyrefly: ignore[missing-attribute]
    return CommandCompleter()
