"""
Every option writes on the object its scope names.

`Option.scope` says who holds an option and `Option.held_by` turns that
into the object to write on: the server, the active window, or the
client a command means. Lillecarl/pymux#223 added it, and three option
classes kept writing to the server whatever their scope said. Nothing
was wrong at the time -- all three were session options, and the server
is what a session option is held by -- so the cost was the next one:
a client-scoped option of one of those classes would write the value on
the server while `show-client-options` read it back off the client.
Lillecarl/pymux#348.

This judges the classes and not the table. Which options exist is
`options.py`; what a class does with a scope is here.
"""

from types import SimpleNamespace

import pytest

from pymux.main import Pymux
from pymux.options import (
    ChoiceOption,
    ExtendedKeys,
    ExtendedKeysOption,
    JustifyOption,
    KeysOption,
    OnOffOption,
    PositiveIntOption,
    Scope,
    StringOption,
)

#: One option of each class that holds its value in an attribute, made
#: client-scoped, with what a person writes and what it becomes.
WRITERS = [
    ("on/off", lambda: OnOffOption("judged", scope=Scope.CLIENT), "on", True),
    ("string", lambda: StringOption("judged", scope=Scope.CLIENT), "desk", "desk"),
    (
        "choice",
        lambda: ChoiceOption("judged", ("one", "two"), scope=Scope.CLIENT),
        "two",
        "two",
    ),
    ("int", lambda: PositiveIntOption("judged", scope=Scope.CLIENT), "5", 5),
    ("keys", lambda: KeysOption("judged", scope=Scope.CLIENT), "vi", True),
    (
        "extended keys",
        lambda: ExtendedKeysOption("judged", scope=Scope.CLIENT),
        "on",
        ExtendedKeys.ON,
    ),
    ("justify", lambda: JustifyOption("judged", scope=Scope.CLIENT), "left", "left"),
]


@pytest.mark.parametrize(
    "option,written,becomes",
    [(make, written, becomes) for _name, make, written, becomes in WRITERS],
    ids=[name for name, _make, _written, _becomes in WRITERS],
)
def test_a_client_option_is_written_on_the_client(option, written, becomes):
    pymux = Pymux()
    client = SimpleNamespace()

    option().set_value(pymux, written, target=client)

    assert client.judged == becomes
    assert not hasattr(pymux, "judged"), "the value went to the server"


@pytest.mark.parametrize(
    "option,written,becomes",
    [(make, written, becomes) for _name, make, written, becomes in WRITERS],
    ids=[name for name, _make, _written, _becomes in WRITERS],
)
def test_a_session_option_is_written_on_the_server(option, written, becomes):
    "The other half: a scope of its own must not have moved the default."
    pymux = Pymux()
    made = option()
    made.scope = Scope.SESSION

    made.set_value(pymux, written)

    assert pymux.judged == becomes
