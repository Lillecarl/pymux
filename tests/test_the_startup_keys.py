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

from pymux.commands.commands import COMMANDS_TO_HANDLERS
from pymux.key_mappings import pymux_key_to_prompt_toolkit_key_sequence
from pymux.rc import STARTUP_COMMANDS


def bindings():
    """
    Every `bind-key` line, as (key, command name).

    The rest of the line is the command's own arguments, and
    `test_key_mappings.py` covers the key names themselves.

    It splits the way pymux does, with `shlex`, because two of the keys
    are quotation marks and a plain split keeps the quotes around them.
    """
    result = []

    for line in STARTUP_COMMANDS.splitlines():
        words = shlex.split(line, comments=True)
        if words[:1] == ["bind-key"]:
            result.append((words[1], words[2]))

    return result


@pytest.mark.parametrize("key,command", bindings())
def test_every_key_of_the_default_table_has_a_name(key, command):
    "A name the table does not know is dropped, and says nothing."
    assert pymux_key_to_prompt_toolkit_key_sequence(key)


@pytest.mark.parametrize("key,command", bindings())
def test_every_default_binding_reaches_a_command(key, command):
    assert command in COMMANDS_TO_HANDLERS


def test_no_key_is_bound_twice():
    "The second line wins in silence, which is never what was meant."
    keys = [key for key, _ in bindings()]

    assert sorted(keys) == sorted(set(keys))


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
def test_the_strip_has_a_key_for_each_of_its_commands(key, command):
    assert (key, command) in bindings()


def test_the_braces_are_still_tmux_s_own():
    """
    niri moves a window between columns on the two bracket keys, and
    those are `swap-pane` here. tmux's keys stay tmux's, so the strip
    took `<` and `>`. Lillecarl/pymux#212.
    """
    assert ("{", "swap-pane") in bindings()
    assert ("}", "swap-pane") in bindings()
