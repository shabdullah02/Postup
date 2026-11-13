import logging
from unittest.mock import Mock, patch

import pytest
from django.contrib.auth import get_user_model
from django.core.cache import cache
from rest_framework.test import APIClient

from accounts.models import SocialAccount
from accounts.utils.crypto import encrypt_token
from analytics.metrics import PAGE_METRICS, THREADS_METRICS

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
                            "period": "day",
                            "values": [
                                {"value": 3, "end_time": "2026-10-03T07:00:00+0000"},
                                {"value": 4, "end_time": "2026-10-02T07:00:00+0000"},
                            ],
                        }
                    ]
                }
            )
        )
    return payloads


def create_threads_account(api_setup, scopes="threads_basic,threads_manage_insights"):
    return SocialAccount.objects.create(
        user=api_setup["user"],
        provider=SocialAccount.Provider.THREADS,
        external_id="threads-user-123",
        name="Threads user",
        token_encrypted=encrypt_token(TOKEN),
        scopes=scopes,
    )


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


def test_threads_overview_uses_common_shape_and_date_range(api_setup):
    account = create_threads_account(api_setup)
    graph_data = []
    for metric in THREADS_METRICS.values():
        if metric == "followers_count":
            graph_data.append(
                {"name": metric, "total_value": {"value": 123}}
            )
        else:
            graph_data.append(
                {
                    "name": metric,
                    "period": "day",
                    "values": [
                        {"value": 4, "end_time": "2026-10-03T07:00:00+0000"},
                        {"value": 3, "end_time": "2026-10-02T07:00:00+0000"},
                    ],
                }
            )

    with patch(
        "analytics.services.requests.get",
        return_value=graph_response({"data": graph_data}),
    ) as mock_get:
        response = api_setup["client"].get(
            "/api/analytics/overview/",
            {"account": account.pk, "days": "7"},
        )

    assert response.status_code == 200
    assert set(response.data) == {"range", "totals", "series", "unavailable"}
    assert response.data["range"]["days"] == 7
    assert response.data["totals"]["views"] == 7
    assert response.data["totals"]["followers_total"] == 123
    assert response.data["series"]["views"] == [
        {"date": "2026-10-01", "value": 3},
        {"date": "2026-10-02", "value": 4},
    ]
    assert response.data["series"]["net_follower_growth"] is None
    assert response.data["unavailable"] == []
    assert mock_get.call_args.args[0] == (
        "https://graph.threads.net/v1.0/threads-user-123/threads_insights"
    )
    params = mock_get.call_args.kwargs["params"]
    assert params["metric"] == ",".join(THREADS_METRICS.values())
    assert isinstance(params["since"], int)
    assert isinstance(params["until"], int)
    assert params["access_token"] == TOKEN


def test_threads_posts_normalize_post_insights(api_setup):
    account = create_threads_account(api_setup)
    insights = [
        {
            "name": metric,
            "values": [{"value": value}],
        }
        for metric, value in {
            "views": 100,
            "likes": 12,
            "replies": 3,
            "reposts": 4,
            "quotes": 2,
            "shares": 1,
        }.items()
    ]
    with patch(
        "analytics.services.requests.get",
        side_effect=[
            graph_response(
                {
                    "data": [
                        {
                            "id": "thread-media-456",
                            "text": "A Threads post",
                            "timestamp": "2026-10-01T12:00:00+0000",
                            "permalink": "https://www.threads.net/@user/post/456",
                        }
                    ]
                }
            ),
            graph_response({"data": insights}),
        ],
    ) as mock_get:
        response = api_setup["client"].get(
            "/api/analytics/posts/",
            {"account": account.pk},
        )

    assert response.status_code == 200
    assert response.data == [
        {
            "id": "thread-media-456",
            "message": "A Threads post",
            "created_time": "2026-10-01T12:00:00+0000",
            "permalink_url": "https://www.threads.net/@user/post/456",
            "reactions": 12,
            "comments": 3,
            "shares": 1,
            "views": 100,
            "reposts": 4,
            "quotes": 2,
        }
    ]
    assert mock_get.call_args_list[0].args[0] == (
        "https://graph.threads.net/v1.0/threads-user-123/threads"
    )
    assert mock_get.call_args_list[1].args[0] == (
        "https://graph.threads.net/v1.0/thread-media-456/threads_insights"
    )
    assert mock_get.call_args_list[1].kwargs["params"]["metric"] == (
        "views,likes,replies,reposts,quotes,shares"
    )


def test_threads_permission_error_requests_reconnection(api_setup):
    account = create_threads_account(api_setup)
    error_response = graph_response(
        {"error": {"code": 10, "message": "Permission denied."}},
        ok=False,
    )
    error_response.status_code = 400

    with patch("analytics.services.requests.get", return_value=error_response):
        response = api_setup["client"].get(
            "/api/analytics/overview/",
            {"account": account.pk},
        )

    assert response.status_code == 403
    assert response.data == {
        "detail": "Reconnect Threads to grant analytics access.",
        "code": "missing_permission",
    }


def test_threads_overview_without_insights_scope_is_forbidden(api_setup):
    account = create_threads_account(api_setup, scopes="threads_basic")

    with patch("analytics.services.requests.get") as mock_get:
        response = api_setup["client"].get(
            "/api/analytics/overview/",
            {"account": account.pk},
        )

    assert response.status_code == 403
    assert response.data["detail"] == "Reconnect Threads to grant analytics access."
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


def test_graph_failure_logs_safe_details_and_preserves_api_response(api_setup, caplog):
    error_response = graph_response({"error": {"code": 200, "message": "x"}}, ok=False)
    error_response.status_code = 400

    with (
        caplog.at_level(logging.WARNING, logger="analytics.services"),
        patch("analytics.services.requests.get", return_value=error_response),
    ):
        response = api_setup["client"].get(
            "/api/analytics/posts/",
            {"account": api_setup["account"].pk},
        )

    assert response.status_code == 502
    assert response.data == {
        "detail": "Analytics unavailable. Try again later.",
        "code": "facebook_unavailable",
    }
    assert "endpoint=posts" in caplog.text
    assert "400" in caplog.text
    assert "code=200" in caplog.text
    assert TOKEN not in caplog.text


def test_invalid_metric_does_not_fail_other_overview_metrics(api_setup, caplog):
    payloads = overview_payloads()
    invalid_metric = graph_response(
        {
            "error": {
                "code": 100,
                "message": "Invalid metric page_media_view",
            }
        },
        ok=False,
    )
    invalid_metric.status_code = 400

    with (
        caplog.at_level(logging.WARNING, logger="analytics.services"),
        patch(
            "analytics.services.requests.get",
            side_effect=[invalid_metric, *payloads[1:]],
        ),
    ):
        response = api_setup["client"].get(
            "/api/analytics/overview/",
            {"account": api_setup["account"].pk, "days": "7"},
        )

    assert response.status_code == 200
    first_key = next(iter(PAGE_METRICS))
    assert response.data["totals"][first_key] is None
    assert response.data["series"][first_key] is None
    assert response.data["unavailable"] == [
        {
            "key": "views",
            "metric": "page_media_view",
            "code": "metric_unavailable",
        }
    ]
    assert response.data["totals"]["reach"] == 7
    assert response.data["range"]["days"] == 7
    assert "code=100" in caplog.text
    assert "Invalid metric page_media_view" in caplog.text
    assert TOKEN not in caplog.text


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
    assert response.data["series"]["views"][1] == {
        "date": "2026-10-02",
        "value": 3,
    }
    assert response.data["totals"]["net_follower_growth"] == 0
    assert response.data["unavailable"] == []
    assert mock_get.call_count == len(PAGE_METRICS)
    assert all(call.kwargs["timeout"] == 20 for call in mock_get.call_args_list)
    assert [
        call.kwargs["params"]["metric"] for call in mock_get.call_args_list
    ] == list(PAGE_METRICS.values())
    assert all(
        call.kwargs["params"]["period"] == "day"
        for call in mock_get.call_args_list
    )
    assert PAGE_METRICS["views"] == "page_media_view"
    assert PAGE_METRICS["reach"] == "page_total_media_view_unique"
    assert PAGE_METRICS["followers_total"] == "page_follows"


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
