"""
Where the configuration file is.

**A client reads it too, not only the server.** A client option is
announced by the client that holds it, so the client has to find the
same file the server finds -- and over SSH they are two files on two
machines, which is the point. So this cannot live in the entry point:
`client/` would have to import one to ask a question about a path.
Lillecarl/pyterm#225.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence

from pymux.commands.aliases import ALIASES
from pymux.grammar import ParseError, parse

__all__ = [
    "client_options_in",
    "config_paths",
    "find_configs",
    "plugin_dir",
    "tmux_config_paths",
]

#: The command that names a client option. Everything else in the file
#: belongs to the server, and the server reads the whole file itself.
SET_CLIENT_OPTION = "set-client-option"


def config_paths(environ: Mapping[str, str] | None = None) -> list[str]:
    """
    Where pymux looks for a configuration file, best first.

    The XDG path comes first, because that is where a configuration
    file belongs and where every terminal this collection is measured
    against now looks. `~/.pymux.conf` comes second and stays: it is
    the only path pymux ever read, so dropping it would break every
    configuration that exists. pymux reads the first that is there;
    tmux reads all of its own, `tmux_config_paths`.

    `$XDG_CONFIG_HOME` has to be an absolute path. The specification
    says an implementation ignores a relative one, and this does.

    Lillecarl/pyterm#198.
    """
    return [
        os.path.join(_config_home(environ), "pymux", "pymux.conf"),
        os.path.expanduser("~/.pymux.conf"),
    ]


def plugin_dir(environ: Mapping[str, str] | None = None) -> str:
    "Where a person's own plugins are: `$XDG_CONFIG_HOME/pymux/plugins`."
    return os.path.join(_config_home(environ), "pymux", "plugins")


def terminals_file(environ: Mapping[str, str] | None = None) -> str:
    "Where a person's own terminals are: `$XDG_CONFIG_HOME/pymux/terminals.py`."
    return os.path.join(_config_home(environ), "pymux", "terminals.py")


def _config_home(environ: Mapping[str, str] | None = None) -> str:
    if environ is None:
        environ = os.environ

    config_home = environ.get("XDG_CONFIG_HOME") or ""
    if not os.path.isabs(config_home):
        config_home = os.path.expanduser("~/.config")
    return config_home


def tmux_config_paths(environ: Mapping[str, str] | None = None) -> list[str]:
    """
    Where tmux looks for its configuration, in the order it reads them.

    `TMUX_CONF` in tmux's `Makefile.am`: `/etc/tmux.conf`,
    `~/.tmux.conf`, `$XDG_CONFIG_HOME/tmux/tmux.conf`,
    `~/.config/tmux/tmux.conf`. `expand_paths` in `tmux.c` drops the
    XDG one when the variable is not set, and a path named twice.
    Lillecarl/pyterm#1027.
    """
    if environ is None:
        environ = os.environ

    paths = ["/etc/tmux.conf", os.path.expanduser("~/.tmux.conf")]
    config_home = environ.get("XDG_CONFIG_HOME")
    if config_home:
        paths.append(os.path.join(config_home, "tmux", "tmux.conf"))
    paths.append(os.path.expanduser("~/.config/tmux/tmux.conf"))
    return list(dict.fromkeys(paths))


def find_configs(environ: Mapping[str, str] | None = None, *, tmux: bool = False) -> list[str]:
    """
    The configuration files to read: the first of pymux's own that is
    there, or with `tmux`, every one of tmux's that is there, as
    `start_cfg` in tmux's `cfg.c` reads them.

    This is separate from `run` for the reason `parse_arguments` is: a
    test can ask which file an environment names without starting a
    server.
    """
    # A file, and not only something with that name. A directory called
    # `pymux.conf` would answer `os.path.exists` and then fail to open,
    # which reads as a broken configuration rather than as no
    # configuration.
    if tmux:
        return [os.path.abspath(path) for path in tmux_config_paths(environ) if os.path.isfile(path)]
    for path in config_paths(environ):
        if os.path.isfile(path):
            return [os.path.abspath(path)]
    return []


def client_options_in(paths: Sequence[str]) -> list[tuple[str, str]]:
    """
    The client options configuration files name, in their own order.

    A client announces these when it attaches, so that a person can
    say in configuration what their terminal is. Lillecarl/pyterm#225.

    **This reads the file and does not run it.** The file is the
    server's, and the server runs all of it; a client takes the two or
    three lines that are about itself and leaves the rest alone.
    The server's grammar, `pymux.grammar`, reads it here too, so the
    two readings of one file are the same. A `%if` here is false: a
    format needs the server to answer it.

    Nothing here says a file is wrong. The server reads the same file
    and says it once, where a person is looking; saying it twice, from
    two processes, on two machines, is worse than not saying it.
    """
    found = []
    for path in paths:
        try:
            with open(path) as opened:
                commands = parse(opened.read(), condition=lambda _format: False)
        except OSError, ParseError:
            continue
        for command in commands:
            one = _client_option(command.strings())
            if one is not None:
                found.append(one)
    return found


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
