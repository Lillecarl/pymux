"""
The link rules of a directory: text that `show-urls` labels as a link
though it is not one, such as an issue number.

They live in a `pymux.toml`, the nearest one up from the pane's
directory. The search stops at a directory that holds `.git` or `.jj`,
so a repository's rules never reach past it, and at `/`.
Lillecarl/pymux#776.

    [[links]]
    match = '(?<![\\w/])#(\\d+)\\b'
    url = "https://github.com/Lillecarl/pymux/issues/{1}"

`url` is a `str.format` template: `{0}` is the whole match, `{1}` the
first group, and a named group goes by its name.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import NamedTuple

from pyte.links import LinkRule

__all__ = ["CONFIG_NAME", "Rules", "find_config", "rules_for"]

CONFIG_NAME = "pymux.toml"

#: What marks the top of a repository, where the search stops.
_REPOSITORY_MARKS = (".git", ".jj")


class Rules(NamedTuple):
    rules: list[LinkRule]
    #: What was wrong with the file, one message a rule. A broken rule
    #: is left out and the others still apply.
    errors: list[str]


_NONE = Rules([], [])

#: Each file read, by path, with the mtime it had.
_read: dict[Path, tuple[int, Rules]] = {}


def find_config(directory: str) -> Path | None:
    "The `pymux.toml` that holds the rules of `directory`, or None."
    if not directory or not Path(directory).is_absolute():
        return None
    here = Path(directory)
    for each in (here, *here.parents):
        candidate = each / CONFIG_NAME
        if candidate.is_file():
            return candidate
        if any((each / mark).exists() for mark in _REPOSITORY_MARKS):
            return None
    return None


def rules_for(directory: str) -> Rules:
    "The link rules of `directory`, read again only when the file changed."
    path = find_config(directory)
    if path is None:
        return _NONE
    try:
        mtime = path.stat().st_mtime_ns
    except OSError:
        return _NONE
    cached = _read.get(path)
    if cached is not None and cached[0] == mtime:
        return cached[1]
    rules = _parse(path)
    _read[path] = (mtime, rules)
    return rules


def _parse(path: Path) -> Rules:
    try:
        data = tomllib.loads(path.read_text())
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        return Rules([], ["%s: %s" % (path, error)])
    entries = data.get("links", [])
    if not isinstance(entries, list):
        return Rules([], ["%s: links is not a list of tables" % (path,)])
    rules: list[LinkRule] = []
    errors: list[str] = []
    for number, entry in enumerate(entries, 1):
        try:
            rules.append(_rule_of(entry))
        except (TypeError, KeyError, ValueError, IndexError, re.error) as error:
            errors.append("%s: link %d: %s" % (path, number, error))
    return Rules(rules, errors)


def _rule_of(entry: object) -> LinkRule:
    if not isinstance(entry, dict) or not isinstance(entry.get("match"), str) or not isinstance(entry.get("url"), str):
        raise TypeError("a link needs a match and a url, both strings")
    pattern = re.compile(entry["match"])
    # A template that names a group the pattern lacks fails here, on
    # read, and not on the first match.
    entry["url"].format("", *[""] * pattern.groups, **dict.fromkeys(pattern.groupindex, ""))
    return LinkRule(pattern, entry["url"])
