"""
The keys pymux binds before a person's configuration is read.

`rc.py` is the whole default vocabulary, and nothing checked it. Two
things can go wrong in a line there and neither raises: a key name the
mapping table does not know is dropped, and a command name with a typo
in it fails only when somebody presses the key.

The strip's three commands added five lines to that file and made both
worth testing. Lillecarl/pymux#212.
"""

import shlex

import pytest

from pymux.commands import parser_tree
from pymux.key_mappings import pymux_key_to_prompt_toolkit_key_sequence
from pymux.rc import STARTUP_COMMANDS


def command_names():
    "Every name the tree registers, aliases included."
    _parser, subparsers = parser_tree()
    return set(subparsers.choices)


def bindings():
    """
    Every `bind-key` line, as (table, key, command name).

    The rest of the line is the command's own arguments, and
    `test_key_mappings.py` covers the key names themselves. A line
    without `-T` binds into the prefix table, what a bare `bind-key`
    has always meant.

    It splits the way pymux does, with `shlex`, because two of the keys
    are quotation marks and a plain split keeps the quotes around them.
    """
    result = []

    for line in STARTUP_COMMANDS.splitlines():
        words = shlex.split(line, comments=True)
        if words[:1] != ["bind-key"]:
            continue
        words = words[1:]
        table = "prefix"
        if words[:1] == ["-T"]:
            table = words[1]
            words = words[2:]
        elif words[:1] == ["-n"]:
            table = "root"
            words = words[1:]
        result.append((table, words[0], words[1]))

    return result


@pytest.mark.parametrize("table,key,command", bindings())
def test_every_key_of_default_table_has_name(table, key, command):
    "A name the table does not know is dropped, and says nothing."
    assert pymux_key_to_prompt_toolkit_key_sequence(key)


@pytest.mark.parametrize("table,key,command", bindings())
def test_every_default_binding_reaches_command(table, key, command):
    assert command in command_names()


def test_no_key_is_bound_twice():
    "The second line wins in silence, which is never what was meant."
    pairs = [(table, key) for table, key, _ in bindings()]

    assert sorted(pairs) == sorted(set(pairs))


def test_a_mode_binds_its_own_keys_beside_the_prefix_table():
    """
    The same key is two bindings when the tables differ: `h` follows
    the prefix as one thing and answers in pane-management as
    another. Lillecarl/pymux#395.
    """
    tables = {(table, key) for table, key, _ in bindings()}

    assert ("prefix", "h") in tables
    assert ("pane-management", "h") in tables


# ----------------------------------------------------------------------
# The strip.


@pytest.mark.parametrize(
    "key,command",
    [
        ("<", "consume-or-expel"),
        (">", "consume-or-expel"),
        ("H", "move-column"),
        ("L", "move-column"),
        ("W", "switch-column-width"),
    ],
)
def test_strip_has_key_for_each_of_its_commands(key, command):
    assert ("prefix", key, command) in bindings()


def test_braces_are_still_tmux_s_own():
    """
    niri moves a window between columns on the two bracket keys, and
    those are `swap-pane` here. tmux's keys stay tmux's, so the strip
    took `<` and `>`. Lillecarl/pymux#212.
    """
    assert ("prefix", "{", "swap-pane") in bindings()
    assert ("prefix", "}", "swap-pane") in bindings()
