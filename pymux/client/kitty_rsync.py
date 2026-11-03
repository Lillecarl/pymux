"""
kitty's rsync deltas, for the file transfer protocol (OSC 5113).
https://sw.kovidgoyal.net/kitty/file-transfer-protocol/#transmitting-binary-deltas.
Lillecarl/pymux#912.

A port of kitty's `tools/rsync`. The side that holds the old file makes a
`Patcher`, sends its signature, and applies the delta that comes back.
The side that holds the new file makes a `Differ`, reads the signature,
and writes the delta. Every integer is little-endian. Blocks hash with
XXH3-64 and the rsync rolling checksum, and the whole file with
XXH3-128, which the delta's last operation carries.
"""

from __future__ import annotations

import math
import struct
from collections.abc import Callable, Iterator
from enum import IntEnum
from itertools import accumulate
from typing import BinaryIO, Final

import xxhash

__all__ = ["Differ", "Patcher", "RsyncError", "weak_hash"]

#: kitty's `DefaultBlockSize`, for a file of no size.
DEFAULT_BLOCK_SIZE: Final = 6 * 1024
#: kitty's `MaxBlockSize`: the square root of a terabyte.
MAX_BLOCK_SIZE: Final = 1024 * 1024

#: What `Differ.delta` gives at a time, and the most one data operation holds.
DELTA_BYTES: Final = 1024 * 1024

_M: Final = 1 << 16
_HEADER: Final = struct.Struct("<HHHHI")
_BLOCK: Final = struct.Struct("<QIQ")
_INDEX: Final = struct.Struct("<Q")
_RANGE: Final = struct.Struct("<QI")
_SIZE32: Final = struct.Struct("<I")
_SIZE16: Final = struct.Struct("<H")


class RsyncError(Exception):
    "A signature or a delta that kitty's rsync would refuse."


class _Op(IntEnum):
    BLOCK = 0
    DATA = 1
    HASH = 2
    BLOCK_RANGE = 3


def weak_hash(block: bytes | bytearray | memoryview) -> tuple[int, int]:
    """
    The rsync rolling checksum of a whole block, as `(alpha, beta)`.

    kitty weighs byte `i` of `l` with `l - i`, which is the sum of the
    prefix sums. Its sums wrap at 2**32, a multiple of the modulus, so
    Python's exact sums give the same remainder.
    """
    return sum(block) % _M, sum(accumulate(block)) % _M


def _records(run: bytes, block_size: int, first: int) -> bytes:
    """
    The signature records of the blocks of `run`, numbered from `first`;
    the last block may be short. `pymux.graphics.kernels.install_rsync()`
    puts `pymux_rs.rsync_records` in its place. Lillecarl/pymux#921.
    """
    out = bytearray()
    for index, at in enumerate(range(0, len(run), block_size), first):
        block = run[at : at + block_size]
        alpha, beta = weak_hash(block)
        out += _BLOCK.pack(index, alpha + _M * beta, xxhash.xxh3_64_intdigest(block))
    return bytes(out)


class Patcher:
    "The side that holds the old file: its signature, then the delta applied to it."

    def __init__(self, expected_size: int) -> None:
        size = max(0, expected_size)
        block_size = round(math.sqrt(size)) if size > 0 else DEFAULT_BLOCK_SIZE
        self.block_size = max(1, min(block_size, MAX_BLOCK_SIZE))
        self._unconsumed = bytearray()
        self._checksum = xxhash.xxh3_128()
        self._checksum_done = False

    def signature(self, old: BinaryIO) -> Iterator[bytes]:
        "The header, then one record per block of `old`, read from where it stands."
        yield _HEADER.pack(0, 0, 0, 0, self.block_size)
        index = 0
        blocks = max(1, DELTA_BYTES // self.block_size)
        while run := old.read(blocks * self.block_size):
            yield _records(run, self.block_size, index)
            index += -(-len(run) // self.block_size)

    def apply(self, data: bytes, old: BinaryIO, write: Callable[[bytes], object]) -> None:
        "Apply what of the delta in `data` is whole; the rest waits for more."
        self._unconsumed += data
        view = memoryview(self._unconsumed)
        at = 0
        try:
            while at < len(view):
                used = self._operation(view, at, old, write)
                if used == 0:
                    break
                at += used
        finally:
            view.release()
        del self._unconsumed[:at]

    def finish(self) -> None:
        if self._unconsumed:
            raise RsyncError("There are %d leftover bytes in the delta" % (len(self._unconsumed),))
        if not self._checksum_done:
            raise RsyncError("The checksum was not received at the end of the delta data")

    def _operation(self, view: memoryview, at: int, old: BinaryIO, write: Callable[[bytes], object]) -> int:
        "Apply the operation at `at`, and say its length: 0 when it is not whole yet."
        left = len(view) - at
        kind = view[at]
        match kind:
            case _Op.BLOCK:
                if left < 1 + _INDEX.size:
                    return 0
                (index,) = _INDEX.unpack_from(view, at + 1)
                self._copy(index, index, old, write)
                return 1 + _INDEX.size
            case _Op.BLOCK_RANGE:
                if left < 1 + _RANGE.size:
                    return 0
                index, more = _RANGE.unpack_from(view, at + 1)
                self._copy(index, index + more, old, write)
                return 1 + _RANGE.size
            case _Op.DATA:
                if left < 1 + _SIZE32.size:
                    return 0
                (size,) = _SIZE32.unpack_from(view, at + 1)
                start = at + 1 + _SIZE32.size
                if len(view) < start + size:
                    return 0
                self._write(bytes(view[start : start + size]), write)
                return 1 + _SIZE32.size + size
            case _Op.HASH:
                if left < 1 + _SIZE16.size:
                    return 0
                (size,) = _SIZE16.unpack_from(view, at + 1)
                start = at + 1 + _SIZE16.size
                if len(view) < start + size:
                    return 0
                expected = bytes(view[start : start + size])
                actual = self._checksum.digest()
                if actual != expected:
                    raise RsyncError(
                        "Failed to verify overall file checksum actual: %s != expected: %s"
                        % (actual.hex(), expected.hex())
                    )
                self._checksum_done = True
                return 1 + _SIZE16.size + size
        raise RsyncError("record has unknown operation type: %d" % (kind,))

    def _copy(self, first: int, last: int, old: BinaryIO, write: Callable[[bytes], object]) -> None:
        for index in range(first, last + 1):
            old.seek(index * self.block_size)
            self._write(old.read(self.block_size), write)

    def _write(self, data: bytes, write: Callable[[bytes], object]) -> None:
        self._checksum.update(data)
        write(data)


class _Blocks:
    """
    The blocks of a signature, and the search for one of them in new bytes.
    `pymux.graphics.kernels.install_rsync()` puts `pymux_rs.RsyncBlocks`
    in its place, which answers the same. Lillecarl/pymux#916.
    """

    def __init__(self, block_size: int) -> None:
        self.block_size = block_size
        #: Each weak hash, with the strong hash and the index of every block that has it.
        self._by_weak: dict[int, list[tuple[int, int]]] = {}

    def add(self, records: bytes) -> None:
        "Hold the blocks of whole signature records."
        for index, weak_value, strong_value in _BLOCK.iter_unpack(records):
            self._by_weak.setdefault(weak_value, []).append((strong_value, index))

    def find(self, buffer: bytearray, start: int, limit: int) -> tuple[int, int | None]:
        """
        Roll a window of one block from `start` a byte at a time, and answer
        the first place a block of the signature stands, with its index. The
        first such block in the signature wins. At `limit`, or where the
        window cannot move on in `buffer`, answer the place with None.
        """
        size = self.block_size
        if start + size > len(buffer):
            return start, None
        by_weak = self._by_weak
        view = memoryview(buffer)
        try:
            alpha, beta = weak_hash(view[start : start + size])
            while True:
                candidates = by_weak.get(alpha + _M * beta)
                if candidates is not None:
                    strong = xxhash.xxh3_64_intdigest(view[start : start + size])
                    for value, index in candidates:
                        if value == strong:
                            return start, index
                if start + size >= len(buffer):
                    return start, None
                leaving, entering = buffer[start], buffer[start + size]
                alpha = (alpha - leaving + entering) % _M
                beta = (beta - size * leaving + alpha) % _M
                start += 1
                if start >= limit:
                    return start, None
        finally:
            view.release()


class Differ:
    "The side that holds the new file: a signature in, the delta out."

    def __init__(self) -> None:
        self.block_size = 0
        self._unconsumed = bytearray()
        self._blocks: _Blocks | None = None

    def add_signature(self, data: bytes) -> None:
        self._unconsumed += data
        if not self.block_size:
            if len(self._unconsumed) < _HEADER.size:
                return
            version, checksum, strong, weak, block_size = _HEADER.unpack_from(self._unconsumed)
            for name, value in (("version", version), ("checksum_type", checksum), ("strong_hash", strong)):
                if value:
                    raise RsyncError("Invalid %s in signature header: %d" % (name, value))
            if weak:
                raise RsyncError("Invalid weak_hash in signature header: %d" % (weak,))
            if block_size == 0:
                raise RsyncError("rsync signature header has zero block size")
            if block_size > MAX_BLOCK_SIZE:
                raise RsyncError(
                    "rsync signature header has too large block size %d > %d" % (block_size, MAX_BLOCK_SIZE)
                )
            self.block_size = block_size
            self._blocks = _Blocks(block_size)
            del self._unconsumed[: _HEADER.size]
        whole = len(self._unconsumed) - len(self._unconsumed) % _BLOCK.size
        assert self._blocks is not None
        self._blocks.add(bytes(self._unconsumed[:whole]))
        del self._unconsumed[:whole]

    def finish_signature(self) -> None:
        if self._unconsumed:
            raise RsyncError("There were %d leftover bytes in the signature data" % (len(self._unconsumed),))
        if not self.block_size:
            raise RsyncError("No header was found in the signature data")

    def delta(self, new: BinaryIO) -> Iterator[bytes]:
        """
        The delta that makes the signed file into `new`, in parts of about
        `DELTA_BYTES`. A window of one block rolls over `new` a byte at a
        time, and jumps a block where the block is one the signature has.
        """
        size = self.block_size
        blocks = self._blocks
        if blocks is None:
            raise RsyncError("No header was found in the signature data")
        checksum = xxhash.xxh3_128()
        out = bytearray()
        buffer = bytearray()
        ended = False
        #: The window's start, and the start of the bytes no operation carries yet.
        start = data = 0
        #: The run of matched blocks not written yet, first and last.
        run: tuple[int, int] | None = None

        def fill(needed: int) -> bool:
            nonlocal ended
            while len(buffer) < needed and not ended:
                more = new.read(max(needed - len(buffer), DELTA_BYTES))
                if not more:
                    ended = True
                    break
                checksum.update(more)
                buffer.extend(more)
            return len(buffer) >= needed

        def flush_run() -> None:
            nonlocal run
            if run is not None:
                first, last = run
                if first == last:
                    out.append(_Op.BLOCK)
                    out.extend(_INDEX.pack(first))
                else:
                    out.append(_Op.BLOCK_RANGE)
                    out.extend(_RANGE.pack(first, last - first))
                run = None

        def flush_data(end: int) -> None:
            nonlocal data
            if end > data:
                flush_run()
                out.append(_Op.DATA)
                out.extend(_SIZE32.pack(end - data))
                out.extend(buffer[data:end])
                data = end

        while True:
            fill(start + size + 1)
            start, match = blocks.find(buffer, start, data + DELTA_BYTES)
            if match is not None:
                flush_data(start)
                if run is not None and run[1] + 1 == match:
                    run = (run[0], match)
                else:
                    flush_run()
                    run = (match, match)
                start += size
                data = start
            elif start - data >= DELTA_BYTES:
                flush_data(start)
            elif ended:
                break
            if data > 2 * DELTA_BYTES:
                del buffer[:data]
                start -= data
                data = 0
            if len(out) >= DELTA_BYTES:
                yield bytes(out)
                out.clear()
        flush_data(len(buffer))
        flush_run()
        digest = checksum.digest()
        out.append(_Op.HASH)
        out.extend(_SIZE16.pack(len(digest)))
        out.extend(digest)
        yield bytes(out)
