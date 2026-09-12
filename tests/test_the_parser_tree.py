"""
One description of a command, three readers of it.

The tree of argparse parsers is what parses a command, what the shell
completes through, and what the command bar of a client completes
from -- in process, through argcomplete. These tests ask each reader
the questions a person asks it. Lillecarl/pymux#307.
"""

import os
import shlex
import subprocess
import sys

import pytest
from prompt_toolkit.document import Document

from pymux.commands import call_command_handler, the_parser
from pymux.commands.completer import create_command_completer
from pymux.main import Pymux


# ----------------------------------------------------------------------
# The tree: what a command takes.


def _parser_of(command):
    _parser, subparsers = the_parser()
    return subparsers.choices[command]


def test_every_alias_points_at_a_registered_command():
    "The check at the foot of aliases.py, said out loud."
    from pymux.commands.aliases import ALIASES

    _parser, subparsers = the_parser()
    assert ALIASES
    for alias, command in ALIASES.items():
        assert command in subparsers.choices, alias


def test_a_value_option_is_read_as_its_dest():
    """
    A handler asks for the value by the dest its declaration names:
    `-t` of kill-pane lands in `target_pane`.
    """
    parser = _parser_of("kill-pane")
    assert parser.parse_args(["-t", "%1"]).target_pane == "%1"
    assert parser.parse_args([]).target_pane is None


def test_a_flag_is_read_as_true_or_false():
    parser = _parser_of("break-pane")
    assert parser.parse_args(["-d"]).d is True
    assert parser.parse_args([]).d is False


def test_a_positional_is_read_as_its_name():
    parser = _parser_of("rename-window")
    assert parser.parse_args(["editor"]).name == "editor"


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


def _offered(line, pymux):
    """
    What the command bar offers for a line typed up to its end, as
    (word, help) pairs. A completion is the whole word the typed one
    becomes.
    """
    completer = create_command_completer(pymux)
    out = []
    for c in completer.get_completions(Document(line, len(line)), None):
        meta = c.display_meta
        plain = "".join(text for _style, text, *_ in meta) if meta else ""
        out.append((c.text, plain))
    return out


def _flags_of(command):
    "Every option string the command's parser takes."
    return {
        option
        for action in _parser_of(command)._actions
        for option in action.option_strings
    }


def test_every_command_offers_its_flags():
    """
    The tree answers for all of the commands, and the flags come from
    the same declaration that parses them.
    """
    pymux = Pymux()
    for command in ("select-pane", "new-window", "capture-pane", "resize-window"):
        offered = {word for word, _said in _offered(command + " -", pymux)}
        for flag in _flags_of(command):
            assert flag in offered, (command, flag, offered)


def test_a_flag_carries_its_help_beside_it():
    pymux = Pymux()
    offered = dict(_offered("new-window -", pymux))
    assert "Leave the new window unfocused." in offered["-d"]
    assert "Where the program starts." in offered["-c"]


def test_a_partially_typed_flag_offers_what_matches():
    pymux = Pymux()
    text = [word for word, _said in _offered("capture-pane -J", pymux)]
    assert text == ["-J"]


def test_a_word_matches_anywhere_in_a_command():
    """
    The command bar is where the tmux spellings live, and
    remembering them exactly is the failure mode: `option` reaches
    `set-option` without the set. Lillecarl/pymux#269.
    """
    pymux = Pymux()
    text = [word for word, _said in _offered("option", pymux)]
    assert "set-option" in text
    assert "set-window-option" in text


def test_a_flag_offers_itself_without_the_dash():
    """
    The ask of Lillecarl/pymux#148 went one further: a flag lists
    without the dash at all. A flag says nothing about itself, and
    its help says plenty, so the help is what a dashless word
    matches. Lillecarl/pymux#269.
    """
    pymux = Pymux()
    offered = dict(_offered("new-window unfocus", pymux))
    assert "-d" in offered


def test_a_value_matches_loosely():
    pymux = Pymux()
    values = [word for word, _said in _offered("set-option status on", pymux)]
    assert "on" in values


def test_an_alias_offers_the_full_name():
    pymux = Pymux()
    text = [word for word, _said in _offered("selectp", pymux)]
    assert text == ["select-pane"]


def test_set_option_offers_the_option_names_then_their_values():
    pymux = Pymux()
    names = [word for word, _said in _offered("set-option s", pymux)]
    assert "status" in names

    pymux.options["status"].set_value(pymux, "off")
    values = [word for word, _said in _offered("set-option status ", pymux)]
    assert "on" in values
    assert "off" in values


def test_select_layout_offers_the_layout_names():
    pymux = Pymux()
    names = [word for word, _said in _offered("select-layout e", pymux)]
    assert "even-horizontal" in names


def test_bind_key_offers_a_key_then_the_command_then_its_arguments():
    pymux = Pymux()

    keys = [word for word, _said in _offered("bind-key ho", pymux)]
    assert "home" in keys

    commands = [word for word, _said in _offered("bind-key x set-o", pymux)]
    assert "set-option" in commands

    args = [word for word, _said in _offered("bind-key x set-option -g", pymux)]
    assert args == ["-g"]


def test_a_bare_tab_after_a_command_lists_its_flags():
    """
    An empty word is the question, and the flags are one part of the
    answer. Lillecarl/pymux#148.
    """
    pymux = Pymux()
    offered = [word for word, _said in _offered("split-window ", pymux)]
    assert "-v" in offered
    assert "-h" in offered
    assert "-c" in offered


def test_a_bare_tab_after_set_option_lists_option_names_too():
    """
    The positional that has an answer of its own offers it. argcomplete
    offers the flags of the command beside it, the way it does for
    every program it completes.
    """
    pymux = Pymux()
    offered = [word for word, _said in _offered("set-option ", pymux)]
    assert "status" in offered
    assert "-g" in offered


def test_a_bare_tab_after_select_pane_lists_its_flags():
    pymux = Pymux()
    offered = [word for word, _said in _offered("select-pane ", pymux)]
    assert "-L" in offered
    assert "-t" in offered


def test_send_keys_offers_key_names_until_l_says_text():
    pymux = Pymux()
    keys = [word for word, _said in _offered("send-keys C", pymux)]
    assert keys

    literal = _offered("send-keys -l C", pymux)
    assert literal == []


# ----------------------------------------------------------------------
# What a command says it does.


def test_every_command_says_what_it_does():
    "The palette and the completion of the shell read it."
    _parser, subparsers = the_parser()
    # One pseudo action per parser: its metavar names the command and
    # any aliases with it, and its help is what both offer.
    said = {action.metavar: (action.help or "") for action in subparsers._choices_actions}
    parsers = {id(parser) for parser in subparsers.choices.values()}
    assert len(said) == len(parsers)
    empty = sorted(name for name, text in said.items() if not text.strip())
    assert empty == []
    unfinished = sorted(
        name
        for name, text in said.items()
        if not text.rstrip().endswith((".", ":", ")", "`"))
    )
    assert unfinished == []


def test_the_palette_says_what_a_command_does():
    pymux = Pymux()
    offered = dict(_offered("split-w", pymux))
    assert "Split this window into two panes, side by side or stacked." in (
        offered["split-window"]
    )


def test_an_alias_says_what_the_command_it_names_does():
    pymux = Pymux()
    offered = dict(_offered("selectp", pymux))
    assert "Focus a pane beside this one, or rotate the panes of the window." in (
        offered["select-pane"]
    )


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
