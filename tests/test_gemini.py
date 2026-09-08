from __future__ import annotations

import json
from types import SimpleNamespace

from organizer.gemini import GeminiCataloguer, render_prompt
from organizer.models import BOOK_REPLY_SCHEMA, DEFAULT_PROMPT


def test_gemini_receives_the_fixed_schema_outside_the_prompt():
    raw = json.dumps({
        "title": "كتاب",
        "author": "مؤلف",
        "editor": "",
        "publisher": "ناشر",
        "publication_year": "١٤٤٧ هـ",
        "edition_number": "الثانية",
        "volume_number": "٣",
        "topic": "تاريخ",
        "confidence": {
            "title": 95, "author": 90, "editor": 0, "publisher": 80,
            "publication_year": 90, "edition_number": 85, "volume_number": 95, "topic": 75,
        },
        "missing_fields": ["editor"],
    }, ensure_ascii=False)
    calls = []
    models = SimpleNamespace(generate_content=lambda **kwargs: calls.append(kwargs) or SimpleNamespace(text=raw, usage_metadata=None))
    client = GeminiCataloguer("secret-key", client=SimpleNamespace(models=models))
    prompt = "استخرج البيانات المطلوبة فقط"

    result = client.extract(b"jpeg", model="gemini-test", prompt=prompt)

    assert result.data.title == "كتاب"
    assert result.data.publication_year == "١٤٤٧ هـ"
    assert calls[0]["contents"][0] == prompt
    assert calls[0]["config"].response_json_schema == BOOK_REPLY_SCHEMA
    assert "properties" not in calls[0]["contents"][0]


def test_prompt_variables_are_replaced_without_appending_json_schema():
    rendered = render_prompt(
        "{{file_name}} {{page_number}} {{max_pages}} {{previous_results}} {{missing_fields}}",
        file_name="book.pdf",
        page_number=2,
        max_pages=5,
        previous={"title": "كتاب"},
        missing_fields=["publication_year", "edition_number", "volume_number"],
    )

    assert rendered.startswith("book.pdf 2 5")
    assert '"title":"كتاب"' in rendered
    assert "سنة النشر، رقم الطبعة، رقم المجلد" in rendered
    assert "json_schema" not in rendered


def test_publication_fields_have_explicit_extraction_guidance():
    properties = BOOK_REPLY_SCHEMA["properties"]

    assert "١٤٤٣" in properties["publication_year"]["description"]
    assert "الطبعة الثانية" in properties["edition_number"]["description"]
    assert "المجلد" in properties["volume_number"]["description"]
    assert "{{missing_fields}}" in DEFAULT_PROMPT
    assert "اقرأ جميع النصوص" in DEFAULT_PROMPT
