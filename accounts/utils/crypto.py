from cryptography.fernet import Fernet
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured


def get_fernet():
    key = getattr(settings, "TOKEN_ENCRYPTION_KEY", None)
    if not key:
        raise ImproperlyConfigured("TOKEN_ENCRYPTION_KEY is missing. Set it in the environment or .env file.")
    return Fernet(key.encode())


def encrypt_token(token: str) -> bytes:
    if token is None:
        raise ValueError("token cannot be None")
    return get_fernet().encrypt(token.encode())


def decrypt_token(token_encrypted: bytes) -> str:
    if token_encrypted is None:
        raise ValueError("token_encrypted cannot be None")
    return get_fernet().decrypt(token_encrypted).decode()
