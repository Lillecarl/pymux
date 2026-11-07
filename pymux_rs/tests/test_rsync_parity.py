"""
`pymux_rs.RsyncBlocks` gives the delta `pymux.client.kitty_rsync._Blocks`
gives, byte for byte, and the delta patches the old file into the new.

Files that share blocks and files that share none, blocks that repeat so
the first in the signature has to win, and a run of new bytes past
`DELTA_BYTES`, where a data operation ends. Lillecarl/pyterm#918.
"""

from __future__ import annotations

import io
import random

import pymux_rs
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from pymux.client import kitty_rsync
from pymux.graphics import kernels

#: The references, taken before any test could have installed a kernel.
PURE_BLOCKS = kitty_rsync._Blocks
PURE_RECORDS = kitty_rsync._records


def delta(old: bytes, new: bytes, blocks: type) -> bytes:
    patcher = kitty_rsync.Patcher(len(old))
    differ = kitty_rsync.Differ()
    saved = kitty_rsync._Blocks
    kitty_rsync._Blocks = blocks
    try:
        for part in patcher.signature(io.BytesIO(old)):
            differ.add_signature(part)
        differ.finish_signature()
        return b"".join(differ.delta(io.BytesIO(new)))
    finally:
        kitty_rsync._Blocks = saved


def patched(old: bytes, delta_bytes: bytes) -> bytes:
    patcher = kitty_rsync.Patcher(len(old))
    out = bytearray()
    patcher.apply(delta_bytes, io.BytesIO(old), out.extend)
    patcher.finish()
    return bytes(out)


def assert_same(old: bytes, new: bytes) -> None:
    native = delta(old, new, pymux_rs.RsyncBlocks)
    assert native == delta(old, new, PURE_BLOCKS)
    assert patched(old, native) == new


@st.composite
def pairs(draw):
    "An old file, and a new one cut from its pieces, repeats and new bytes."
    pieces = draw(st.lists(st.binary(min_size=1, max_size=40), min_size=1, max_size=6))
    old = b"".join(draw(st.sampled_from(pieces)) for _ in range(draw(st.integers(0, 12))))
    new = b"".join(
        draw(st.one_of(st.sampled_from(pieces), st.binary(max_size=30))) for _ in range(draw(st.integers(0, 12)))
    )
    return old, new


@given(pairs())
@settings(max_examples=1500, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_the_same_delta_is_written(pair):
    assert_same(*pair)


def test_a_repeated_block_is_found_first_in_the_signature():
    block = bytes(range(64))
    old = block * 64
    assert_same(old, b"x" + block * 3)


@pytest.mark.parametrize("changed", [0, 1, 4096])
def test_a_large_file_gives_the_same_delta(changed):
    "Past `DELTA_BYTES` of new bytes, and over a file that changed in places."
    rng = random.Random(changed)
    old = rng.randbytes(3 * kitty_rsync.DELTA_BYTES + 12345)
    new = bytearray(old)
    for _ in range(changed):
        new[rng.randrange(len(new))] ^= 0xFF
    assert_same(old, bytes(new))


def test_unrelated_files_split_their_data_the_same():
    rng = random.Random(916)
    assert_same(rng.randbytes(2 * 1024 * 1024), rng.randbytes(2 * 1024 * 1024 + 7))


@given(st.binary(max_size=3000), st.integers(1, 700), st.integers(0, 2**40))
@settings(max_examples=1500, deadline=None)
def test_the_same_signature_is_written(run, block_size, first):
    "Lillecarl/pyterm#923."
    assert pymux_rs.rsync_records(run, block_size, first) == PURE_RECORDS(run, block_size, first)


def test_a_large_signature_is_the_same():
    old = random.Random(921).randbytes(5 * kitty_rsync.DELTA_BYTES + 3)
    patcher = kitty_rsync.Patcher(len(old))
    pure = b"".join(patcher.signature(io.BytesIO(old)))
    assert pymux_rs.rsync_records(old, patcher.block_size, 0) == pure[12:]


def test_install_puts_the_kernels_in_place(monkeypatch):
    monkeypatch.setattr(kitty_rsync, "_Blocks", PURE_BLOCKS)
    monkeypatch.setattr(kitty_rsync, "_records", PURE_RECORDS)
    kernels.install_rsync()
    assert kitty_rsync._Blocks is pymux_rs.RsyncBlocks
    assert kitty_rsync._records is pymux_rs.rsync_records


def test_pure_keeps_the_python(monkeypatch):
    monkeypatch.setattr(kitty_rsync, "_Blocks", PURE_BLOCKS)
    monkeypatch.setattr(kitty_rsync, "_records", PURE_RECORDS)
    monkeypatch.setenv(kernels.PURE, "1")
    kernels.install_rsync()
    assert kitty_rsync._Blocks is PURE_BLOCKS
    assert kitty_rsync._records is PURE_RECORDS
