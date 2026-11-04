from django.core import signing


EMAIL_VERIFICATION_SALT = "accounts.email-verification"
EMAIL_VERIFICATION_MAX_AGE = 24 * 60 * 60


def make_verification_token(user):
    return signing.dumps(
        {"user_id": user.pk, "email": user.email},
        salt=EMAIL_VERIFICATION_SALT,
    )


def read_verification_token(token):
    return signing.loads(
        token,
        salt=EMAIL_VERIFICATION_SALT,
        max_age=EMAIL_VERIFICATION_MAX_AGE,
    )