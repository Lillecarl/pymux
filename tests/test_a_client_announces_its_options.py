"""
A client reads its own configuration file and says what it holds.

A client option belongs to the terminal a person is sitting at, and
only that side can read the file that names it: over SSH the server's
configuration is another machine's. So the client takes the
`set-client-option` lines out of its own file and sends them with
`start-gui`. Lillecarl/pymux#223.
"""

import contextvars
import json
from contextlib import asynccontextmanager

import anyio

from pymux.config import client_options_in
from pymux.main import Pymux
from pymux.nearest import NEAREST
from pymux.pipes.memory import connect_in_memory
from pymux.server import ServerConnection
from pymux.style import DEFAULT_THEME


# ----------------------------------------------------------------------
# What the client takes out of the file.


def _written(tmp_path, text: str) -> str:
    path = tmp_path / "pymux.conf"
    path.write_text(text)
    return str(path)


def test_no_file_says_nothing():
    assert client_options_in(None) == []
    assert client_options_in("/nowhere/pymux.conf") == []


def test_it_takes_the_client_lines(tmp_path):
    path = _written(
        tmp_path,
        "set-option status off\n"
        "set-client-option theme grey\n"
        "bind-key x kill-pane\n"
        "set-client-option swap-light-and-dark-colors on\n",
    )

    assert client_options_in(path) == [
        ("theme", "grey"),
        ("swap-light-and-dark-colors", "on"),
    ]


def test_the_rest_of_the_file_is_the_servers(tmp_path):
    "A client reads the file and does not run it."
    path = _written(tmp_path, "set-option status off\nnew-window\n")

    assert client_options_in(path) == []


def test_a_comment_is_a_comment(tmp_path):
    path = _written(
        tmp_path,
        "# set-client-option theme grey\n   # and this one\nset-client-option theme default\n",
    )

    assert client_options_in(path) == [("theme", "default")]


def test_a_quoted_value_arrives_whole(tmp_path):
    "`shlex`, the way `handle_command` splits it."
    path = _written(tmp_path, 'set-client-option theme "base16:gruvbox-dark-hard"\n')

    assert client_options_in(path) == [("theme", "base16:gruvbox-dark-hard")]


def test_the_alias_counts(tmp_path):
    path = _written(tmp_path, "setc theme grey\n")

    assert client_options_in(path) == [("theme", "grey")]


def test_two_commands_on_one_line(tmp_path):
    path = _written(tmp_path, "setc theme grey ; setc swap-light-and-dark-colors on\n")

    assert client_options_in(path) == [
        ("theme", "grey"),
        ("swap-light-and-dark-colors", "on"),
    ]


def test_a_target_in_a_file_names_nobody(tmp_path):
    """
    `-t` names another client, and a configuration file is read by the
    client it belongs to. There is nothing here for it to name.
    """
    path = _written(tmp_path, "set-client-option -t somewhere:/dev/pts/3 theme grey\n")

    assert client_options_in(path) == []


def test_a_read_sets_nothing(tmp_path):
    "One word is `set-client-option theme` with no value, which is a read."
    path = _written(tmp_path, "set-client-option theme\n")

    assert client_options_in(path) == []


def test_a_line_that_does_not_parse_is_left_to_the_server(tmp_path):
    "The server reads the same file and says it once, where a person looks."
    path = _written(tmp_path, 'set-client-option theme "unclosed\n')

    assert client_options_in(path) == []


# ----------------------------------------------------------------------
# What the server does with them.


def _start_gui(announced) -> str:
    return json.dumps(
        {
            "cmd": "start-gui",
            "detach-others": False,
            "color-depth": None,
            "term": "xterm-256color",
            "colorterm": "",
            "hostname": "somewhere-else",
            "client-options": announced,
            "data": "",
        }
    )


async def _attached(pymux: Pymux, announced) -> ServerConnection:
    server_end, client_end = connect_in_memory()

    # A context of its own, which is what both real transports do. Two
    # clients in one default context save and restore each other's
    # application, and the second one never starts. Lillecarl/pymux#230.
    context = contextvars.copy_context()
    connection = context.run(lambda: ServerConnection(pymux, server_end))
    pymux.connections.append(connection)

    client_end.write_nowait(_start_gui(announced))

    with anyio.fail_after(5.0):
        while connection.client_state is None and not connection._closed:
            await anyio.sleep(0.005)

    return connection


@asynccontextmanager
async def a_server():
    "A server that is stopped whatever the test does to it."
    pymux = Pymux()
    pymux.test_mode = True

    async with pymux.running():
        try:
            yield pymux
        finally:
            for connection in list(pymux.connections):
                connection.detach_and_close()
            pymux.stop()


async def test_the_client_draws_with_what_it_announced():
    async with a_server() as pymux:
        client = (await _attached(pymux, [["theme", "grey"]])).client_state

        assert client.theme == "grey"
        assert (
            client.style.get_attrs_for_style_str("class:statusbar").bgcolor == "5f5f87"
        )


async def test_a_client_that_announces_nothing_starts_on_the_search():
    """
    Which draws the default until its terminal says something.
    Lillecarl/pymux#346.
    """
    async with a_server() as pymux:
        client = (await _attached(pymux, [])).client_state

        assert client.theme == NEAREST
        assert client.theme_in_use == DEFAULT_THEME


async def test_two_clients_keep_their_own():
    async with a_server() as pymux:
        one = await _attached(pymux, [["theme", "grey"]])
        two = await _attached(pymux, [])

        assert one.client_state.theme == "grey"
        assert two.client_state.theme == NEAREST


async def test_a_bad_value_never_fails_the_attach():
    """
    A person with a typo gets their panes and a message. Refusing to
    attach would leave them with a terminal they cannot use and a file
    they may not be able to reach.
    """
    async with a_server() as pymux:
        client = (
            await _attached(
                pymux, [["theme", "nosuchtheme"], ["swap-light-and-dark-colors", "on"]]
            )
        ).client_state

        assert client is not None, "the attach was refused"
        assert client.theme == NEAREST
        assert "nosuchtheme" in (client.message or "")
        # And the line after the bad one still ran.
        assert client.swap_dark_and_light is True


def test_the_server_reads_the_same_line_and_does_nothing(tmp_path):
    """
    The server reads the whole file, this line included, and it has no
    client to set it on. A command that raised here would take
    `source-file` with it and pymux would draw nothing at all, which is
    what Lillecarl/pymux#199 was.
    """
    from pymux.commands import handle_command

    path = _written(tmp_path, "set-client-option theme grey\nset-option status off\n")

    pymux = Pymux()
    handle_command(pymux, "source-file %s" % (path,))

    assert list(pymux.startup_errors) == []
    # And the line under it still ran, so the file was not abandoned.
    assert pymux.enable_status is False


def test_a_person_who_types_it_with_nobody_attached_is_told():
    "A no-op is for a file being read. Typed, it has to say why nothing happened."
    from pymux.commands import handle_command

    pymux = Pymux()
    handle_command(pymux, "set-client-option theme grey")

    assert "no client" in "\n".join(pymux.message_log)


async def test_a_name_nobody_offers_never_fails_the_attach():
    async with a_server() as pymux:
        client = (await _attached(pymux, [["not-an-option", "on"]])).client_state

        assert client is not None, "the attach was refused"
        assert "not-an-option" in (client.message or "")
