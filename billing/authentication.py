from rest_framework.authentication import SessionAuthentication
from rest_framework.exceptions import AuthenticationFailed

from billing.models import Subscription


class SubscriptionSessionAuthentication(SessionAuthentication):
    def authenticate(self, request):
        result = super().authenticate(request)
        if result is None:
            return None

        user, auth = result
        if not user.is_active:
            raise AuthenticationFailed("This account is deactivated.", code="account_deactivated")

        try:
            subscription = user.subscription
        except Subscription.DoesNotExist as exc:
            raise AuthenticationFailed(
                "This account has no subscription.", code="subscription_required"
            ) from exc
        if subscription.status == "suspended":
            raise AuthenticationFailed("This account is suspended.", code="account_suspended")
        return user, auth