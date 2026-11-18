"""
tmux's user options: any name that starts with `@`.

tmux keeps one, gives it no meaning, and hands it back in `#{@name}`
and `show-options @name`. Plugins and themes are configured this way:
`@plugin`, `@continuum-restore`, `@catppuccin_flavour`. pymux keeps
one value for the server, one for every session and one for every
window: the globals of `set -s`, `set -g` and `setw -g`, which is
every user option a configuration in `checks.pymux-tmux-corpus`
writes. Lillecarl/pyterm#1032.

A value for one session, window or pane is Lillecarl/pyterm#1038.
"""

from __future__ import annotations

import argparse
from typing import TYPE_CHECKING

from pymux.options.options import Scope, a_file_speaks

if TYPE_CHECKING:
    from pymux.core.main import Pymux

__all__ = ["SCOPES", "find", "is_user_option", "mark", "read_scope", "scope_of"]

#: What holds a user option, in the order `format_find` (tmux's
#: format.c) reads them, less the pane's and the window's own.
SCOPES = (Scope.SERVER, Scope.WINDOW, Scope.SESSION)


def is_user_option(name: str) -> bool:
    return name.startswith("@")


def scope_of(pymux: Pymux, args: argparse.Namespace, command_scope: Scope) -> Scope:
    """
    The global a write or a read names: tmux's `options_scope_from_flags`.

    `-s` is the server's, `-w` or a window command the windows', and
    anything else the sessions'. A configuration file writes the
    globals, `-g` or not, as it does for every other option.
    """
    if getattr(args, "p", False):
        raise _not_kept("pane")
    if getattr(args, "s", False):
        return Scope.SERVER
    scope = Scope.WINDOW if command_scope is Scope.WINDOW or getattr(args, "w", False) else Scope.SESSION
    if not getattr(args, "g", 0) and not a_file_speaks(pymux):
        raise _not_kept(str(scope))
    return scope


def read_scope(args: argparse.Namespace, command_scope: Scope) -> Scope:
    "The global a read names. Without `-g` it is what a session or window inherits, which is the same."
    if getattr(args, "s", False):
        return Scope.SERVER
    return Scope.WINDOW if command_scope is Scope.WINDOW else Scope.SESSION


def _not_kept(what: str) -> ValueError:
    return ValueError("pymux keeps a user option for every %s alike, so it takes -g: Lillecarl/pyterm#1038" % (what,))


def find(pymux: Pymux, name: str) -> str | None:
    "What `#{@name}` reads: the first scope that holds it."
    for scope in SCOPES:
        value = pymux.user_options[scope].get(name)
        if value is not None:
            return value
    return None


def mark(pymux: Pymux, scope: Scope, name: str) -> None:
    """
    A value a person set survives an upgrade, and the file the new
    server reads says every other. Lillecarl/pyterm#969.
    """
    marked = pymux.set_by_hand["user_options"]
    key = "%s %s" % (scope, name)
    if a_file_speaks(pymux):
        marked.discard(key)
    else:
        marked.add(key)
