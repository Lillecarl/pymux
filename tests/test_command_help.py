"""
`pymux <command> --help` prints that command's own help, for every
command and every mode word, with no server. Lillecarl/pymux#845.
"""

from __future__ import annotations

import pytest

from pymux.commands import build_parser_tree
from pymux.entry_points.arguments import Mode, parse_arguments
from pymux.entry_points.help import MODE_HELP

_PARSER, _SUBPARSERS = build_parser_tree()

#: Every word the command table answers, aliases too, with its parser.
COMMANDS = sorted(_SUBPARSERS.choices.items())


def help_printed(capsys, *words: str) -> str:
    "What `pymux <words>` prints, when it prints help and leaves."
    with pytest.raises(SystemExit) as left:
        parse_arguments(list(words))
    assert left.value.code == 0
    return capsys.readouterr().out


@pytest.mark.parametrize(("word", "parser"), COMMANDS, ids=[word for word, _parser in COMMANDS])
def test_every_command_answers_its_own_help(capsys, word, parser):
    said = help_printed(capsys, word, "--help")
    assert said.startswith("usage: %s" % (parser.prog,))


@pytest.mark.parametrize("mode", sorted(set(Mode) - {Mode.IMGCAT}))
def test_every_mode_word_answers_its_own_help(capsys, mode):
    said = help_printed(capsys, mode, "--help")
    if mode in MODE_HELP:
        assert said.startswith("usage: pymux %s" % (mode,))
        # argparse wraps the description to the width of the terminal.
        assert MODE_HELP[mode] in " ".join(said.split())
    else:
        # `ls` and `list-sessions` hand their flags to the server, so
        # the help is the server's command.
        assert said.startswith("usage: pymux list-sessions")


def test_the_help_says_what_the_command_does(capsys):
    said = help_printed(capsys, "kill-pane", "--help")
    assert "Kill a pane, or the active one." in said
    assert "-t <target-pane>" in said


def test_an_alias_answers_with_its_command(capsys):
    said = help_printed(capsys, "ls", "--help")
    assert said.startswith("usage: pymux list-sessions")


def test_a_short_flag_asks_where_the_command_has_none(capsys):
    said = help_printed(capsys, "kill-pane", "-h")
    assert said.startswith("usage: pymux kill-pane")


def test_a_short_flag_of_the_command_is_its_own():
    "`split-window -h` splits side by side."
    _options, mode, command = parse_arguments(["split-window", "-h"])
    assert (mode, command) == (None, "split-window -h")


def test_help_after_a_separator_is_a_word():
    _options, _mode, command = parse_arguments(["send-keys", "--", "--help"])
    assert command == "send-keys -- --help"


def test_the_first_pane_keeps_its_help():
    "After `integrated` the words are a program's command line."
    _options, mode, command = parse_arguments(["integrated", "htop", "--help"])
    assert (mode, command) == (Mode.INTEGRATED, "htop --help")


def test_imgcat_reads_its_own_help():
    options, mode, _command = parse_arguments(["imgcat", "--help"])
    assert mode is Mode.IMGCAT
    assert options.imgcat_words == ["--help"]
