"""
The schemes of the base16 spec, as pymux themes.

`set-option theme base16:<name>` chooses one. The schemes live in the
tinted-theming/schemes repository, converted to JSON when the package
is built, because a theme that needs a YAML parser to be read is a
theme that cannot be read anywhere.

A base16 scheme is sixteen colours and nothing else, and the spec
documents what each one means: base00 the background, base03 the
muted tone, base05 the foreground, base08 the red, and so on. That
is the whole of what a theme reads in it -- the chrome derives from
the anchors, and the palette a pane answers a program with is the
sixteen verbatim, in the order the spec's own templates number them.
Lillecarl/pymux#282, Lillecarl/pymux#283.

The collection also says, per scheme, whether it is a dark one or a
light one; nothing here needs that, because the roles say so by
contrast and not by assertion.
"""

import json
import os
from functools import lru_cache
from pathlib import Path

from pymux.style import create_theme, derive, roles_of_palette

__all__ = ["base16_roles", "base16_theme", "names"]

#: The converted collection: one JSON object, a scheme's name to its
#: sixteen. The package carries it beside this module; a run from the
#: source tree names where it is with the environment, which is what
#: the checks do.
_SCHEMES_FILE = "base16-schemes.json"


def _schemes_path() -> Path | None:
    from_env = os.environ.get("PYMUX_BASE16_SCHEMES")
    if from_env:
        found = Path(from_env)
        return found if found.is_file() else None
    beside = Path(__file__).parent / _SCHEMES_FILE
    return beside if beside.is_file() else None


@lru_cache(maxsize=None)
def _schemes() -> dict:
    """
    Every scheme the collection holds, by its name.

    A run with no collection at all -- nobody built the package with
    it and nothing named a path -- holds none, and every name raises
    `KeyError`, which is what the option reports.
    """
    path = _schemes_path()
    if path is None:
        return {}
    with open(path) as opened:
        return json.load(opened)


def names() -> list[str]:
    """
    Every scheme the collection holds, by the name
    `set-option theme base16:<name>` takes.
    """
    return sorted(_schemes())


#: The letters in the order a terminal numbers its palette: the
#: mapping the spec's own terminal template writes, which asks for a
#: scheme's backgrounds and text in the first eight slots and its
#: eight accents in the rest.
_TERMINAL_ORDER = (
    "base00", "base08", "base0B", "base0A",
    "base0D", "base0E", "base0C", "base05",
    "base03", "base08", "base0B", "base0A",
    "base0D", "base0E", "base0C", "base07",
)


def base16_roles(name: str) -> dict[str, str]:
    """
    The roles of one base16 scheme.

    A name the collection does not hold raises `KeyError`, which is
    what the option turns into an error a person can read.
    """
    palette = _schemes()[name]
    return roles_of_palette([palette[letter] for letter in _TERMINAL_ORDER])


def base16_theme(name: str):
    """
    The scheme of one base16 scheme, as the chrome's style.

    The same roles the pane answers from, put through the same
    `derive` every theme goes through.
    """
    return create_theme(derive(base16_roles(name)))
