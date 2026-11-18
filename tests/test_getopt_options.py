"""
A command's options read as tmux's getopt reads them: `-t=` is -t
with the value `=`, the pane under the mouse. Lillecarl/pyterm#1034.
"""

from __future__ import annotations

import pytest

from pymux.commands import CommandParser


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
