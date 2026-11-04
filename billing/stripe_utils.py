from datetime import datetime, timezone

from django.conf import settings
from django.core.mail import send_mail
from django.db import transaction
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
        price = _get(items[0], "price", {})
        price_id = price if isinstance(price, str) else _get(price, "id")
    return _plan_for_price_id(price_id)


def _plan_for_price_id(price_id):
    pro_price_ids = {
        settings.STRIPE_PRICE_ID_PRO,
        settings.STRIPE_PRO_PRICE_ID,
    }
    if price_id and price_id in pro_price_ids:
        return Subscription.Plan.PRO
    if price_id and price_id == settings.STRIPE_AGENCY_PRICE_ID:
        return Subscription.Plan.AGENCY
    return None


def _invoice_price_plan(invoice):
    lines = _get(_get(invoice, "lines", {}), "data", []) or []
    for line in lines:
        price = _get(line, "price")
        if not price:
            details = _get(_get(line, "pricing", {}), "price_details", {}) or {}
            price = _get(details, "price")
        price_id = price if isinstance(price, str) else _get(price, "id")
        plan = _plan_for_price_id(price_id)
        if plan:
            return plan
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
        subscription.plan = Subscription.Plan.FREE
    elif stripe_status in {"trialing", "active", "incomplete", "past_due", "canceled"}:
        subscription.status = stripe_status
        if stripe_status == "canceled":
            subscription.plan = Subscription.Plan.FREE
    elif stripe_status in {"unpaid", "paused", "incomplete_expired"}:
        subscription.status = Subscription.Status.SUSPENDED
    subscription_id = _get(event_object, "id")
    customer_id = _get(event_object, "customer")
    plan = _price_plan(event_object)
    if subscription_id:
        subscription.stripe_subscription_id = subscription_id
    if customer_id:
        subscription.stripe_customer_id = customer_id
    if plan and stripe_status in {"active", "trialing"}:
        subscription.plan = plan
    period_end = _period_end(event_object)
    if period_end:
        subscription.current_period_end = period_end
        subscription.next_billing_date = period_end
    trial_end = _get(event_object, "trial_end")
    if trial_end:
        subscription.trial_ends_at = datetime.fromtimestamp(trial_end, tz=timezone.utc)
    cancel_at_period_end = _get(event_object, "cancel_at_period_end")
    if cancel_at_period_end is not None:
        subscription.cancel_at_period_end = bool(cancel_at_period_end)
    canceled_at = _get(event_object, "canceled_at")
    if canceled_at:
        subscription.canceled_at = datetime.fromtimestamp(canceled_at, tz=timezone.utc)
    if event_type.endswith(".deleted"):
        subscription.canceled_at = subscription.canceled_at or django_timezone.now()
        subscription.cancel_at_period_end = False
    if subscription.started_at is None:
        subscription.started_at = django_timezone.now()
    subscription.save()
    if event_type == "customer.subscription.trial_will_end":
        from accounts.emails import send_trial_ending_email

        send_trial_ending_email(subscription.user)


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
        if subscription.status != Subscription.Status.CANCELED:
            subscription.status = Subscription.Status.PAST_DUE
            from accounts.emails import send_payment_failed_email

            send_payment_failed_email(subscription.user)
    elif event_type in {"invoice.paid", "invoice.payment_succeeded"}:
        if subscription.status != Subscription.Status.CANCELED:
            subscription.status = Subscription.Status.ACTIVE
            plan = _invoice_price_plan(invoice)
            if plan:
                subscription.plan = plan
        paid_at = _get(invoice, "status_transitions", {}) or {}
        paid_at = _get(paid_at, "paid_at") or _get(invoice, "created")
        if paid_at:
            subscription.last_payment_at = datetime.fromtimestamp(paid_at, tz=timezone.utc)
    if subscription_id:
        subscription.stripe_subscription_id = subscription_id
    period_end = _get(invoice, "period_end")
    if not period_end:
        lines = _get(_get(invoice, "lines", {}), "data", []) or []
        period_ends = [
            _get(_get(line, "period", {}) or {}, "end")
            for line in lines
            if _get(_get(line, "period", {}) or {}, "end")
        ]
        if period_ends:
            period_end = max(period_ends)
    if period_end:
        subscription.current_period_end = datetime.fromtimestamp(period_end, tz=timezone.utc)
        subscription.next_billing_date = subscription.current_period_end
    subscription.save()


def _apply_setup_intent_event(setup_intent):
    customer_id = _get(setup_intent, "customer")
    payment_method_id = _get(setup_intent, "payment_method")
    if not customer_id or not payment_method_id:
        return
    if not isinstance(payment_method_id, str):
        payment_method_id = _get(payment_method_id, "id")
    if not payment_method_id:
        return
    if not Subscription.objects.filter(stripe_customer_id=customer_id).exists():
        return

    stripe = get_stripe()
    stripe.Customer.modify(
        customer_id,
        invoice_settings={"default_payment_method": payment_method_id},
    )


@transaction.atomic
def process_stripe_event(event):
    event_id = _get(event, "id")
    event_type = _get(event, "type", "")
    if not event_id:
        raise ValueError("Stripe event has no id.")

    event_row, created = StripeEvent.objects.select_for_update().get_or_create(
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
    elif event_type == "setup_intent.succeeded":
        _apply_setup_intent_event(event_object)

    event_row.processed_at = django_timezone.now()
    event_row.save(update_fields=["processed_at"])
    return True