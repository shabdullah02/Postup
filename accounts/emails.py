import logging
from urllib.parse import quote

from django.conf import settings
from django.core.mail import send_mail

from accounts.email_tokens import make_verification_token


logger = logging.getLogger(__name__)


def send_email(user, subject, body):
    if not user.email:
        return False
    try:
        return bool(send_mail(subject, body, settings.DEFAULT_FROM_EMAIL, [user.email]))
    except Exception as exc:
        logger.warning("Email delivery failed (%s)", type(exc).__name__)
        return False


def send_account_email(user, subject, body):
    return send_email(user, subject, body)


def send_verification_email(user):
    token = make_verification_token(user)
    link = f"{settings.FRONTEND_URL.rstrip('/')}/verify-email/{quote(token, safe='')}"
    return send_account_email(
        user,
        "Verify your Postup email address",
        f"Verify your email address within 24 hours using this link:\n{link}\n\n"
        "If you did not create this account, you can ignore this message.",
    )


def send_password_changed_email(user):
    return send_account_email(
        user,
        "Your Postup password was changed",
        "Your Postup account password was changed. If you did not make this change, reset your password immediately.",
    )


def send_password_reset_email(user, uid, token):
    link = (
        f"{settings.FRONTEND_URL.rstrip('/')}/accounts/password-reset"
        f"?uid={uid}&token={token}"
    )
    return send_account_email(
        user,
        "Reset your Postup password",
        f"Use this link to choose a new password:\n{link}\n\n"
        "If you did not request a password reset, you can ignore this message.",
    )


def send_payment_failed_email(user):
    return send_account_email(
        user,
        "Payment failed for your Postup subscription",
        "We could not process your latest subscription payment. Please update your payment method in your billing portal.",
    )


def send_trial_ending_email(user):
    return send_account_email(
        user,
        "Your Postup trial is ending soon",
        "Your Postup trial is ending soon. Choose a paid plan to keep using plan-gated features.",
    )


def send_renewal_reminder_email(user, days_remaining, current_period_end):
    return send_account_email(
        user,
        "Your Postup subscription renews soon",
        f"Your subscription period ends in {days_remaining} day(s), on {current_period_end:%Y-%m-%d}.",
    )