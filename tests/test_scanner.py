from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from google.genai import errors

from organizer.database import Library
from organizer.gemini import GeminiCataloguer, GeminiRateLimit
from organizer.rate_limits import RateLimitReservation
from organizer.scanner import CATALOG_FIELDS, ScanManager, provider_quota_wait_seconds


class FakeLibrary:
    def __init__(self) -> None:
        self.book = {
            "id": "book-1", "file": "book.pdf", "pageCount": 5,
            **{field: "" for field in CATALOG_FIELDS},
        }
        self.book["title"] = "عنوان قديم"
        self.current_page = 1
        self.cancel_requested = False
        self.skip_requested = False
        self.paused = False
        self.scan_updates = []
        self.api_reservations = 0
        self.api_completions = 0

    def get_book(self, _book_id: str):
        return {**self.book, "status": "complete"}

    def source_path(self, _book_id: str) -> Path:
        return Path("book.pdf")

    def latest_scan(self):
        return {
            "id": "scan-1", "cancelRequested": self.cancel_requested,
            "skipRequested": self.skip_requested, "paused": self.paused,
            "currentPage": self.current_page,
        }

    def update_scan(self, _scan_id: str, **fields):
        self.scan_updates.append(fields)
        if "current_page" in fields:
            self.current_page = fields["current_page"]

    def update_scan_result(self, _book_id: str, *, values, **_kwargs):
        self.book.update(values)

    def mark_book_skipped(self, _book_id: str, _max_pages: int):
        raise AssertionError("The scan should not be skipped")

    def reserve_api_request(self, _model: str, **_kwargs):
        self.api_reservations += 1
        return 0.0, "", 1, 1, self.api_reservations

    def complete_api_request(self, _reservation_id: int, **_kwargs):
        self.api_completions += 1

    def defer_api_requests(self, _model: str, **_kwargs):
        pass


def extraction(values: dict[str, str]):
    complete_values = {field: values.get(field, "") for field in CATALOG_FIELDS}
    confidence = SimpleNamespace(**{
        field: 90 if complete_values[field] else 0 for field in CATALOG_FIELDS
    })
    response = SimpleNamespace(
        **complete_values,
        confidence=confidence,
        missing_fields=[field for field in CATALOG_FIELDS if not complete_values[field]],
    )
    return SimpleNamespace(data=response)


def test_daily_wait_is_reported_and_skip_stays_responsive(monkeypatch):
    library = FakeLibrary()
    monkeypatch.setattr("organizer.scanner.time.sleep", lambda _delay: setattr(library, "skip_requested", True))
    manager = ScanManager(library, SimpleNamespace())
    manager._rate_limiter = SimpleNamespace(reserve=lambda _model: RateLimitReservation(
        wait_seconds=3600,
        window="day",
        published_requests_per_minute=5,
        safe_requests_per_minute=4,
        requests_per_day=20,
        minute_requests_used=0,
        day_requests_used=20,
    ))

    assert manager._wait_for_api_slot("scan-1", "gemini-3.8-flash") is None
    first_update = library.scan_updates[0]
    assert first_update["buffer_until"] > 0
    assert {key: value for key, value in first_update.items() if key != "buffer_until"} == {
        "rate_limit_window": "day",
        "rate_limit_rpm": 4,
        "rate_limit_rpd": 20,
    }
    assert library.scan_updates[-1] == {"buffer_until": 0, "rate_limit_window": ""}


def test_scan_sends_all_selected_pages_in_one_reserved_request(monkeypatch):
    library = FakeLibrary()
    manager = ScanManager(library, SimpleNamespace())
    calls = []
    response = extraction({
        "title": "كتاب", "author": "مؤلف", "publisher": "ناشر",
        "publication_year": "٢٠٢٦", "edition_number": "الثانية",
        "topic": "التاريخ الإسلامي والتراجم",
    })

    monkeypatch.setattr(
        "organizer.scanner.render_page",
        lambda _path, page, **_kwargs: str(page).encode(),
    )

    class Client:
        def extract(self, images, **_kwargs):
            calls.append([int(image.decode()) for image in images])
            return response

    outcome = manager._scan_book(
        "scan-1", "book-1", max_pages=5, prompt_template="{{previous_results}}",
        model="gemini-3.8-flash", client=Client(),
    )

    assert outcome == "complete"
    assert calls == [[1, 2, 3, 4, 5]]
    assert library.api_reservations == 1
    assert library.api_completions == 1
    assert library.book["publication_year"] == "٢٠٢٦"
    assert library.book["edition_number"] == "الثانية"
    assert library.book["volume_number"] == ""


def test_scan_uses_the_actual_pdf_page_count_when_it_is_below_the_user_limit(monkeypatch):
    library = FakeLibrary()
    library.book["pageCount"] = 2
    manager = ScanManager(library, SimpleNamespace())
    calls = []
    complete_result = {
        "title": "كتاب", "author": "مؤلف", "editor": "محقق",
        "publisher": "ناشر", "publication_year": "٢٠٢٦",
        "edition_number": "الثانية", "volume_number": "٣",
        "topic": "التاريخ الإسلامي والتراجم",
    }

    monkeypatch.setattr(
        "organizer.scanner.render_page",
        lambda _path, page, **_kwargs: str(page).encode(),
    )

    class Client:
        def extract(self, images, **_kwargs):
            calls.append([int(image.decode()) for image in images])
            return extraction(complete_result)

    manager._scan_book(
        "scan-1", "book-1", max_pages=5, prompt_template="{{previous_results}}",
        model="gemini-3.8-flash", client=Client(),
    )

    assert calls == [[1, 2]]
    assert library.api_reservations == 1
    assert library.api_completions == 1


def test_failed_api_attempt_keeps_its_rate_limit_slot(monkeypatch):
    library = FakeLibrary()
    manager = ScanManager(library, SimpleNamespace())

    monkeypatch.setattr(
        "organizer.scanner.render_page",
        lambda _path, page, **_kwargs: str(page).encode(),
    )

    class Client:
        def extract(self, _images, **_kwargs):
            raise RuntimeError("Google rejected the request")

    outcome = manager._scan_book(
        "scan-1", "book-1", max_pages=1, prompt_template="{{previous_results}}",
        model="gemini-3.8-flash", client=Client(),
    )

    assert outcome == "failed"
    assert library.api_reservations == 1
    assert library.api_completions == 1


def test_provider_tpm_limit_waits_and_retries_the_same_book(monkeypatch):
    library = FakeLibrary()
    manager = ScanManager(library, SimpleNamespace())
    waits = []
    calls = 0
    response = extraction({
        "title": "كتاب", "author": "مؤلف", "publisher": "ناشر", "topic": "تاريخ",
    })

    monkeypatch.setattr(
        "organizer.scanner.render_page",
        lambda _path, page, **_kwargs: str(page).encode(),
    )
    manager._wait_for_provider_quota = lambda _scan_id, failure, _retry: waits.append(failure) or True

    class Client:
        def extract(self, _images, **_kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise GeminiRateLimit(
                    quota_type="tpm",
                    quota_id="GenerateContentInputTokensPerModelPerMinute-FreeTier",
                    quota_metric="generativelanguage.googleapis.com/input_token_count",
                    retry_after_seconds=12,
                    provider_message="You exceeded your current quota.",
                )
            return response

    outcome = manager._scan_book(
        "scan-1", "book-1", max_pages=1, prompt_template="{{previous_results}}",
        model="gemini-3.8-flash", client=Client(),
    )

    assert outcome == "complete"
    assert calls == 2
    assert len(waits) == 1
    assert waits[0].quota_type == "tpm"
    assert library.api_reservations == 2
    assert library.api_completions == 2


def test_unknown_provider_429_stops_instead_of_retrying_or_advancing(monkeypatch):
    library = FakeLibrary()
    manager = ScanManager(library, SimpleNamespace())
    monkeypatch.setattr(
        "organizer.scanner.render_page",
        lambda _path, page, **_kwargs: str(page).encode(),
    )

    class Client:
        def extract(self, _images, **_kwargs):
            raise GeminiRateLimit(
                quota_type="unknown",
                provider_message="You exceeded your current quota.",
            )

    with pytest.raises(GeminiRateLimit):
        manager._scan_book(
            "scan-1", "book-1", max_pages=1, prompt_template="{{previous_results}}",
            model="gemini-3.8-flash", client=Client(),
        )

    assert library.api_reservations == 1
    assert library.api_completions == 1


def test_provider_retry_delay_never_shortens_the_minute_backoff():
    failure = GeminiRateLimit(quota_type="rpm", retry_after_seconds=12.5)

    assert provider_quota_wait_seconds(failure) == 61
    assert provider_quota_wait_seconds(failure, retry_number=2) == 121
    assert provider_quota_wait_seconds(
        GeminiRateLimit(quota_type="rpm", retry_after_seconds=150),
    ) == 151


def test_provider_wait_exposes_quota_reason_to_the_scan_ui(monkeypatch):
    library = FakeLibrary()
    manager = ScanManager(library, SimpleNamespace())
    monotonic = iter((10.0, 72.0))
    monkeypatch.setattr("organizer.scanner.time.monotonic", lambda: next(monotonic))
    monkeypatch.setattr("organizer.scanner.time.time", lambda: 1_000.0)
    failure = GeminiRateLimit(
        quota_type="tpm",
        quota_id="GenerateContentInputTokensPerModelPerMinute-FreeTier",
        retry_after_seconds=0,
    )

    assert manager._wait_for_provider_quota("scan-1", failure) is True
    assert library.scan_updates[0]["buffer_until"] == 1_061_000
    assert library.scan_updates[0]["rate_limit_window"] == "provider-tpm"
    assert "رموز الإدخال في الدقيقة" in library.scan_updates[0]["error"]
    assert library.scan_updates[-1] == {
        "buffer_until": 0,
        "rate_limit_window": "",
        "error": "",
    }


def test_daily_provider_limit_without_retry_info_waits_a_full_day():
    assert provider_quota_wait_seconds(GeminiRateLimit(quota_type="rpd")) == 86_401
    assert provider_quota_wait_seconds(GeminiRateLimit(quota_type="tpd")) == 86_401


def test_short_provider_delay_does_not_repeatedly_retry_a_daily_limit():
    failure = GeminiRateLimit(quota_type="rpd", retry_after_seconds=5)
    assert provider_quota_wait_seconds(failure) == 86_401
    assert provider_quota_wait_seconds(failure, retry_number=2) == 172_801


def test_scan_error_identifies_the_book_file(monkeypatch):
    library = FakeLibrary()
    manager = ScanManager(library, SimpleNamespace())
    saved_results = []
    library.update_scan_result = lambda _book_id, **values: saved_results.append(values)

    def fail_render(*_args, **_kwargs):
        raise RuntimeError("الصفحة تالفة")

    monkeypatch.setattr("organizer.scanner.render_page", fail_render)

    outcome = manager._scan_book(
        "scan-1", "book-1", max_pages=5, prompt_template="{{previous_results}}",
        model="gemini-3.8-flash", client=SimpleNamespace(),
    )

    assert outcome == "failed"
    assert saved_results[0]["error"] == 'تعذر فحص "book.pdf": الصفحة تالفة'


@pytest.mark.parametrize("code", [401, 403])
def test_rejected_account_stops_entire_queue_after_one_request(tmp_path, monkeypatch, code):
    library = Library(tmp_path / "data")
    books = []
    for filename in ("الحب في الميزان.pdf", "next.pdf"):
        path = tmp_path / filename
        path.write_bytes(b"%PDF-test")
        books.append(library.create_referenced_book(path, 1))
    scan_id = library.create_scan([book["id"] for book in books], 1, model="gemini-3.8-flash")
    calls = []

    def reject(**kwargs):
        calls.append(kwargs)
        raise errors.ClientError(code, {"error": {
            "code": code,
            "message": "The bound service account is deleted or disabled. "
                       "The service account bound to the API key must be active.",
            "status": "UNAUTHENTICATED" if code == 401 else "PERMISSION_DENIED",
            "details": [{
                "@type": "type.googleapis.com/google.rpc.ErrorInfo",
                "reason": "ACCOUNT_STATE_INVALID",
                "metadata": {"service": "generativelanguage.googleapis.com"},
            }],
        }})

    client = GeminiCataloguer("test-key", client=SimpleNamespace(
        models=SimpleNamespace(generate_content=reject),
    ))
    monkeypatch.setattr("organizer.scanner.GeminiCataloguer", lambda _key: client)
    monkeypatch.setattr("organizer.scanner.render_page", lambda *_args, **_kwargs: b"page")
    manager = ScanManager(library, SimpleNamespace(get=lambda: "test-key"))

    manager._run(scan_id)

    assert len(calls) == 1
    assert library.latest_scan()["state"] == "failed"
    assert "ACCOUNT_STATE_INVALID" in library.latest_scan()["error"]
    assert library.scan_items(scan_id)[1]["state"] == "waiting"
    assert library.get_book(books[1]["id"])["status"] == "waiting"


def test_repeated_known_quota_errors_stop_after_three_attempts(monkeypatch):
    library = FakeLibrary()
    manager = ScanManager(library, SimpleNamespace())
    monkeypatch.setattr("organizer.scanner.render_page", lambda *_args, **_kwargs: b"page")
    waits = []
    manager._wait_for_provider_quota = lambda *args: waits.append(args) or True
    calls = []

    class Client:
        def extract(self, *_args, **_kwargs):
            calls.append(True)
            if len(calls) <= 3:
                raise GeminiRateLimit(quota_type="tpm", retry_after_seconds=1)
            return extraction({"title": "book"})

    with pytest.raises(GeminiRateLimit):
        manager._scan_book(
            "scan-1", "book-1", max_pages=1, prompt_template="extract",
            model="gemini-3.8-flash", client=Client(),
        )

    assert len(calls) == 3
    assert len(waits) == 2
    assert library.api_reservations == 3
    assert library.api_completions == 3
