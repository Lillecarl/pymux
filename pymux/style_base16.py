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

from pymux.style import _blend, _other_of, _readable, create_theme, derive

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


def base16_roles(name: str) -> dict[str, str]:
    """
    The roles of one base16 scheme, from the letters it names.

    The spec names what each letter is for, and the chrome reads them
    directly -- base01 is the lighter background the bars draw on,
    base02 the selection the matches sit on, base04 the darker text
    the border takes -- rather than blending those steps back out of
    two colours, which is what a terminal palette can do and a scheme
    is richer than. The blend remains for the roles finer than any
    letter: the cut column, the suggestion, the bright of a signal.
    The palette a pane answers a program with is the sixteen in the
    order the spec's own terminal template numbers them.
    Lillecarl/pymux#285.

    A name the collection does not hold raises `KeyError`, which is
    what the option turns into an error a person can read.
    """
    p = _schemes()[name]

    def blend(a, b, towards_b):
        # A letter finds its colour here; a literal hex is itself.
        return _blend(p.get(a, a), p.get(b, b), towards_b)

    return {
        # The screen the chrome draws on, and the text on it.
        "pane": p["base00"],
        "surface": p["base00"],
        "surface-raised": p["base01"],
        "text": p["base05"],
        "text-bright": p["base07"],
        "text-dark": _other_of(p["base00"]),
        "text-muted": p["base03"],
        "soft": p["base06"],
        "border": p["base04"],
        # A focused pane's bar. The green of the default theme is the
        # signal too, and the spec's green is base0B.
        "focus": p["base0B"],
        "focus-strong": blend("base0B", "base05", 0.25),
        "focus-border": p["base0B"],
        "alarm": p["base08"],
        # The status bar, on the scheme's green.
        "signal": p["base0B"],
        "signal-bright": blend("base0B", "base05", 0.25),
        "signal-text": _readable(p["base0B"]),
        "command": p["base01"],
        "suggestion": p["base01"],
        "suggestion-text": p["base04"],
        "notice": blend("base0B", "base05", 0.35),
        "notice-text": _readable(blend("base0B", "base05", 0.35)),
        # Finer than any letter: a column that only just separates.
        "cut": blend("base00", "base05", 0.08),
        "warn": p["base0A"],
        "warn-bright": p["base09"],
        # A pane that has ended, and the confirmation that asks.
        "danger": p["base08"],
        "danger-strong": blend("base08", "#000000", 0.25),
        "danger-deep": blend("base08", "#000000", 0.6),
        # The search toolbar, and the matches on the selection
        # background, one step up for the match a person is on.
        "search": p["base0B"],
        "search-prompt-text": _readable(p["base0B"]),
        "search-match": p["base02"],
        "search-match-current": p["base03"],
        "search-match-current-text": _readable(p["base03"]),
        # The hue of the pop-ups: the scheme's blue.
        "accent": p["base0D"],
        # What a pane answers a program with: the sixteen, in the
        # order the spec's own terminal template numbers them.
        **{
            "color-%i" % index: p[letter]
            for index, letter in enumerate(
                (
                    "base00", "base08", "base0B", "base0A",
                    "base0D", "base0E", "base0C", "base05",
                    "base03", "base08", "base0B", "base0A",
                    "base0D", "base0E", "base0C", "base07",
                )
            )
        },
    }


def base16_theme(name: str):
    """
    The scheme of one base16 scheme, as the chrome's style.

    The same roles the pane answers from, put through the same
    `derive` every theme goes through.
    """
    return create_theme(derive(base16_roles(name)))
