"""
The files of a drop: a text/uri-list read as kitty reads it, base64 in
chunks, and the copy of files dropped from another machine.

A terminal whose drop comes from another machine says so on its
text/uri-list (`X=1`). The paths name files there, so pymux pulls each
one over the protocol into a directory of its own on the server, and
pastes those paths instead. A directory arrives as a list of names under
a handle, and each name is asked for in turn, breadth first, as the
spec recommends. Lillecarl/pymux#892.
"""

from __future__ import annotations

import base64
import os
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Final
from urllib.parse import unquote, urlparse

from platformdirs import user_cache_dir
from pyte.sequences.kitty_dnd import DndPacket, DndType

from ..log import logger

__all__ = [
    "MAX_COPY_BYTES",
    "MAX_COPY_ENTRIES",
    "Base64Stream",
    "RemoteCopy",
    "decoded",
    "drops_directory",
    "entries_of",
    "new_drop_root",
    "paths_of",
]

#: The most a copy takes, in bytes and in entries. Past either the copy
#: stops and nothing is pasted: a drop is a person's gesture, and this is
#: the bound on a terminal that keeps sending.
MAX_COPY_BYTES: Final = 8 << 30
MAX_COPY_ENTRIES: Final = 100_000

#: The `X` of a response that names a symbolic link; a higher one is a
#: directory's handle.
_SYMLINK: Final = 1
_FILE_URL: Final = "file://"


def drops_directory() -> Path:
    "Where the copies of remote drops go: one directory each, under the user's cache."
    return Path(user_cache_dir("pymux")) / "drops"


class Base64Stream:
    """
    Base64 that arrives in chunks. A chunk need not end on a group, and
    padding is optional at the end of the last; a chunk that ends in
    padding ends its groups.
    """

    def __init__(self) -> None:
        self.carry = ""

    def feed(self, chunk: str) -> bytes:
        "The bytes the groups so far hold. Raises ValueError."
        text = self.carry + chunk
        whole = len(text) if text.endswith("=") else len(text) // 4 * 4
        self.carry = text[whole:]
        return base64.b64decode(text[:whole], validate=True)

    def close(self) -> bytes:
        "The bytes of an unpadded last group. Raises ValueError."
        carry, self.carry = self.carry, ""
        return base64.b64decode(carry + "=" * (-len(carry) % 4), validate=True) if carry else b""


def decoded(chunks: list[str]) -> bytes:
    "The data of a chain of base64 chunks. Raises ValueError."
    stream = Base64Stream()
    return b"".join(stream.feed(chunk) for chunk in chunks) + stream.close()


def entries_of(uri_list: str) -> list[str]:
    "The entries of a text/uri-list as kitty's get_nth_file_url counts them: no blank line and no comment."
    entries = []
    for line in uri_list.replace("\r", "\n").split("\n"):
        line = line.rstrip(" \t")
        if line and not line.startswith("#"):
            entries.append(line)
    return entries


def paths_of(uri_list: str) -> list[str]:
    "The paths of the file URLs in a text/uri-list, and any other entry as it is: kitty's parse_uri_list."
    found = []
    for entry in entries_of(uri_list):
        if not entry.startswith(_FILE_URL):
            found.append(entry)
            continue
        try:
            path = urlparse(entry, allow_fragments=False).path
        except ValueError:
            found.append(entry)
            continue
        if path:
            found.append(unquote(path))
    return found


def _safe_name(name: str) -> str | None:
    "A name from another machine that is one entry of a directory here, or None."
    if not name or name in (".", "..") or "/" in name or "\0" in name:
        return None
    return name


def _top_name(url: str) -> str:
    path = unquote(urlparse(url, allow_fragments=False).path).rstrip("/")
    return _safe_name(path.rpartition("/")[2]) or "dropped"


@dataclass
class _Item:
    "One thing to ask for, and where it goes."

    request: DndPacket
    path: Path
    top: bool = False
    #: A directory handle to close instead: nothing is asked.
    close: bool = False


@dataclass
class _Arriving:
    "The response to the item asked for, as it arrives."

    kind: int = 0
    file: BinaryIO | None = None
    stream: Base64Stream = field(default_factory=Base64Stream)
    parts: list[str] = field(default_factory=list)


class RemoteCopy:
    """
    One drop from another machine, copied file by file into `root`.

    Asks one thing at a time: `start` and `took` return the packets to
    send. `done` says the copy ended; `pasted` is then what to paste, in
    the order of the list, and None when the copy failed.
    """

    def __init__(self, root: Path, uri_index: int, uri_list: str) -> None:
        self.root = root
        self.queue: deque[_Item] = deque()
        self.current: _Item | None = None
        self.arriving = _Arriving()
        self.order: list[str | Path] = []
        self.failed: set[Path] = set()
        self.size = 0
        self.entries = 0
        self.done = False
        self.ok = True
        taken: set[str] = set()
        for number, entry in enumerate(entries_of(uri_list), start=1):
            if not entry.startswith(_FILE_URL):
                self.order.append(entry)
                continue
            name = _top_name(entry)
            unique, n = name, 1
            while unique in taken:
                n += 1
                unique = "%s-%d" % (name, n)
            taken.add(unique)
            path = root / unique
            self.order.append(path)
            self.queue.append(_Item(DndPacket(DndType.REQUEST, cell_x=uri_index, cell_y=number), path, top=True))

    @property
    def pasted(self) -> list[str] | None:
        if not self.ok:
            return None
        return [str(item) for item in self.order if item not in self.failed]

    def start(self) -> list[DndPacket]:
        self.root.mkdir(parents=True, exist_ok=True)
        return self._next()

    def _next(self) -> list[DndPacket]:
        "Close what is due, and ask for the next item; or end."
        out = []
        while self.queue:
            item = self.queue.popleft()
            if item.close:
                out.append(item.request)
                continue
            self.current = item
            self.arriving = _Arriving()
            out.append(item.request)
            return out
        self.current = None
        self.done = True
        return out

    def _matches(self, packet: DndPacket) -> bool:
        asked = self.current.request if self.current is not None else None
        return asked is not None and (packet.cell_x, packet.cell_y, packet.pixel_y) == (
            asked.cell_x,
            asked.cell_y,
            asked.pixel_y,
        )

    def took(self, packet: DndPacket) -> list[DndPacket]:
        "One packet of a response. Raises nothing; a failure ends the copy."
        item = self.current
        if item is None or not self._matches(packet):
            return []
        try:
            if packet.type == DndType.REQUEST_ERROR:
                logger.info("A dropped file did not come: %s, for %s.", packet.payload, item.path)
                self._lost(item)
                return self._next()
            # An empty directory's handle comes on its end alone.
            self.arriving.kind = packet.pixel_x
            if packet.payload or packet.more:
                self._chunk(item, packet)
                return []
            self._finish(item)
            return self._next()
        except (OSError, ValueError) as error:
            logger.warning("A dropped file could not be copied to %s: %s.", item.path, error)
            self._lost(item)
            return self._next()
        except _TooMuch as error:
            logger.warning("A drop from another machine was too large: %s.", error)
            self.stop()
            return []

    def _chunk(self, item: _Item, packet: DndPacket) -> None:
        arriving = self.arriving
        if arriving.kind == 0:
            if arriving.file is None:
                arriving.file = open(item.path, "wb")
            data = arriving.stream.feed(packet.payload)
            self.size += len(data)
            if self.size > MAX_COPY_BYTES:
                raise _TooMuch("more than %d bytes" % MAX_COPY_BYTES)
            arriving.file.write(data)
        else:
            arriving.parts.append(packet.payload)

    def _finish(self, item: _Item) -> None:
        arriving = self.arriving
        self.entries += 1
        if self.entries > MAX_COPY_ENTRIES:
            raise _TooMuch("more than %d entries" % MAX_COPY_ENTRIES)
        if arriving.kind == 0:
            with arriving.file or open(item.path, "wb") as file:
                file.write(arriving.stream.close())
            arriving.file = None
        elif arriving.kind == _SYMLINK:
            os.symlink(os.fsdecode(decoded(arriving.parts)), item.path)
        else:
            self._directory(item, arriving.kind, decoded(arriving.parts))

    def _directory(self, item: _Item, handle: int, listing: bytes) -> None:
        item.path.mkdir(exist_ok=True)
        names = os.fsdecode(listing).split("\0") if listing else []
        for number, name in enumerate(names, start=1):
            safe = _safe_name(name)
            if safe is None:
                logger.info("Skipped a dropped entry with an unsafe name: %r.", name)
                continue
            self.queue.append(_Item(DndPacket(DndType.REQUEST, cell_x=number, pixel_y=handle), item.path / safe))
        self.queue.append(_Item(DndPacket(DndType.REQUEST, pixel_y=handle), item.path, close=True))

    def _lost(self, item: _Item) -> None:
        if self.arriving.file is not None:
            self.arriving.file.close()
            self.arriving.file = None
        if item.top:
            self.failed.add(item.path)

    def stop(self) -> None:
        "End the copy here, with nothing pasted."
        if self.arriving.file is not None:
            self.arriving.file.close()
        self.queue.clear()
        self.current = None
        self.done = True
        self.ok = False


class _TooMuch(Exception):
    "A copy past MAX_COPY_BYTES or MAX_COPY_ENTRIES."


def new_drop_root(base: Path) -> Path:
    "A directory for one drop's copy that no other drop uses."
    stamp = time.strftime("%Y%m%d-%H%M%S")
    n = 1
    while True:
        root = base / ("%s-%d" % (stamp, n))
        if not root.exists():
            return root
        n += 1
