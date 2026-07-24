from __future__ import annotations

import json
import os
from pathlib import Path

from cryptography.fernet import Fernet

from app.config import get_settings

settings = get_settings()


def _load_or_create_file_key(path: Path) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        return path.read_bytes().strip()
    except FileNotFoundError:
        key = Fernet.generate_key()
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            return path.read_bytes().strip()
        with os.fdopen(descriptor, "wb") as key_file:
            key_file.write(key)
        return key


def _fernet() -> Fernet:
    key = (
        settings.token_encryption_key.encode("utf-8")
        if settings.token_encryption_key
        else _load_or_create_file_key(settings.token_key_file)
    )
    return Fernet(key)


def encrypt_json(payload: dict) -> str:
    return _fernet().encrypt(json.dumps(payload).encode("utf-8")).decode("utf-8")


def decrypt_json(payload: str) -> dict:
    return json.loads(_fernet().decrypt(payload.encode("utf-8")).decode("utf-8"))
