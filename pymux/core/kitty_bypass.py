"""
The key a pane's `kitten transfer --permissions-bypass` encrypts its
password to, and the server's reading of what it sent. Lillecarl/pyterm#919.

The kitten sends a password only encrypted, as kitty's `kitty-1`: X25519
with the key in `KITTY_PUBLIC_KEY`, SHA-256 of the shared secret as an
AES-256-GCM key, and `timestamp:id;password` inside (kitty's
`tools/crypto`). Every pane gets this server's key. The server checks
the timestamp and passes the client only the protocol's `sha256:` form,
which the client compares with `file-transfer-bypass` from its own
configuration file. So the password never leaves this process.

The key lives in a file beside the socket, readable by its owner
alone, so a server after an upgrade still holds the key its panes were
given.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from contextlib import suppress
from pathlib import Path
from typing import Final

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat

__all__ = ["BypassKey", "bypass_matches", "encode_bypass"]

#: kitty's prefix of an encrypted bypass, and of its public key's protocol.
ENCRYPTED: Final = "kitty-1:"
_PROTOCOL: Final = "1"
#: The protocol's plain form, which the client reads.
SHA256: Final = "sha256:"
#: How far from now kitty lets the timestamp of a bypass be.
MAX_SKEW_NS: Final = 5 * 60 * 10**9

_KEY_BYTES: Final = 32


def encode_bypass(request_id: str, password: str) -> str:
    "kitty's `encode_bypass`: the SHA-256 of `id;password`."
    return SHA256 + hashlib.sha256((request_id + ";" + password).encode("utf-8", "replace")).hexdigest()


def bypass_matches(password: str, request_id: str, bypass: str) -> bool:
    "Whether `bypass` is the session's, under `password`. No password lets nothing pass, as in kitty."
    if not password or not bypass.startswith(SHA256):
        return False
    return hmac.compare_digest(encode_bypass(request_id, password), bypass)


class BypassKey:
    "The server's X25519 key, from its file or new."

    def __init__(self, path: Path | None) -> None:
        self._private = _load(path) if path is not None else None
        if self._private is None:
            self._private = X25519PrivateKey.generate()
            if path is not None:
                _store(path, self._private)
        raw = self._private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        #: What a pane gets as `KITTY_PUBLIC_KEY`.
        self.public = "%s:%s" % (_PROTOCOL, base64.b85encode(raw).decode("ascii"))

    def plain(self, request_id: str, bypass: str, now_ns: int | None = None) -> str:
        """
        The `sha256:` form of a session's bypass, or `""` for one that
        does not open: a blob for another key, a timestamp more than five
        minutes off, or a password for another session. A `sha256:` form
        passes as it came.
        """
        if bypass.startswith(SHA256):
            return bypass
        if not bypass.startswith(ENCRYPTED):
            return ""
        try:
            sealed = json.loads(bypass[len(ENCRYPTED) :])
            if sealed.get("enc_proto", _PROTOCOL) != _PROTOCOL:
                return ""
            theirs = X25519PublicKey.from_public_bytes(base64.b85decode(sealed["pubkey"]))
            assert self._private is not None
            key = hashlib.sha256(self._private.exchange(theirs)).digest()
            opened = AESGCM(key).decrypt(
                base64.b85decode(sealed["iv"]),
                base64.b85decode(sealed["encrypted"]) + base64.b85decode(sealed["tag"]),
                None,
            )
            stamp, _colon, payload = opened.decode("utf-8").partition(":")
            now = time.time_ns() if now_ns is None else now_ns
            if abs(now - int(stamp)) > MAX_SKEW_NS:
                return ""
        except Exception:
            return ""
        session, _semicolon, password = payload.partition(";")
        if session != request_id:
            return ""
        return encode_bypass(request_id, password)


def _load(path: Path) -> X25519PrivateKey | None:
    with suppress(OSError, ValueError):
        raw = path.read_bytes()
        if len(raw) == _KEY_BYTES:
            return X25519PrivateKey.from_private_bytes(raw)
    return None


def _store(path: Path, key: X25519PrivateKey) -> None:
    raw = key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    with suppress(OSError):
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "wb") as file:
            file.write(raw)
