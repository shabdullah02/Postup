from datetime import datetime, timezone

from django.conf import settings
from django.utils import timezone as django_timezone
from rest_framework import status
from rest_framework.exceptions import APIException

from billing.models import StripeEvent, Subscription


class StripeConfigurationError(APIException):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    default_detail = "Billing is not configured."
    default_code = "billing_unavailable"


def get_stripe():
    if not settings.STRIPE_SECRET_KEY:
        raise StripeConfigurationError()
    import stripe

    stripe.api_key = settings.STRIPE_SECRET_KEY
    return stripe


def _get(data, key, default=None):
    if isinstance(data, dict):
        return data.get(key, default)
    getter = getattr(data, "get", None)
    return getter(key, default) if getter else getattr(data, key, default)


def _period_end(subscription_object):
    timestamp = _get(subscription_object, "current_period_end")
    if timestamp:
        return datetime.fromtimestamp(timestamp, tz=timezone.utc)
    return None


def _price_plan(subscription_object):
    items = _get(_get(subscription_object, "items", {}), "data", []) or []
    price_id = None
    if items:
        price_id = _get(_get(items[0], "price", {}), "id")
    if price_id and price_id == settings.STRIPE_PRO_PRICE_ID:
        return Subscription.Plan.PRO
    if price_id and price_id == settings.STRIPE_AGENCY_PRICE_ID:
        return Subscription.Plan.AGENCY
    return None


def _subscription_for_event(subscription_object):
    subscription_id = _get(subscription_object, "id")
    customer_id = _get(subscription_object, "customer")
    metadata = _get(subscription_object, "metadata", {}) or {}
    user_id = _get(metadata, "user_id")
    query = Subscription.objects.all()
    if subscription_id:
        found = query.filter(stripe_subscription_id=subscription_id).first()
        if found:
            return found
    if customer_id:
        found = query.filter(stripe_customer_id=customer_id).first()
        if found:
            return found
    if user_id:
        return query.filter(user_id=user_id).first()
    return None


def _apply_subscription_event(event_type, event_object):
    subscription = _subscription_for_event(event_object)
    if not subscription:
        return

    stripe_status = _get(event_object, "status")
    if event_type.endswith(".deleted"):
        subscription.status = Subscription.Status.CANCELED
    elif stripe_status in {"trialing", "active", "past_due", "canceled"}:
        subscription.status = stripe_status
    elif stripe_status in {"unpaid", "paused", "incomplete_expired"}:
        subscription.status = Subscription.Status.SUSPENDED
    subscription_id = _get(event_object, "id")
    customer_id = _get(event_object, "customer")
    plan = _price_plan(event_object)
    if subscription_id:
        subscription.stripe_subscription_id = subscription_id
    if customer_id:
        subscription.stripe_customer_id = customer_id
    if plan:
        subscription.plan = plan
    period_end = _period_end(event_object)
    if period_end:
        subscription.current_period_end = period_end
    subscription.save()


def _apply_invoice_event(event_type, invoice):
    customer_id = _get(invoice, "customer")
    subscription_id = _get(invoice, "subscription")
    if not subscription_id:
        parent = _get(invoice, "parent", {}) or {}
        details = _get(parent, "subscription_details", {}) or {}
        subscription_id = _get(details, "subscription")
    query = Subscription.objects.all()
    subscription = None
    if subscription_id:
        subscription = query.filter(stripe_subscription_id=subscription_id).first()
    if subscription is None and customer_id:
        subscription = query.filter(stripe_customer_id=customer_id).first()
    if subscription is None:
        return

    if event_type == "invoice.payment_failed":
        subscription.status = Subscription.Status.PAST_DUE
    elif event_type in {"invoice.paid", "invoice.payment_succeeded"}:
        if subscription.status != Subscription.Status.CANCELED:
            subscription.status = Subscription.Status.ACTIVE
    if subscription_id:
        subscription.stripe_subscription_id = subscription_id
    subscription.save()


def process_stripe_event(event):
    event_id = _get(event, "id")
    event_type = _get(event, "type", "")
    if not event_id:
        raise ValueError("Stripe event has no id.")

    event_row, created = StripeEvent.objects.get_or_create(
        event_id=event_id,
        defaults={"event_type": event_type},
    )
    if not created and event_row.processed_at:
        return False

    data = _get(event, "data", {}) or {}
    event_object = _get(data, "object", {}) or {}
    if event_type.startswith("customer.subscription."):
        _apply_subscription_event(event_type, event_object)
    elif event_type.startswith("invoice."):
        _apply_invoice_event(event_type, event_object)

    event_row.processed_at = django_timezone.now()
    event_row.save(update_fields=["processed_at"])
    return True