"""
Files a pane asks for from this machine: the client's half of
`pymux receive`. Lillecarl/pymux#743, phase 2.

**This side is the authority.** The server runs on the far machine with
whatever runs in its panes, so this client trusts nothing it sends but
the request itself. It asks the person on its own terminal before it
reads a byte; it holds the grant that "a" gives; and it reads
`upload-paths` and `upload-grant` from its own configuration file, never
from the server. It does not announce those two to the server at all,
so the server cannot read the allow-list either.

The decisions are pure functions here. `TerminalClient` does the I/O.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Iterable, Iterator
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final

from libpymux.protocol import Field, Message, Packet
from prompt_toolkit.utils import get_cwidth
from pyte.control import ESC

from ..downloads import MAX_DOWNLOAD_BYTES, FinishedDownload, file_packets

__all__ = [
    "DEFAULT_GRANT_MINUTES",
    "LOCAL_CLIENT_OPTIONS",
    "Answer",
    "LocalOption",
    "UploadSettings",
    "Wanted",
    "allowed",
    "answer_of",
    "done_packet",
    "expand",
    "inspect",
    "listed_packet",
    "listing",
    "outcome_packets",
    "question",
    "read",
    "size_text",
    "upload_settings",
]

#: How long "a" lets one server read without asking, when the client's
#: configuration names no length.
DEFAULT_GRANT_MINUTES: Final = 10

SECONDS_PER_MINUTE: Final = 60

#: The most names one `list-files` answers with.
MAX_LISTED: Final = 1000

#: What separates the directories of `upload-paths`, as in `PATH`.
PATH_SEPARATOR: Final = ":"

ELLIPSIS: Final = "…"
ESCAPE: Final = ESC
INTERRUPT: Final = "\x03"

#: The units `size_text` counts in, each 1000 of the one before.
_UNITS: Final = ("B", "kB", "MB", "GB")
_STEP: Final = 1000


class LocalOption(StrEnum):
    """
    The client options only the client's own configuration file sets.
    `set-client-option` on the server refuses them.
    """

    #: Directories whose files pass without a question, `:` between them.
    UPLOAD_PATHS = "upload-paths"
    #: How many minutes "a" lets one server read without asking.
    UPLOAD_GRANT = "upload-grant"


LOCAL_CLIENT_OPTIONS: Final = frozenset(LocalOption)


class Answer(StrEnum):
    "What the person can answer the question with."

    ONCE = "y"
    GRANT = "a"
    NO = "n"


class NotSent(Exception):
    "A path that is not sent. The message says why, to the person on the server."


@dataclass(frozen=True, slots=True)
class UploadSettings:
    "What the client's own configuration file says about uploads."

    roots: tuple[Path, ...] = ()
    grant_seconds: float = DEFAULT_GRANT_MINUTES * SECONDS_PER_MINUTE


@dataclass(frozen=True, slots=True)
class Wanted:
    """
    One path a request names, as this machine sees it: resolved, with
    its size and the file it is. `refused` says why it is not sent.
    """

    asked: str
    path: Path
    size: int = 0
    identity: tuple[int, int] = (0, 0)
    refused: str | None = None


def expand(asked: str, home: Path) -> Path:
    "A path a pane named, on this machine: `~` and a path that is not absolute start at `home`."
    if asked == "~":
        return home
    if asked.startswith("~/"):
        return home / asked[2:]
    path = Path(asked)
    return path if path.is_absolute() else home / path


def upload_settings(announced: Iterable[tuple[str, str]], home: Path) -> UploadSettings:
    """
    The upload settings among the client options of a configuration
    file. The last line for an option wins, as `set-client-option`
    does; a length that is not a number of minutes is left out.
    """
    roots: tuple[Path, ...] = ()
    grant = DEFAULT_GRANT_MINUTES * SECONDS_PER_MINUTE
    for name, value in announced:
        if name == LocalOption.UPLOAD_PATHS:
            roots = tuple(expand(part, home) for part in value.split(PATH_SEPARATOR) if part)
        elif name == LocalOption.UPLOAD_GRANT:
            with suppress(ValueError):
                minutes = float(value)
                if minutes >= 0:
                    grant = minutes * SECONDS_PER_MINUTE
    return UploadSettings(roots, grant)


def _too_big(asked: str) -> str:
    return "%s is larger than %d MiB, the most one file may carry" % (asked, MAX_DOWNLOAD_BYTES // (1024 * 1024))


def inspect(asked: list[str], home: Path) -> list[Wanted]:
    """
    Each path resolved, with every symlink followed and no `..` left,
    and its size. It reads no content. **It blocks**, so it runs off a
    loop that draws.
    """
    found = []
    for one in asked:
        try:
            path = expand(one, home).resolve(strict=True)
            status = path.stat()
        except OSError as error:
            found.append(Wanted(one, Path(one), refused="%s: %s" % (one, error.strerror or error)))
            continue
        if not stat.S_ISREG(status.st_mode):
            found.append(Wanted(one, path, refused="%s is not a file" % (one,)))
        elif status.st_size > MAX_DOWNLOAD_BYTES:
            found.append(Wanted(one, path, refused=_too_big(one)))
        else:
            found.append(Wanted(one, path, status.st_size, (status.st_dev, status.st_ino)))
    return found


def _under(path: Path, roots: Iterable[Path]) -> bool:
    for root in roots:
        try:
            real = root.resolve(strict=True)
        except OSError:
            continue
        if path.is_relative_to(real):
            return True
    return False


def _on_the_way(path: Path, roots: Iterable[Path]) -> bool:
    "Whether `path` lies under a root, or a root lies under it."
    for root in roots:
        try:
            real = root.resolve(strict=True)
        except OSError:
            continue
        if path.is_relative_to(real) or real.is_relative_to(path):
            return True
    return False


def listing(asked: str, home: Path, roots: tuple[Path, ...], granted: bool) -> list[str]:
    """
    The names in a directory a pane asked about, for completing
    `pymux receive`. A directory's name ends in `/`. **It blocks.**

    Inside a grant, every name. Without one, only the names that lead
    into `upload-paths` or lie under it, so a completion shows nothing
    the person has not let pass. Names are only names: no content is
    read, and at most `MAX_LISTED` come back.
    """
    try:
        directory = expand(asked or "~", home).resolve(strict=True)
        children = sorted(directory.iterdir())
    except OSError:
        return []
    names = []
    for child in children[:MAX_LISTED]:
        try:
            real = child.resolve(strict=True)
        except OSError:
            continue
        if not granted and not _on_the_way(real, roots):
            continue
        names.append(child.name + "/" if real.is_dir() else child.name)
    return names


def listed_packet(request: str, names: list[str]) -> Message:
    "The answer to `list-files`."
    return {Field.CMD: Packet.FILES_LISTED, Field.REQUEST: request, Field.PATHS: names}


def allowed(wanted: Iterable[Wanted], roots: tuple[Path, ...]) -> bool:
    """
    Whether every path that would be read lies under `upload-paths`.
    Both sides are resolved, so a symlink or `..` out of a directory
    is outside it.
    """
    return all(one.refused is not None or _under(one.path, roots) for one in wanted)


def size_text(size: int) -> str:
    "A size for a person: 2.1 MB."
    value = float(size)
    for unit in _UNITS:
        if value < _STEP or unit == _UNITS[-1]:
            return "%d %s" % (value, unit) if unit == _UNITS[0] else "%.1f %s" % (value, unit)
        value /= _STEP
    raise AssertionError("unreachable")


def _shown(text: str) -> str:
    "Text from the server, with nothing in it the terminal would act on."
    return "".join(char if char.isprintable() else "?" for char in text)


def _home_as_tilde(path: Path, home: Path) -> str:
    if path.is_relative_to(home):
        return str(Path("~") / path.relative_to(home))
    return str(path)


def question(pane: str, host: str, wanted: list[Wanted], grant_seconds: float, home: Path, columns: int) -> str:
    """
    The question, cut to fit `columns` cells. It names the file each
    path resolved to, which is the one that would be read. The list of
    files is what gets cut, so the answers always show.
    """
    listed = _shown(
        ", ".join(
            "%s (%s)" % (_home_as_tilde(one.path, home), size_text(one.size)) for one in wanted if one.refused is None
        )
    )
    asking = _shown("pane %s on %s asks for " % (pane, host))
    answers = ": y once / a for %d min / n" % (grant_seconds // SECONDS_PER_MINUTE,)
    room = columns - 1 - get_cwidth(asking) - get_cwidth(answers)
    if get_cwidth(listed) > room:
        while listed and get_cwidth(listed) > room - get_cwidth(ELLIPSIS):
            listed = listed[:-1]
        listed += ELLIPSIS
    text = asking + listed + answers
    while text and get_cwidth(text) >= columns:
        text = text[:-1]
    return text


def answer_of(keys: str) -> Answer | None:
    """
    The answer one read of the keyboard gives, or None for no answer.
    Only a single key answers, so a paste or fast typing never does.
    Escape and Ctrl-C say no.
    """
    if keys in (ESCAPE, INTERRUPT):
        return Answer.NO
    if len(keys) != 1:
        return None
    with suppress(ValueError):
        return Answer(keys.lower())
    return None


def read(wanted: Wanted) -> bytes:
    """
    The content of a path that was inspected. **It blocks.**

    The file it opens has to be the one the question named: the last
    component is not followed, and the device and inode must be the
    ones `inspect` saw, so a swap in between is refused.
    """
    try:
        descriptor = os.open(wanted.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as error:
        raise NotSent("%s: %s" % (wanted.asked, error.strerror or error)) from None
    with os.fdopen(descriptor, "rb") as opened:
        status = os.fstat(opened.fileno())
        if (status.st_dev, status.st_ino) != wanted.identity:
            raise NotSent("%s changed after the person was asked" % (wanted.asked,))
        data = opened.read(MAX_DOWNLOAD_BYTES + 1)
    if len(data) > MAX_DOWNLOAD_BYTES:
        raise NotSent(_too_big(wanted.asked))
    return data


def _refusal(request: str, asked: str, why: str) -> Message:
    return {Field.CMD: Packet.UPLOAD, Field.REQUEST: request, Field.PATH: asked, Field.ERROR: why}


def outcome_packets(request: str, wanted: Wanted) -> Iterator[Message]:
    """
    The `upload` packets of one path: its parts, or why it is not sent.
    **It blocks**: the read happens before the first packet.
    """
    if wanted.refused is not None:
        yield _refusal(request, wanted.asked, wanted.refused)
        return
    try:
        data = read(wanted)
    except NotSent as error:
        yield _refusal(request, wanted.asked, str(error))
        return
    for packet in file_packets(FinishedDownload(wanted.path.name, data, multipart=False)):
        yield {**packet, Field.CMD: Packet.UPLOAD, Field.REQUEST: request, Field.PATH: wanted.asked}


def done_packet(request: str, error: str | None = None) -> Message:
    "The end of the answer to `request`, with `error` when none of it is sent."
    packet: Message = {Field.CMD: Packet.UPLOAD_DONE, Field.REQUEST: request}
    if error is not None:
        packet[Field.ERROR] = error
    return packet
