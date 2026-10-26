"""
Key/value tags a caller puts on a session, a window, a pane or a job.

A tag says what the caller wants to say about the thing, so a script
or an agent finds its own things by tag, and not by an id it has to
remember. They are not a pane's user vars: the program in the pane
sets those. Lillecarl/pymux#762.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from pymux.commands import CommandException

__all__ = ["Tags", "carries", "parse_tag", "parse_tags", "spelled"]

#: A key to its value, or to None for a bare key.
type Tags = dict[str, str | None]


def parse_tag(word: str) -> tuple[str, str | None]:
    """
    One `--tag`: a bare key, or a key with a value.

    A bare key carries no value -- NULL in the database, not an empty
    string -- so `key` and `key=` stay two different tags. A repeated
    key keeps its last value.
    """
    key, separator, value = word.partition("=")
    if not key:
        raise CommandException("a tag names a key: %r" % word)
    return key, value if separator else None


def parse_tags(words: Iterable[str]) -> Tags:
    return dict(parse_tag(word) for word in words)


def carries(tags: Mapping[str, str | None], wanted: Iterable[tuple[str, str | None]]) -> bool:
    "Whether `tags` holds every wanted tag: a bare key takes any value."
    for key, value in wanted:
        if key not in tags:
            return False
        if value is not None and tags[key] != value:
            return False
    return True


def spelled(tags: Mapping[str, str | None]) -> str:
    "The tags as `--tag` takes them, comma separated: `key=value,bare`."
    return ",".join(key if value is None else "%s=%s" % (key, value) for key, value in tags.items())
