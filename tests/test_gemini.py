from __future__ import annotations

import json
from types import SimpleNamespace

from google import genai
from PIL import Image

from organizer.gemini import GeminiCataloguer, _encode_jpeg_with_limit, render_prompt
from organizer.models import BOOK_REPLY_SCHEMA, DEFAULT_PROMPT


def test_gemini_client_disables_automatic_http_retries(monkeypatch):
    captured = {}
    fake_client = SimpleNamespace()

    def build_client(**kwargs):
        captured.update(kwargs)
        return fake_client

    monkeypatch.setattr(genai, "Client", build_client)

    cataloguer = GeminiCataloguer("secret-key")

    assert cataloguer.client is fake_client
    assert captured["http_options"].retry_options.attempts == 1


def test_rendered_page_is_compressed_to_its_request_budget():
    image = Image.effect_noise((1200, 1800), 100).convert("RGB")
    try:
        encoded = _encode_jpeg_with_limit(image, 180_000)
    finally:
        image.close()

    assert len(encoded) <= 180_000


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

    result = client.extract(
        [b"page-one", b"page-two", b"page-three"],
        model="gemini-test",
        prompt=prompt,
    )

    assert result.data.title == "كتاب"
    assert result.data.publication_year == "١٤٤٧ هـ"
    assert len(calls) == 1
    assert calls[0]["contents"][0] == prompt
    assert len(calls[0]["contents"]) == 4
    assert [part.inline_data.data for part in calls[0]["contents"][1:]] == [
        b"page-one", b"page-two", b"page-three",
    ]
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


def test_default_prompt_uses_a_closed_islamic_sciences_taxonomy():
    expected_topics = (
        "التفسير", "علوم القرآن", "الحديث", "علوم الحديث", "الفقه",
        "أصول الفقه", "العقيدة", "السيرة النبوية", "التاريخ الإسلامي والتراجم",
        "اللغة العربية", "الأخلاق والتزكية", "الدعوة", "الفرق والأديان",
        "الثقافة والفكر الإسلامي", "الموسوعات والمجاميع", "أخرى",
    )

    assert "اختر قيمة topic من هذه القائمة فقط" in DEFAULT_PROMPT
    assert "لا تخترع تصنيفًا جديدًا" in DEFAULT_PROMPT
    for topic in expected_topics:
        assert topic in DEFAULT_PROMPT
