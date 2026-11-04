from rest_framework.throttling import ScopedRateThrottle

from billing.entitlements import is_exempt


class SuperuserExemptScopedRateThrottle(ScopedRateThrottle):
    def allow_request(self, request, view):
        if is_exempt(getattr(request, "user", None)):
            return True
        return super().allow_request(request, view)