from unittest.mock import Mock, patch

import pytest
from django.contrib.auth import get_user_model
from django.core.cache import cache
from rest_framework.test import APIClient

from accounts.models import SocialAccount
from accounts.utils.crypto import encrypt_token
from analytics.metrics import PAGE_METRICS

pytestmark = pytest.mark.django_db

TOKEN = "analytics-super-secret-token"


def graph_response(payload, ok=True):
    response = Mock(ok=ok)
    response.json.return_value = payload
    return response


@pytest.fixture
def api_setup():
    cache.clear()
    user = get_user_model().objects.create_user(username="analytics-user")
    other_user = get_user_model().objects.create_user(username="other-analytics-user")
    account = SocialAccount.objects.create(
        user=user,
        provider=SocialAccount.Provider.FACEBOOK,
        external_id="page-123",
        name="Analytics Page",
        token_encrypted=encrypt_token(TOKEN),
        scopes="pages_show_list,read_insights",
    )
    client = APIClient()
    client.force_authenticate(user=user)
    yield {
        "client": client,
        "user": user,
        "other_user": other_user,
        "account": account,
    }
    cache.clear()


def overview_payloads():
    payloads = []
    for metric in PAGE_METRICS.values():
        payloads.append(
            graph_response(
                {
                    "data": [
                        {
                            "name": metric,
                            "values": [
                                {"value": 4, "end_time": "2026-10-01T07:00:00+0000"},
                                {"value": 3, "end_time": "2026-10-02T07:00:00+0000"},
                            ],
                        }
                    ]
                }
            )
        )
    return payloads


def test_anonymous_user_is_denied(api_setup):
    client = APIClient()
    response = client.get(
        "/api/analytics/overview/",
        {"account": api_setup["account"].pk},
    )
    assert response.status_code == 403


def test_another_users_account_is_not_found(api_setup):
    client = APIClient()
    client.force_authenticate(user=api_setup["other_user"])
    response = client.get(
        "/api/analytics/overview/",
        {"account": api_setup["account"].pk},
    )
    assert response.status_code == 404


def test_missing_read_insights_scope_is_forbidden(api_setup):
    account = api_setup["account"]
    account.scopes = "pages_show_list"
    account.save(update_fields=["scopes"])

    with patch("analytics.services.requests.get") as mock_get:
        response = api_setup["client"].get(
            "/api/analytics/overview/",
            {"account": account.pk},
        )

    assert response.status_code == 403
    assert response.data["code"] == "missing_permission"
    mock_get.assert_not_called()


def test_needs_reauth_account_is_rejected_without_graph_request(api_setup):
    account = api_setup["account"]
    account.status = SocialAccount.Status.NEEDS_REAUTH
    account.save(update_fields=["status"])

    with patch("analytics.services.requests.get") as mock_get:
        response = api_setup["client"].get(
            "/api/analytics/overview/",
            {"account": account.pk},
        )

    assert response.status_code == 409
    assert response.data["code"] == "needs_reauth"
    mock_get.assert_not_called()


def test_graph_error_190_requires_reauthentication(api_setup):
    with patch(
        "analytics.services.requests.get",
        return_value=graph_response({"error": {"code": 190}}, ok=False),
    ):
        response = api_setup["client"].get(
            "/api/analytics/overview/",
            {"account": api_setup["account"].pk},
        )

    assert response.status_code == 409
    assert response.data["code"] == "needs_reauth"
    api_setup["account"].refresh_from_db()
    assert api_setup["account"].status == SocialAccount.Status.NEEDS_REAUTH


def test_invalid_metric_does_not_fail_other_overview_metrics(api_setup):
    payloads = overview_payloads()
    payloads[0] = graph_response({"error": {"code": 100}}, ok=False)

    with patch("analytics.services.requests.get", side_effect=payloads):
        response = api_setup["client"].get(
            "/api/analytics/overview/",
            {"account": api_setup["account"].pk, "days": "7"},
        )

    assert response.status_code == 200
    first_key = next(iter(PAGE_METRICS))
    assert response.data["totals"][first_key] is None
    assert response.data["series"][first_key] is None
    assert response.data["unavailable"][0]["key"] == first_key
    assert response.data["totals"]["reach"] == 7
    assert response.data["range"]["days"] == 7


def test_invalid_days_is_rejected(api_setup):
    with patch("analytics.services.requests.get") as mock_get:
        response = api_setup["client"].get(
            "/api/analytics/overview/",
            {"account": api_setup["account"].pk, "days": "14"},
        )

    assert response.status_code == 400
    mock_get.assert_not_called()


def test_overview_shape_and_metric_totals(api_setup):
    with patch(
        "analytics.services.requests.get",
        side_effect=overview_payloads(),
    ) as mock_get:
        response = api_setup["client"].get(
            "/api/analytics/overview/",
            {"account": api_setup["account"].pk, "days": "7"},
        )

    assert response.status_code == 200
    assert set(response.data) == {"range", "totals", "series", "unavailable"}
    assert response.data["range"]["days"] == 7
    assert response.data["totals"]["views"] == 7
    assert response.data["totals"]["followers_total"] == 3
    assert response.data["series"]["views"][0] == {
        "date": "2026-10-01",
        "value": 4,
    }
    assert response.data["totals"]["net_follower_growth"] == 0
    assert response.data["unavailable"] == []
    assert mock_get.call_count == len(PAGE_METRICS)
    assert all(call.kwargs["timeout"] == 20 for call in mock_get.call_args_list)


def test_posts_shape_and_message_truncation(api_setup):
    post_message = "a" * 150
    with patch(
        "analytics.services.requests.get",
        return_value=graph_response(
            {
                "data": [
                    {
                        "id": "post-1",
                        "message": post_message,
                        "created_time": "2026-10-01T12:00:00+0000",
                        "permalink_url": "https://facebook.com/post-1",
                        "shares": {"count": 5},
                        "reactions": {"summary": {"total_count": 8}},
                        "comments": {"summary": {"total_count": 2}},
                    }
                ]
            }
        ),
    ) as mock_get:
        response = api_setup["client"].get(
            "/api/analytics/posts/",
            {"account": api_setup["account"].pk},
        )

    assert response.status_code == 200
    assert response.data == [
        {
            "id": "post-1",
            "message": f"{'a' * 137}...",
            "created_time": "2026-10-01T12:00:00+0000",
            "permalink_url": "https://facebook.com/post-1",
            "reactions": 8,
            "comments": 2,
            "shares": 5,
        }
    ]
    assert mock_get.call_args.kwargs["timeout"] == 20


def test_identical_request_uses_cache(api_setup):
    with patch(
        "analytics.services.requests.get",
        side_effect=overview_payloads(),
    ) as mock_get:
        first = api_setup["client"].get(
            "/api/analytics/overview/",
            {"account": api_setup["account"].pk, "days": "7"},
        )
        call_count = mock_get.call_count
        second = api_setup["client"].get(
            "/api/analytics/overview/",
            {"account": api_setup["account"].pk, "days": "7"},
        )

    assert first.status_code == second.status_code == 200
    assert mock_get.call_count == call_count


def test_response_never_contains_access_token(api_setup):
    payloads = overview_payloads()
    with patch("analytics.services.requests.get", side_effect=payloads):
        response = api_setup["client"].get(
            "/api/analytics/overview/",
            {"account": api_setup["account"].pk},
        )
    assert TOKEN not in response.content.decode()
