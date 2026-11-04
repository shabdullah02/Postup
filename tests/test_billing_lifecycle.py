from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.utils import timezone
from rest_framework.test import APIClient

from billing.models import AdminAuditLog, StripeEvent, Subscription
from billing.stripe_utils import process_stripe_event


pytestmark = pytest.mark.django_db


@pytest.fixture
def user():
    return get_user_model().objects.create_user(
        username="lifecycle-user", email="lifecycle@example.com"
    )


def client_for(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


@pytest.fixture
def staff_user():
    return get_user_model().objects.create_user(
        username="lifecycle-admin",
        email="lifecycle-admin@example.com",
        is_staff=True,
    )


def test_subscription_detail_returns_lifecycle_fields(user):
    response = client_for(user).get("/api/billing/subscription/")

    assert response.status_code == 200
    assert set(response.data) == {
        "plan", "status", "started_at", "current_period_end", "next_billing_date",
        "cancel_at_period_end", "trial_ends_at", "days_remaining",
    }
    assert response.data["cancel_at_period_end"] is False


def test_free_subscription_with_stale_cancellation_data_has_no_expiry_fields(user):
    subscription = user.subscription
    subscription.cancel_at_period_end = True
    subscription.current_period_end = timezone.now() + timedelta(days=30)
    subscription.next_billing_date = subscription.current_period_end
    subscription.save(
        update_fields=[
            "cancel_at_period_end",
            "current_period_end",
            "next_billing_date",
        ]
    )

    client = client_for(user)
    me = client.get("/api/billing/me/")
    details = client.get("/api/billing/subscription/")

    for data in (me.data, details.data):
        assert data["plan"] == Subscription.Plan.FREE
        assert data["cancel_at_period_end"] is False
        assert data["current_period_end"] is None
        assert data["next_billing_date"] is None
        assert data["days_remaining"] is None


def test_clean_free_subscriptions_resets_stale_billing_period(user):
    free_subscription = user.subscription
    free_subscription.cancel_at_period_end = True
    free_subscription.current_period_end = timezone.now() + timedelta(days=30)
    free_subscription.next_billing_date = free_subscription.current_period_end
    free_subscription.save()
    paid_user = get_user_model().objects.create_user(
        username="paid-lifecycle-user",
        email="paid-lifecycle@example.com",
    )
    paid_subscription = paid_user.subscription
    paid_subscription.plan = Subscription.Plan.PRO
    paid_subscription.cancel_at_period_end = True
    paid_subscription.current_period_end = timezone.now() + timedelta(days=30)
    paid_subscription.next_billing_date = paid_subscription.current_period_end
    paid_subscription.save()

    call_command("clean_free_subscriptions")

    free_subscription.refresh_from_db()
    paid_subscription.refresh_from_db()
    assert free_subscription.cancel_at_period_end is False
    assert free_subscription.current_period_end is None
    assert free_subscription.next_billing_date is None
    assert paid_subscription.cancel_at_period_end is True
    assert paid_subscription.current_period_end is not None


@pytest.mark.parametrize("provider", ["stripe"])
def test_cancel_is_admin_only_and_resume_requires_scheduled_end(
    user, staff_user, provider, settings
):
    settings.BILLING_PROVIDER = provider
    subscription = staff_user.subscription
    subscription.plan = Subscription.Plan.PRO
    subscription.status = Subscription.Status.ACTIVE
    subscription.current_period_end = timezone.now() + timedelta(days=30)
    subscription.stripe_subscription_id = "sub_lifecycle"
    subscription.save(
        update_fields=[
            "plan",
            "status",
            "current_period_end",
            "stripe_subscription_id",
        ]
    )
    stripe = SimpleNamespace(Subscription=SimpleNamespace(modify=Mock()))
    user_client = client_for(user)
    admin_client = client_for(staff_user)

    with patch("billing.views.get_stripe", return_value=stripe):
        user_cancel = user_client.post("/api/billing/cancel/")
        user_cancel_at_period_end = user_client.post(
            "/api/billing/cancel-at-period-end/"
        )
        resume_before_schedule = admin_client.post("/api/billing/resume/")
        cancel = admin_client.post("/api/billing/cancel/")
        subscription.refresh_from_db()
        scheduled = admin_client.get("/api/billing/me/")
        resume = admin_client.post("/api/billing/resume/")

    assert user_cancel.status_code == 403
    assert user_cancel_at_period_end.status_code == 403
    assert resume_before_schedule.status_code == 400
    assert cancel.status_code == 200
    assert scheduled.data["plan"] == Subscription.Plan.PRO
    assert scheduled.data["cancel_at_period_end"] is True
    assert scheduled.data["current_period_end"] is not None
    assert resume.status_code == 200
    assert stripe.Subscription.modify.call_args_list[0].kwargs == {
        "cancel_at_period_end": True,
    }
    assert stripe.Subscription.modify.call_args_list[1].kwargs == {
        "cancel_at_period_end": False,
    }
    subscription.refresh_from_db()
    assert subscription.cancel_at_period_end is False
    assert set(AdminAuditLog.objects.values_list("action", flat=True)) == {
        "subscription_cancel_at_period_end", "subscription_resumed",
    }


@pytest.mark.parametrize("provider", ["stripe"])
def test_change_plan_rejects_same_plan_and_waits_for_webhook(user, provider, settings):
    settings.BILLING_PROVIDER = provider
    subscription = user.subscription
    subscription.plan = Subscription.Plan.PRO
    subscription.stripe_subscription_id = "sub_lifecycle"
    subscription.save(update_fields=["plan", "stripe_subscription_id"])
    client = client_for(user)

    same = client.post("/api/billing/change-plan/", {"plan": "pro"}, format="json")
    assert same.status_code == 400

    stripe_subscription = SimpleNamespace(
        retrieve=Mock(return_value={"items": {"data": [{"id": "si_item"}]}}),
        modify=Mock(),
    )
    stripe = SimpleNamespace(Subscription=stripe_subscription)
    with patch("billing.views.get_stripe", return_value=stripe), patch(
        "billing.views.settings.STRIPE_AGENCY_PRICE_ID", "price_agency"
    ):
        changed = client.post(
            "/api/billing/change-plan/", {"plan": "agency"}, format="json"
        )

    assert changed.status_code == 200
    assert changed.data == {"plan": "pro", "requested_plan": "agency"}
    stripe_subscription.modify.assert_called_once_with(
        "sub_lifecycle",
        items=[{"id": "si_item", "price": "price_agency"}],
        proration_behavior="create_prorations",
    )
    subscription.refresh_from_db()
    assert subscription.plan == Subscription.Plan.PRO
    assert AdminAuditLog.objects.filter(action="subscription_plan_changed").exists()


@pytest.mark.parametrize("provider", ["stripe"])
def test_invoice_response_contains_only_allowed_fields(user, provider, settings):
    settings.BILLING_PROVIDER = provider
    user.subscription.stripe_customer_id = "cus_lifecycle"
    user.subscription.save(update_fields=["stripe_customer_id"])
    stripe = SimpleNamespace(Invoice=SimpleNamespace(list=Mock(return_value={
        "data": [{
            "id": "in_safe",
            "amount_paid": 1200,
            "currency": "usd",
            "status": "paid",
            "created": 123,
            "hosted_invoice_url": "https://invoice.example/in_safe",
            "customer_email": "private@example.com",
            "payment_intent": "pi_private",
        }],
    })))
    with patch("billing.views.get_stripe", return_value=stripe):
        response = client_for(user).get("/api/billing/invoices/")

    assert response.status_code == 200
    assert response.data["results"] == [{
        "id": "in_safe",
        "amount": 1200,
        "currency": "usd",
        "status": "paid",
        "date": 123,
        "hosted_invoice_url": "https://invoice.example/in_safe",
    }]


def test_stripe_trial_and_payment_events_notify_once(user):
    subscription = user.subscription
    subscription.stripe_subscription_id = "sub_lifecycle"
    subscription.save(update_fields=["stripe_subscription_id"])
    trial_event = {
        "id": "evt_trial_end",
        "type": "customer.subscription.trial_will_end",
        "data": {"object": {"id": "sub_lifecycle", "customer": "cus_lifecycle"}},
    }
    failed_event = {
        "id": "evt_payment_failed",
        "type": "invoice.payment_failed",
        "data": {"object": {"subscription": "sub_lifecycle", "customer": "cus_lifecycle"}},
    }
    with patch("accounts.emails.send_trial_ending_email") as trial_email, patch(
        "accounts.emails.send_payment_failed_email"
    ) as payment_email:
        assert process_stripe_event(trial_event) is True
        assert process_stripe_event(trial_event) is False
        assert process_stripe_event(failed_event) is True

    assert trial_email.call_count == 1
    payment_email.assert_called_once_with(user)
    assert StripeEvent.objects.count() == 2
    subscription.refresh_from_db()
    assert subscription.status == Subscription.Status.PAST_DUE


def test_expire_command_leaves_subscription_state_to_webhooks(user):
    subscription = user.subscription
    subscription.plan = Subscription.Plan.PRO
    subscription.status = Subscription.Status.ACTIVE
    subscription.current_period_end = timezone.now() - timedelta(days=1)
    subscription.save()

    call_command("expire_subscriptions")

    subscription.refresh_from_db()
    assert subscription.plan == Subscription.Plan.PRO
    assert subscription.status == Subscription.Status.ACTIVE
    assert subscription.current_period_end is not None