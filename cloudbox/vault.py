"""Secret handling: instance passwords and node shared secrets at rest."""

from __future__ import annotations

import os
import secrets
import string
import stat
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

PASSWORD_ALPHABET = string.ascii_letters + string.digits


class Vault:
    """Small wrapper around Fernet with a key file created on first run.

    Passwords have to be shown back to their owner (this is a root shell, not a
    website login), so they cannot be hashed. Encrypting them at rest at least
    keeps a stolen database backup from handing over every container.
    """

    def __init__(self, key_path: Path) -> None:
        self.key_path = key_path
        self._fernet = Fernet(self._load_key(key_path))

    @staticmethod
    def _load_key(path: Path) -> bytes:
        if path.exists():
            return path.read_bytes().strip()
        key = Fernet.generate_key()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(key)
        try:
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
        return key

    def seal(self, plaintext: str) -> str:
        if not plaintext:
            return ""
        return self._fernet.encrypt(plaintext.encode()).decode()

    def open(self, token: str) -> str:
        if not token:
            return ""
        try:
            return self._fernet.decrypt(token.encode()).decode()
        except InvalidToken:
            return ""


def new_password(length: int = 18) -> str:
    return "".join(secrets.choice(PASSWORD_ALPHABET) for _ in range(length))


def new_node_secret() -> str:
    return secrets.token_urlsafe(32)
