import secrets
from datetime import timedelta

import pyotp
from django.contrib.auth.hashers import check_password, make_password
from django.core.cache import cache
from django.utils import timezone

from accounts.utils.crypto import decrypt_token, encrypt_token


PENDING_LOGIN_KEY = "pending_2fa_user_id"
PENDING_LOGIN_EXPIRES_KEY = "pending_2fa_expires_at"
RECOVERY_CODE_COUNT = 10
TOTP_FAILURE_LIMIT = 5
TOTP_FAILURE_WINDOW = 15 * 60


def get_totp_secret(profile):
    if not profile.totp_secret_encrypted:
        return None
    return decrypt_token(profile.totp_secret_encrypted)


def store_totp_secret(profile, secret):
    profile.totp_secret_encrypted = encrypt_token(secret)
    profile.save(update_fields=["totp_secret_encrypted"])


def verify_totp(profile, code):
    if not isinstance(code, str):
        return False
    secret = get_totp_secret(profile)
    return bool(secret and pyotp.TOTP(secret).verify(code, valid_window=1))


def generate_recovery_codes():
    plaintext_codes = [secrets.token_hex(5).upper() for _ in range(RECOVERY_CODE_COUNT)]
    return plaintext_codes, [make_password(code) for code in plaintext_codes]


def consume_recovery_code(profile, code):
    if not isinstance(code, str):
        return False
    for index, encoded in enumerate(profile.recovery_codes):
        if check_password(code, encoded):
            profile.recovery_codes.pop(index)
            profile.save(update_fields=["recovery_codes"])
            return True
    return False


def totp_failure_key(user_id, ip_address):
    return f"postup:2fa-fail:{user_id}:{ip_address}"


def record_totp_failure(user_id, ip_address):
    key = totp_failure_key(user_id, ip_address)
    attempts = cache.get(key, 0) + 1
    cache.set(key, attempts, TOTP_FAILURE_WINDOW)
    return attempts


def totp_attempts_exhausted(user_id, ip_address):
    return cache.get(totp_failure_key(user_id, ip_address), 0) >= TOTP_FAILURE_LIMIT


def clear_totp_failures(user_id, ip_address):
    cache.delete(totp_failure_key(user_id, ip_address))


def pending_login_expiry():
    return (timezone.now() + timedelta(minutes=5)).isoformat()