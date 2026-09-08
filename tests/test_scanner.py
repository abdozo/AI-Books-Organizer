from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from organizer.scanner import CATALOG_FIELDS, ScanManager


class FakeLibrary:
    def __init__(self) -> None:
        self.book = {
            "id": "book-1", "file": "book.pdf", "pageCount": 5,
            **{field: "" for field in CATALOG_FIELDS},
        }
        self.book["title"] = "عنوان قديم"
        self.current_page = 1

    def get_book(self, _book_id: str):
        return {**self.book, "status": "complete"}

    def source_path(self, _book_id: str) -> Path:
        return Path("book.pdf")

    def latest_scan(self):
        return {
            "id": "scan-1", "cancelRequested": False, "skipRequested": False,
            "paused": False, "currentPage": self.current_page,
        }

    def update_scan(self, _scan_id: str, **fields):
        if "current_page" in fields:
            self.current_page = fields["current_page"]

    def update_scan_result(self, _book_id: str, *, values, **_kwargs):
        self.book.update(values)

    def mark_book_skipped(self, _book_id: str, _max_pages: int):
        raise AssertionError("The scan should not be skipped")


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


def test_scan_continues_to_page_limit_while_any_catalog_field_is_missing(monkeypatch):
    library = FakeLibrary()
    manager = ScanManager(library, SimpleNamespace())
    calls = []
    responses = [
        extraction({
            "title": "كتاب", "author": "مؤلف", "publisher": "ناشر", "topic": "تاريخ",
        }),
        extraction({"publication_year": "٢٠٢٦"}),
        extraction({"edition_number": "الثانية"}),
        extraction({}),
        extraction({}),
    ]

    monkeypatch.setattr("organizer.scanner.render_page", lambda _path, page: str(page).encode())

    class Client:
        def extract(self, image, **_kwargs):
            page = int(image.decode())
            calls.append(page)
            return responses[page - 1]

    outcome = manager._scan_book(
        "scan-1", "book-1", max_pages=5, prompt_template="{{previous_results}}",
        model="gemini-test", client=Client(),
    )

    assert outcome == "complete"
    assert calls == [1, 2, 3, 4, 5]
    assert library.book["publication_year"] == "٢٠٢٦"
    assert library.book["edition_number"] == "الثانية"
    assert library.book["volume_number"] == ""


def test_scan_stops_as_soon_as_every_catalog_field_is_found(monkeypatch):
    library = FakeLibrary()
    manager = ScanManager(library, SimpleNamespace())
    calls = []
    first_page = {
        "title": "كتاب", "author": "مؤلف", "editor": "محقق",
        "publisher": "ناشر", "publication_year": "٢٠٢٦",
        "edition_number": "الثانية", "topic": "تاريخ",
    }
    responses = [extraction(first_page), extraction({"volume_number": "٣"})]

    monkeypatch.setattr("organizer.scanner.render_page", lambda _path, page: str(page).encode())

    class Client:
        def extract(self, image, **_kwargs):
            page = int(image.decode())
            calls.append(page)
            return responses[page - 1]

    manager._scan_book(
        "scan-1", "book-1", max_pages=5, prompt_template="{{previous_results}}",
        model="gemini-test", client=Client(),
    )

    assert calls == [1, 2]
