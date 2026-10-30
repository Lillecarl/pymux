"""
Files that a program in a pane sends to the person, in iTerm2's protocol.

iTerm2 names two forms under "OSC 1337". `File=args:b64` carries the
whole file in one sequence. `MultipartFile=args`, then `FilePart=b64`
any number of times, then `FileEnd`, carries it in parts, so that no one
sequence grows with the file. The argument `inline=1` makes the file an
image to draw, which is not a download: this module reads it the same
way and marks it, and the pane draws it (Lillecarl/pymux#789).

`DownloadReader` reads the sequences of one pane and gives back each
finished file. `Pymux.send_download` decides who saves it, and
`pymux send` writes the multipart form with `multipart_sequences`.
Lillecarl/pymux#743.

The command line of `pymux send-file` imports this module, so it
imports only the standard library, the protocol and the enums.
"""

from __future__ import annotations

import base64
import binascii
import os
import secrets
from collections.abc import Iterator
from dataclasses import dataclass, field
from enum import StrEnum
from typing import BinaryIO, Final, TextIO

from libpymux.protocol import Field, Message, Packet

from .enums import FileTransfer

__all__ = [
    "DEFAULT_NAME",
    "FILE_KEYS",
    "MAX_DOWNLOAD_BYTES",
    "PACKET_BYTES",
    "PART_BYTES",
    "DownloadReader",
    "DownloadRefused",
    "FileKey",
    "FinishedDownload",
    "file_packets",
    "file_sequence",
    "multipart_sequences",
    "route_of",
    "send_files",
    "sequences_of",
]

#: The largest file one download may carry. The server holds all of it
#: before it routes it, and the pymux client holds all of it before it
#: writes it, so this is a bound on the memory of both.
MAX_DOWNLOAD_BYTES: Final = 64 * 1024 * 1024

#: The bytes of the file in one `FilePart`. A multiple of three, so each
#: part is whole base64 with no padding: 64 KiB of text on the wire.
PART_BYTES: Final = 48 * 1024

#: The bytes of the file in one `file` packet to the pymux client. A
#: multiple of three, for the reason `PART_BYTES` gives. A key the
#: person types waits behind at most one packet, so it stays small.
PACKET_BYTES: Final = 192 * 1024

#: The name of a file whose sender gave none.
DEFAULT_NAME: Final = "download"

#: The value of `inline` that makes a file an image to draw.
_INLINE: Final = "1"

#: "OSC 1337" and its terminator, around every sequence this writes.
_OSC: Final = "\x1b]1337;"
_ST: Final = "\x1b\\"


class FileKey(StrEnum):
    'The subcommands of "OSC 1337" that carry a file.'

    FILE = "File"
    MULTIPART_FILE = "MultipartFile"
    FILE_PART = "FilePart"
    FILE_END = "FileEnd"


FILE_KEYS: Final = frozenset(FileKey)


class FileArgument(StrEnum):
    "The arguments of `File=` and `MultipartFile=` that a download reads."

    #: The name of the file, in base64.
    NAME = "name"
    #: The size of the file in bytes, as the sender says it.
    SIZE = "size"
    INLINE = "inline"


class DownloadRefused(Exception):
    "A download that is not saved. The message is for the person."


@dataclass(frozen=True, slots=True)
class FinishedDownload:
    """
    One whole file from a pane. `multipart` says which form it came in,
    so that a terminal it passes through gets the same form.
    """

    name: str
    data: bytes
    multipart: bool
    #: `inline=1`: an image to draw in the pane, not a file to save.
    #: Lillecarl/pymux#789.
    inline: bool = False
    #: Every argument the sender gave, which an image's size is read from.
    arguments: dict[str, str] = field(default_factory=dict, compare=False)


def _arguments_of(text: str) -> dict[str, str]:
    "The `key=value` pairs of `File=` and `MultipartFile=`, split on `;`."
    arguments: dict[str, str] = {}
    for part in text.split(";"):
        key, _eq, value = part.partition("=")
        if key:
            arguments[key] = value
    return arguments


def _name_of(arguments: dict[str, str]) -> str:
    """
    The name the sender gave, decoded. A name that is not base64 is no
    name. The pymux client sanitises it before it touches a path.
    """
    encoded = arguments.get(FileArgument.NAME, "")
    try:
        name = base64.b64decode(encoded, validate=True).decode("utf-8", "replace")
    except binascii.Error:
        return DEFAULT_NAME
    return name or DEFAULT_NAME


def _too_big(name: str) -> DownloadRefused:
    return DownloadRefused(
        "%s is larger than %d MiB, the most a download may carry." % (name, MAX_DOWNLOAD_BYTES // (1024 * 1024))
    )


def _check_declared_size(name: str, arguments: dict[str, str]) -> None:
    "Refuse a file whose sender says it is too large, before any of it arrives."
    size = arguments.get(FileArgument.SIZE, "")
    if size.isdigit() and int(size) > MAX_DOWNLOAD_BYTES:
        raise _too_big(name)


def _decoded_length(base64_length: int) -> int:
    "The most bytes this much base64 can decode to."
    return base64_length * 3 // 4


def _decode(name: str, data: str) -> bytes:
    try:
        return base64.b64decode(data, validate=True)
    except binascii.Error:
        raise DownloadRefused("%s did not arrive as base64." % (name,)) from None


@dataclass(slots=True)
class _Open:
    "A multipart download between its `MultipartFile` and its `FileEnd`."

    name: str
    parts: list[str]
    arguments: dict[str, str] = field(default_factory=dict)
    length: int = 0
    #: Read the rest of its parts and drop them: a file that grew too large.
    skipping: bool = False


class DownloadReader:
    """
    The downloads of one pane: each sequence in, each whole file out.

    **A pane holds one of these.** The parts of a multipart file arrive
    in separate sequences, with output of the program between them, and
    only the pane they come from can join them.
    """

    __slots__ = ("_open",)

    def __init__(self) -> None:
        self._open: _Open | None = None

    def read(self, key: str, rest: str) -> FinishedDownload | None:
        """
        Read one file subcommand, the part after `key=`. A whole file
        comes back; anything else is `None`.

        Raises `DownloadRefused` for a file that is too large or not
        base64. A refused multipart file reads its remaining parts and
        drops them.
        """
        if key == FileKey.FILE:
            return self._read_file(rest)
        if key == FileKey.MULTIPART_FILE:
            self._begin(rest)
            return None
        if key == FileKey.FILE_PART:
            self._add_part(rest)
            return None
        if key == FileKey.FILE_END:
            return self._end()
        return None

    def _read_file(self, rest: str) -> FinishedDownload | None:
        text, _colon, data = rest.partition(":")
        arguments = _arguments_of(text)
        name = _name_of(arguments)
        _check_declared_size(name, arguments)
        if _decoded_length(len(data)) > MAX_DOWNLOAD_BYTES:
            raise _too_big(name)
        inline = arguments.get(FileArgument.INLINE) == _INLINE
        return FinishedDownload(name, _decode(name, data), multipart=False, inline=inline, arguments=arguments)

    def _begin(self, rest: str) -> None:
        arguments = _arguments_of(rest)
        name = _name_of(arguments)
        # A new file ends one that never sent its `FileEnd`.
        self._open = _Open(name, [], arguments=arguments)
        try:
            _check_declared_size(name, arguments)
        except DownloadRefused:
            self._open.skipping = True
            raise

    def _add_part(self, data: str) -> None:
        held = self._open
        if held is None or held.skipping:
            return
        held.length += len(data)
        if _decoded_length(held.length) > MAX_DOWNLOAD_BYTES:
            held.skipping = True
            held.parts.clear()
            raise _too_big(held.name)
        held.parts.append(data)

    def _end(self) -> FinishedDownload | None:
        held, self._open = self._open, None
        if held is None or held.skipping:
            return None
        # A part need not be whole base64 on its own: only the joined
        # parts decode.
        return FinishedDownload(
            held.name,
            _decode(held.name, "".join(held.parts)),
            multipart=True,
            inline=held.arguments.get(FileArgument.INLINE) == _INLINE,
            arguments=held.arguments,
        )


def _arguments(name: str, size: int) -> str:
    encoded = base64.b64encode(name.encode("utf-8")).decode("ascii")
    return "%s=%s;%s=%d;%s=0" % (FileArgument.NAME, encoded, FileArgument.SIZE, size, FileArgument.INLINE)


def file_sequence(name: str, data: bytes) -> str:
    "The whole file in one `File=` sequence."
    payload = base64.b64encode(data).decode("ascii")
    return "%s%s=%s:%s%s" % (_OSC, FileKey.FILE, _arguments(name, len(data)), payload, _ST)


def multipart_sequences(name: str, data: bytes, part_bytes: int = PART_BYTES) -> Iterator[str]:
    "The file as `MultipartFile`, its `FilePart`s and `FileEnd`."
    yield "%s%s=%s%s" % (_OSC, FileKey.MULTIPART_FILE, _arguments(name, len(data)), _ST)
    for start in range(0, len(data), part_bytes):
        part = base64.b64encode(data[start : start + part_bytes]).decode("ascii")
        yield "%s%s=%s%s" % (_OSC, FileKey.FILE_PART, part, _ST)
    yield "%s%s%s" % (_OSC, FileKey.FILE_END, _ST)


def sequences_of(download: FinishedDownload) -> Iterator[str]:
    "The download again, in the form the pane sent it."
    if download.multipart:
        yield from multipart_sequences(download.name, download.data)
    else:
        yield file_sequence(download.name, download.data)


def route_of(chosen: FileTransfer, over_ssh: bool, terminal_saves: bool) -> FileTransfer:
    """
    Who saves a download for a client: `PYMUX` or `PASSTHROUGH`.

    `AUTO` asks whether the client came over SSH. Such a client runs on
    the person's machine, and the server's machine is somewhere else, so
    the pymux client saves the file. A client on the server's machine
    draws on a terminal that is either there too or at the end of a
    plain shell over SSH, and only that terminal reaches the person, if
    it saves downloads. One that does not leaves it to the pymux client,
    which writes to this machine's downloads directory.
    """
    if chosen is not FileTransfer.AUTO:
        return chosen
    if over_ssh or not terminal_saves:
        return FileTransfer.PYMUX
    return FileTransfer.PASSTHROUGH


def file_packets(download: FinishedDownload) -> Iterator[Message]:
    "The download as `file` packets to the pymux client, `last` on the final one."
    transfer = secrets.token_hex(8)
    starts = range(0, max(len(download.data), 1), PACKET_BYTES)
    for start in starts:
        yield {
            Field.CMD: Packet.FILE,
            Field.TRANSFER: transfer,
            Field.NAME: download.name,
            Field.DATA: base64.b64encode(download.data[start : start + PACKET_BYTES]).decode("ascii"),
            Field.LAST: start == starts[-1],
        }


def send_files(paths: list[str], out: BinaryIO, err: TextIO) -> int:
    """
    `pymux send-file`: write each file to `out` as an iTerm2 multipart
    download. Returns the exit status.

    **It writes to its own terminal**, which in a pane is the pane. So
    the file takes the road every program's download takes, and a
    terminal that is not pymux but speaks iTerm2's protocol saves it
    too. The multipart form keeps each sequence small.
    """
    if not paths:
        err.write("usage: pymux send-file FILE...\n")
        return 2
    status = 0
    for path in paths:
        try:
            size = os.path.getsize(path)
            if size > MAX_DOWNLOAD_BYTES:
                raise _too_big(path)
            with open(path, "rb") as source:
                data = source.read()
        except (OSError, DownloadRefused) as error:
            err.write("pymux send-file: %s\n" % (error,))
            status = 1
            continue
        out.writelines(sequence.encode("ascii") for sequence in multipart_sequences(os.path.basename(path), data))
        out.flush()
    return status
