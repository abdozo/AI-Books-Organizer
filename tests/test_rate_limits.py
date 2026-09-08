from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from organizer.database import Library
from organizer.rate_limits import (
    MODEL_QUOTAS,
    ModelQuota,
    PersistentRateLimiter,
    model_options,
    quota_for,
)


def test_model_quotas_match_the_supplied_ai_studio_limits():
    expected = {
        "gemini-3.5-flash-lite": (15, 500),
        "gemini-3.1-flash-lite": (15, 500),
        "gemini-3.8-flash": (5, 20),
        "gemini-3.5-flash": (5, 20),
        "gemini-3.7-flash": (5, 20),
        "gemini-2.5-flash": (5, 20),
        "gemini-2.5-flash-lite": (10, 20),
        "gemini-3-flash-preview": (5, 20),
        "gemini-3.6-flash": (5, 20),
    }

    assert {
        model: (quota.requests_per_minute, quota.requests_per_day)
        for model, quota in MODEL_QUOTAS.items()
    } == expected


def test_every_model_keeps_one_request_below_the_minute_limit():
    assert ModelQuota(15, 500).safe_requests_per_minute == 14
    assert ModelQuota(5, 20).safe_requests_per_minute == 4
    assert ModelQuota(2, 20).safe_requests_per_minute == 1
    assert all(
        option["safeRequestsPerMinute"] == option["requestsPerMinute"] - 1
        for option in model_options()
    )


def test_unknown_model_is_rejected_instead_of_using_an_unverified_limit():
    with pytest.raises(ValueError, match="لا توجد حدود طلبات موثقة"):
        quota_for("gemini-unknown")


@pytest.mark.parametrize(
    ("model", "allowed"),
    (("gemini-3.5-flash-lite", 14), ("gemini-3.8-flash", 4)),
)
def test_rate_limiter_uses_the_safe_rolling_minute_limit(tmp_path, model, allowed):
    now = [100_000.0]
    limiter = PersistentRateLimiter(Library(tmp_path), clock=lambda: now[0])

    for request_number in range(1, allowed + 1):
        reservation = limiter.reserve(model)
        assert reservation.wait_seconds == 0
        assert reservation.minute_requests_used == request_number

    blocked = limiter.reserve(model)
    assert blocked.wait_seconds == 60
    assert blocked.window == "minute"
    assert blocked.safe_requests_per_minute == allowed

    now[0] += 59.5
    assert limiter.reserve(model).wait_seconds == 0.5
    now[0] += 0.5
    assert limiter.reserve(model).wait_seconds == 0


def test_daily_limit_is_persistent_across_limiter_instances(tmp_path):
    now = [200_000.0]
    library = Library(tmp_path)
    first = PersistentRateLimiter(library, clock=lambda: now[0])
    oldest = now[0]

    for batch in range(5):
        for _ in range(4):
            assert first.reserve("gemini-3.8-flash").wait_seconds == 0
        if batch < 4:
            now[0] += 61

    now[0] += 61
    restarted = PersistentRateLimiter(Library(tmp_path), clock=lambda: now[0])
    blocked = restarted.reserve("gemini-3.8-flash")

    assert blocked.window == "day"
    assert blocked.day_requests_used == 20
    assert blocked.wait_seconds == oldest + (24 * 60 * 60) - now[0]

    now[0] = oldest + (24 * 60 * 60)
    assert restarted.reserve("gemini-3.8-flash").wait_seconds == 0


def test_atomic_reservations_do_not_exceed_the_safe_limit(tmp_path):
    now = 300_000.0
    library = Library(tmp_path)

    def reserve_once(_request_number):
        limiter = PersistentRateLimiter(library, clock=lambda: now)
        return limiter.reserve("gemini-3.8-flash").wait_seconds

    with ThreadPoolExecutor(max_workers=8) as executor:
        waits = list(executor.map(reserve_once, range(8)))

    assert waits.count(0) == 4
    assert waits.count(60) == 4
