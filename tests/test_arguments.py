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

from __future__ import annotations

import pytest

from pymux.entry_points.run_pymux import Mode, _flag_args, parse_arguments


def parse(*argv):
    "Return (mode, command) for a command line."
    _options, mode, command = parse_arguments(list(argv))
    return mode, command


# ----------------------------------------------------------------------
# The mode word.


def test_no_arguments_is_no_mode_and_no_command():
    assert parse() == (None, None)


@pytest.mark.parametrize("mode", [Mode.INTEGRATED, Mode.START_SERVER])
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
    assert parse("integrated", "--", "htop") == (Mode.INTEGRATED, "htop")


def test_separator_on_its_own_leaves_no_command():
    assert parse("integrated", "--") == (Mode.INTEGRATED, None)


def test_only_first_separator_goes():
    "A second one is an argument of the program that runs."
    assert parse("integrated", "--", "sh", "--", "x") == (
        Mode.INTEGRATED,
        "sh -- x",
    )


def test_separator_keeps_option_of_program():
    "That is what a separator is for: the option belongs to the pane."
    assert parse("integrated", "--", "ls", "--color") == (
        Mode.INTEGRATED,
        "ls --color",
    )


# ----------------------------------------------------------------------
# Quoting.


def test_argument_with_space_stays_one_argument():
    "Lillecarl/pymux#39 is the other half of this."
    assert parse("integrated", "python3", "-c", "import sys") == (
        Mode.INTEGRATED,
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
    assert (options.socket, mode) == ("/tmp/x", Mode.INTEGRATED)


def test_option_after_mode_word_is_read():
    options, mode, _command = parse_arguments(["integrated", "-S", "/tmp/x"])
    assert (options.socket, mode) == ("/tmp/x", Mode.INTEGRATED)


def test_option_after_mode_word_wins():
    "The second pass suppresses defaults, so it only sets what is given."
    options, _mode, _command = parse_arguments(["-S", "/tmp/before", "integrated", "-S", "/tmp/after"])
    assert options.socket == "/tmp/after"


def test_option_before_mode_word_is_not_lost_by_second_pass():
    options, _mode, _command = parse_arguments(["-S", "/tmp/before", "integrated", "-d"])
    assert options.socket == "/tmp/before"
    assert options.detach_others is True


def test_option_after_separator_belongs_to_program():
    options, _mode, command = parse_arguments(["integrated", "--", "x", "-d"])
    assert options.detach_others is False
    assert command == "x -d"


# ----------------------------------------------------------------------
# The flags of the invocation that starts a server.
#
# `new-session` is answered by the client when no server is up yet, so
# it is read here and never by `new_session.register`'s parser. A flag
# it did not know went into a set nobody reads, and a `--long` one
# became a word of the command the first pane runs: the session started
# and nothing was said, on the one route where nobody is watching
# stderr. Lillecarl/pymux#458.


def flags_of(*args):
    "What the client reads out of a `new-session` line."
    return _flag_args(
        list(args),
        flags_with_value=("s", "n", "c", "x", "y", "F", "J"),
        flags_alone=("d", "P"),
    )


def test_a_flag_alone_is_read():
    assert flags_of("-d") == ({"d"}, {}, [])


def test_a_value_is_read_beside_its_flag_or_glued_to_it():
    assert flags_of("-s", "name") == (set(), {"s": "name"}, [])
    assert flags_of("-sname") == (set(), {"s": "name"}, [])


def test_several_flags_in_one_argument_are_all_read():
    "getopt reads `-dP` as both. Only the first was taken."
    assert flags_of("-dP") == ({"d", "P"}, {}, [])


def test_a_flag_and_a_value_in_one_argument():
    assert flags_of("-dsname") == ({"d"}, {"s": "name"}, [])


def test_the_command_is_what_is_left():
    assert flags_of("-d", "-s", "here", "sleep 60") == (
        {"d"},
        {"s": "here"},
        ["sleep 60"],
    )


def test_a_short_flag_nobody_declared_is_refused():
    with pytest.raises(ValueError) as refused:
        flags_of("-Z", "sleep 60")
    assert str(refused.value) == "unrecognized arguments: -Z"


def test_a_long_flag_nobody_declared_is_refused():
    "It used to become the first word of what the pane runs."
    with pytest.raises(ValueError) as refused:
        flags_of("--zzz-not-a-flag", "sleep 60")
    assert str(refused.value) == "unrecognized arguments: --zzz-not-a-flag"


def test_a_flag_that_wants_a_value_and_has_none_is_refused():
    with pytest.raises(ValueError) as refused:
        flags_of("-d", "-s")
    assert str(refused.value) == "argument -s: expected one argument"


def test_everything_after_a_separator_is_the_command():
    assert flags_of("-d", "--", "-x", "--y") == ({"d"}, {}, ["-x", "--y"])
