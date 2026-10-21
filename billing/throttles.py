import time

from django.core.cache import cache
from rest_framework.throttling import BaseThrottle

from billing.entitlements import ensure_subscription_active
from billing.plans import PLAN_LIMITS


class PlanAIGenerationThrottle(BaseThrottle):
    def allow_request(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return True

        subscription = ensure_subscription_active(request.user)
        limit = PLAN_LIMITS[subscription.plan]["ai_generations_per_hour"]
        window = int(time.time() // 3600)
        self.key = f"postup:ai:{request.user.pk}:{window}"
        self.limit = limit
        self.used = cache.get(self.key, 0)
        if self.used >= limit:
            self.duration = 3600 - int(time.time() % 3600)
            return False

        try:
            cache.add(self.key, 0, timeout=3600)
            cache.incr(self.key)
        except ValueError:
            cache.set(self.key, 1, timeout=3600)
        return True

    def wait(self):
        return getattr(self, "duration", None)