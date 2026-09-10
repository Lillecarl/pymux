"""
What completes a command as it is typed.

The parser tree of the commands is the one description of what a
command takes, and this completer is one of its three readers: the
words typed so far are walked against the parser of the command, and
what the next word can be is read off the action it would land in.

The values that only the running server knows — the names of the
options, the words an option takes, the layout names and the keys —
are attached by dest name in `_VALUE_COMPLETERS` below. Everything
else, the flags and their help lines, comes from the tree.
"""

import argparse
from functools import partial

from prompt_toolkit.completion import Completer, Completion, WordCompleter
from prompt_toolkit.document import Document

from pymux.arrangement import LayoutTypes
from pymux.key_spelling import KeyCompleter

from .aliases import ALIASES
from .commands import COMMANDS_TO_HANDLERS, COMMANDS_TO_PARSERS
from .utils import wrap_argument

#: The nargs of a positional that takes the rest of the line. The
#: option of a command that `bind-key` runs lands in one, however much
#: it looks like an option of `bind-key`.
REMAINDER = argparse.REMAINDER

__all__ = ["create_command_completer"]


def create_command_completer(pymux):
    return ShlexCompleter(partial(get_completions_for_parts, pymux=pymux))


class CommandCompleter(Completer):
    """
    Completer for command names.
    """

    def __init__(self):
        # Completer for full command names.
        self._command_completer = WordCompleter(
            sorted(COMMANDS_TO_HANDLERS.keys()),
            ignore_case=True,
            WORD=True,
            match_middle=True,
        )

        # Completer for aliases.
        self._aliases_completer = WordCompleter(
            sorted(ALIASES.keys()), ignore_case=True, WORD=True, match_middle=True
        )

    def get_completions(self, document, complete_event):
        # First, complete on full command names.
        found = False

        for c in self._command_completer.get_completions(document, complete_event):
            found = True
            yield c

        # When no matches are found, complete aliases instead.
        # The completion however, inserts the full name.
        if not found:
            for c in self._aliases_completer.get_completions(document, complete_event):
                full_name = ALIASES.get(c.text)

                yield Completion(
                    full_name,
                    start_position=c.start_position,
                    display="%s (%s)" % (c.text, full_name),
                )


_command_completer = CommandCompleter()
_layout_type_completer = WordCompleter(sorted(t.value for t in LayoutTypes), WORD=True)
#: What completes the key of `bind-key` and `send-keys`.
#:
#: Both read a chord as well as a tmux name, so both offer the chord:
#: a list of the older spelling beside a command that takes either is a
#: person never finding out about ctrl+Home. Lillecarl/pymux#234.
#:
#: Not the prefix. `bind-key` says whether a binding needs it with
#: `-n`, and `send-keys` sends to a pane, where `send-prefix` is the
#: command that sends the prefix on. Neither takes it as part of a key.
_keys_completer = KeyCompleter(offer_the_prefix=False)


def _value_completer_for(command, dest, parser, slots, pymux):
    """
    The completer of a value, or None when the words are not its business.

    `slots` holds the words already in positional places, which is how
    `set-option` knows which option a value belongs to.
    """
    if command in ("set-option", "set-window-option"):
        options = pymux.options if command == "set-option" else pymux.window_options

        if dest == "option":
            return WordCompleter(sorted(options.keys()), sentence=True)
        if dest == "value" and slots:
            option = options.get(slots[-1])
            if option:
                return WordCompleter(
                    sorted(option.get_all_values(pymux)), sentence=True
                )

    elif command == "select-layout" and dest == "layout_type":
        return _layout_type_completer

    elif command == "compose-key" and dest == "default":
        return _keys_completer

    elif command == "bind-key" and dest == "key":
        return _keys_completer

    return None


def _flags_completer(parser, last_part):
    "The flags of a command that match what is typed, with their help."
    choices = {}
    for action in parser._actions:
        for option in action.option_strings:
            if option in choices or not option.startswith(last_part):
                continue
            if action.help:
                choices[option] = action.help
            elif action.nargs != 0 and action.metavar:
                choices[option] = "<%s>" % (str(action.metavar).strip("<>"),)
            else:
                choices[option] = ""
    if not choices:
        return None
    return WordCompleter(sorted(choices), meta_dict=choices, sentence=True)


def _where_the_word_goes(parser, parts, last_part):
    """
    What the word being typed will land in, read off the parser.

    Returns one of:

    - `("flags", partial)` — it starts a flag of this command.
    - `(action, slots)` — it completes the value of the option, or
      fills the positional; `slots` holds the words already in
      positional places.
    - `(None, slots)` — nothing to offer from the tree.
    """
    flags = {}
    for action in parser._actions:
        for option in action.option_strings:
            flags[option] = action

    positionals = [a for a in parser._actions if not a.option_strings]

    slots: list = []
    pending = None
    for word in parts:
        if pending is not None:
            # This word is the value of the option before it.
            slots.append(word)
            pending = None
            continue
        action = flags.get(word)
        if action is not None and action.nargs != 0:
            pending = action
            continue
        if action is not None or word == "--":
            continue
        slots.append(word)

    if pending is not None:
        # The value of an option, even when it starts with a dash:
        # `capture-pane -S -5` is a line number, not a flag.
        return (pending, slots)

    if not positionals:
        if last_part.startswith("-"):
            return ("flags", last_part)
        return (None, slots)

    # One positional takes one word, and the last one takes the rest.
    index = min(len(slots), len(positionals) - 1)
    landing = positionals[index]
    # A dash starts a flag, unless what it would land in takes the
    # rest of the line: the option of a command that `bind-key` runs
    # starts with a dash, and it is not an option of `bind-key`.
    if last_part.startswith("-") and landing.nargs != REMAINDER:
        return ("flags", last_part)
    return (landing, slots)


def get_completions_for_parts(parts, last_part, complete_event, pymux):
    # Resolve aliases.
    if len(parts) > 0:
        parts = [ALIASES.get(parts[0], parts[0])] + parts[1:]

    if len(parts) == 0:
        # New command.
        yield from _command_completer.get_completions(
            Document(last_part), complete_event
        )
        return

    parser = COMMANDS_TO_PARSERS.get(parts[0])
    if parser is None:
        return

    what, slots = _where_the_word_goes(parser, parts[1:], last_part)

    if what == "flags":
        completer = _flags_completer(parser, last_part)
        if completer:
            yield from completer.get_completions(
                Document(last_part), complete_event
            )
        return

    if what is None:
        # A command with no positional of its own: an empty word asks
        # what it takes, and the flags are the answer.
        if not last_part:
            yield from _the_flags(parser, complete_event)
        return

    if parts[0] == "bind-key" and not what.option_strings and what.dest == "arguments":
        # The command that is bound, and then its own arguments. The
        # same question again, one word further in. The first word in
        # `slots` is the key that was typed, and it is not part of the
        # question.
        yield from get_completions_for_parts(slots[1:], last_part, complete_event, pymux)
        return

    if parts[0] == "send-keys" and not what.option_strings and what.dest == "keys":
        # The keys are names to offer while they are the first thing
        # after the command and no `-l` says they are text.
        if not slots and "-l" not in parts[1:]:
            yield from _keys_completer.get_completions(
                Document(last_part), complete_event
            )
        return

    dest = what.dest
    completer = _value_completer_for(parts[0], dest, parser, slots, pymux)
    if completer is None and not last_part:
        # An empty word over a positional that has no answer of its
        # own asks what the command takes. `set-option` answers with
        # the names of the options; `split-window` has nothing to
        # say about a program to run, so the flags are the answer.
        # Lillecarl/pymux#148.
        yield from _the_flags(parser, complete_event)
        return
    if completer:
        yield from completer.get_completions(Document(last_part), complete_event)


def _the_flags(parser, complete_event):
    completer = _flags_completer(parser, "")
    if completer:
        yield from completer.get_completions(Document(""), complete_event)


class ShlexCompleter(Completer):
    """
    Completer that can be used when the input is parsed with shlex.
    """

    def __init__(self, get_completions_for_parts):
        assert callable(get_completions_for_parts)
        self.get_completions_for_parts = get_completions_for_parts

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor

        parts, part_start_pos = self.parse(text)

        for c in self.get_completions_for_parts(parts[:-1], parts[-1], complete_event):
            yield Completion(
                wrap_argument(parts[-1][: c.start_position] + c.text),
                start_position=part_start_pos - len(document.text),
                display=c.display,
                display_meta=c.display_meta,
            )

    @classmethod
    def parse(cls, text):
        """
        Parse the given text. Returns a tuple:
        (list_of_parts, start_pos_of_the_last_part).
        """
        OUTSIDE, IN_DOUBLE, IN_SINGLE = 0, 1, 2

        iterator = enumerate(text)
        state = OUTSIDE
        parts = []
        current_part = ""
        part_start_pos = 0

        for i, c in iterator:  # XXX: correctly handle empty strings.
            if state == OUTSIDE:
                if c.isspace():
                    # New part.
                    if current_part:
                        parts.append(current_part)
                    part_start_pos = i + 1
                    current_part = ""
                elif c == '"':
                    state = IN_DOUBLE
                elif c == "'":
                    state = IN_SINGLE
                else:
                    current_part += c

            elif state == IN_SINGLE:
                if c == "'":
                    state = OUTSIDE
                elif c == "\\":
                    next(iterator)
                    current_part += c
                else:
                    current_part += c

            elif state == IN_DOUBLE:
                if c == '"':
                    state = OUTSIDE
                elif c == "\\":
                    next(iterator)
                    current_part += c
                else:
                    current_part += c

        parts.append(current_part)
        return parts, part_start_pos


# assert ShlexCompleter.parse('"hello" world') == (['hello', 'world'], 8)
