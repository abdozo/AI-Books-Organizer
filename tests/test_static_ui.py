from __future__ import annotations

import re
from pathlib import Path


APP_JS = Path(__file__).resolve().parents[1] / "static" / "app.js"
APP_HTML = Path(__file__).resolve().parents[1] / "ai-books-organizer.html"
STATIC_DIR = Path(__file__).resolve().parents[1] / "static"


def test_favicon_assets_are_linked_and_present():
    source = APP_HTML.read_text(encoding="utf-8")

    assert 'href="/static/favicon-32.png"' in source
    assert 'href="/static/app-icon-512.png"' in source
    assert 'href="/static/apple-touch-icon.png"' in source
    assert (STATIC_DIR / "favicon-32.png").is_file()
    assert (STATIC_DIR / "app-icon-512.png").is_file()
    assert (STATIC_DIR / "apple-touch-icon.png").is_file()


def test_gemini_models_are_selectable_and_sorted_by_daily_free_quota():
    source = APP_JS.read_text(encoding="utf-8")
    entries = re.findall(
        r'\{ value: "([^"]+)", label: "[^"]+", dailyRequests: (\d+) \}',
        source,
    )

    assert '<select class="input" id="modelInput"' in source
    assert [model for model, _ in entries] == [
        "gemini-3.5-flash-lite",
        "gemini-3.1-flash-lite",
        "gemini-3.8-flash",
        "gemini-3.5-flash",
        "gemini-3.7-flash",
        "gemini-2.5-flash",
        "gemini-2.5-flash-lite",
        "gemini-3-flash-preview",
        "gemini-3.6-flash",
    ]
    quotas = [int(quota) for _, quota in entries]
    assert quotas == sorted(quotas, reverse=True)
    assert all(quota > 0 for quota in quotas)


def test_publication_fields_are_editable_without_archive_routes():
    source = APP_JS.read_text(encoding="utf-8")

    assert '["publication_year", "سنة النشر"]' in source
    assert '["edition_number", "رقم الطبعة"]' in source
    assert '["volume_number", "رقم المجلد"]' in source
    assert "publication_year:" not in source.split("const entityTypes =", 1)[1].split("};", 1)[0]
