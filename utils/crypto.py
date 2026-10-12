import os
from cryptography.fernet import Fernet


def _fernet():
    key = os.environ.get("TOKEN_ENCRYPTION_KEY")
    if not key:
        raise RuntimeError("TOKEN_ENCRYPTION_KEY is not set")
    return Fernet(key.encode())


def encrypt_token(plaintext: str) -> bytes:
    return _fernet().encrypt(plaintext.encode())


def decrypt_token(ciphertext: bytes) -> str:
    return _fernet().decrypt(ciphertext).decode()
