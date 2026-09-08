from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from organizer.rate_limits import RateLimitReservation
from organizer.scanner import CATALOG_FIELDS, ScanManager


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
        return 0.0, "", 1, 1


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


def test_daily_wait_is_reported_and_skip_stays_responsive():
    library = FakeLibrary()
    library.skip_requested = True
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

    assert manager._wait_for_api_slot("scan-1", "gemini-3.8-flash") is False
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
