from __future__ import annotations

import sqlite3

from organizer.database import Library
from organizer.models import DEFAULT_PROMPT, PREVIOUS_DEFAULT_PROMPT


def test_manual_books_and_entity_changes_are_persisted(tmp_path):
    library = Library(tmp_path)
    first = library.create_manual_book({
        "title": "كتاب أول",
        "author": "مؤلف قديم",
        "editor": "",
        "publisher": "دار الكتب",
        "publication_year": "٢٠٢٤",
        "edition_number": "الثانية",
        "volume_number": "١",
        "topic": "تاريخ",
        "path": "",
    })
    second = library.create_manual_book({
        "title": "كتاب ثان",
        "author": "مؤلف آخر",
        "editor": "",
        "publisher": "دار الكتب",
        "publication_year": "١٤٤٥ هـ",
        "edition_number": "الأولى",
        "volume_number": "٢",
        "topic": "تاريخ",
        "path": "",
    })

    affected = library.change_entities("author", ["مؤلف قديم", "مؤلف آخر"], "المؤلف الصحيح")

    assert affected == 2
    assert {book["author"] for book in library.list_books()} == {"المؤلف الصحيح"}
    assert library.get_book(first["id"])["history"][0]["text"].startswith("عُدّل")
    assert library.get_book(first["id"])["publication_year"] == "٢٠٢٤"
    assert library.get_book(first["id"])["edition_number"] == "الثانية"
    assert library.get_book(first["id"])["volume_number"] == "١"

    library.change_entities("publisher", ["دار الكتب"], None)
    assert library.get_book(second["id"])["publisher"] == ""
    assert library.get_book(second["id"])["status"] == "review"


def test_prompt_and_model_settings_do_not_contain_a_schema_column(tmp_path):
    library = Library(tmp_path)
    library.save_settings("تعليمات مخصصة", "gemini-test-model")

    assert library.setting("prompt") == "تعليمات مخصصة"
    assert library.setting("model") == "gemini-test-model"
    with library.connect() as db:
        columns = [row[1] for row in db.execute("PRAGMA table_info(settings)")]
    assert columns == ["key", "value"]


def test_existing_database_is_migrated_with_publication_fields(tmp_path):
    database = tmp_path / "library.sqlite3"
    with sqlite3.connect(database) as db:
        db.executescript("""
            CREATE TABLE books (
              id TEXT PRIMARY KEY, title TEXT NOT NULL, author TEXT NOT NULL DEFAULT '',
              editor TEXT NOT NULL DEFAULT '', publisher TEXT NOT NULL DEFAULT '',
              topic TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'review',
              file_name TEXT NOT NULL DEFAULT '', stored_path TEXT NOT NULL DEFAULT '',
              source_label TEXT NOT NULL DEFAULT '', page_count INTEGER NOT NULL DEFAULT 0,
              pages_checked INTEGER NOT NULL DEFAULT 0, max_pages INTEGER NOT NULL DEFAULT 0,
              confidence INTEGER NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0,
              error TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """)
        db.execute("INSERT INTO settings VALUES ('prompt', ?)", (PREVIOUS_DEFAULT_PROMPT,))

    library = Library(tmp_path)
    with library.connect() as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(books)")}

    assert {"publication_year", "edition_number", "volume_number"} <= columns
    assert library.setting("prompt") == DEFAULT_PROMPT


def test_scan_results_persist_publication_fields(tmp_path):
    library = Library(tmp_path)
    book = library.create_manual_book({"title": "كتاب"})
    values = {
        "title": "كتاب", "author": "مؤلف", "editor": "", "publisher": "ناشر",
        "publication_year": "٢٠٢٦", "edition_number": "الرابعة",
        "volume_number": "٥", "topic": "تاريخ",
    }

    library.update_scan_result(
        book["id"], values=values, pages_checked=2, max_pages=5,
        confidence=90, final=True,
    )

    saved = library.get_book(book["id"])
    assert saved["publication_year"] == "٢٠٢٦"
    assert saved["edition_number"] == "الرابعة"
    assert saved["volume_number"] == "٥"
