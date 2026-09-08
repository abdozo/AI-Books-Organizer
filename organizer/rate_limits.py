from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from .database import Library


MINUTE_SECONDS = 60.0
DAY_SECONDS = 24 * 60 * 60.0
RPM_SAFETY_MARGIN = 1


@dataclass(frozen=True)
class ModelQuota:
    requests_per_minute: int
    requests_per_day: int

    @property
    def safe_requests_per_minute(self) -> int:
        """Keep one request below the published RPM limit when possible."""
        return max(1, self.requests_per_minute - RPM_SAFETY_MARGIN)


@dataclass(frozen=True)
class ModelDefinition:
    identifier: str
    label: str
    quota: ModelQuota


@dataclass(frozen=True)
class RateLimitReservation:
    wait_seconds: float
    window: str
    published_requests_per_minute: int
    safe_requests_per_minute: int
    requests_per_day: int
    minute_requests_used: int
    day_requests_used: int
    reservation_id: int | None = None


# Limits supplied in the Google AI Studio CSV. The number to the right of each
# slash is the quota; the number to the left is only the usage snapshot at export.
# Only text-output models offered by this app are listed.
MODEL_DEFINITIONS = (
    ModelDefinition("gemini-3.5-flash-lite", "Gemini 3.5 Flash Lite", ModelQuota(15, 500)),
    ModelDefinition("gemini-3.1-flash-lite", "Gemini 3.1 Flash Lite", ModelQuota(15, 500)),
    ModelDefinition("gemini-3.8-flash", "Gemini 3.8 Flash", ModelQuota(5, 20)),
    ModelDefinition("gemini-3.5-flash", "Gemini 3.5 Flash", ModelQuota(5, 20)),
    ModelDefinition("gemini-3.7-flash", "Gemini 3.7 Flash", ModelQuota(5, 20)),
    ModelDefinition("gemini-2.5-flash", "Gemini 2.5 Flash", ModelQuota(5, 20)),
    ModelDefinition("gemini-2.5-flash-lite", "Gemini 2.5 Flash Lite", ModelQuota(10, 20)),
    ModelDefinition("gemini-3-flash-preview", "Gemini 3 Flash", ModelQuota(5, 20)),
    ModelDefinition("gemini-3.6-flash", "Gemini 3.6 Flash", ModelQuota(5, 20)),
)
MODEL_QUOTAS = {definition.identifier: definition.quota for definition in MODEL_DEFINITIONS}


def quota_for(model: str) -> ModelQuota:
    try:
        return MODEL_QUOTAS[model]
    except KeyError as exc:
        raise ValueError(
            "لا توجد حدود طلبات موثقة لهذا النموذج. اختر نموذجًا من قائمة الإعدادات"
        ) from exc


def model_options() -> list[dict[str, str | int]]:
    return [
        {
            "value": definition.identifier,
            "label": definition.label,
            "requestsPerMinute": definition.quota.requests_per_minute,
            "safeRequestsPerMinute": definition.quota.safe_requests_per_minute,
            "dailyRequests": definition.quota.requests_per_day,
        }
        for definition in MODEL_DEFINITIONS
    ]


class PersistentRateLimiter:
    """Atomically reserve requests in rolling minute and day windows."""

    def __init__(self, library: Library, clock: Callable[[], float] = time.time):
        self._library = library
        self._clock = clock

    def reserve(self, model: str) -> RateLimitReservation:
        """Reserve one API attempt, or return when a safe slot will be available."""
        quota = quota_for(model)
        now = self._clock()
        (
            wait_seconds,
            window,
            minute_used,
            day_used,
            reservation_id,
        ) = self._library.reserve_api_request(
            model,
            reserved_at=now,
            minute_seconds=MINUTE_SECONDS,
            day_seconds=DAY_SECONDS,
            minute_limit=quota.safe_requests_per_minute,
            day_limit=quota.requests_per_day,
        )
        return RateLimitReservation(
            wait_seconds=wait_seconds,
            window=window,
            published_requests_per_minute=quota.requests_per_minute,
            safe_requests_per_minute=quota.safe_requests_per_minute,
            requests_per_day=quota.requests_per_day,
            minute_requests_used=minute_used,
            day_requests_used=day_used,
            reservation_id=reservation_id,
        )

    def complete(self, reservation: RateLimitReservation) -> None:
        """Move a used slot forward to response completion to cover network delay."""
        if reservation.reservation_id is None:
            return
        self._library.complete_api_request(
            reservation.reservation_id,
            completed_at=self._clock(),
        )
