from rest_framework.exceptions import PermissionDenied

from accounts.models import get_profile
from billing.entitlements import is_exempt


def ensure_email_verified(user):
    if is_exempt(user):
        return
    verified = get_profile(user).email_verified
    if not verified:
        raise PermissionDenied(
            "Verify your email address before using this feature.",
            code="email_not_verified",
        )