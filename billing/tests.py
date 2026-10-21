from datetime import timedelta
from unittest.mock import patch

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.sessions.models import Session
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import SocialAccount
from billing.models import AdminAuditLog, StripeEvent, Subscription
from posts.models import Post


pytestmark = pytest.mark.django_db


@pytest.fixture
def user():
    return get_user_model().objects.create_user(
        username="billing-user",
        password="Valid-password-for-billing-6!",
        email="billing@example.com",
    )


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
    subscription.stripe_subscription_id = "sub_to_cancel"
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
    assert subscription.stripe_subscription_id == ""


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


def test_webhook_rejects_invalid_signature(user):
    class SignatureVerificationError(Exception):
        pass

    with override_settings(
        STRIPE_SECRET_KEY="sk_test_mocked",
        STRIPE_WEBHOOK_SECRET="whsec_mocked",
    ), patch(
        "stripe.Webhook.construct_event",
        side_effect=SignatureVerificationError("invalid"),
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
def test_test_subscribe_changes_plan(user):
    response = make_client(user).post(
        "/api/billing/test-subscribe/", {"plan": "pro"}, format="json"
    )

    user.subscription.refresh_from_db()
    assert response.status_code == 200
    assert user.subscription.plan == Subscription.Plan.PRO
    assert user.subscription.status == Subscription.Status.ACTIVE
    assert response.data["plan"] == Subscription.Plan.PRO
    assert response.data["status"] == Subscription.Status.ACTIVE
    assert response.data["current_period_end"] == user.subscription.current_period_end


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
    assert user.subscription.stripe_customer_id == "cus_test"
    assert create_session.call_args.kwargs["line_items"] == [
        {"price": "price_pro_test", "quantity": 1}
    ]