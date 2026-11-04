from datetime import timedelta
import os
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
import stripe
from django.contrib.auth import get_user_model
from django.contrib.sessions.models import Session
from django.conf import settings
from django.core.cache import cache
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.billing.gateway import (
    PaymentGatewayError,
    _card_brand,
    _luhn_valid,
    process_payment,
    validate_card,
)
from accounts.models import SocialAccount
from billing.models import AdminAuditLog, StripeEvent, Subscription
from billing.stripe_utils import process_stripe_event
from posts.models import Post


pytestmark = pytest.mark.django_db


@pytest.fixture
def user():
    user = get_user_model().objects.create_user(
        username="billing-user",
        password="Valid-password-for-billing-6!",
        email="billing@example.com",
    )
    user.profile.email_verified = True
    user.profile.save(update_fields=["email_verified"])
    return user


@pytest.fixture
def staff_user():
    return get_user_model().objects.create_user(
        username="billing-admin",
        password="Valid-password-for-admin-6!",
        is_staff=True,
    )


@pytest.fixture
def superuser():
    return get_user_model().objects.create_superuser(
        username="billing-root",
        password="Valid-password-for-root-6!",
        email="root@example.com",
    )


def make_client(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def valid_mock_card(number="4242 4242 4242 4242", cvv="123"):
    return {
        "card_number": number,
        "exp_month": 12,
        "exp_year": timezone.localdate().year + 1,
        "cvv": cvv,
    }


def test_mock_card_validation_luhn_expiry_and_cvv():
    assert _luhn_valid("4242424242424242")
    assert not _luhn_valid("4242424242424241")
    assert _card_brand("4242424242424242") == "Visa"
    assert _card_brand("5555555555554444") == "Mastercard"
    assert _card_brand("378282246310005") == "Amex"
    assert _card_brand("6011111111111117") == "Discover"
    assert _card_brand("3530111333300000") == "Other"

    expired = valid_mock_card()
    expired["exp_month"] = 1
    expired["exp_year"] = 2000
    _, errors = validate_card(expired)
    assert "expiry" in errors

    bad_month = valid_mock_card()
    bad_month["exp_month"] = 13
    _, errors = validate_card(bad_month)
    assert "expiry" in errors

    wrong_visa_cvv = valid_mock_card(cvv="1234")
    _, errors = validate_card(wrong_visa_cvv)
    assert "cvv" in errors

    wrong_amex_cvv = valid_mock_card("3782 822463 10005", cvv="123")
    _, errors = validate_card(wrong_amex_cvv)
    assert "cvv" in errors

    valid_amex = valid_mock_card("3782 822463 10005", cvv="1234")
    validated, errors = validate_card(valid_amex)
    assert not errors
    assert validated["brand"] == "Amex"

@override_settings(BILLING_PROVIDER="mock")
def test_payment_gateway_uses_configured_mock_provider(user):
    with patch("accounts.billing.gateway._process_mock_payment") as mock_payment, patch(
        "accounts.billing.gateway._process_stripe_payment"
    ) as stripe_payment:
        result = process_payment(user, {"card_number": "test"})

    assert result is mock_payment.return_value
    mock_payment.assert_called_once_with(user, {"card_number": "test"})
    stripe_payment.assert_not_called()


@override_settings(BILLING_PROVIDER="stripe")
def test_payment_gateway_uses_configured_stripe_provider(user):
    with patch("accounts.billing.gateway._process_mock_payment") as mock_payment, patch(
        "accounts.billing.gateway._process_stripe_payment"
    ) as stripe_payment:
        result = process_payment(user, {"setup_intent_id": "seti_test"})

    assert result is stripe_payment.return_value
    stripe_payment.assert_called_once_with(user, {"setup_intent_id": "seti_test"})
    mock_payment.assert_not_called()

@override_settings(BILLING_PROVIDER="unsupported")
def test_payment_gateway_rejects_unsupported_provider(user):
    with pytest.raises(PaymentGatewayError):
        process_payment(user, {})


@override_settings(BILLING_PROVIDER="mock")
def test_mock_subscribe_succeeds_and_stores_only_card_summary(user, caplog):
    cache.clear()
    card = valid_mock_card()
    pan = "4242424242424242"
    cvv = card["cvv"]

    response = make_client(user).post(
        "/api/billing/mock-subscribe/",
        card,
        format="json",
    )

    user.subscription.refresh_from_db()
    saved_data = Subscription.objects.filter(user=user).values().get()
    assert response.status_code == 200
    assert user.subscription.plan == Subscription.Plan.PRO
    assert user.subscription.status == Subscription.Status.ACTIVE
    assert user.subscription.current_period_end > timezone.now()
    assert user.subscription.card_brand == "Visa"
    assert user.subscription.card_last4 == "4242"
    assert user.subscription.card_exp_month == 12
    assert pan not in str(saved_data)
    persisted_card_summary = {
        key: saved_data[key]
        for key in ("card_brand", "card_last4", "card_exp_month", "card_exp_year")
    }
    assert cvv not in str(persisted_card_summary)
    assert not {"card_number", "cvv"} & set(saved_data)
    payment_method = make_client(user).get("/api/billing/payment-method/")
    assert payment_method.data == {
        "brand": "Visa",
        "last4": "4242",
        "exp_month": 12,
        "exp_year": card["exp_year"],
    }
    assert pan not in caplog.text
    assert cvv not in caplog.text
    assert pan not in str(response.data)
    assert cvv not in str(response.data)
    assert not make_client(user).session.get("card_number")

@override_settings(BILLING_PROVIDER="mock")
def test_mock_subscribe_can_select_agency_plan(user):
    cache.clear()
    card = valid_mock_card()
    card["plan"] = Subscription.Plan.AGENCY

    response = make_client(user).post(
        "/api/billing/mock-subscribe/",
        card,
        format="json",
    )

    user.subscription.refresh_from_db()
    assert response.status_code == 200
    assert response.data["plan"] == Subscription.Plan.AGENCY
    assert user.subscription.plan == Subscription.Plan.AGENCY


@override_settings(BILLING_PROVIDER="mock")
def test_mock_subscribe_rejects_free_plan_without_changing_saved_card(user):
    cache.clear()
    subscription = user.subscription
    subscription.card_brand = "Visa"
    subscription.card_last4 = "4242"
    subscription.card_exp_month = 8
    subscription.card_exp_year = timezone.localdate().year + 1
    subscription.save()

    card = valid_mock_card()
    card["plan"] = Subscription.Plan.FREE
    response = make_client(user).post(
        "/api/billing/mock-subscribe/",
        card,
        format="json",
    )

    subscription.refresh_from_db()
    assert response.status_code == 400
    assert response.data["code"] == "invalid_plan"
    assert subscription.card_brand == "Visa"
    assert subscription.card_last4 == "4242"


@override_settings(BILLING_PROVIDER="mock")
def test_decline_card_returns_safe_error(user):
    cache.clear()
    subscription = user.subscription
    subscription.card_brand = "Mastercard"
    subscription.card_last4 = "4444"
    subscription.card_exp_month = 10
    subscription.card_exp_year = timezone.localdate().year + 1
    subscription.save()
    card = valid_mock_card("4000 0000 0000 0002", cvv="123")
    response = make_client(user).post(
        "/api/billing/mock-subscribe/",
        card,
        format="json",
    )

    assert response.status_code == 400
    assert response.data["detail"] == "Card declined."
    assert response.data["code"] == "payment_failed"
    user.subscription.refresh_from_db()
    assert user.subscription.plan == Subscription.Plan.FREE
    assert user.subscription.card_brand == "Mastercard"
    assert user.subscription.card_last4 == "4444"
    assert "4000000000000002" not in str(response.data)
    assert "123" not in str(response.data)

@override_settings(BILLING_PROVIDER="mock")
def test_mock_gateway_accepts_valid_card_numbers_other_than_decline_card(user):
    card = valid_mock_card("5555 5555 5555 4444", cvv="123")

    response = make_client(user).post(
        "/api/billing/mock-subscribe/",
        card,
        format="json",
    )

    user.subscription.refresh_from_db()
    assert response.status_code == 200
    assert user.subscription.plan == Subscription.Plan.PRO
    assert user.subscription.card_brand == "Mastercard"
    assert user.subscription.card_last4 == "4444"


@override_settings(BILLING_PROVIDER="mock")
def test_payment_method_is_null_before_first_successful_mock_payment(user):
    response = make_client(user).get("/api/billing/payment-method/")

    assert response.status_code == 200
    assert response.data is None


@override_settings(BILLING_PROVIDER="mock")
def test_mock_subscribe_returns_field_errors_without_echoing_card(user):
    cache.clear()
    card = valid_mock_card("4242 4242 4242 4241", cvv="12")
    response = make_client(user).post(
        "/api/billing/mock-subscribe/",
        card,
        format="json",
    )

    assert response.status_code == 400
    assert response.data["errors"]["card_number"] == "Enter a valid card number."
    assert "cvv" in response.data["errors"]
    user.subscription.refresh_from_db()
    assert user.subscription.card_brand is None
    assert user.subscription.card_last4 is None
    assert user.subscription.card_exp_month is None
    assert user.subscription.card_exp_year is None
    assert "4242424242424241" not in str(response.data)
    assert "12" not in str(response.data)


@override_settings(BILLING_PROVIDER="mock")
def test_mock_routes_hide_stripe_endpoints_and_cancel_at_period_end(user):
    client = make_client(user)
    for path in (
        "/api/billing/setup-intent/",
        "/api/billing/subscribe/",
        "/api/billing/webhook/",
        "/api/billing/checkout/",
        "/api/billing/portal/",
        "/api/billing/change-plan/",
        "/api/billing/resume/",
    ):
        response = client.post(path, {}, format="json")
        assert response.status_code == 404, path

    subscription = user.subscription
    subscription.status = Subscription.Status.ACTIVE
    subscription.plan = Subscription.Plan.PRO
    subscription.current_period_end = timezone.now() + timedelta(days=30)
    subscription.save(update_fields=["status", "plan", "current_period_end"])
    response = client.post("/api/billing/cancel/")

    subscription.refresh_from_db()
    assert response.status_code == 403
    assert subscription.cancel_at_period_end is False


@override_settings(BILLING_PROVIDER="mock")
def test_mock_invoice_list_returns_empty_results_without_subscription_row(user):
    Subscription.objects.filter(user=user).delete()

    response = make_client(user).get("/api/billing/invoices/")

    assert response.status_code == 200
    assert response.data == {"results": []}
    assert Subscription.objects.filter(user=user).exists()


@override_settings(BILLING_PROVIDER="mock")
def test_mock_billing_page_endpoints_return_2xx(user, staff_user):
    client = APIClient()
    client.force_login(user)
    statuses = {
        "me": client.get("/api/billing/me/").status_code,
        "subscription": client.get("/api/billing/subscription/").status_code,
        "payment-method": client.get("/api/billing/payment-method/").status_code,
        "invoices": client.get("/api/billing/invoices/").status_code,
        "mock-subscribe": client.post(
            "/api/billing/mock-subscribe/",
            valid_mock_card(),
            format="json",
        ).status_code,
    }
    subscription = staff_user.subscription
    subscription.plan = Subscription.Plan.PRO
    subscription.status = Subscription.Status.ACTIVE
    subscription.current_period_end = timezone.now() + timedelta(days=30)
    subscription.save(update_fields=["status", "plan", "current_period_end"])
    admin_client = make_client(staff_user)
    statuses["cancel"] = admin_client.post("/api/billing/cancel/").status_code

    print(f"Mock billing endpoint status codes: {statuses}")
    assert all(200 <= response_status < 300 for response_status in statuses.values())


def test_django_starts_with_no_stripe_environment_or_package():
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("STRIPE_")
    }
    from cryptography.fernet import Fernet

    env.update(
        {
            "BILLING_PROVIDER": "mock",
            "SECRET_KEY": "test-key",
            "TOKEN_ENCRYPTION_KEY": Fernet.generate_key().decode(),
            "FB_APP_ID": "123456",
            "FB_APP_SECRET": "test-app-secret",
            "ALLOWED_HOSTS": "localhost",
        }
    )
    script = """
import sys
class BlockStripe:
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "stripe" or fullname.startswith("stripe."):
            raise ModuleNotFoundError("Stripe is disabled in this test")
sys.meta_path.insert(0, BlockStripe())
import dotenv
dotenv.load_dotenv = lambda *args, **kwargs: None
import django
django.setup()
from django.urls import get_resolver
get_resolver().url_patterns
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=settings.BASE_DIR,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def activate_free_plan(user):
    subscription = user.subscription
    subscription.status = Subscription.Status.ACTIVE
    subscription.current_period_end = timezone.now() + timedelta(days=30)
    subscription.save()
    return subscription


def test_post_create_is_blocked_at_free_monthly_limit(user):
    subscription = activate_free_plan(user)
    account = SocialAccount.objects.create(
        user=user,
        provider=SocialAccount.Provider.FACEBOOK,
        external_id="billing-page",
        name="Billing Page",
        token_encrypted=b"encrypted",
    )
    Post.objects.bulk_create([
        Post(user=user, account=account, content=f"Existing post {number}")
        for number in range(10)
    ])

    response = make_client(user).post(
        "/api/posts/",
        {"account": account.pk, "content": "One too many"},
        format="json",
    )

    assert subscription.plan == Subscription.Plan.FREE
    assert response.status_code == 402
    assert response.data["code"] == "plan_limit"
    assert Post.objects.filter(user=user).count() == 10


def test_scheduling_old_draft_is_blocked_at_monthly_limit(user):
    activate_free_plan(user)
    account = SocialAccount.objects.create(
        user=user,
        provider=SocialAccount.Provider.FACEBOOK,
        external_id="schedule-limit-page",
        name="Schedule Limit Page",
        token_encrypted=b"encrypted",
    )
    Post.objects.bulk_create([
        Post(user=user, account=account, content=f"Existing post {number}")
        for number in range(10)
    ])
    old_draft = Post.objects.create(
        user=user, account=account, content="Old draft"
    )
    old_draft.created_at = timezone.now() - timedelta(days=40)
    old_draft.save(update_fields=["created_at"])

    response = make_client(user).post(
        f"/api/posts/{old_draft.pk}/schedule/",
        {"scheduled_at": (timezone.now() + timedelta(days=1)).isoformat()},
        format="json",
    )

    assert response.status_code == 402
    assert response.data["code"] == "plan_limit"
    old_draft.refresh_from_db()
    assert old_draft.status == Post.Status.DRAFT


def test_facebook_connect_is_blocked_at_free_account_limit(user):
    activate_free_plan(user)
    SocialAccount.objects.create(
        user=user,
        provider=SocialAccount.Provider.FACEBOOK,
        external_id="existing-page",
        name="Existing Page",
        token_encrypted=b"encrypted",
    )

    response = make_client(user).get("/api/auth/connect/facebook/")

    assert response.status_code == 402
    assert response.data["code"] == "plan_limit"


def test_non_staff_cannot_access_admin_api(user):
    response = make_client(user).get("/api/admin/users/")

    assert response.status_code == 403
    assert response.data["code"] == "permission_denied"


def test_admin_user_list_is_paginated_with_plan_and_status(staff_user, user):
    response = make_client(staff_user).get("/api/admin/users/?page_size=1")

    assert response.status_code == 200
    assert response.data["count"] == 2
    assert len(response.data["results"]) == 1
    assert response.data["next"] is not None
    assert {"plan", "status"}.issubset(response.data["results"][0])


def test_staff_cannot_suspend_superuser(staff_user, superuser):
    response = make_client(staff_user).patch(
        f"/api/admin/users/{superuser.pk}/subscription/",
        {"status": Subscription.Status.SUSPENDED},
        format="json",
    )

    assert response.status_code == 400
    assert response.data["code"] == "protected_user"
    assert response.data["detail"] == (
        "Superuser accounts cannot be suspended, canceled, deactivated or deleted from the admin panel."
    )


def test_staff_cannot_deactivate_superuser(staff_user, superuser):
    response = make_client(staff_user).post(
        f"/api/admin/users/{superuser.pk}/deactivate/"
    )

    assert response.status_code == 400
    assert response.data["code"] == "protected_user"
    superuser.refresh_from_db()
    assert superuser.is_active


def test_staff_cannot_delete_superuser(staff_user, superuser):
    response = make_client(staff_user).delete(
        f"/api/admin/users/{superuser.pk}/",
        {"confirm": superuser.username},
        format="json",
    )

    assert response.status_code == 400
    assert response.data["code"] == "protected_user"
    assert get_user_model().objects.filter(pk=superuser.pk).exists()


def test_admin_cannot_deactivate_themselves(staff_user):
    response = make_client(staff_user).post(
        f"/api/admin/users/{staff_user.pk}/deactivate/"
    )

    assert response.status_code == 400
    assert response.data["code"] == "protected_user"
    staff_user.refresh_from_db()
    assert staff_user.is_active


def test_normal_user_can_be_suspended_and_deactivated(staff_user, user):
    client = make_client(staff_user)
    suspend_response = client.patch(
        f"/api/admin/users/{user.pk}/subscription/",
        {"status": Subscription.Status.SUSPENDED},
        format="json",
    )
    deactivate_response = client.post(
        f"/api/admin/users/{user.pk}/deactivate/"
    )

    user.refresh_from_db()
    assert suspend_response.status_code == 200
    assert deactivate_response.status_code == 200
    assert not user.is_active
    assert user.subscription.status == Subscription.Status.SUSPENDED


def test_admin_cancel_resets_plan_and_subscription_fields(staff_user, user):
    subscription = user.subscription
    subscription.plan = Subscription.Plan.AGENCY
    subscription.status = Subscription.Status.ACTIVE
    subscription.current_period_end = timezone.now() + timedelta(days=30)
    subscription.next_billing_date = subscription.current_period_end
    subscription.cancel_at_period_end = True
    subscription.stripe_subscription_id = "sub_to_cancel"
    subscription.card_brand = "Visa"
    subscription.card_last4 = "4242"
    subscription.card_exp_month = 12
    subscription.card_exp_year = timezone.localdate().year + 1
    subscription.save()

    response = make_client(staff_user).patch(
        f"/api/admin/users/{user.pk}/subscription/",
        {"status": Subscription.Status.CANCELED},
        format="json",
    )

    subscription.refresh_from_db()
    assert response.status_code == 200
    assert subscription.status == Subscription.Status.CANCELED
    assert subscription.plan == Subscription.Plan.FREE
    assert subscription.current_period_end is None
    assert subscription.next_billing_date is None
    assert subscription.cancel_at_period_end is False
    assert subscription.canceled_at is not None
    assert subscription.stripe_subscription_id == ""
    assert subscription.card_brand is None
    assert subscription.card_last4 is None
    assert subscription.card_exp_month is None
    assert subscription.card_exp_year is None
    assert AdminAuditLog.objects.filter(
        who=staff_user, target=user, action="subscription_updated"
    ).exists()

    client = make_client(user)
    billing = client.get("/api/billing/me/")
    subscription_detail = client.get("/api/billing/subscription/")
    payment_method = client.get("/api/billing/payment-method/")
    assert billing.data["plan"] == Subscription.Plan.FREE
    assert billing.data["status"] == Subscription.Status.CANCELED
    assert billing.data["cancel_at_period_end"] is False
    assert billing.data["current_period_end"] is None
    assert billing.data["next_billing_date"] is None
    assert billing.data["days_remaining"] is None
    assert subscription_detail.data["plan"] == Subscription.Plan.FREE
    assert payment_method.data is None

    resubscribe = client.post(
        "/api/billing/mock-subscribe/",
        valid_mock_card("5555 5555 5555 4444"),
        format="json",
    )
    subscription.refresh_from_db()
    assert resubscribe.status_code == 200
    assert subscription.plan == Subscription.Plan.PRO
    assert subscription.status == Subscription.Status.ACTIVE


def test_admin_delete_requires_username_confirmation(staff_user, user):
    client = make_client(staff_user)
    actor_log = AdminAuditLog.objects.create(
        who=user,
        who_identifier=f"{user.pk}:{user.username}",
        target=staff_user,
        target_identifier=f"{staff_user.pk}:{staff_user.username}",
        action="prior_action",
    )

    rejected = client.delete(f"/api/admin/users/{user.pk}/")
    accepted = client.delete(
        f"/api/admin/users/{user.pk}/",
        {"confirm": user.username},
        format="json",
    )

    assert rejected.status_code == 400
    assert get_user_model().objects.filter(pk=user.pk).exists() is False
    assert accepted.status_code == 204
    assert AdminAuditLog.objects.filter(action="user_hard_deleted").exists()
    actor_log.refresh_from_db()
    assert actor_log.who is None
    assert actor_log.who_identifier == f"{user.pk}:{user.username}"


@override_settings(BILLING_PROVIDER="stripe")
def test_deactivate_user_cancels_local_resources_and_stripe(staff_user, user):
    subscription = user.subscription
    subscription.stripe_subscription_id = "sub_mocked"
    subscription.save(update_fields=["stripe_subscription_id", "updated_at"])
    account = SocialAccount.objects.create(
        user=user,
        provider=SocialAccount.Provider.FACEBOOK,
        external_id="deactivate-page",
        name="Deactivate Page",
        token_encrypted=b"encrypted-token",
    )
    post = Post.objects.create(
        user=user,
        account=account,
        content="Scheduled post",
        status=Post.Status.SCHEDULED,
        scheduled_at=timezone.now() + timedelta(days=1),
    )
    client = make_client(staff_user)
    session = client.session
    session["_auth_user_id"] = str(user.pk)
    session.save()
    target_session_key = session.session_key

    with patch("billing.admin_api._cancel_stripe_subscription") as cancel_stripe:
        response = client.post(f"/api/admin/users/{user.pk}/deactivate/")

    assert response.status_code == 200
    cancel_stripe.assert_called_once_with("sub_mocked")
    user.refresh_from_db()
    subscription.refresh_from_db()
    account.refresh_from_db()
    post.refresh_from_db()
    assert not user.is_active
    assert subscription.status == Subscription.Status.SUSPENDED
    assert account.status == SocialAccount.Status.DISCONNECTED
    assert bytes(account.token_encrypted) == b""
    assert post.status == Post.Status.DRAFT
    assert post.scheduled_at is None
    assert not Session.objects.filter(session_key=target_session_key).exists()
    assert AdminAuditLog.objects.filter(action="user_deactivated", target=user).exists()


def test_suspended_user_cannot_login_or_call_authenticated_api(user):
    user.subscription.status = Subscription.Status.SUSPENDED
    user.subscription.save(update_fields=["status", "updated_at"])

    login_response = APIClient().post(
        "/api/auth/login/",
        {"username": user.username, "password": "Valid-password-for-billing-6!"},
        format="json",
    )
    api_client = APIClient()
    api_client.force_login(user)
    api_response = api_client.get("/api/auth/me/")

    assert login_response.status_code == 403
    assert login_response.data["code"] == "account_suspended"
    assert api_response.status_code == 403
    assert api_response.data["code"] == "account_suspended"


@override_settings(BILLING_PROVIDER="stripe")
def test_webhook_rejects_invalid_signature():
    with override_settings(
        STRIPE_SECRET_KEY="sk_test_mocked",
        STRIPE_WEBHOOK_SECRET="whsec_mocked",
    ), patch(
        "stripe.Webhook.construct_event",
        side_effect=stripe.SignatureVerificationError("invalid", "invalid"),
    ):
        response = APIClient().post(
            "/api/billing/webhook/",
            data=b"{}",
            content_type="application/json",
            HTTP_STRIPE_SIGNATURE="invalid-signature",
        )

    assert response.status_code == 400
    assert response.data["code"] == "invalid_signature"
    assert not StripeEvent.objects.exists()


@override_settings(BILLING_PROVIDER="stripe")
def test_webhook_accepts_and_deduplicates_subscription_event(user):
    activate_free_plan(user)
    event = {
        "id": "evt_subscription_update",
        "type": "customer.subscription.updated",
        "data": {
            "object": {
                "id": "sub_stripe_test",
                "customer": "cus_stripe_test",
                "status": "active",
                "current_period_end": int((timezone.now() + timedelta(days=30)).timestamp()),
                "metadata": {"user_id": str(user.pk)},
                "items": {"data": [{"price": {"id": "price_pro_test"}}]},
            }
        },
    }
    replay = {
        **event,
        "data": {"object": {**event["data"]["object"], "status": "canceled"}},
    }
    with override_settings(
        BILLING_PROVIDER="stripe",
        STRIPE_SECRET_KEY="sk_test_mocked",
        STRIPE_WEBHOOK_SECRET="whsec_mocked",
        STRIPE_PRO_PRICE_ID="price_pro_test",
    ), patch("stripe.Webhook.construct_event", side_effect=[event, replay]) as construct:
        client = APIClient()
        first = client.post(
            "/api/billing/webhook/", data=b"{}", content_type="application/json",
            HTTP_STRIPE_SIGNATURE="valid-signature",
        )
        second = client.post(
            "/api/billing/webhook/", data=b"{}", content_type="application/json",
            HTTP_STRIPE_SIGNATURE="valid-signature",
        )

    user.subscription.refresh_from_db()
    assert first.status_code == 200
    assert second.status_code == 200
    assert second.data["duplicate"] is True
    assert user.subscription.plan == Subscription.Plan.PRO
    assert user.subscription.status == Subscription.Status.ACTIVE
    assert user.subscription.stripe_subscription_id == "sub_stripe_test"
    assert StripeEvent.objects.filter(event_id="evt_subscription_update").count() == 1
    assert construct.call_count == 2


def test_billing_me_reports_usage_and_limits(user):
    activate_free_plan(user)
    account = SocialAccount.objects.create(
        user=user,
        provider=SocialAccount.Provider.FACEBOOK,
        external_id="billing-me-page",
        name="Billing Me Page",
        token_encrypted=b"encrypted",
    )
    Post.objects.create(user=user, account=account, content="One post")

    response = make_client(user).get("/api/billing/me/")

    assert response.status_code == 200
    assert response.data["plan"] == Subscription.Plan.FREE
    assert response.data["usage"]["connected_accounts"] == {"used": 1, "limit": 1}
    assert response.data["usage"]["posts_per_month"] == {"used": 1, "limit": 10}


@override_settings(DEBUG=True)
def test_fake_test_subscribe_endpoint_is_removed(user):
    response = make_client(user).post(
        "/api/billing/test-subscribe/", {"plan": "pro"}, format="json"
    )

    user.subscription.refresh_from_db()
    assert response.status_code == 404
    assert user.subscription.plan == Subscription.Plan.FREE
    assert user.subscription.status == Subscription.Status.TRIALING


@override_settings(DEBUG=False)
def test_test_subscribe_returns_404_when_debug_is_disabled(user):
    response = make_client(user).post(
        "/api/billing/test-subscribe/", {"plan": "agency"}, format="json"
    )

    user.subscription.refresh_from_db()
    assert response.status_code == 404
    assert user.subscription.plan == Subscription.Plan.FREE
    assert user.subscription.status == Subscription.Status.TRIALING


@override_settings(
    BILLING_PROVIDER="stripe",
    STRIPE_SECRET_KEY="sk_test_mocked",
    STRIPE_PUBLISHABLE_KEY="pk_test_mocked",
)
def test_setup_intent_lazily_creates_customer(user):
    stripe_client = SimpleNamespace(
        Customer=SimpleNamespace(
            create=Mock(return_value=SimpleNamespace(id="cus_setup"))
        ),
        SetupIntent=SimpleNamespace(
            create=Mock(return_value=SimpleNamespace(
                id="seti_setup",
                client_secret="seti_secret",
            ))
        ),
    )
    with patch("billing.views.get_stripe", return_value=stripe_client):
        response = make_client(user).post("/api/billing/setup-intent/")

    user.subscription.refresh_from_db()
    assert response.status_code == 200
    assert response.data == {
        "setup_intent_id": "seti_setup",
        "client_secret": "seti_secret",
        "publishable_key": "pk_test_mocked",
    }
    assert user.subscription.stripe_customer_id == "cus_setup"
    stripe_client.SetupIntent.create.assert_called_once_with(
        customer="cus_setup",
        payment_method_types=["card"],
        usage="off_session",
    )


@override_settings(BILLING_PROVIDER="stripe")
def test_payment_method_returns_only_safe_card_details(user):
    user.subscription.plan = Subscription.Plan.PRO
    user.subscription.stripe_customer_id = "cus_card"
    user.subscription.save(update_fields=["plan", "stripe_customer_id"])
    stripe_client = SimpleNamespace(
        Customer=SimpleNamespace(
            retrieve=Mock(return_value={
                "invoice_settings": {"default_payment_method": "pm_card"},
            })
        ),
        PaymentMethod=SimpleNamespace(
            retrieve=Mock(return_value={
                "id": "pm_card",
                "card": {
                    "brand": "visa",
                    "last4": "4242",
                    "exp_month": 12,
                    "exp_year": 2030,
                    "fingerprint": "private",
                },
            })
        ),
    )
    with patch("billing.views.get_stripe", return_value=stripe_client):
        response = make_client(user).get("/api/billing/payment-method/")

    assert response.status_code == 200
    assert response.data == {
        "brand": "visa",
        "last4": "4242",
        "exp_month": 12,
        "exp_year": 2030,
    }


@override_settings(BILLING_PROVIDER="stripe")
def test_free_plan_does_not_expose_a_saved_stripe_payment_method(user):
    user.subscription.stripe_customer_id = "cus_canceled"
    user.subscription.save(update_fields=["stripe_customer_id"])
    stripe_client = SimpleNamespace(
        Customer=SimpleNamespace(retrieve=Mock()),
    )

    with patch("billing.views.get_stripe", return_value=stripe_client):
        response = make_client(user).get("/api/billing/payment-method/")

    assert response.status_code == 200
    assert response.data is None
    stripe_client.Customer.retrieve.assert_not_called()


@override_settings(BILLING_PROVIDER="stripe")
def test_subscribe_rejects_missing_payment_method(user):
    cache.clear()
    user.subscription.stripe_customer_id = "cus_missing_card"
    user.subscription.save(update_fields=["stripe_customer_id"])
    stripe_client = SimpleNamespace(
        Customer=SimpleNamespace(
            retrieve=Mock(return_value={"invoice_settings": {}})
        ),
        Subscription=SimpleNamespace(create=Mock()),
    )
    with patch("billing.stripe_utils.get_stripe", return_value=stripe_client), patch(
        "billing.views.settings.STRIPE_PRICE_ID_PRO", "price_pro_test"
    ):
        response = make_client(user).post("/api/billing/subscribe/")

    assert response.status_code == 400
    assert response.data["code"] == "payment_method_required"
    stripe_client.Subscription.create.assert_not_called()


@override_settings(BILLING_PROVIDER="stripe")
def test_subscribe_returns_payment_intent_secret_without_updating_plan(user):
    cache.clear()
    subscription = user.subscription
    subscription.stripe_customer_id = "cus_ready"
    subscription.save(update_fields=["stripe_customer_id"])
    stripe_client = SimpleNamespace(
        Customer=SimpleNamespace(
            retrieve=Mock(return_value={
                "invoice_settings": {"default_payment_method": "pm_ready"},
            })
        ),
        Subscription=SimpleNamespace(
            create=Mock(return_value={
                "id": "sub_new",
                "latest_invoice": {
                    "payment_intent": {"client_secret": "pi_secret"},
                },
            })
        ),
    )
    with patch("billing.stripe_utils.get_stripe", return_value=stripe_client), patch(
        "billing.views.settings.STRIPE_PRICE_ID_PRO", "price_pro_test"
    ):
        response = make_client(user).post("/api/billing/subscribe/")

    subscription.refresh_from_db()
    assert response.status_code == 201
    assert response.data == {
        "subscription_id": "sub_new",
        "client_secret": "pi_secret",
    }
    assert subscription.stripe_subscription_id == "sub_new"
    assert subscription.plan == Subscription.Plan.FREE
    assert subscription.status == Subscription.Status.TRIALING
    stripe_client.Subscription.create.assert_called_once()


@override_settings(BILLING_PROVIDER="stripe")
def test_subscribe_uses_confirmed_setup_intent_before_webhook(user):
    cache.clear()
    subscription = user.subscription
    subscription.stripe_customer_id = "cus_setup_fast"
    subscription.save(update_fields=["stripe_customer_id"])
    stripe_client = SimpleNamespace(
        SetupIntent=SimpleNamespace(
            retrieve=Mock(return_value={
                "status": "succeeded",
                "customer": "cus_setup_fast",
                "payment_method": "pm_setup_fast",
            })
        ),
        Customer=SimpleNamespace(modify=Mock()),
        Subscription=SimpleNamespace(
            create=Mock(return_value={"id": "sub_setup_fast"})
        ),
    )
    with patch("billing.stripe_utils.get_stripe", return_value=stripe_client), patch(
        "billing.views.settings.STRIPE_PRICE_ID_PRO", "price_pro_test"
    ):
        response = make_client(user).post(
            "/api/billing/subscribe/",
            {"setup_intent_id": "seti_setup_fast"},
            format="json",
        )

    assert response.status_code == 201
    stripe_client.SetupIntent.retrieve.assert_called_once_with("seti_setup_fast")
    stripe_client.Customer.modify.assert_called_once_with(
        "cus_setup_fast",
        invoice_settings={"default_payment_method": "pm_setup_fast"},
    )
    stripe_client.Subscription.create.assert_called_once()


@override_settings(BILLING_PROVIDER="stripe")
def test_subscribe_rejects_unverified_email(user):
    user.profile.email_verified = False
    user.profile.save(update_fields=["email_verified"])

    response = make_client(user).post("/api/billing/subscribe/")

    assert response.status_code == 403
    assert response.data["code"] == "email_not_verified"


def test_setup_intent_webhook_sets_default_payment_method_once(user):
    user.subscription.stripe_customer_id = "cus_setup"
    user.subscription.save(update_fields=["stripe_customer_id"])
    stripe_client = SimpleNamespace(
        Customer=SimpleNamespace(modify=Mock())
    )
    event = {
        "id": "evt_setup_intent",
        "type": "setup_intent.succeeded",
        "data": {
            "object": {
                "customer": "cus_setup",
                "payment_method": "pm_setup",
            },
        },
    }

    with patch("billing.stripe_utils.get_stripe", return_value=stripe_client):
        assert process_stripe_event(event) is True
        assert process_stripe_event(event) is False

    stripe_client.Customer.modify.assert_called_once_with(
        "cus_setup",
        invoice_settings={"default_payment_method": "pm_setup"},
    )


def test_incomplete_subscription_webhook_does_not_grant_paid_plan(user):
    user.subscription.stripe_subscription_id = "sub_pending"
    user.subscription.save(update_fields=["stripe_subscription_id"])
    event = {
        "id": "evt_subscription_incomplete",
        "type": "customer.subscription.created",
        "data": {
            "object": {
                "id": "sub_pending",
                "customer": "cus_pending",
                "status": "incomplete",
                "items": {"data": [{"price": {"id": "price_pro_test"}}]},
            },
        },
    }

    with override_settings(STRIPE_PRICE_ID_PRO="price_pro_test"):
        assert process_stripe_event(event) is True

    user.subscription.refresh_from_db()
    assert user.subscription.plan == Subscription.Plan.FREE
    assert user.subscription.status == Subscription.Status.INCOMPLETE


def test_invoice_paid_webhook_activates_plan_and_period(user):
    subscription = user.subscription
    subscription.stripe_subscription_id = "sub_paid"
    subscription.status = Subscription.Status.INCOMPLETE
    subscription.save(update_fields=["stripe_subscription_id", "status"])
    period_end = int((timezone.now() + timedelta(days=30)).timestamp())
    event = {
        "id": "evt_invoice_paid",
        "type": "invoice.paid",
        "data": {
            "object": {
                "subscription": "sub_paid",
                "customer": "cus_paid",
                "lines": {
                    "data": [{
                        "price": {"id": "price_pro_test"},
                        "period": {"end": period_end},
                    }],
                },
                "status_transitions": {"paid_at": period_end - 30},
            },
        },
    }

    with override_settings(STRIPE_PRICE_ID_PRO="price_pro_test"):
        assert process_stripe_event(event) is True

    subscription.refresh_from_db()
    assert subscription.plan == Subscription.Plan.PRO
    assert subscription.status == Subscription.Status.ACTIVE
    assert int(subscription.current_period_end.timestamp()) == period_end


@override_settings(
    BILLING_PROVIDER="stripe",
    STRIPE_SECRET_KEY="sk_test_mocked",
    STRIPE_PRO_PRICE_ID="price_pro_test",
)
def test_checkout_creates_customer_and_checkout_session(user):
    with patch("stripe.Customer.create", return_value=type("Customer", (), {"id": "cus_test"})()), patch(
        "stripe.checkout.Session.create",
        return_value=type("CheckoutSession", (), {"url": "https://checkout.stripe.test/session"})(),
    ) as create_session:
        response = make_client(user).post(
            "/api/billing/checkout/", {"plan": "pro"}, format="json"
        )

    assert response.status_code == 201
    assert response.data["url"] == "https://checkout.stripe.test/session"
    user.subscription.refresh_from_db()
    assert user.subscription.stripe_customer_id == "cus_test"
    assert create_session.call_args.kwargs["line_items"] == [
        {"price": "price_pro_test", "quantity": 1}
    ]