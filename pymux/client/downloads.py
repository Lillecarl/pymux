"""
The files a server sends this client to save: a pane's download, when
`file-transfer` says the pymux client saves it. Lillecarl/pymux#743.

The name comes from a program in a pane, so it is only a name: the
directory is this machine's, and a name never adds a path to it. A file
that is already there stays as it was, and the new one takes a suffix.
"""

from __future__ import annotations

import base64
import binascii
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Final

from libpymux.protocol import Field

from ..downloads import DEFAULT_NAME, MAX_DOWNLOAD_BYTES

__all__ = [
    "DOWNLOAD_DIR_VARIABLE",
    "Downloads",
    "Whole",
    "download_directory",
    "safe_name",
    "save",
    "save_whole",
]

#: The variable that names the person's downloads directory.
DOWNLOAD_DIR_VARIABLE: Final = "XDG_DOWNLOAD_DIR"

#: The directory under the home directory that holds downloads when the
#: variable names none.
DOWNLOADS: Final = "Downloads"

#: The longest name most file systems take, in bytes.
MAX_NAME_BYTES: Final = 255

#: How many suffixed names a save tries before it gives up.
MAX_SUFFIX: Final = 1000

#: The names that are a directory and not a file.
_NOT_FILE_NAMES: Final = frozenset(["", ".", ".."])


def safe_name(name: str) -> str:
    """
    A name with no path in it: the last component, with no control
    character, and short enough for a file system. A name that leaves
    nothing is `DEFAULT_NAME`.
    """
    last = PurePosixPath(name.replace("\\", "/")).name
    printable = "".join(char for char in last if char.isprintable()).strip()
    if printable in _NOT_FILE_NAMES:
        return DEFAULT_NAME
    while len(printable.encode("utf-8")) > MAX_NAME_BYTES:
        printable = printable[:-1]
    return printable


def download_directory(environment: Mapping[str, str], home: Path, current: Path) -> Path:
    """
    Where a download goes: `XDG_DOWNLOAD_DIR`, then `~/Downloads`, else
    the current directory. A directory that is not there is skipped.
    """
    named = environment.get(DOWNLOAD_DIR_VARIABLE, "")
    if named:
        # user-dirs.dirs writes it as "$HOME/Downloads".
        path = Path(named.replace("$HOME", str(home))).expanduser()
        if path.is_dir():
            return path
    downloads = home / DOWNLOADS
    if downloads.is_dir():
        return downloads
    return current


def _candidates(name: str) -> list[str]:
    "The name, then `stem (1).suffix` and on, in the order a save tries them."
    path = PurePosixPath(name)
    stem, suffix = path.stem, path.suffix
    return [name] + ["%s (%d)%s" % (stem, number, suffix) for number in range(1, MAX_SUFFIX)]


def save(directory: Path, name: str, data: bytes) -> Path:
    """
    Write a new file in `directory`, and return its path. It never
    replaces a file: `O_EXCL` makes the check and the create one step.
    """
    for candidate in _candidates(safe_name(name)):
        path = directory / candidate
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
        except FileExistsError:
            continue
        with os.fdopen(descriptor, "wb") as written:
            written.write(data)
        return path
    raise FileExistsError("Every name for %s in %s is taken." % (name, directory))


@dataclass(slots=True)
class _Incoming:
    name: str
    parts: list[bytes] = field(default_factory=list)
    length: int = 0


class Downloads:
    """
    The files arriving for this client, until each is whole.

    The server bounds a download to `MAX_DOWNLOAD_BYTES`, and this
    bounds it again: this side holds every part in memory too.
    """

    def __init__(self) -> None:
        self._arriving: dict[str, _Incoming] = {}
        #: Transfers given up on, whose later parts are dropped.
        self._refused: set[str] = set()

    def clear(self) -> None:
        "Forget the parts of every file. A new connection sends none of the rest."
        self._arriving.clear()
        self._refused.clear()

    def _refuse(self, transfer: str, last: bool, message: str) -> str:
        self._arriving.pop(transfer, None)
        if not last:
            self._refused.add(transfer)
        return message

    def receive(self, packet: Mapping[str, object]) -> Whole | str | None:
        """
        Take one `file` packet. The last part answers with the file,
        whole; a refusal answers with the message for the person.
        """
        transfer = str(packet.get(Field.TRANSFER, ""))
        last = bool(packet.get(Field.LAST))
        if transfer in self._refused:
            if last:
                self._refused.discard(transfer)
            return None
        name = str(packet.get(Field.NAME) or DEFAULT_NAME)
        incoming = self._arriving.setdefault(transfer, _Incoming(name))
        try:
            data = base64.b64decode(str(packet.get(Field.DATA, "")), validate=True)
        except binascii.Error:
            return self._refuse(transfer, last, "Could not save %s: it did not arrive as base64." % (name,))
        incoming.length += len(data)
        if incoming.length > MAX_DOWNLOAD_BYTES:
            return self._refuse(
                transfer,
                last,
                "Could not save %s: it is larger than %d MiB." % (name, MAX_DOWNLOAD_BYTES // (1024 * 1024)),
            )
        incoming.parts.append(data)
        if not last:
            return None

        del self._arriving[transfer]
        return Whole(name, b"".join(incoming.parts))


@dataclass(frozen=True, slots=True)
class Whole:
    "A file whose every part arrived, for `save_whole` to write."

    name: str
    data: bytes


def save_whole(whole: Whole) -> str:
    """
    Write a file that arrived whole, and say where it went or why not.

    **Never on a loop that draws.** 64 MiB stopped drawing and typing
    for as long as the write took. Lillecarl/pymux#747.
    """
    directory = download_directory(os.environ, Path.home(), Path.cwd())
    try:
        path = save(directory, whole.name, whole.data)
    except OSError as error:
        return "Could not save %s: %s" % (whole.name, error)
    return "Saved %s." % (path,)
