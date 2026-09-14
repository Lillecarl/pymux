"""
Where the configuration file is.

**A client reads it too, not only the server.** A client option is
announced by the client that holds it, so the client has to find the
same file the server finds -- and over SSH they are two files on two
machines, which is the point. So this cannot live in the entry point:
`client/` would have to import one to ask a question about a path.
Lillecarl/pymux#223.
"""

from __future__ import annotations

import os
import shlex

from pymux.commands.aliases import ALIASES

__all__ = [
    "client_options_in",
    "config_paths",
    "find_config",
]

#: The command that names a client option. Everything else in the file
#: belongs to the server, and the server reads the whole file itself.
SET_CLIENT_OPTION = "set-client-option"


def config_paths(environ=None) -> list[str]:
    """
    Where pymux looks for a configuration file, best first.

    The XDG path comes first, because that is where a configuration
    file belongs and where every terminal this collection is measured
    against now looks. `~/.pymux.conf` comes second and stays: it is
    the only path pymux ever read, so dropping it would break every
    configuration that exists. tmux reads its two in the same order.

    `$XDG_CONFIG_HOME` has to be an absolute path. The specification
    says an implementation ignores a relative one, and this does.

    Lillecarl/pymux#196.
    """
    if environ is None:
        environ = os.environ

    config_home = environ.get("XDG_CONFIG_HOME") or ""
    if not os.path.isabs(config_home):
        config_home = os.path.expanduser("~/.config")

    return [
        os.path.join(config_home, "pymux", "pymux.conf"),
        os.path.expanduser("~/.pymux.conf"),
    ]


def find_config(environ=None) -> str | None:
    """
    The first configuration file that is there, or `None`.

    This is separate from `run` for the reason `parse_arguments` is: a
    test can ask which file an environment names without starting a
    server.
    """
    for path in config_paths(environ):
        # A file, and not only something with that name. A directory
        # called `pymux.conf` would answer `os.path.exists` and then
        # fail to open, which reads as a broken configuration rather
        # than as no configuration.
        if os.path.isfile(path):
            return os.path.abspath(path)
    return None


def client_options_in(path: str | None) -> list[tuple[str, str]]:
    """
    The client options a configuration file names, in its own order.

    A client announces these when it attaches, so that a person can
    say in configuration what their terminal is. Lillecarl/pymux#223.

    **This reads the file and does not run it.** The file is the
    server's, and the server runs all of it; a client takes the two or
    three lines that are about itself and leaves the rest alone.
    Splitting the same way `handle_command` splits is what keeps the
    two readings of one line the same: `shlex`, a leading `#` for a
    comment, a bare `;` between commands, and the aliases.

    Nothing here says a file is wrong. The server reads the same file
    and says it once, where a person is looking; saying it twice, from
    two processes, on two machines, is worse than not saying it.
    """
    if not path:
        return []

    try:
        with open(path, "r") as opened:
            lines = opened.readlines()
    except OSError:
        return []

    found = []
    for line in lines:
        for parts in _commands_in(line):
            one = _client_option(parts)
            if one is not None:
                found.append(one)
    return found


def _commands_in(line: str) -> list[list[str]]:
    "The commands of one line, split the way `handle_command` splits."
    line = line.strip()
    if not line or line.startswith("#"):
        return []

    try:
        parts = shlex.split(line)
    except ValueError:
        return []

    commands: list[list[str]] = [[]]
    for part in parts:
        if part == ";":
            commands.append([])
        else:
            commands[-1].append(part)
    return commands


def _client_option(parts: list[str]) -> tuple[str, str] | None:
    "The name and the value one command sets, when it sets one."
    if not parts or ALIASES.get(parts[0], parts[0]) != SET_CLIENT_OPTION:
        return None

    rest = parts[1:]
    # A flag means `-t`, which names another client. A configuration
    # file is read by the client it belongs to, so there is nothing
    # here for it to name.
    #
    # Two words are a name and a value. One word is a read, and a
    # client that is attaching has nothing to read it back to.
    if len(rest) != 2 or rest[0].startswith("-"):
        return None
    return rest[0], rest[1]
