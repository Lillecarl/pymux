"""
What a pymux command line means.

`parse_arguments` reads it and answers three things: the options, the
mode word, and the command as one string. Everything after that is
about starting a server, so this is the last place where a mistake is
still cheap.

A command is one string on purpose. That is what reaches a pane and
what a server reads over the socket, and `shlex.quote` is what keeps an
argument with a space in it in one piece on the way there.
`Pymux._create_pane` takes it apart again with `shlex.split`.
"""

import pytest

from pymux.entry_points.run_pymux import parse_arguments


def parse(*argv):
    "Return (mode, command) for a command line."
    _options, mode, command = parse_arguments(list(argv))
    return mode, command


# ----------------------------------------------------------------------
# The mode word.


def test_no_arguments_is_no_mode_and_no_command():
    assert parse() == (None, None)


@pytest.mark.parametrize("mode", ["standalone", "integrated", "start-server"])
def test_mode_word_is_read_as_mode(mode):
    assert parse(mode) == (mode, None)


def test_word_that_is_not_mode_is_command_for_server():
    assert parse("split-window") == (None, "split-window")


def test_mode_with_arguments_that_takes_no_pane_is_one_command():
    "`pymux list-sessions -F x` goes to the server as it stands."
    assert parse("list-sessions", "-F", "x") == (None, "list-sessions -F x")


# ----------------------------------------------------------------------
# The separator.


def test_separator_does_not_become_command():
    """
    argparse consumes one "--" while it assigns positional arguments,
    and the second pass declares none, so it hands the separator back.
    It used to become the first word of the command, and a pane then
    ran "--" and closed at once. Lillecarl/pymux#41.
    """
    assert parse("integrated", "--", "htop") == ("integrated", "htop")


def test_separator_on_its_own_leaves_no_command():
    assert parse("integrated", "--") == ("integrated", None)


def test_only_first_separator_goes():
    "A second one is an argument of the program that runs."
    assert parse("integrated", "--", "sh", "--", "x") == (
        "integrated",
        "sh -- x",
    )


def test_separator_keeps_option_of_program():
    "That is what a separator is for: the option belongs to the pane."
    assert parse("integrated", "--", "ls", "--color") == (
        "integrated",
        "ls --color",
    )


# ----------------------------------------------------------------------
# Quoting.


def test_argument_with_space_stays_one_argument():
    "Lillecarl/pymux#39 is the other half of this."
    assert parse("integrated", "python3", "-c", "import sys") == (
        "integrated",
        "python3 -c 'import sys'",
    )


def test_command_survives_round_trip():
    "What `parse_arguments` writes, `shlex.split` reads back."
    import shlex

    argv = ["sh", "-c", "echo one two; sleep 30"]
    _mode, command = parse("integrated", *argv)
    assert shlex.split(command) == argv


def test_empty_argument_survives_as_well():
    import shlex

    _mode, command = parse("integrated", "sh", "-c", "")
    assert shlex.split(command) == ["sh", "-c", ""]


# ----------------------------------------------------------------------
# Options before and after the mode word.


def test_option_before_mode_word_is_read():
    options, mode, _command = parse_arguments(["-S", "/tmp/x", "integrated"])
    assert (options.socket, mode) == ("/tmp/x", "integrated")


def test_option_after_mode_word_is_read():
    options, mode, _command = parse_arguments(["integrated", "-S", "/tmp/x"])
    assert (options.socket, mode) == ("/tmp/x", "integrated")


def test_option_after_mode_word_wins():
    "The second pass suppresses defaults, so it only sets what is given."
    options, _mode, _command = parse_arguments(
        ["-S", "/tmp/before", "integrated", "-S", "/tmp/after"]
    )
    assert options.socket == "/tmp/after"


def test_option_before_mode_word_is_not_lost_by_second_pass():
    options, _mode, _command = parse_arguments(
        ["-S", "/tmp/before", "integrated", "-d"]
    )
    assert options.socket == "/tmp/before"
    assert options.detach_others is True


def test_option_after_separator_belongs_to_program():
    options, _mode, command = parse_arguments(["integrated", "--", "x", "-d"])
    assert options.detach_others is False
    assert command == "x -d"
