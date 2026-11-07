"""
kitty's bypass password for its file transfer: the server's key, its
reading of an encrypted bypass, and the client's check. Lillecarl/pyterm#919.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import time
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from pyte.sequences.file_transfer import Action, TransferCommand

from pymux.client.kitty_transfer import REFUSED, Question, Transfers
from pymux.core.kitty_bypass import BypassKey, bypass_matches, encode_bypass


def sealed(public: str, plaintext: str) -> str:
    "What the kitten's `encode_bypass` sends: kitty's `tools/crypto` `Encrypt_data`, under `kitty-1:`."
    protocol, _colon, encoded = public.partition(":")
    assert protocol == "1"
    theirs = X25519PublicKey.from_public_bytes(base64.b85decode(encoded))
    mine = X25519PrivateKey.generate()
    key = hashlib.sha256(mine.exchange(theirs)).digest()
    iv = os.urandom(12)
    output = AESGCM(key).encrypt(iv, plaintext.encode(), None)
    fields = {
        "version": [0, 0, 0],
        "iv": base64.b85encode(iv).decode(),
        "tag": base64.b85encode(output[-16:]).decode(),
        "pubkey": base64.b85encode(mine.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode(),
        "encrypted": base64.b85encode(output[:-16]).decode(),
    }
    return "kitty-1:" + json.dumps(fields)


def test_kittys_example_hashes_as_the_protocol_says():
    "The protocol document's own example."
    assert (
        encode_bypass("mysession", "mypassword")
        == "sha256:192bd215915eeaa8c2b2a4c0f8f851826497d12b30036d8b5b1b4fc4411caf2c"
    )


def test_an_encrypted_bypass_becomes_its_hash(tmp_path: Path):
    key = BypassKey(tmp_path / "key")
    bypass = sealed(key.public, "%d:s1;sesame" % (time.time_ns(),))
    assert key.plain("s1", bypass) == encode_bypass("s1", "sesame")


def test_a_bypass_for_another_session_or_too_old_opens_nothing(tmp_path: Path):
    key = BypassKey(tmp_path / "key")
    assert key.plain("s2", sealed(key.public, "%d:s1;sesame" % (time.time_ns(),))) == ""
    old = time.time_ns() - 6 * 60 * 10**9
    assert key.plain("s1", sealed(key.public, "%d:s1;sesame" % (old,))) == ""
    other = BypassKey(tmp_path / "other")
    assert other.plain("s1", sealed(key.public, "%d:s1;sesame" % (time.time_ns(),))) == ""
    assert key.plain("s1", "nonsense") == ""


def test_the_key_outlives_the_server_and_only_its_owner_reads_it(tmp_path: Path):
    path = tmp_path / "key"
    first = BypassKey(path)
    assert BypassKey(path).public == first.public
    assert path.stat().st_mode & 0o777 == 0o600


def test_no_password_lets_nothing_pass():
    assert not bypass_matches("", "s1", encode_bypass("s1", ""))
    assert bypass_matches("sesame", "s1", encode_bypass("s1", "sesame"))
    assert not bypass_matches("sesame", "s1", encode_bypass("s1", "other"))


def _statuses(effects: list) -> list[str]:
    return [effect.status for effect in effects if isinstance(effect, TransferCommand)]


def test_a_matching_bypass_skips_the_question(tmp_path: Path):
    transfers = Transfers(tmp_path, lambda paths: False, bypass=lambda: "sesame")
    effects = transfers.take(TransferCommand(Action.SEND, id="w", bypass=encode_bypass("w", "sesame")).param(), "%1")
    assert not any(isinstance(effect, Question) for effect in effects)
    assert _statuses(effects) == ["OK"]


def test_a_wrong_bypass_refuses_without_a_question(tmp_path: Path):
    "As kitty does."
    transfers = Transfers(tmp_path, lambda paths: True, bypass=lambda: "sesame")
    effects = transfers.take(TransferCommand(Action.SEND, id="w", bypass=encode_bypass("w", "guess")).param(), "%1")
    assert not any(isinstance(effect, Question) for effect in effects)
    assert _statuses(effects) == ["EPERM:" + REFUSED]

    (tmp_path / "a.bin").write_bytes(b"x")
    effects = transfers.take(
        TransferCommand(Action.RECEIVE, id="r", size=1, bypass=encode_bypass("r", "guess")).param(), "%1"
    )
    effects += transfers.take(TransferCommand(Action.FILE, id="r", file_id="0", name="a.bin").param(), "%1")
    assert not any(isinstance(effect, Question) for effect in effects)
    assert _statuses(effects) == ["EPERM:" + REFUSED]
