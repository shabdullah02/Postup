from datetime import timedelta

from django.core.cache import cache
from django.db.models import Q
from django.utils import timezone
from rest_framework import status
from rest_framework.exceptions import APIException

from accounts.models import SocialAccount
from billing.models import Subscription
from billing.plans import PLAN_LIMITS
from posts.models import Post


class PlanLimitExceeded(APIException):
    status_code = status.HTTP_402_PAYMENT_REQUIRED
    default_detail = "Your subscription does not allow this action."
    default_code = "plan_limit"


def get_subscription(user):
    try:
        return user.subscription
    except Subscription.DoesNotExist:
        raise PlanLimitExceeded("A subscription is required to use this feature.")


def ensure_subscription_active(user):
    subscription = get_subscription(user)
    if subscription.status not in {Subscription.Status.ACTIVE, Subscription.Status.TRIALING}:
        raise PlanLimitExceeded("An active or trialing subscription is required.")
    if subscription.current_period_end and subscription.current_period_end <= timezone.now():
        raise PlanLimitExceeded("Your subscription period has ended.")
    return subscription


def get_usage(user, metric):
    now = timezone.localtime()
    if metric == "connected_accounts":
        return SocialAccount.objects.filter(user=user).exclude(
            status=SocialAccount.Status.DISCONNECTED
        ).count()
    if metric == "posts_per_month":
        period_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        return Post.objects.filter(user=user).filter(
            Q(created_at__gte=period_start)
            | Q(posted_at__gte=period_start)
            | Q(status=Post.Status.SCHEDULED)
        ).count()
    if metric == "ai_generations_per_hour":
        return cache.get(f"postup:ai:{user.pk}:{int(timezone.now().timestamp() // 3600)}", 0)
    raise ValueError(f"Unknown plan metric: {metric}")


def ensure_within_plan_limit(user, metric, additional=1):
    subscription = ensure_subscription_active(user)
    limit = PLAN_LIMITS[subscription.plan][metric]
    usage = get_usage(user, metric)
    if usage + additional > limit:
        raise PlanLimitExceeded(f"Your {subscription.plan} plan limit for {metric} has been reached.")
    return subscription


def add_period(value, days):
    return (value or timezone.now()) + timedelta(days=days)