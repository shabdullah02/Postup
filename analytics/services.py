import logging
from datetime import datetime, time, timedelta

import requests
from cryptography.fernet import InvalidToken
from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from accounts.models import SocialAccount
from accounts.utils.crypto import decrypt_token
from socials.adapters import AuthExpiredError

from .metrics import PAGE_METRICS

CACHE_TTL_SECONDS = 15 * 60
logger = logging.getLogger(__name__)


class AnalyticsUnavailableError(Exception):
    pass


def _cache_key(account_id, kind, days):
    return f"analytics:{account_id}:{kind}:{days}"


def _access_token(account):
    try:
        return decrypt_token(account.token_encrypted)
    except InvalidToken as exc:
        _mark_needs_reauth(account)
        raise AuthExpiredError from exc


def _mark_needs_reauth(account):
    account.status = SocialAccount.Status.NEEDS_REAUTH
    account.save(update_fields=["status"])


def _log_caught_exception(exc, endpoint):
    logger.warning(
        "Analytics request failed endpoint=%s exception=%s",
        endpoint,
        type(exc).__name__,
    )


def _graph_error_payload(response, endpoint):
    try:
        payload = response.json()
    except (KeyError, ValueError, AttributeError, TypeError) as exc:
        if isinstance(exc, (KeyError, TypeError, ValueError)):
            _log_caught_exception(exc, endpoint)
        return {}
    if not isinstance(payload, dict):
        return {}
    error = payload.get("error")
    return error if isinstance(error, dict) else {}


def _graph_error_code(error, endpoint):
    try:
        return int(error.get("code"))
    except (KeyError, TypeError, ValueError) as exc:
        _log_caught_exception(exc, endpoint)
        return None


def _request_graph(url, params, account, endpoint):
    try:
        response = requests.get(url, params=params, timeout=20)
    except requests.RequestException as exc:
        logger.warning("%s", type(exc).__name__)
        raise AnalyticsUnavailableError from exc

    if response.ok:
        try:
            payload = response.json()
        except (KeyError, ValueError, AttributeError, TypeError) as exc:
            if isinstance(exc, (KeyError, TypeError, ValueError)):
                _log_caught_exception(exc, endpoint)
            raise AnalyticsUnavailableError from exc
        if not isinstance(payload, dict):
            raise AnalyticsUnavailableError
        error = payload.get("error")
        if not isinstance(error, dict):
            return payload
    else:
        error = _graph_error_payload(response, endpoint)
    message = error.get("message")
    token = params.get("access_token")
    if isinstance(message, str) and isinstance(token, str):
        message = message.replace(token, "[redacted]")
    logger.warning(
        "Graph request failed endpoint=%s status=%s code=%s "
        "error_subcode=%s type=%s message=%s",
        endpoint,
        response.status_code,
        error.get("code"),
        error.get("error_subcode"),
        error.get("type"),
        message,
    )
    error_code = _graph_error_code(error, endpoint)
    if error_code == 190:
        _mark_needs_reauth(account)
        raise AuthExpiredError
    if error_code == 100:
        return None
    raise AnalyticsUnavailableError


def _insights_url(account):
    return (
        f"{settings.FB_GRAPH_BASE_URL}/{settings.FB_GRAPH_VERSION}/"
        f"{account.external_id}/insights"
    )


def _get_metric_series(account, token, metric, since, until):
    payload = _request_graph(
        _insights_url(account),
        {
            "metric": metric,
            "period": "day",
            "since": since,
            "until": until,
            "access_token": token,
        },
        account,
        f"insights:{metric}",
    )
    if payload is None:
        return None

    data = payload.get("data")
    if not isinstance(data, list):
        raise AnalyticsUnavailableError
    metric_data = next(
        (
            item
            for item in data
            if isinstance(item, dict) and item.get("name") == metric
        ),
        None,
    )
    if metric_data is None:
        return []

    if metric_data.get("period") != "day":
        raise AnalyticsUnavailableError

    values = metric_data.get("values")
    if not isinstance(values, list):
        raise AnalyticsUnavailableError

    series = []
    for item in values:
        if not isinstance(item, dict):
            continue
        date_value = item.get("end_time")
        value = item.get("value")
        if not isinstance(date_value, str) or isinstance(value, bool):
            continue
        try:
            number = float(value)
            # Insights end_time marks the exclusive end of each daily bucket.
            date = (
                datetime.fromisoformat(date_value.replace("Z", "+00:00")).date()
                - timedelta(days=1)
            )
        except (TypeError, ValueError) as exc:
            _log_caught_exception(exc, f"insights:{metric}")
            continue
        if number.is_integer():
            number = int(number)
        series.append({"date": date.isoformat(), "value": number})
    return sorted(series, key=lambda item: item["date"])


def _series_total(series):
    return sum(item["value"] for item in series)


def get_page_overview(account, days):
    days = min(days, 90)
    key = _cache_key(account.pk, "overview", days)
    cached = cache.get(key)
    if cached is not None:
        return cached

    token = _access_token(account)
    until_date = timezone.localdate()
    since_date = until_date - timedelta(days=days - 1)
    local_tz = timezone.get_current_timezone()
    since_timestamp = int(
        timezone.make_aware(datetime.combine(since_date, time.min), local_tz).timestamp()
    )
    until_timestamp = int(
        timezone.make_aware(datetime.combine(until_date, time.max), local_tz).timestamp()
    )

    series = {}
    totals = {}
    unavailable = []
    for key_name, metric in PAGE_METRICS.items():
        metric_series = _get_metric_series(
            account,
            token,
            metric,
            since_timestamp,
            until_timestamp,
        )
        series[key_name] = metric_series
        if metric_series is None:
            totals[key_name] = None
            unavailable.append(
                {
                    "key": key_name,
                    "metric": metric,
                    "code": "metric_unavailable",
                }
            )
        elif key_name == "followers_total":
            totals[key_name] = (
                metric_series[-1]["value"] if metric_series else None
            )
        else:
            totals[key_name] = _series_total(metric_series)

    follows = series["follows"]
    unfollows = series["unfollows"]
    if follows is None or unfollows is None:
        series["net_follower_growth"] = None
        totals["net_follower_growth"] = None
    else:
        follows_by_date = {item["date"]: item["value"] for item in follows}
        unfollows_by_date = {item["date"]: item["value"] for item in unfollows}
        dates = sorted(follows_by_date.keys() | unfollows_by_date.keys())
        net_growth = [
            {
                "date": date,
                "value": follows_by_date.get(date, 0) - unfollows_by_date.get(date, 0),
            }
            for date in dates
        ]
        series["net_follower_growth"] = net_growth
        totals["net_follower_growth"] = _series_total(net_growth)

    result = {
        "range": {
            "since": since_date.isoformat(),
            "until": until_date.isoformat(),
            "days": days,
        },
        "totals": totals,
        "series": series,
        "unavailable": unavailable,
    }
    if not unavailable:
        cache.set(key, result, CACHE_TTL_SECONDS)
    return result


def _summary_count(value):
    if not isinstance(value, dict):
        return None
    summary = value.get("summary")
    if not isinstance(summary, dict):
        return None
    count = summary.get("total_count")
    return count if isinstance(count, int) and not isinstance(count, bool) else None


def get_page_posts(account, limit=10):
    key = _cache_key(account.pk, "posts", limit)
    cached = cache.get(key)
    if cached is not None:
        return cached

    token = _access_token(account)
    url = (
        f"{settings.FB_GRAPH_BASE_URL}/{settings.FB_GRAPH_VERSION}/"
        f"{account.external_id}/posts"
    )
    payload = _request_graph(
        url,
        {
            "fields": (
                "id,message,created_time,permalink_url,shares,"
                "reactions.summary(true).limit(0),comments.summary(true).limit(0)"
            ),
            "limit": limit,
            "access_token": token,
        },
        account,
        "posts",
    )
    if payload is None:
        raise AnalyticsUnavailableError

    data = payload.get("data")
    if not isinstance(data, list):
        raise AnalyticsUnavailableError

    posts = []
    for item in data:
        if not isinstance(item, dict):
            continue
        message = item.get("message")
        if isinstance(message, str) and len(message) > 140:
            message = f"{message[:137]}..."
        shares = item.get("shares")
        share_count = shares.get("count") if isinstance(shares, dict) else None
        posts.append(
            {
                "id": item.get("id"),
                "message": message,
                "created_time": item.get("created_time"),
                "permalink_url": item.get("permalink_url"),
                "reactions": _summary_count(item.get("reactions")),
                "comments": _summary_count(item.get("comments")),
                "shares": (
                    share_count
                    if isinstance(share_count, int) and not isinstance(share_count, bool)
                    else None
                ),
            }
        )

    cache.set(key, posts, CACHE_TTL_SECONDS)
    return posts
