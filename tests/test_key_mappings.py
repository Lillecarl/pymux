"""
The names a person writes for a key, and what they reach.

`bind-key` takes a key by name, so this table is the whole vocabulary
of a configuration file. Nothing tested it until Lillecarl/pymux#204,
which is how "M-J" came to be unbindable: the table names "M-a" to
"M-z" one at a time and no uppercase form, and tmux binds "M-J".
"""

import pytest
from prompt_toolkit.keys import Keys

from pymux.key_mappings import (
    PYMUX_TO_PROMPT_TOOLKIT_KEYS,
    pymux_key_to_prompt_toolkit_key_sequence,
)


def reached(key):
    return pymux_key_to_prompt_toolkit_key_sequence(key)


# ----------------------------------------------------------------------
# Alt and one character.


def test_a_lowercase_alt_key_is_escape_and_the_letter():
    assert reached("M-a") == (Keys.Escape, "a")


@pytest.mark.parametrize("key", ["M-J", "M-K", "M-R"])
def test_the_uppercase_alt_keys_tmux_binds(key):
    """
    tmux binds these three out of the box, so a ported configuration
    reaches them on its first Alt line. Lillecarl/pymux#204.
    """
    assert reached(key) == (Keys.Escape, key[2])


def test_alt_and_a_punctuation_mark():
    "The rule is the character, not a list of the ones we thought of."
    assert reached("M-!") == (Keys.Escape, "!")
    assert reached("M-/") == (Keys.Escape, "/")


def test_case_is_kept_after_the_prefix():
    "`M-a` and `M-A` are two keys, and only the prefix is case blind."
    assert reached("M-A") != reached("M-a")
    assert reached("m-A") == reached("M-A")


def test_a_digit_still_comes_out_of_the_table():
    assert reached("M-1") == PYMUX_TO_PROMPT_TOOLKIT_KEYS["M-1"]


# ----------------------------------------------------------------------
# What must keep working around it.


@pytest.mark.parametrize("key", ["M-C-Left", "M-C-a", "M-Up", "M-C-\\"])
def test_the_longer_alt_names_still_come_out_of_the_table(key):
    assert reached(key) == PYMUX_TO_PROMPT_TOOLKIT_KEYS[key]


def test_the_prefixes_are_case_blind():
    assert reached("c-a") == reached("C-a")
    assert reached("m-c-a") == reached("M-C-a")
    assert reached("c-s-a") == reached("C-S-a")


def test_one_character_is_itself():
    assert reached("a") == ("a",)
    assert reached("A") == ("A",)


@pytest.mark.parametrize("key", ["Nonsense", "M-Nope", "C-Nope", ""])
def test_a_key_nobody_names_is_still_refused(key):
    """
    The fallback answers `M-` and one character, and must not answer
    anything longer: a typo has to say so rather than bind quietly.
    """
    with pytest.raises(ValueError):
        reached(key)


# ----------------------------------------------------------------------
# The table is also the completion list.


def test_the_table_still_completes_the_written_out_alt_keys():
    """
    `commands/completer.py` and `options.py` list the keys of this
    table, so the entries the fallback could now answer stay in it.
    Losing them would stop "M-a" completing.
    """
    for letter in "az09":
        assert "M-%s" % (letter,) in PYMUX_TO_PROMPT_TOOLKIT_KEYS


# ----------------------------------------------------------------------
# The line a person actually writes.


def bound(command):
    "Run one command the way a configuration file does, and complain."
    from pymux.commands.commands import handle_command
    from pymux.main import Pymux

    pymux = Pymux()
    handle_command(pymux, command)
    return pymux.startup_errors


def test_a_configuration_file_can_bind_an_uppercase_alt_key():
    "The reported line. It answered `Invalid key: 'M-J'`."
    assert bound("bind-key -n M-J swap-pane -D") == []


def test_a_configuration_file_still_refuses_a_key_nobody_names():
    complaints = bound("bind-key -n M-Nope swap-pane -D")

    assert complaints
    assert "M-Nope" in complaints[0]
