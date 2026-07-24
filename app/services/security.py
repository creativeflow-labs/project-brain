from __future__ import annotations

import json

from cryptography.fernet import Fernet

from app.config import get_settings

settings = get_settings()


def _fernet() -> Fernet:
    if not settings.token_encryption_key:
        raise RuntimeError("TOKEN_ENCRYPTION_KEY가 설정되지 않았습니다.")
    return Fernet(settings.token_encryption_key.encode("utf-8"))


def encrypt_json(payload: dict) -> str:
    return _fernet().encrypt(json.dumps(payload).encode("utf-8")).decode("utf-8")


def decrypt_json(payload: str) -> dict:
    return json.loads(_fernet().decrypt(payload.encode("utf-8")).decode("utf-8"))

