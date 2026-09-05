"""
A line of a configuration file that fails says so.

`startup()` reads the file while the first application is still being
built. `show_message` needs a client state and finds none at that
moment, and `add_command_error` writes into a list that only exists
while a `run-command` packet is being handled. So both answers were
dropped and the file looked as though it had run.

That cost an hour once: `set -g status off` is the tmux spelling, pymux
takes `set-option <option> <value>` with no flags, and the line never
ran. Lillecarl/pymux#38.
"""
from pymux.commands.commands import call_command_handler
from pymux.main import Pymux


def source(tmp_path, text):
    "Read a configuration file, and return (pymux, the errors it kept)."
    path = tmp_path / "pymux.conf"
    path.write_text(text)
    pymux = Pymux()
    call_command_handler("source-file", pymux, [str(path)])
    return pymux, pymux.startup_errors


# ----------------------------------------------------------------------
# What is kept.


def test_a_file_that_is_right_keeps_nothing(tmp_path):
    _pymux, errors = source(tmp_path, "set status off\n")
    assert errors == []


def test_the_tmux_spelling_of_a_global_option_is_reported(tmp_path):
    """
    The line that started this.

    pymux takes `set-option <option> <value>` and has no `-g`, because
    there is one session per server. So the line carries three words for
    two slots and docopt rejects it with the usage string. That message
    says nothing about which word was wrong, which is the argument of
    Lillecarl/pymux#48. What matters here is that it is said at all.
    """
    _pymux, errors = source(tmp_path, "set -g status off\n")
    assert len(errors) == 1
    assert "set-option <option> <value>" in errors[0]


def test_a_command_that_does_not_exist_is_reported(tmp_path):
    _pymux, errors = source(tmp_path, "not-a-command\n")
    assert len(errors) == 1
    assert "invalid command: not-a-command" in errors[0]


def test_a_bad_value_is_reported(tmp_path):
    _pymux, errors = source(tmp_path, "set status maybe\n")
    assert len(errors) == 1


def test_every_failing_line_is_reported(tmp_path):
    _pymux, errors = source(
        tmp_path, "set -g status off\nnot-a-command\nset status off\n"
    )
    assert len(errors) == 2


# ----------------------------------------------------------------------
# What the message says.


def test_the_message_names_the_file_and_the_line(tmp_path):
    "Without it a person reads the complaint and hunts for the line."
    _pymux, errors = source(tmp_path, "set status off\nset -g status off\n")
    assert errors[0].startswith("%s line 2: " % (tmp_path / "pymux.conf"))


def test_a_comment_and_a_blank_line_do_not_shift_the_count(tmp_path):
    _pymux, errors = source(tmp_path, "# a comment\n\nset -g status off\n")
    assert "line 3: " in errors[0]


# ----------------------------------------------------------------------
# Who is told.


def test_the_first_client_is_told(tmp_path):
    "And the message is what the errors said."

    class AClientState:
        message = None

    pymux, errors = source(tmp_path, "set -g status off\n")
    errors_before = list(errors)
    assert errors_before

    state = AClientState()
    pymux.report_startup_errors(state)
    assert state.message == errors_before[0]


def test_the_second_client_is_not_told(tmp_path):
    "It did not make the mistake, and it cannot act on the message."

    class AClientState:
        message = None

    pymux, _errors = source(tmp_path, "set -g status off\n")
    first, second = AClientState(), AClientState()
    pymux.report_startup_errors(first)
    pymux.report_startup_errors(second)
    assert first.message is not None
    assert second.message is None


def test_a_client_with_nothing_to_report_gets_no_message(tmp_path):
    class AClientState:
        message = "something else"

    pymux, _errors = source(tmp_path, "set status off\n")
    state = AClientState()
    pymux.report_startup_errors(state)
    assert state.message == "something else"


# ----------------------------------------------------------------------
# The reading of one line does not leak into the next.


def test_the_place_is_cleared_after_the_file(tmp_path):
    pymux, _errors = source(tmp_path, "set -g status off\n")
    assert pymux.sourcing is None


def test_a_missing_file_is_still_an_error_of_its_own(tmp_path):
    "`source-file` raised for that before, and still does."
    from pymux.commands.commands import CommandException

    pymux = Pymux()
    try:
        call_command_handler(
            "source-file", pymux, [str(tmp_path / "not-there.conf")]
        )
    except CommandException:  # pragma: no cover - it is caught below
        raise AssertionError("the handler should catch this itself")
    assert len(pymux.startup_errors) == 1
    assert "IOError" in pymux.startup_errors[0]
