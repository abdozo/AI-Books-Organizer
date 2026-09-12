from __future__ import annotations

import json
from types import SimpleNamespace

from google import genai
from google.genai import errors
from PIL import Image
import pypdfium2 as pdfium
import pytest

from organizer.gemini import (
    GeminiAuthenticationFailure,
    GeminiCataloguer,
    GeminiRateLimit,
    _encode_jpeg_with_limit,
    _quota_type,
    inspect_pdf,
    render_prompt,
)
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


@pytest.mark.parametrize("code", [401, 403])
def test_authentication_failure_redacts_key_and_explains_account_check(code):
    def reject(**_kwargs):
        raise errors.ClientError(code, {"error": {
            "code": code, "message": "Rejected secret-key", "status": "UNAUTHENTICATED",
        }})

    client = GeminiCataloguer("secret-key", client=SimpleNamespace(
        models=SimpleNamespace(generate_content=reject),
    ))
    with pytest.raises(GeminiAuthenticationFailure) as captured:
        client.extract(b"page", model="gemini-test", prompt="extract")

    assert "secret-key" not in str(captured.value)
    assert "[API_KEY]" in str(captured.value)
    assert "حساب الخدمة" in str(captured.value)


def test_gemini_429_preserves_tpm_quota_details_and_retry_delay():
    response_json = {
        "error": {
            "code": 429,
            "message": "You exceeded your current quota.",
            "status": "RESOURCE_EXHAUSTED",
            "details": [
                {
                    "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                    "violations": [
                        {
                            "quotaMetric": "generativelanguage.googleapis.com/generate_content_free_tier_requests",
                            "quotaId": "GenerateRequestsPerMinutePerProjectPerModel-FreeTier",
                        },
                        {
                            "quotaMetric": "generativelanguage.googleapis.com/generate_content_free_tier_input_token_count",
                            "quotaId": "GenerateContentInputTokensPerModelPerMinute-FreeTier",
                            "quotaDimensions": {"model": "gemini-test", "location": "global"},
                            "quotaValue": "250000",
                        }
                    ],
                },
                {
                    "@type": "type.googleapis.com/google.rpc.RetryInfo",
                    "retryDelay": "42s",
                },
            ],
        }
    }

    def reject(**_kwargs):
        raise errors.ClientError(429, response_json)

    client = GeminiCataloguer(
        "secret-key",
        client=SimpleNamespace(models=SimpleNamespace(generate_content=reject)),
    )

    with pytest.raises(GeminiRateLimit) as captured:
        client.extract(b"page", model="gemini-test", prompt="extract")

    failure = captured.value
    assert failure.quota_type == "tpm"
    assert failure.quota_id == "GenerateContentInputTokensPerModelPerMinute-FreeTier"
    assert failure.quota_metric.endswith("input_token_count")
    assert failure.retry_after_seconds == 42
    assert "رموز الإدخال في الدقيقة" in str(failure)


@pytest.mark.parametrize(
    ("quota_id", "expected"),
    (
        ("GenerateRequestsPerMinutePerProjectPerModel-FreeTier", "rpm"),
        ("GenerateContentInputTokensPerModelPerMinute-FreeTier", "tpm"),
        ("GenerateRequestsPerDayPerProjectPerModel-FreeTier", "rpd"),
        ("GenerateContentInputTokensPerModelPerDay-FreeTier", "tpd"),
        ("GenerateImagesPerMinutePerProjectPerModel-FreeTier", "ipm"),
    ),
)
def test_quota_ids_are_classified_by_their_google_window(quota_id, expected):
    assert _quota_type(quota_id) == expected


def test_rendered_page_is_compressed_to_its_request_budget():
    image = Image.effect_noise((1200, 1800), 100).convert("RGB")
    try:
        encoded = _encode_jpeg_with_limit(image, 180_000)
    finally:
        image.close()

    assert len(encoded) <= 180_000


def test_pdf_page_count_has_no_upper_limit(monkeypatch, tmp_path):
    class LargePdf:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def __len__(self):
            return 6001

    monkeypatch.setattr(pdfium, "PdfDocument", lambda _source: LargePdf())

    assert inspect_pdf(tmp_path / "large.pdf") == 6001


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
