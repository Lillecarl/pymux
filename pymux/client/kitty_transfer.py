"""
The terminal's half of kitty's file transfer, OSC 5113, on the machine
the person sits at. https://sw.kovidgoyal.net/kitty/file-transfer-protocol/.
Lillecarl/pyterm#914.

A program in a pane speaks to its terminal, and only kitty answers. For
any other terminal the server passes each command here, and this client
answers as kitty would. `Transfers` follows kitty's `FileTransmission`
(`kitty/file_transmission.py`), with these differences:

- The person is asked on this client's terminal, as `pymux receive`
  asks. A program that reads files names them before it needs an
  answer, so the question names them too, and a grant or `upload-paths`
  lets them pass.
- A program reads only the files that the listing named.
- A file goes in parts of `READ_BYTES`, each once the server says the
  pane's program took the last (`pymux.client.kitty_terminal`), where
  kitty waits for its child's queue to drain.
- Deltas follow kitty's rsync (`pymux.client.kitty_rsync`). A file
  patched in place keeps the mode the old one had.
- A bypass password reaches here as the protocol's `sha256:` form, which
  the server made of what the pane encrypted to its key
  (`pymux.core.kitty_bypass`). It is checked against
  `file-transfer-bypass` from this client's own file. As in kitty, one
  that does not match refuses the session without a question.

Every call blocks on the file system, so the client makes them off the
loop that draws, one at a time and in order.
"""

from __future__ import annotations

import errno
import os
import stat
import tempfile
import time
import zlib
from collections import defaultdict, deque
from collections.abc import Callable, Iterator
from contextlib import suppress
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import BinaryIO, Final

from pyte.sequences.file_transfer import (
    CHUNK_BYTES,
    Action,
    Compression,
    FileType,
    Malformed,
    TransferCommand,
    TransmissionType,
)

from ..core.kitty_bypass import bypass_matches
from ..graphics import kernels
from .kitty_rsync import Differ, Patcher, RsyncError

__all__ = [
    "REFUSED",
    "Question",
    "Status",
    "Transfers",
    "Unasked",
    "absolute",
]

#: kitty's `EXPIRE_TIME`: a session idle this long is dropped.
EXPIRE_SECONDS: Final = 600.0

#: kitty's `MAX_ACTIVE_RECEIVES` and `MAX_ACTIVE_SENDS`, each.
MAX_SESSIONS: Final = 10

#: kitty's bounds on what one reading session names and queues.
MAX_SPECS: Final = 8192
MAX_QUEUED: Final = 32768

#: The most of a file one `Transfers.pump` reads: kitty's `next_chunk`.
READ_BYTES: Final = 1024 * 1024

#: The `quiet` from which acknowledgements, then errors too, stay unsaid.
_NO_ACKNOWLEDGEMENTS: Final = 1
_NO_ERRORS: Final = 2

#: What the kitten shows when the person says no: kitty's own words.
REFUSED: Final = "User refused the transfer"

#: The mode of a file whose sender gave none.
_NEW_FILE_MODE: Final = 0o666


class Status(StrEnum):
    "The codes of a `status` that are not an errno name."

    OK = "OK"
    STARTED = "STARTED"
    CANCELED = "CANCELED"
    PROGRESS = "PROGRESS"
    EINVAL = "EINVAL"
    EPERM = "EPERM"
    EISDIR = "EISDIR"
    ENOENT = "ENOENT"


@dataclass(frozen=True, slots=True)
class Question:
    """
    Ask the person about a session of `pane`: one that writes files on
    this machine, or one that reads `paths` from it.
    """

    session: str
    pane: str
    reads: bool
    paths: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Unasked:
    "The session went before the person answered: take its question down."

    session: str


type Effect = TransferCommand | Question | Unasked


class _Failed(Exception):
    "A status that is an error, for the program."

    def __init__(self, code: str, message: str, file_id: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.file_id = file_id


def _code_of(error: OSError) -> str:
    return errno.errorcode.get(error.errno, "EFAIL") if error.errno is not None else "EFAIL"


def _status(
    session: str,
    code: str,
    message: str = "",
    file_id: str = "",
    name: str = "",
    size: int = -1,
    transmission_type: TransmissionType = TransmissionType.SIMPLE,
) -> TransferCommand:
    return TransferCommand(
        Action.STATUS,
        id=session,
        file_id=file_id,
        status=code + (":" + message if message else ""),
        name=name,
        size=size,
        transmission_type=transmission_type,
    )


def absolute(name: str, home: Path) -> str:
    "A path a program named, read as kitty reads it: `~/` and a relative path start at the home directory."
    if name.startswith("~/"):
        return os.path.join(home, name[2:].lstrip("/"))
    if os.path.isabs(name):
        return name
    return os.path.normpath(os.path.join(home, name))


# ----------------------------------------------------------------------
# Writing: the program sends files, and they land here.


class _Patch:
    """
    A file a program sends as a delta: kitty's `PatchFile`. The old file
    is read through no symlink, and the new one is written beside it and
    renamed over it.
    """

    def __init__(self, path: str, old: os.stat_result) -> None:
        self.path = path
        kernels.install_rsync()
        self.patcher = Patcher(old.st_size)
        self.old = open(os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW), "rb", closefd=True)
        self.new = tempfile.NamedTemporaryFile("wb", dir=os.path.dirname(path), delete=False)
        os.chmod(self.new.name, stat.S_IMODE(old.st_mode))

    def signature(self) -> bytes:
        signature = b"".join(self.patcher.signature(self.old))
        self.old.seek(0)
        return signature

    def write(self, data: bytes) -> int:
        self.patcher.apply(data, self.old, self.new.write)
        return self.new.tell()

    def finish(self) -> None:
        self.patcher.finish()
        self.new.close()
        self.old.close()
        os.replace(self.new.name, self.path)

    def abandon(self) -> None:
        self.old.close()
        if not self.new.closed:
            self.new.close()
            with suppress(OSError):
                os.unlink(self.new.name)


class _Destination:
    "One file a program sends, as kitty's `DestFile`."

    def __init__(self, command: TransferCommand, home: Path) -> None:
        self.name = absolute(command.name, home)
        try:
            self.existing: os.stat_result | None = os.stat(self.name, follow_symlinks=False)
        except OSError:
            self.existing = None
        # A hard link or a symlink there is replaced, never written through.
        self.needs_unlink = self.existing is not None and (
            self.existing.st_nlink > 1 or stat.S_ISLNK(self.existing.st_mode)
        )
        self.file_id = command.file_id
        self.file_type = command.file_type
        self.mtime = command.mtime
        self.permissions = stat.S_IMODE(command.permissions) if command.permissions != -1 else -1
        self.decompressor = zlib.decompressobj(wbits=0) if command.compression is Compression.ZLIB else None
        self.closed = self.file_type is FileType.DIRECTORY
        self.file: BinaryIO | None = None
        self.failed = False
        self.written = 0
        self.link_target = b""
        self.patch: _Patch | None = None

    @property
    def can_patch_in_place(self) -> bool:
        "kitty's test: a delta only for a regular file with no other name."
        return self.existing is not None and not self.needs_unlink and stat.S_ISREG(self.existing.st_mode)

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            if self.file is not None:
                self.file.close()
                self.file = None
            if self.patch is not None:
                self.patch.abandon()
                self.patch = None

    def _make_parents(self) -> str:
        parent = os.path.dirname(self.name)
        if parent:
            os.makedirs(parent, exist_ok=True)
        return parent

    def apply_metadata(self, is_symlink: bool = False) -> None:
        if self.permissions != -1:
            if is_symlink:
                with suppress(NotImplementedError):
                    os.chmod(self.name, self.permissions, follow_symlinks=False)
            else:
                os.chmod(self.name, self.permissions)
        if self.mtime != -1:
            if is_symlink:
                with suppress(NotImplementedError):
                    os.utime(self.name, ns=(self.mtime, self.mtime), follow_symlinks=False)
            else:
                os.utime(self.name, ns=(self.mtime, self.mtime))

    def _unlink_existing(self, force: bool = False) -> None:
        if force or self.needs_unlink:
            with suppress(FileNotFoundError):
                os.unlink(self.name)
            self.existing = None
            self.needs_unlink = False

    def write(self, files: dict[str, _Destination], data: bytes, is_last: bool) -> None:
        if self.file_type is FileType.DIRECTORY:
            raise _Failed(Status.EISDIR, "Cannot write data to a directory entry", self.file_id)
        if self.closed:
            raise _Failed(Status.EINVAL, "Cannot write to a closed file", self.file_id)
        if self.file_type in (FileType.SYMLINK, FileType.LINK):
            self.link_target += data
            self.written += len(data)
            if is_last:
                self._link(files)
            return
        decompressed = self.decompressor.decompress(data) if self.decompressor is not None else data
        if is_last and self.decompressor is not None:
            decompressed += self.decompressor.flush()
        if self.patch is not None:
            try:
                self.written = self.patch.write(decompressed)
                if is_last:
                    self.patch.finish()
                    self.patch = None
            except RsyncError as error:
                raise _Failed(Status.EINVAL, str(error), self.file_id) from error
            if is_last:
                self.close()
                self.apply_metadata()
            return
        if self.file is None:
            self._make_parents()
            self._unlink_existing()
            flags = os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_CLOEXEC | os.O_NOFOLLOW
            mode = self.permissions if self.permissions != -1 else _NEW_FILE_MODE
            self.file = open(os.open(self.name, flags, mode), "r+b", closefd=True)
        if decompressed or is_last:
            self.file.write(decompressed)
            self.written = self.file.tell()
        if is_last:
            self.close()
            self.apply_metadata()

    def _link(self, files: dict[str, _Destination]) -> None:
        target = self.link_target.decode("utf-8", "replace")
        parent = self._make_parents()
        self._unlink_existing(force=True)
        kind, _colon, rest = target.partition(":")
        if kind == "fid":
            target = self._target_named(files, rest)
            if self.file_type is FileType.SYMLINK:
                target = os.path.relpath(target, os.path.dirname(self.name))
        elif kind == "fid_abs":
            target = self._target_named(files, rest)
        elif kind == "path":
            target = rest
            if not os.path.isabs(target) and self.file_type is FileType.LINK:
                target = os.path.join(parent, target)
        else:
            raise _Failed(Status.EINVAL, "Unknown link target type", self.file_id)
        if self.file_type is FileType.SYMLINK:
            os.symlink(target, self.name)
        else:
            os.link(target, self.name)
        self.close()
        self.apply_metadata(is_symlink=True)

    def _target_named(self, files: dict[str, _Destination], file_id: str) -> str:
        target = files.get(file_id)
        if target is None:
            raise _Failed(Status.EINVAL, "The link target %s is not in this session" % (file_id,), self.file_id)
        return target.name


@dataclass(slots=True)
class _Session:
    id: str
    pane: str
    quiet: int
    used: float
    accepted: bool = False
    asked: bool = False
    #: What a bypass password said, or `None` for a session with none.
    bypass_ok: bool | None = None

    @property
    def acknowledges(self) -> bool:
        return self.quiet < _NO_ACKNOWLEDGEMENTS

    @property
    def says_errors(self) -> bool:
        return self.quiet < _NO_ERRORS


@dataclass(slots=True)
class _Writing(_Session):
    "A program's `send`: kitty's `ActiveReceive`."

    files: dict[str, _Destination] = field(default_factory=dict)

    def close(self) -> None:
        for destination in self.files.values():
            with suppress(OSError):
                destination.close()
        self.files = {}


# ----------------------------------------------------------------------
# Reading: the program asks for files here.


class _Source:
    "One file a program asked for the data of, as kitty's `SourceFile`."

    def __init__(self, command: TransferCommand) -> None:
        self.file_id = command.file_id
        self.path = command.name
        self.status = os.stat(self.path, follow_symlinks=False)
        if stat.S_ISDIR(self.status.st_mode):
            raise _Failed(Status.EINVAL, "Cannot send a directory", self.file_id)
        self.target = b""
        self.file: BinaryIO | None = None
        self.compressor: zlib._Compress | None = None
        self.transmitted = False
        #: Reads the program's signature of its copy, then writes the delta.
        self.differ: Differ | None = None
        if command.transmission_type is TransmissionType.RSYNC:
            kernels.install_rsync()
            self.differ = Differ()
        self.waiting_for_signature = self.differ is not None
        self.delta: Iterator[bytes] | None = None
        if stat.S_ISLNK(self.status.st_mode):
            self.target = os.readlink(self.path).encode("utf-8")
            return
        self.file = open(os.open(self.path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW), "rb", closefd=True)
        if command.compression is Compression.ZLIB:
            self.compressor = zlib.compressobj()

    @property
    def ready(self) -> bool:
        return not self.transmitted and not self.waiting_for_signature

    def add_signature(self, command: TransferCommand) -> None:
        if self.differ is None:
            raise _Failed(Status.EINVAL, "Signature data for file that is not using rsync: %s" % (self.file_id,))
        try:
            self.differ.add_signature(command.data)
            if command.action is Action.END_DATA:
                self.differ.finish_signature()
                self.waiting_for_signature = False
        except RsyncError as error:
            raise _Failed(Status.EINVAL, str(error), self.file_id) from error

    def close(self) -> None:
        if self.file is not None:
            self.file.close()
            self.file = None

    def next_chunk(self) -> bytes:
        if self.target:
            self.transmitted = True
            data = self.target
        elif self.file is None:
            self.transmitted = True
            data = b""
        elif self.differ is not None:
            if self.delta is None:
                self.delta = self.differ.delta(self.file)
            data = next(self.delta, b"")
            if not data:
                self.transmitted = True
        else:
            data = self.file.read(READ_BYTES)
            if not data or self.file.tell() >= self.status.st_size:
                self.transmitted = True
        if self.compressor is not None:
            data = self.compressor.compress(data)
            if self.transmitted:
                data += self.compressor.flush()
        if self.transmitted:
            self.close()
        return data


def _chunks(data: bytes, session: str, file_id: str, mark_last: bool) -> Iterator[TransferCommand]:
    "kitty's `split_for_transfer`: `data` commands, the last one `end_data` when `mark_last`."
    view = memoryview(data)
    while len(view):
        action = Action.END_DATA if mark_last and len(view) <= CHUNK_BYTES else Action.DATA
        yield TransferCommand(action, id=session, file_id=file_id, data=bytes(view[:CHUNK_BYTES]))
        view = view[CHUNK_BYTES:]


@dataclass(slots=True)
class _Reading(_Session):
    "A program's `receive`: kitty's `ActiveSend`."

    expected: int = 0
    specs: list[tuple[str, str]] = field(default_factory=list)
    #: Every path the listing named, the only ones whose data is sent.
    listed: set[str] = field(default_factory=set)
    listing_sent: bool = False
    queued: dict[str, _Source] = field(default_factory=dict)
    active: _Source | None = None
    pending: deque[TransferCommand] = field(default_factory=deque)

    @property
    def specs_complete(self) -> bool:
        return self.expected <= len(self.specs)

    @property
    def has_data(self) -> bool:
        return self.listing_sent and bool(
            self.pending or self.active is not None or any(source.ready for source in self.queued.values())
        )

    def close(self) -> None:
        if self.active is not None:
            self.active.close()
            self.active = None
        for source in self.queued.values():
            source.close()
        self.queued = {}

    def next_command(self) -> TransferCommand | None:
        if self.pending:
            return self.pending.popleft()
        source = self.active
        if source is None:
            source = next((one for one in self.queued.values() if one.ready), None)
            if source is None:
                return None
            del self.queued[source.file_id]
            self.active = source
        while True:
            chunk = source.next_chunk()
            if source.transmitted:
                self.active = None
                break
            if chunk:
                break
        if chunk:
            self.pending.extend(_chunks(chunk, self.id, source.file_id, mark_last=source.transmitted))
            return self.pending.popleft()
        return TransferCommand(Action.END_DATA, id=self.id, file_id=source.file_id)


def _listing(specs: list[tuple[str, str]], session: str, home: Path) -> Iterator[TransferCommand | _Failed]:
    """
    The metadata of every file the specs name, directories walked and
    symlinks not followed: kitty's `iter_file_metadata`. A second name
    for a regular file is a hard link to the first.
    """
    by_inode: defaultdict[tuple[int, int], list[TransferCommand]] = defaultdict(list)
    counter = iter(range(1 << 62))

    def entry(path: str, spec_id: str, status: os.stat_result | None = None, parent: str = "") -> TransferCommand:
        if status is None:
            status = os.stat(path, follow_symlinks=False)
        if stat.S_ISLNK(status.st_mode):
            file_type = FileType.SYMLINK
        elif stat.S_ISDIR(status.st_mode):
            file_type = FileType.DIRECTORY
        elif stat.S_ISREG(status.st_mode):
            file_type = FileType.REGULAR
        else:
            raise ValueError("Not an appropriate file type")
        found = TransferCommand(
            Action.FILE,
            id=session,
            file_id=spec_id,
            mtime=status.st_mtime_ns,
            permissions=stat.S_IMODE(status.st_mode),
            name=path,
            status=str(next(counter)),
            size=status.st_size,
            file_type=file_type,
            parent=parent,
        )
        by_inode[(status.st_dev, status.st_ino)].append(found)
        return found

    def walk(directory: TransferCommand) -> None:
        try:
            names = os.listdir(directory.name)
        except OSError:
            return
        for name in names:
            try:
                child = entry(os.path.join(directory.name, name), directory.file_id, parent=directory.status)
            except ValueError, OSError:
                continue
            if child.file_type is FileType.DIRECTORY:
                walk(child)

    for spec_id, spec in specs:
        path = absolute(spec, home)
        try:
            status = os.stat(path, follow_symlinks=False)
            readable = os.access(path, os.R_OK, follow_symlinks=False)
        except OSError as error:
            yield _Failed(_code_of(error), "Failed to read spec", spec_id)
            continue
        if not readable:
            yield _Failed(Status.EPERM, "No permission to read spec", spec_id)
            continue
        try:
            found = entry(path, spec_id, status)
        except ValueError:
            yield _Failed(Status.EINVAL, "Not a valid filetype", spec_id)
            continue
        if found.file_type is FileType.DIRECTORY:
            walk(found)

    for entries in by_inode.values():
        first = entries[0]
        if first.file_type is FileType.SYMLINK:
            with suppress(OSError):
                target = os.stat(os.path.realpath(first.name), follow_symlinks=False)
                pointed = by_inode.get((target.st_dev, target.st_ino))
                if pointed is not None:
                    first = replace(first, data=pointed[0].status.encode("utf-8"))
        yield first
        if len(entries) > 1 and first.file_type is FileType.REGULAR:
            for other in entries[1:]:
                if other.file_type is FileType.REGULAR:
                    yield replace(other, file_type=FileType.LINK, data=first.status.encode("utf-8"))


# ----------------------------------------------------------------------
# The sessions of one connection.


class Transfers:
    """
    Every session the panes of one server hold with this machine. Each
    call takes one thing that happened and gives back what to do: the
    commands for the programs, and the questions for the person.

    `may_read` says whether a grant or `upload-paths` lets these paths
    pass without a question, and `bypass` is `file-transfer-bypass`.
    **Both block.**
    """

    def __init__(
        self,
        home: Path,
        may_read: Callable[[list[str]], bool],
        clock: Callable[[], float] = time.monotonic,
        bypass: Callable[[], str] = str,
    ) -> None:
        self.home = home
        self.may_read = may_read
        self.clock = clock
        self.bypass = bypass
        self.writing: dict[str, _Writing] = {}
        self.reading: dict[str, _Reading] = {}

    @property
    def has_data(self) -> bool:
        "Whether `pump` has file data to send."
        return any(session.has_data for session in self.reading.values())

    def take(self, param: str, pane: str = "") -> list[Effect]:
        "One command of a pane's program, as OSC 5113 carries it after its code."
        try:
            command = TransferCommand.decode(param)
        except Malformed:
            return []
        if not command.id:
            return []
        out: list[Effect] = []
        if command.action is Action.CANCEL:
            if command.id in self.writing:
                self._writing_said(command, pane, out)
                return out
            if command.id in self.reading:
                self._reading_said(command, pane, out)
                return out
        self._prune(out)
        if command.id in self.writing or command.action is Action.SEND:
            self._writing_said(command, pane, out)
        if command.id in self.reading or command.action is Action.RECEIVE:
            self._reading_said(command, pane, out)
        return out

    def answered(self, session: str, yes: bool, why: str = REFUSED) -> list[Effect]:
        "The person's answer about `session`, or `why` nobody could ask."
        out: list[Effect] = []
        writing = self.writing.get(session)
        if writing is not None:
            if not yes:
                self._drop_writing(session, out, unask=False)
                if writing.says_errors:
                    out.append(_status(session, Status.EPERM, why))
                return out
            writing.accepted = True
            if writing.acknowledges:
                out.append(_status(session, Status.OK))
            return out
        reading = self.reading.get(session)
        if reading is not None:
            if not yes:
                self._drop_reading(session, out, unask=False)
                if reading.says_errors:
                    out.append(_status(session, Status.EPERM, why))
                return out
            reading.accepted = True
            if reading.acknowledges:
                out.append(_status(session, Status.OK))
            if reading.specs_complete:
                self._send_listing(reading, out)
        return out

    def pump(self) -> list[Effect]:
        "The next part of the data a program asked for, at most `READ_BYTES` of one file."
        out: list[Effect] = []
        for session in list(self.reading.values()):
            if not session.has_data:
                continue
            session.used = self.clock()
            sent = 0
            while sent < READ_BYTES:
                try:
                    command = session.next_command()
                except OSError as error:
                    file_id = session.active.file_id if session.active is not None else ""
                    if session.says_errors:
                        out.append(_status(session.id, _code_of(error), "Failed to read data from file", file_id))
                    self._drop_reading(session.id, out)
                    break
                if command is None:
                    break
                out.append(command)
                sent += len(command.data)
            break
        return out

    def close(self) -> None:
        "The connection went: every session ends, and nobody is told."
        for writing in self.writing.values():
            writing.close()
        for reading in self.reading.values():
            reading.close()
        self.writing.clear()
        self.reading.clear()

    # The program sends.

    def _writing_said(self, command: TransferCommand, pane: str, out: list[Effect]) -> None:
        session = self.writing.get(command.id)
        if session is None:
            if command.action is not Action.SEND or len(self.writing) >= MAX_SESSIONS:
                return
            session = _Writing(command.id, pane, command.quiet, self.clock())
            self.writing[command.id] = session
            if command.bypass:
                out.extend(self.answered(command.id, bypass_matches(self.bypass(), command.id, command.bypass)))
                return
            session.asked = True
            out.append(Question(command.id, pane, reads=False))
            return
        if command.action is Action.SEND or not session.accepted:
            # kitty drops a session that says anything before its answer.
            self._drop_writing(command.id, out)
            return
        session.used = self.clock()
        match command.action:
            case Action.CANCEL:
                self._drop_writing(command.id, out)
                if session.acknowledges:
                    out.append(_status(command.id, Status.CANCELED))
            case Action.FILE:
                self._start_file(session, command, out)
            case Action.DATA | Action.END_DATA:
                self._add_data(session, command, out)
            case Action.FINISH:
                for destination in sorted(
                    (one for one in session.files.values() if one.file_type is FileType.DIRECTORY),
                    key=lambda one: len(one.name),
                    reverse=True,
                ):
                    # The directory's OK went out already.
                    with suppress(OSError):
                        destination.apply_metadata()
                self._drop_writing(command.id, out)

    def _start_file(self, session: _Writing, command: TransferCommand, out: list[Effect]) -> None:
        if command.file_id in session.files:
            if session.says_errors:
                out.append(
                    _status(
                        session.id, Status.EINVAL, "The file_id %s already exists" % (command.file_id,), command.file_id
                    )
                )
            return
        try:
            destination = _Destination(command, self.home)
        except Exception as error:
            if session.says_errors:
                out.append(_status(session.id, Status.EINVAL, str(error), command.file_id))
            return
        session.files[command.file_id] = destination
        if destination.file_type is FileType.DIRECTORY:
            try:
                os.makedirs(destination.name, exist_ok=True)
            except OSError as error:
                if session.says_errors:
                    out.append(_status(session.id, _code_of(error), "Failed to create directory", destination.file_id))
                return
            out.append(_status(session.id, Status.OK, file_id=destination.file_id, name=destination.name))
            return
        if not session.acknowledges:
            return
        size = destination.existing.st_size if destination.existing is not None else -1
        patches = (
            command.transmission_type is TransmissionType.RSYNC
            and destination.file_type is FileType.REGULAR
            and destination.can_patch_in_place
        )
        signature = b""
        if patches and destination.existing is not None:
            try:
                destination.patch = _Patch(destination.name, destination.existing)
                signature = destination.patch.signature()
            except OSError as error:
                destination.close()
                destination.failed = True
                if session.says_errors:
                    out.append(
                        _status(session.id, _code_of(error), "Failed to open file to read signature", command.file_id)
                    )
                return
        out.append(
            _status(
                session.id,
                Status.STARTED,
                file_id=destination.file_id,
                name=destination.name,
                size=size,
                transmission_type=TransmissionType.RSYNC if patches else TransmissionType.SIMPLE,
            )
        )
        if patches:
            out.extend(_chunks(signature, session.id, destination.file_id, mark_last=False))
            out.append(TransferCommand(Action.END_DATA, id=session.id, file_id=destination.file_id))

    def _add_data(self, session: _Writing, command: TransferCommand, out: list[Effect]) -> None:
        destination = session.files.get(command.file_id)
        if destination is None:
            if session.says_errors:
                out.append(
                    _status(
                        session.id, Status.EINVAL, "Cannot write to a file without first starting it", command.file_id
                    )
                )
            return
        if destination.failed:
            return
        before = destination.written
        try:
            destination.write(session.files, command.data, command.action is Action.END_DATA)
        except Exception as error:
            destination.failed = True
            with suppress(Exception):
                destination.close()
            if session.says_errors:
                if isinstance(error, _Failed):
                    out.append(_status(session.id, error.code, error.message, error.file_id))
                elif isinstance(error, OSError):
                    out.append(_status(session.id, _code_of(error), str(error), command.file_id))
                else:
                    out.append(_status(session.id, Status.EINVAL, str(error), command.file_id))
            return
        if not session.acknowledges:
            return
        if destination.closed:
            out.append(
                _status(
                    session.id, Status.OK, file_id=destination.file_id, name=destination.name, size=destination.written
                )
            )
        elif destination.written > before:
            out.append(_status(session.id, Status.PROGRESS, file_id=destination.file_id, size=destination.written))

    def _drop_writing(self, session: str, out: list[Effect], unask: bool = True) -> None:
        dropped = self.writing.pop(session, None)
        if dropped is None:
            return
        dropped.close()
        if unask and not dropped.accepted:
            out.append(Unasked(session))

    # The program reads.

    def _reading_said(self, command: TransferCommand, pane: str, out: list[Effect]) -> None:
        session = self.reading.get(command.id)
        if session is None:
            if command.action is not Action.RECEIVE or len(self.reading) >= MAX_SESSIONS:
                return
            session = _Reading(command.id, pane, command.quiet, self.clock(), expected=command.size)
            if command.bypass:
                session.bypass_ok = bypass_matches(self.bypass(), command.id, command.bypass)
            self.reading[command.id] = session
            if session.specs_complete:
                self._ask_to_read(session, out)
            return
        if command.action is Action.RECEIVE:
            self._drop_reading(command.id, out)
            return
        session.used = self.clock()
        match command.action:
            case Action.FILE:
                try:
                    if session.listing_sent:
                        self._queue_source(session, command)
                    else:
                        if len(session.specs) >= MAX_SPECS or session.specs_complete:
                            raise _Failed(Status.EINVAL, "Too many file specs")
                        session.specs.append((command.file_id, command.name))
                except OSError as error:
                    if session.says_errors:
                        out.append(_status(session.id, _code_of(error), "Failed to add send file", command.file_id))
                    self._drop_reading(session.id, out)
                    return
                except _Failed as error:
                    self._drop_reading(session.id, out)
                    if session.says_errors:
                        out.append(_status(session.id, error.code, error.message, error.file_id))
                    return
                if not session.listing_sent and session.specs_complete:
                    if session.accepted:
                        self._send_listing(session, out)
                    elif not session.asked:
                        self._ask_to_read(session, out)
                return
            case Action.DATA | Action.END_DATA:
                try:
                    source = session.queued.get(command.file_id)
                    if source is None:
                        raise _Failed(Status.EINVAL, "Signature data for unknown file_id: %s" % (command.file_id,))
                    source.add_signature(command)
                except _Failed as error:
                    self._drop_reading(session.id, out)
                    if session.says_errors:
                        out.append(_status(session.id, error.code, error.message, error.file_id))
                return
            case Action.STATUS | Action.FINISH:
                self._drop_reading(session.id, out)
                return
        if not session.accepted:
            self._drop_reading(command.id, out)
            return
        if command.action is Action.CANCEL:
            self._drop_reading(command.id, out)
            if session.acknowledges:
                out.append(_status(command.id, Status.CANCELED))

    def _ask_to_read(self, session: _Reading, out: list[Effect]) -> None:
        if session.bypass_ok is not None:
            out.extend(self.answered(session.id, session.bypass_ok))
            return
        session.asked = True
        paths = [absolute(spec, self.home) for _spec_id, spec in session.specs]
        if self.may_read(paths):
            out.extend(self.answered(session.id, True))
            return
        out.append(Question(session.id, session.pane, reads=True, paths=tuple(spec for _id, spec in session.specs)))

    def _queue_source(self, session: _Reading, command: TransferCommand) -> None:
        if len(session.queued) > MAX_QUEUED:
            raise _Failed(Status.EINVAL, "Too many queued files")
        if command.name not in session.listed:
            raise _Failed(Status.EPERM, "%s is not in the listing" % (command.name,), command.file_id)
        session.queued[command.file_id] = _Source(command)

    def _send_listing(self, session: _Reading, out: list[Effect]) -> None:
        sent = False
        for found in _listing(session.specs, session.id, self.home):
            sent = True
            if isinstance(found, _Failed):
                if session.says_errors:
                    out.append(_status(session.id, found.code, found.message, found.file_id))
                continue
            if found.file_type in (FileType.REGULAR, FileType.SYMLINK):
                session.listed.add(found.name)
            out.append(found)
        if sent:
            out.append(_status(session.id, Status.OK, name=str(self.home)))
            session.listing_sent = True
        else:
            out.append(_status(session.id, Status.ENOENT, "No files found"))
            self._drop_reading(session.id, out)

    def _drop_reading(self, session: str, out: list[Effect], unask: bool = True) -> None:
        dropped = self.reading.pop(session, None)
        if dropped is None:
            return
        dropped.close()
        if unask and dropped.asked and not dropped.accepted:
            out.append(Unasked(session))

    def _prune(self, out: list[Effect]) -> None:
        now = self.clock()
        for session in [one.id for one in self.writing.values() if now - one.used > EXPIRE_SECONDS]:
            self._drop_writing(session, out)
        for session in [one.id for one in self.reading.values() if now - one.used > EXPIRE_SECONDS]:
            self._drop_reading(session, out)
