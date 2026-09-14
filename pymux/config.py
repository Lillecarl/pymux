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

__all__ = [
    "config_paths",
    "find_config",
]


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
