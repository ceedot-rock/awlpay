"""Encrypted local wallet storage for the awlpay SDK.

The wallet file lives at ``~/.awlpay/wallets.enc``. The password is used
only to derive an AES-256-GCM key (PBKDF2-HMAC-SHA256, 600k iterations)
and is never written to disk. Private keys are never printed or logged.
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from .errors import AwlPayError

WALLET_DIR = Path.home() / ".awlpay"
WALLET_FILE = WALLET_DIR / "wallets.enc"

_PBKDF2_ITERATIONS = 600_000
_VERSION = 1


class WrongPassword(AwlPayError):
    """The password did not decrypt the wallet file."""


def _derive_key(password: str, salt: bytes) -> bytes:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=_PBKDF2_ITERATIONS,
    )
    return kdf.derive(password.encode("utf-8"))


def _b64e(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def _b64d(s: str) -> bytes:
    return base64.b64decode(s.encode("ascii"))


def save_wallet(payload: dict, password: str, path: Path = WALLET_FILE) -> None:
    """Encrypt ``payload`` and write it to ``path`` (0600 permissions)."""
    salt = os.urandom(16)
    nonce = os.urandom(12)
    key = _derive_key(password, salt)
    plaintext = json.dumps(payload).encode("utf-8")
    ciphertext = AESGCM(key).encrypt(nonce, plaintext, None)
    envelope = {
        "version": _VERSION,
        "kdf": "pbkdf2-sha256-600k",
        "salt": _b64e(salt),
        "nonce": _b64e(nonce),
        "ciphertext": _b64e(ciphertext),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    # Write atomically-ish: temp file then rename, locked-down perms.
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(envelope), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def load_wallet(password: str, path: Path = WALLET_FILE) -> dict:
    """Decrypt and return the wallet payload. Raises WrongPassword."""
    envelope = json.loads(path.read_text(encoding="utf-8"))
    key = _derive_key(password, _b64d(envelope["salt"]))
    try:
        plaintext = AESGCM(key).decrypt(
            _b64d(envelope["nonce"]), _b64d(envelope["ciphertext"]), None
        )
    except InvalidTag as exc:
        raise WrongPassword(
            "Could not decrypt the wallet file: wrong password (or the file "
            "is corrupted)."
        ) from exc
    return json.loads(plaintext.decode("utf-8"))


def wallet_exists(path: Path = WALLET_FILE) -> bool:
    return path.exists()
