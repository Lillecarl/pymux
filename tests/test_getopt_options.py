"""
A command's options read as tmux's getopt reads them: `-t=` is -t
with the value `=`, the pane under the mouse. Lillecarl/pyterm#1034.
"""

from __future__ import annotations

import pytest
from run_commands import complaints

from pymux.commands import CommandParser
from pymux.core.main import Pymux


def parser() -> CommandParser:
    parser = CommandParser(prog="x")
    parser.add_argument("-F", action="store_true")
    parser.add_argument("-t")
    parser.add_argument("rest", nargs="*")
    return parser


@pytest.mark.parametrize(
    ("words", "flag", "target", "rest"),
    [
        (["-t="], False, "=", []),
        (["-t", "="], False, "=", []),
        (["-Ft="], True, "=", []),
        (["-t=x"], False, "=x", []),
        (["-t", "a", "--", "-t="], False, "a", ["-t="]),
        (["a=b"], False, None, ["a=b"]),
    ],
)
def test_an_equals_after_an_option_is_its_value(words, flag, target, rest):
    args = parser().parse_args(words)
    assert (args.F, args.t, args.rest) == (flag, target, rest)


@pytest.mark.parametrize(
    ("words", "flag", "target", "rest"),
    [
        (["a", "-F"], False, None, ["a", "-F"]),
        (["-F", "a", "-t", "b"], True, None, ["a", "-t", "b"]),
        (["-t", "-F", "a"], False, "-F", ["a"]),
        (["-Ft", "x", "a", "-p 60%"], True, "x", ["a", "-p 60%"]),
        (["-tx", "a", "-F"], False, "x", ["a", "-F"]),
        (["-", "-F"], False, None, ["-", "-F"]),
    ],
)
def test_the_options_end_at_the_first_operand(words, flag, target, rest):
    "tmux's getopt stops there, so a later word that starts with a dash is an operand."
    args = parser().parse_args(words)
    assert (args.F, args.t, args.rest) == (flag, target, rest)


@pytest.mark.parametrize(
    ("words", "output", "flag"),
    [
        (["-o", "-F"], "", True),
        (["-o", "a.png", "-F"], "a.png", True),
        (["-oa.png"], "a.png", False),
    ],
)
def test_an_optional_value_never_starts_with_a_dash(words, output, flag):
    parser = CommandParser(prog="x")
    parser.add_argument("-o", nargs="?", const="")
    parser.add_argument("-F", action="store_true")
    args = parser.parse_args(words)
    assert (args.o, args.F) == (output, flag)


@pytest.mark.parametrize(
    "words",
    [["-F", "a", "-o"], ["-F", "--", "a", "-o"]],
)
def test_a_remainder_holds_no_end_of_options(words):
    parser = CommandParser(prog="x")
    parser.add_argument("-F", action="store_true")
    parser.add_argument("rest", nargs="...")
    assert parser.parse_args(words).rest == ["a", "-o"]


def test_a_value_that_starts_with_a_dash_sets_a_user_option():
    "A real configuration: the fzf-url plugin's options start with -p."
    pymux = Pymux()
    assert complaints(pymux, "set -g @fzf-url-fzf-options '-p 60%,30% --prompt=\"x\"'") == []
    assert complaints(pymux, "set -g -q @other on") == []
