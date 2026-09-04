"""
The terminfo entry that a pane is told to use.

A program has two ways to learn what a terminal can do. It can ask,
which ptterm answers with XTGETTCAP, or it can read the database of
the machine it runs on. Everything built on ncurses reads the
database, so without an entry of our own a pane reads the one for
xterm-256color and never writes a curly underline, whatever the
terminal behind it can draw.

The entry is compiled at build time out of the same table that answers
the query. `PYMUX_TERMINFO` names the directory it lands in, and the
wrapper of the package sets it.

**Naming an entry that is not installed is worse than naming xterm**,
so nothing is claimed until the compiled entry is found. Two things
can hide it: a pymux that was not built with one, and a pane that runs
somewhere else. The second is why the query matters more than the
database: `TERM` crosses an ssh connection and `TERMINFO_DIRS` does
not.
"""
import os
from typing import MutableMapping

__all__ = [
    "ENTRY_NAME",
    "FALLBACK_NAME",
    "database",
    "terminal_name",
    "add_to_environment",
]

#: The name of the entry that describes a pane.
ENTRY_NAME = "pymux"

#: What a pane is told when the entry is not there.
FALLBACK_NAME = "xterm-256color"

#: The variable that names the directory the entry was compiled into.
DATABASE_VARIABLE = "PYMUX_TERMINFO"


def database() -> str | None:
    """
    The directory that holds the compiled entry, or None.

    ncurses stores an entry under the first letter of its name, or
    under the hexadecimal of that letter when the database was built
    to hash. Both are looked for, because either can turn up.
    """
    path = os.environ.get(DATABASE_VARIABLE)
    if not path:
        return None

    first = ENTRY_NAME[0]
    for directory in (first, "%02x" % ord(first)):
        if os.path.exists(os.path.join(path, directory, ENTRY_NAME)):
            return path
    return None


def terminal_name() -> str:
    "The name to put in `TERM` for a pane."
    return ENTRY_NAME if database() else FALLBACK_NAME


def add_to_environment(environment: MutableMapping[str, str]) -> None:
    """
    Point a pane at the database that holds the entry.

    ncurses reads `TERMINFO_DIRS` as a list, and an empty entry in it
    means the place the system keeps. The trailing colon therefore
    says "ours first, then everything that was there before".
    """
    path = database()
    if path is None:
        return

    already = environment.get("TERMINFO_DIRS")
    environment["TERMINFO_DIRS"] = "%s:%s" % (path, already or "")
