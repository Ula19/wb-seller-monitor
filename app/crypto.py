"""Шифрование сессий аккаунтов WB в БД (Fernet, ключ ACCOUNTS_KEY в .env).

В storageState аккаунта живые куки и Bearer (localStorage wbx__tokenData):
утёкшая БД без ключа их не отдаёт. Ключ: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
"""
import json

from cryptography.fernet import Fernet

from app.config import settings


def _fernet() -> Fernet:
    if not settings.accounts_key:
        raise RuntimeError("не задан ACCOUNTS_KEY — сессии аккаунтов не зашифровать")
    return Fernet(settings.accounts_key.encode())


def encrypt_json(obj) -> bytes:
    return _fernet().encrypt(json.dumps(obj).encode())


def decrypt_json(blob: bytes):
    return json.loads(_fernet().decrypt(blob))


if __name__ == "__main__":  # self-check: туда-обратно, чужой ключ не читает
    from unittest.mock import patch

    state = {"cookies": [{"name": "wbx-validation-key", "value": "v"}],
             "origins": [{"localStorage": [{"name": "wbx__tokenData", "value": "t"}]}]}
    with patch.object(settings, "accounts_key", Fernet.generate_key().decode()):
        blob = encrypt_json(state)
        assert b"wbx__tokenData" not in blob and decrypt_json(blob) == state
    with patch.object(settings, "accounts_key", Fernet.generate_key().decode()):
        try:
            decrypt_json(blob)
            raise AssertionError("чужой ключ расшифровал")
        except Exception as e:
            assert type(e).__name__ == "InvalidToken", e
    print("ok")
