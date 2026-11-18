"""
The command grammar, `pymux.grammar`, judged by tmux's own
parse: `source-file -nv` prints what tmux read. Lillecarl/pyterm#1029.
"""

from __future__ import annotations

import os
import re
import subprocess

import pytest
from hypothesis import given
from hypothesis import strategies as st
from prompt_toolkit.keys import Keys
from session import create_session, once
from test_command_mode import press
from test_key_tables import run_as_client

from pymux.commands import call_command_handler
from pymux.core.main import Pymux
from pymux.entry_points.arguments import parse_arguments
from pymux.grammar import (
    Block,
    Command,
    ParseError,
    from_arguments,
    join,
    parse,
    print_arguments,
    quote,
    template_replace,
)
from pymux.options.options import Scope

TMUX = os.environ.get("PYMUX_TMUX")


def shape(commands: list[Command]) -> list[list[object]]:
    return [[shape(word.commands) if isinstance(word, Block) else word for word in c.words] for c in commands]


def words(text: str, **kwargs) -> list[list[object]]:
    kwargs.setdefault("environment", {"HOME": "/home/x"})
    kwargs.setdefault("condition", lambda format: format not in ("", "0"))
    return shape(parse(text, **kwargs))


def test_a_comment_after_a_command_is_not_a_word():
    assert words("set -g @a b # what b is\n# a line\n") == [["set", "-g", "@a", "b"]]


def test_a_hash_inside_a_word_or_quotes_stays():
    assert words("set -g @a '#{pane_id}' a#b") == [["set", "-g", "@a", "#{pane_id}", "a#b"]]


def test_a_backslash_continues_the_line():
    assert words("set -g \\\n    @a b\nshow") == [["set", "-g", "@a", "b"], ["show"]]


def test_a_bare_semicolon_separates_and_an_escaped_one_is_a_word():
    assert words("a 1 ; b 2 \\; c") == [["a", "1"], ["b", "2", ";", "c"]]


def test_a_block_is_one_word_of_commands():
    assert words("bind x {\n  a 1\n  b 2 ; c\n}\nd") == [["bind", "x", [["a", "1"], ["b", "2"], ["c"]]], ["d"]]


def test_variables_and_tilde_expand_outside_single_quotes():
    environment = {"HOME": "/home/x", "V": "v"}
    assert words("a $V ${V}w '$V' \"$V\" ~/f '~' \"~/g\" a~", environment=environment) == [
        ["a", "v", "vw", "$V", "v", "/home/x/f", "~", "/home/x/g", "a~"]
    ]


def test_an_assignment_sets_and_a_later_word_reads_it():
    assigned = []
    result = words("V=1\nW=2 a $V$W", assign=lambda name, value, hidden: assigned.append((name, value, hidden)))
    assert result == [["a", "12"]]
    assert assigned == [("V", "1", False), ("W", "2", False)]


def test_if_elif_else_pick_one_branch():
    text = "%if 0\na\n%elif 1\nb\n%else\nc\n%endif\n%if 1 d %else e %endif"
    assert words(text) == [["b"], ["d"]]


def test_a_nested_if_inside_a_false_one_keeps_nothing():
    assert words("%if 0\n%if 1\na\n%endif\n%endif\nb") == [["b"]]


def test_an_assignment_in_a_false_branch_does_not_run():
    assigned = []
    words("%if 0\nV=1\n%endif", assign=lambda *a: assigned.append(a))
    assert assigned == []


def test_a_pane_id_is_a_word_and_another_percent_word_is_an_error():
    assert words("select-pane -t %12") == [["select-pane", "-t", "%12"]]
    with pytest.raises(ParseError):
        parse("a %foo")


def test_an_unclosed_quote_is_an_error_product_divergence():
    with pytest.raises(ParseError) as raised:
        parse("a\nb 'c\n")
    assert raised.value.line == 2
    with pytest.raises(ParseError):
        parse("bind x {\n a\n")


def test_from_arguments_splits_on_a_trailing_semicolon():
    assert from_arguments(["a", "1;", "b", "\\;", "c;"]) == [["a", "1"], ["b", ";", "c"]]


def test_join_reads_back_as_the_commands():
    assert words(join(["display", "x y", ";", "set", "-g", "@a", "#{b}"])) == [
        ["display", "x y"],
        ["set", "-g", "@a", "#{b}"],
    ]


_WORD = st.text(st.characters(exclude_categories=("Cs",)))


@given(_WORD)
def test_a_quoted_word_reads_back_as_itself(word):
    assert words("a " + quote(word), environment={}) == [["a", word]]


@given(_WORD.filter(bool), _WORD)
def test_a_quoted_first_word_reads_back_as_itself(word, other):
    assert words(quote(word, first=True) + " " + quote(other), environment={}) == [[word, other]]


def source(tmp_path, text):
    "Read a configuration file into a server, and return it and the errors it kept."
    path = tmp_path / "pymux.conf"
    path.write_text(text)
    pymux = Pymux()
    call_command_handler("source-file", pymux, [str(path)])
    return pymux, pymux.startup_errors


def user_option(pymux, name):
    return pymux.user_options[Scope.SESSION].get(name)


def test_a_file_reads_with_the_grammar(tmp_path):
    pymux, errors = source(
        tmp_path,
        "# a comment\n"
        'set -g @a "one two" # after a command\n'
        "set -g \\\n    @b continued\n"
        "%if 1\nset -g @c yes\n%else\nset -g @c no\n%endif\n"
        "V=from-file\nset -g @d $V\n"
        "bind x {\n  set -g @e 1\n  set -g @f 2\n}\n"
        "bind y set -g @g 1 \\; set -g @h 2\n",
    )
    assert errors == []
    assert [user_option(pymux, name) for name in ("@a", "@b", "@c", "@d")] == [
        "one two",
        "continued",
        "yes",
        "from-file",
    ]
    assert pymux.global_environment["V"] == "from-file"
    manager = pymux.key_bindings_manager
    for key, wanted in (("x", "set -g @e 1 \\; set -g @f 2"), ("y", "set -g @g 1 \\; set -g @h 2")):
        binding = manager.binding_on(key)
        assert print_arguments([binding.command, *binding.arguments]) == wanted


def test_hidden_lives_for_one_text_product_divergence(tmp_path):
    pymux, errors = source(tmp_path, "%hidden SECRET=s\nset -g @a $SECRET\n")
    assert errors == []
    assert user_option(pymux, "@a") == "s"
    assert "SECRET" not in pymux.global_environment


def test_a_file_is_parsed_whole_before_it_runs(tmp_path):
    "tmux's order: a `%if` cannot see what a line above it sets."
    pymux, errors = source(tmp_path, "set -g @z 1\n%if #{@z}\nset -g @y seen\n%endif\n")
    assert errors == []
    assert user_option(pymux, "@y") is None


def test_a_file_that_does_not_parse_runs_nothing(tmp_path):
    pymux, errors = source(tmp_path, "set -g @a 1\nset -g @b 'open\n")
    assert len(errors) == 1
    assert "line 2" in errors[0]
    assert user_option(pymux, "@a") is None


async def test_a_key_runs_each_command_of_its_binding():
    async with create_session() as (pymux, state):
        run_as_client(pymux, state, "bind-key x set-option -ga @hits a \\; set-option -ga @hits b")
        run_as_client(pymux, state, 'bind-key y "set-option -ga @hits c ; set-option -ga @hits d"')

        press(state, Keys.ControlB, "x", Keys.ControlB, "y")

        await once(lambda: user_option(pymux, "@hits") == "abcd", 5.0, lambda: repr(user_option(pymux, "@hits")))


def test_the_command_line_splits_on_a_semicolon_word():
    "As tmux's: `pymux display a \\; set -g @x '#{y}'` from a shell."
    _options, _mode, command = parse_arguments(["display", "a b", ";", "set", "-g", "@x", "#{y}"])
    assert words(command, environment={}) == [["display", "a b"], ["set", "-g", "@x", "#{y}"]]


def test_an_unquoted_brace_word_is_a_block_and_its_command_must_exist(tmp_path):
    "tmux: `unknown command: end`. Unchecked, the word `end` reached -t in silence."
    pymux, errors = source(tmp_path, "bind C new-window -a -t {end}\nbind D new-window -a -t '{end}'\n")
    assert len(errors) == 1
    assert "unknown command: end" in errors[0]
    assert pymux.key_bindings_manager.binding_on("D") is None


@pytest.mark.parametrize(
    ("template", "answer", "wanted"),
    [
        ("set-option -g status-bg '%%'", "#ff0000", ["set-option", "-g", "status-bg", "#ff0000"]),
        ("rename-window '%%'", "it's", ["rename-window", "it's"]),
        ('rename-window "%%%"', 'a "b" $c ~', ["rename-window", 'a "b" $c ~']),
        ("display '%1' %%", "x", ["display", "x", "x"]),
    ],
)
def test_a_prompt_answer_fills_the_template_as_tmux_does(template, answer, wanted):
    assert words(template_replace(template, answer), environment={}) == [wanted]


def test_only_the_first_percent_pair_takes_the_answer():
    assert template_replace("a %% b %%", "x") == "a x b %%"


ORACLE_CASES = [
    "set-option -g @a 1 ; X=1\nset-option -g @b 2\n",
    'set-option -g @a "x y" # a comment\n',
    "set-option -g @a 'one $HOME' ; set-option -g @b \"two $HOME ~\"\n",
    'set-option -g @a ~/x ; set-option -g @b "~/y" ; set-option -g @c a~b\n',
    "set-option -g \\\n    @a 1\n",
    'set-option -g @a "\\e\\t\\101\\u00e9" ; set-option -g @b \\s\n',
    "bind-key x {\n  set-option -g @a 1\n  set-option -g @b 2 ; set-option -g @c 3\n}\n",
    "bind-key y set-option -g @a 1 \\; set-option -g @b 2\n",
    "%if 0\nset-option -g @a 1\n%elif 1\nset-option -g @a 2\n%else\nset-option -g @a 3\n%endif\n",
    "%if 1 set-option -g @a 1 %else set-option -g @a 2 %endif\n",
    "set-option -g @a '#{pane_id}' # set-option -g @b 2\n",
    'set-option -g @a "one\n    two"\n',
    "set-option -g @a ${HOME}x ; set-option -g @b a\\;\n",
    "set-option -g @a 1\r\n\nset-option -g @b 2\n",
    "select-pane -t %1\n",
]


def tmux_parse(text: str, tmp_path) -> list[list[object]]:
    "What tmux read from `text`, read back by this grammar."
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    config = tmp_path / "case.conf"
    config.write_text(text)
    environment = {
        "HOME": str(home),
        "PATH": os.environ.get("PATH", ""),
        "TMUX_TMPDIR": str(tmp_path),
        # Else tmux prints a character it cannot judge printable as `_`.
        "LC_ALL": "C.UTF-8",
    }
    done = subprocess.run(
        [TMUX, "-L", "grammar", "-f", "/dev/null", "start-server", ";", "source-file", "-nv", str(config)],
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    printed = [re.sub(r"^.*?:\d+: ", "", line) for line in done.stdout.splitlines()]
    return words("\n".join(printed), environment={})


def as_printed(commands: list[Command]) -> list[Command]:
    "`-v` prints the commands of a block before the command that holds it."
    out: list[Command] = []
    for command in commands:
        for word in command.words:
            if isinstance(word, Block):
                out += as_printed(word.commands)
        out.append(command)
    return out


@pytest.mark.skipif(TMUX is None, reason="PYMUX_TMUX names no tmux")
@pytest.mark.parametrize("text", ORACLE_CASES)
def test_tmux_reads_the_same_commands(text, tmp_path):
    expected = tmux_parse(text, tmp_path)
    ours = parse(text, environment={"HOME": str(tmp_path / "home")}, condition=lambda f: f not in ("", "0"))
    assert shape(as_printed(ours)) == expected
