"""
One key has one binding, whatever a person calls it.

`bind-key` and `unbind-key` both take a name, and one key has several:
"ctrl+a", "C-a" and "c-a" all reach `Keys.ControlA`. The store was
keyed by the text, so each of those was a binding of its own.
Lillecarl/pymux#235.
"""

import pytest

from pymux.commands.commands import handle_command
from pymux.main import Pymux

#: Three names for one key, and one that reads as nothing.
CTRL_A = ("ctrl+a", "C-a", "c-a")


@pytest.fixture
def pymux():
    return Pymux()


def run(pymux, command):
    "Run one command and give back what it complained about."
    before = len(pymux.startup_errors)
    handle_command(pymux, command)
    return pymux.startup_errors[before:]


def bindings(pymux):
    return pymux.key_bindings_manager.custom_bindings


@pytest.mark.parametrize("first", CTRL_A)
@pytest.mark.parametrize("second", CTRL_A)
def test_one_key_holds_one_binding_however_it_is_written(pymux, first, second):
    "The second name binds the same key, so it replaces the first."
    run(pymux, "bind-key -n %s new-window" % (first,))
    run(pymux, "bind-key -n %s kill-pane" % (second,))

    (binding,) = bindings(pymux).values()
    assert binding.command == "kill-pane"


@pytest.mark.parametrize("bound", CTRL_A)
@pytest.mark.parametrize("unbound", CTRL_A)
def test_a_key_unbinds_under_any_of_its_names(pymux, bound, unbound):
    run(pymux, "bind-key -n %s new-window" % (bound,))
    run(pymux, "unbind-key -n %s" % (unbound,))

    assert bindings(pymux) == {}


def test_the_prefix_tells_two_bindings_of_one_key_apart(pymux):
    "The same key with and beside the prefix are two bindings."
    run(pymux, "bind-key -n c-a new-window")
    run(pymux, "bind-key c-a kill-pane")

    assert len(bindings(pymux)) == 2


def test_unbinding_a_key_nobody_bound_says_nothing(pymux):
    "It is not an error in tmux either."
    assert run(pymux, "unbind-key -n c-a") == []


def test_unbinding_a_name_that_reads_as_no_key_is_an_error(pymux):
    """
    It used to be silence, because the name went straight into a
    dictionary that did not hold it. `bind-key` has always said so.
    """
    complaints = run(pymux, "unbind-key -n M-Nope")

    assert complaints
    assert "M-Nope" in complaints[0]


def test_a_caller_asks_for_a_binding_by_any_name_of_its_key(pymux):
    """
    `the_binding_on` is the one way in, so nothing outside has to know
    the shape of the dictionary key. `nix/home-manager-judge.py` did,
    and the gate caught it.
    """
    run(pymux, "bind-key -n ctrl+a new-window")
    manager = pymux.key_bindings_manager

    assert manager.the_binding_on("C-a").command == "new-window"
    assert manager.the_binding_on("c-a", needs_prefix=True) is None


def test_list_keys_shows_the_name_the_person_wrote(pymux):
    """
    The binding is held under the key and not under the name, so the
    name has to be kept beside it. Otherwise the line a person reads
    back is a re-spelling of what they typed.
    """
    run(pymux, "bind-key -n ctrl+a new-window")

    (binding,) = bindings(pymux).values()
    assert binding.written == "ctrl+a"
