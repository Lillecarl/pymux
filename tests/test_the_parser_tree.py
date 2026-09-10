"""
One description of a command, three readers of it.

The tree of argparse parsers is what parses a command, what the shell
completes through, and what the command bar of a client completes
from. These tests ask each reader the questions a person asks it.
Lillecarl/pymux#48.
"""

import os
import shlex
import subprocess
import sys

import pytest
from prompt_toolkit.document import Document

from pymux.commands.commands import (
    COMMANDS_TO_HANDLERS,
    COMMANDS_TO_PARSERS,
    _variables_of,
    call_command_handler,
    get_option_flags_for_command,
)
from pymux.commands.completer import get_completions_for_parts
from pymux.main import Pymux


# ----------------------------------------------------------------------
# The tree: what a command takes.


def test_every_alias_points_at_a_registered_command():
    "The check at the foot of commands.py, said out loud."
    from pymux.commands.aliases import ALIASES

    assert ALIASES
    for alias, command in ALIASES.items():
        assert command in COMMANDS_TO_HANDLERS, alias


def test_a_value_option_is_read_under_both_spellings():
    """
    A handler that asks whether `-t` was given and a handler that
    reads the value ask the same dictionary. That is the shape docopt
    gave, and the shim keeps it, so none of the handlers changed.
    """
    parser = COMMANDS_TO_PARSERS["kill-pane"]
    namespace = parser.parse_args(["-t", "%1"])
    variables = _variables_of(parser, namespace)
    assert variables["-t"] == "%1"
    assert variables["<target-pane>"] == "%1"

    namespace = parser.parse_args([])
    variables = _variables_of(parser, namespace)
    assert variables["-t"] is None
    assert variables["<target-pane>"] is None


def test_a_flag_is_read_as_true_or_false():
    parser = COMMANDS_TO_PARSERS["break-pane"]
    assert _variables_of(parser, parser.parse_args(["-d"]))["-d"] is True
    assert _variables_of(parser, parser.parse_args([]))["-d"] is False


def test_a_positional_is_read_as_its_name():
    parser = COMMANDS_TO_PARSERS["rename-window"]
    variables = _variables_of(parser, parser.parse_args(["editor"]))
    assert variables["<name>"] == "editor"


def test_an_option_of_a_bound_command_is_not_an_option_of_bind_key():
    """
    The command that bind-key runs is a remainder, so its options are
    its own. `-g` reaches the bound command, not bind-key.
    """
    pymux = Pymux()
    call_command_handler("bind-key", pymux, ["x", "set-option", "-g", "status", "off"])
    (binding,) = list(pymux.key_bindings_manager.custom_bindings.values())
    assert binding.command == "set-option"
    assert binding.arguments == ["-g", "status", "off"]


def test_a_dropped_double_dash_still_separates():
    "A `--` the caller wrote is dropped, and what follows is the command."
    pymux = Pymux()
    call_command_handler("bind-key", pymux, ["x", "--", "next-window"])
    (binding,) = list(pymux.key_bindings_manager.custom_bindings.values())
    assert binding.command == "next-window"
    assert binding.arguments == []


# ----------------------------------------------------------------------
# What a bad line says.


def test_a_word_too_many_is_named():
    """
    docopt answered a bad line with the usage string and nothing
    about which word was wrong, which is the cost that started
    Lillecarl/pymux#38 and Lillecarl/pymux#48. argparse names it.
    """
    pymux = Pymux()
    call_command_handler("set-option", pymux, ["-g", "status", "off", "please"])
    (error,) = pymux.startup_errors
    assert "please" in error
    assert "set-option" in error


def test_a_bad_option_is_named():
    pymux = Pymux()
    call_command_handler("display-message", pymux, ["-Z", "hello"])
    (error,) = pymux.startup_errors
    assert "-Z" in error
    assert "display-message" in error


def test_a_missing_required_option_is_named():
    pymux = Pymux()
    call_command_handler("select-window", pymux, [])
    (error,) = pymux.startup_errors
    assert "-t" in error


def test_no_option_at_all_is_the_answer_select_pane_gives():
    "select-pane takes one of six things, and the tree requires one."
    pymux = Pymux()
    call_command_handler("select-pane", pymux, [])
    (error,) = pymux.startup_errors
    assert "required" in error


# ----------------------------------------------------------------------
# The command bar: what it offers.


def _offered(parts, last_part, pymux):
    def plain(meta):
        return "".join(text for _style, text, *_ in meta) if meta else ""

    return [
        (c.text, plain(c.display_meta))
        for c in get_completions_for_parts(parts, last_part, None, pymux)
    ]


def test_every_command_offers_its_flags():
    """
    The old completer answered for four of the fifty commands out of
    a second description of them. The tree answers for all of them.
    """
    pymux = Pymux()
    for command in ("select-pane", "new-window", "capture-pane", "resize-window"):
        text = [
            offered[0] for offered in _offered([command], "-", pymux)
        ]
        for flag in get_option_flags_for_command(command):
            assert flag in text, (command, flag, text)


def test_a_flag_carries_its_help_beside_it():
    pymux = Pymux()
    offered = dict(_offered(["new-window"], "-", pymux))
    assert "Leave the new window unfocused." in offered["-d"]
    assert "Where the program starts." in offered["-c"]


def test_a_partially_typed_flag_offers_what_matches():
    pymux = Pymux()
    text = [offered[0] for offered in _offered(["capture-pane"], "-J", pymux)]
    assert text == ["-J"]


def test_an_alias_offers_the_full_name():
    pymux = Pymux()
    text = [offered[0] for offered in _offered([], "selectp", pymux)]
    assert text == ["select-pane"]


def test_set_option_offers_the_option_names_then_their_values():
    pymux = Pymux()
    names = [offered[0] for offered in _offered(["set-option"], "s", pymux)]
    assert "status" in names

    pymux.options["status"].set_value(pymux, "off")
    values = [offered[0] for offered in _offered(["set-option", "status"], "", pymux)]
    assert "on" in values
    assert "off" in values


def test_select_layout_offers_the_layout_names():
    pymux = Pymux()
    names = [offered[0] for offered in _offered(["select-layout"], "e", pymux)]
    assert "even-horizontal" in names


def test_bind_key_offers_a_key_then_the_command_then_its_arguments():
    pymux = Pymux()

    keys = [offered[0] for offered in _offered(["bind-key"], "ho", pymux)]
    assert "home" in keys

    commands = [offered[0] for offered in _offered(["bind-key", "x"], "set-o", pymux)]
    assert "set-option" in commands

    args = [
        offered[0]
        for offered in _offered(["bind-key", "x", "set-option"], "-g", pymux)
    ]
    assert args == ["-g"]


def test_send_keys_offers_key_names_until_l_says_text():
    pymux = Pymux()
    keys = [offered[0] for offered in _offered(["send-keys"], "C", pymux)]
    assert keys

    literal = _offered(["send-keys", "-l"], "C", pymux)
    assert literal == []


# ----------------------------------------------------------------------
# The shell: what `pymux <TAB>` answers.


def _shell_completes(line: str) -> set:
    """
    Drive the entry point the way the generated script of argcomplete
    does, and read what it answers on file descriptor 8.
    """
    bootstrap = "from pymux.entry_points.run_pymux import run; run()"
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in ("PYMUX", "PYTHONPATH")
    } | {
        "_ARGCOMPLETE": "1",
        "_ARGCOMPLETE_IFS": "\013",
        "_ARGCOMPLETE_SHELL": "bash",
        "COMP_LINE": line,
        "COMP_POINT": str(len(line)),
        "COMP_TYPE": "9",
        "_ARGCOMPLETE_COMP_WORDBREAKS": " \t\n\"'><=;|&(:",
    }
    done = subprocess.run(
        [
            "bash",
            "-c",
            "exec %s -c %s 8>&1 1>/dev/null"
            % (sys.executable, shlex.quote(bootstrap)),
        ],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert done.returncode == 0, done.stderr
    return {c.rstrip() for c in done.stdout.split("\013") if c}


@pytest.mark.parametrize(
    "line, wanted",
    (
        ("pymux spl", {"split-window"}),
        ("pymux select-l", {"select-layout"}),
        ("pymux split-window -", {"-v", "-h", "-t", "-c", "-d", "-P", "-F"}),
        ("pymux capture-pane -", {"-p", "-J", "-t", "-S", "-E"}),
        ("pymux set-option -", {"-g"}),
        ("pymux ", {"split-window", "select-pane", "attach"}),
        ("pymux ls", {"ls"}),
    ),
)
def test_the_shell_answers_from_the_tree(line, wanted):
    offered = _shell_completes(line)
    assert wanted <= offered, (line, wanted, offered)


def test_the_shell_answers_the_entry_point_options():
    offered = _shell_completes("pymux attach -")
    assert "-S" in offered
    assert "--socket" in offered
    assert "--truecolor" in offered
