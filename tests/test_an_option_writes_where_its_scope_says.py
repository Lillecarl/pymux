"""
Every option writes on the object its scope names.

`Option.scope` says who holds an option and `Option.held_by` turns that
into the object to write on: the server, the active window, or the
client a command means. Three option classes once wrote to the server
whatever their scope said, so a client option's value went to the server
while `show-client-options` read it back off the client.
Lillecarl/pymux#348.

This judges `FieldOption`, which every scope's options go through. Which
options exist is each scope's dataclass; what a scope does is here.
"""

from __future__ import annotations

from types import SimpleNamespace

from pymux.core.main import Pymux
from pymux.options.client_options import ClientOptions
from pymux.options.options import ALL_CLIENT_OPTIONS, ALL_OPTIONS, ALL_SERVER_OPTIONS


def test_a_client_option_is_written_on_the_client():
    pymux = Pymux()
    client = SimpleNamespace(options=ClientOptions())

    ALL_CLIENT_OPTIONS["full-screen"].set_value(pymux, "on", target=client)

    assert client.options.full_screen is True
    assert not hasattr(pymux.options, "full_screen"), "the value went to the server"


def test_a_session_option_is_written_on_the_defaults():
    pymux = Pymux()

    ALL_OPTIONS["status-interval"].set_value(pymux, "7")

    assert pymux.session_defaults.status_interval == 7
    assert pymux.sessions == [] or all(not session.options.own() for session in pymux.sessions)


def test_a_server_option_is_written_on_the_server():
    pymux = Pymux()

    ALL_SERVER_OPTIONS["job-ttl"].set_value(pymux, "9")

    assert pymux.options.job_ttl == 9
