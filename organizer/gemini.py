from __future__ import annotations

import io
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from pydantic import ValidationError

from .models import BOOK_REPLY_SCHEMA, SYSTEM_INSTRUCTION, BookExtraction


MODEL_PATTERN = re.compile(r"gemini-[A-Za-z0-9._-]+")
MAX_INLINE_IMAGE_BYTES = 12 * 1024 * 1024


class GeminiFailure(RuntimeError):
    pass


QUOTA_LABELS = {
    "rpm": "حد عدد الطلبات في الدقيقة",
    "tpm": "حد رموز الإدخال في الدقيقة",
    "rpd": "الحد اليومي لعدد الطلبات",
    "tpd": "الحد اليومي لرموز الإدخال",
    "ipm": "حد الصور في الدقيقة",
    "unknown": "أحد حدود استخدام Gemini",
}
QUOTA_PRIORITIES = {
    "unknown": 0,
    "rpm": 1,
    "ipm": 2,
    "tpm": 3,
    "rpd": 4,
    "tpd": 5,
}


class GeminiRateLimit(GeminiFailure):
    """A structured 429 response returned by the Gemini API."""

    def __init__(
        self,
        *,
        quota_type: str,
        quota_id: str = "",
        quota_metric: str = "",
        retry_after_seconds: float | None = None,
        provider_message: str = "",
    ) -> None:
        self.quota_type = quota_type
        self.quota_id = quota_id
        self.quota_metric = quota_metric
        self.retry_after_seconds = retry_after_seconds
        self.provider_message = provider_message
        label = QUOTA_LABELS.get(quota_type, QUOTA_LABELS["unknown"])
        identifier = quota_id or quota_metric
        message = f"أعاد Gemini خطأ 429 بسبب بلوغ {label}."
        if identifier:
            message += f" معرّف الحصة: {identifier}."
        if retry_after_seconds is not None:
            message += f" طلبت Google الانتظار {retry_after_seconds:g} ثانية."
        if provider_message:
            message += f" رسالة Google: {provider_message.strip()[:500]}"
        super().__init__(message)


def _quota_type(*values: str) -> str:
    signal = " ".join(value for value in values if value).lower()
    compact = re.sub(r"[^a-z0-9]+", "", signal)
    daily = "perday" in compact or "daily" in compact
    minute = "perminute" in compact or "minute" in compact
    if "token" in compact and daily:
        return "tpd"
    if "token" in compact and minute:
        return "tpm"
    if "image" in compact and minute:
        return "ipm"
    if "request" in compact and daily:
        return "rpd"
    if "request" in compact and (
        "perminute" in compact or "persecond" in compact or "minute" in compact
    ):
        return "rpm"
    if daily:
        return "rpd"
    return "unknown"


def _retry_delay(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return max(0.0, float(value))
    if isinstance(value, str):
        match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)s\s*", value)
        return float(match.group(1)) if match else None
    if isinstance(value, dict):
        try:
            return max(
                0.0,
                float(value.get("seconds", 0)) + float(value.get("nanos", 0)) / 1_000_000_000,
            )
        except (TypeError, ValueError):
            return None
    return None


def _rate_limit_from_error(exc: Exception, api_key: str) -> GeminiRateLimit | None:
    if getattr(exc, "code", None) != 429:
        return None
    payload = getattr(exc, "details", None)
    error = payload.get("error", payload) if isinstance(payload, dict) else {}
    details = error.get("details", []) if isinstance(error, dict) else []
    provider_message = str(
        getattr(exc, "message", None) or error.get("message", "") or ""
    ).replace(api_key, "[API_KEY]")
    quota_id = ""
    quota_metric = ""
    quota_type = "unknown"
    retry_after_seconds = None
    for detail in details if isinstance(details, list) else []:
        if not isinstance(detail, dict):
            continue
        detail_type = str(detail.get("@type", ""))
        if detail_type.endswith("QuotaFailure"):
            violations = detail.get("violations", [])
            for violation in violations if isinstance(violations, list) else []:
                if not isinstance(violation, dict):
                    continue
                candidate_id = str(violation.get("quotaId", "") or "")
                candidate_metric = str(violation.get("quotaMetric", "") or "")
                candidate_type = _quota_type(candidate_id, candidate_metric, provider_message)
                if (
                    not quota_id
                    or QUOTA_PRIORITIES[candidate_type] > QUOTA_PRIORITIES[quota_type]
                ):
                    quota_type = candidate_type
                    quota_id = candidate_id
                    quota_metric = candidate_metric
        elif detail_type.endswith("RetryInfo"):
            retry_after_seconds = _retry_delay(detail.get("retryDelay"))
    return GeminiRateLimit(
        quota_type=quota_type if quota_type != "unknown" else _quota_type(provider_message),
        quota_id=quota_id,
        quota_metric=quota_metric,
        retry_after_seconds=retry_after_seconds,
        provider_message=provider_message,
    )


def inspect_pdf(source: bytes | Path) -> int:
    try:
        import pypdfium2 as pdfium
    except ImportError as exc:
        raise RuntimeError("حزمة pypdfium2 غير مثبتة. ثبّت متطلبات التطبيق أولًا") from exc
    try:
        document_source = io.BytesIO(source) if isinstance(source, bytes) else source
        with pdfium.PdfDocument(document_source) as pdf:
            count = len(pdf)
    except Exception as exc:
        raise ValueError(f"تعذر فتح ملف PDF: {exc}") from exc
    if count < 1:
        raise ValueError("ملف PDF خالٍ من الصفحات")
    return count


def _encode_jpeg_with_limit(image: Any, max_bytes: int) -> bytes:
    """Keep one rendered page within its share of the inline request budget."""
    from PIL import Image as PillowImage

    if max_bytes < 64 * 1024:
        raise ValueError("ميزانية صورة الصفحة صغيرة جدًا")

    working = image
    try:
        while True:
            last_output = b""
            for quality in (86, 76, 66, 56):
                output = io.BytesIO()
                working.save(output, "JPEG", quality=quality, optimize=True)
                last_output = output.getvalue()
                if len(last_output) <= max_bytes:
                    return last_output

            width, height = working.size
            if min(width, height) <= 640:
                raise GeminiFailure(
                    "تعذر ضغط صفحة الكتاب إلى حجم آمن من دون الإضرار الشديد بوضوح النص"
                )
            shrink = max(0.65, min(0.88, (max_bytes / len(last_output)) ** 0.5 * 0.96))
            resized = working.resize(
                (max(640, round(width * shrink)), max(640, round(height * shrink))),
                resample=PillowImage.Resampling.LANCZOS,
            )
            if working is not image:
                working.close()
            working = resized
    finally:
        if working is not image:
            working.close()


def render_page(
    path: Path,
    page_number: int,
    dpi: int = 160,
    *,
    max_bytes: int = MAX_INLINE_IMAGE_BYTES,
) -> bytes:
    try:
        import pypdfium2 as pdfium
        from PIL import Image
    except ImportError as exc:
        raise RuntimeError("حزم قراءة PDF غير مثبتة. ثبّت متطلبات التطبيق أولًا") from exc
    try:
        with pdfium.PdfDocument(path) as pdf:
            page = pdf[page_number - 1]
            try:
                width, height = page.get_size()
                safe_dpi = min(dpi, int((30_000_000 / max(width * height, 1)) ** 0.5 * 72))
                bitmap = page.render(scale=max(safe_dpi, 72) / 72)
                try:
                    image: Image.Image = bitmap.to_pil().convert("RGB")
                    try:
                        return _encode_jpeg_with_limit(image, max_bytes)
                    finally:
                        image.close()
                finally:
                    bitmap.close()
            finally:
                page.close()
    except Exception as exc:
        raise GeminiFailure(f"تعذر تجهيز الصفحة {page_number}: {exc}") from exc


FIELD_LABELS = {
    "title": "العنوان",
    "author": "المؤلف",
    "editor": "المحقق",
    "publisher": "دار النشر",
    "publication_year": "سنة النشر",
    "edition_number": "رقم الطبعة",
    "volume_number": "رقم المجلد",
    "topic": "الموضوع",
}


def render_prompt(
    template: str,
    *,
    file_name: str,
    page_number: int | str,
    max_pages: int,
    previous: dict[str, Any],
    missing_fields: list[str] | None = None,
) -> str:
    missing_labels = [FIELD_LABELS.get(field, field) for field in (missing_fields or [])]
    replacements = {
        "{{file_name}}": file_name,
        "{{page_number}}": str(page_number),
        "{{max_pages}}": str(max_pages),
        "{{previous_results}}": json.dumps(previous, ensure_ascii=False, separators=(",", ":")),
        "{{missing_fields}}": "، ".join(missing_labels) or "لا توجد حقول ناقصة",
    }
    prompt = template
    for token, value in replacements.items():
        prompt = prompt.replace(token, value)
    return prompt


def _usage(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if hasattr(value, "model_dump"):
        return value.model_dump(exclude_none=True)
    return None


@dataclass
class ExtractionResult:
    data: BookExtraction
    raw: str
    usage: dict[str, Any] | None


class GeminiCataloguer:
    def __init__(self, api_key: str, client: Any | None = None):
        if not api_key.strip():
            raise ValueError("أدخل مفتاح Gemini API")
        self.api_key = api_key.strip()
        if client is not None:
            self.client = client
            return
        try:
            from google import genai
            from google.genai import types
        except ImportError as exc:
            raise RuntimeError("حزمة Google Gen AI غير مثبتة. ثبّت متطلبات التطبيق أولًا") from exc
        self.client = genai.Client(
            api_key=self.api_key,
            http_options=types.HttpOptions(
                retry_options=types.HttpRetryOptions(attempts=1),
            ),
        )

    def extract(
        self,
        image_bytes: bytes | Sequence[bytes],
        *,
        model: str,
        prompt: str,
    ) -> ExtractionResult:
        if not MODEL_PATTERN.fullmatch(model):
            raise ValueError("اسم نموذج Gemini غير صالح")
        pages = [image_bytes] if isinstance(image_bytes, bytes) else list(image_bytes)
        if not pages:
            raise ValueError("يجب إرفاق صورة صفحة واحدة على الأقل")
        try:
            from google.genai import types

            images = [
                types.Part.from_bytes(data=page, mime_type="image/jpeg")
                for page in pages
            ]
            config = types.GenerateContentConfig(
                system_instruction=SYSTEM_INSTRUCTION,
                temperature=0,
                max_output_tokens=4096,
                response_mime_type="application/json",
                response_json_schema=BOOK_REPLY_SCHEMA,
            )
            response = self.client.models.generate_content(
                model=model,
                contents=[prompt, *images],
                config=config,
            )
        except Exception as exc:
            rate_limit = _rate_limit_from_error(exc, self.api_key)
            if rate_limit is not None:
                raise rate_limit from exc
            message = str(exc).replace(self.api_key, "[API_KEY]")
            raise GeminiFailure(message) from exc
        raw = getattr(response, "text", None) or ""
        if not raw.strip():
            raise GeminiFailure("أعاد Gemini ردًا فارغًا")
        try:
            data = BookExtraction.model_validate_json(raw)
        except (ValidationError, json.JSONDecodeError) as exc:
            raise GeminiFailure(f"رد Gemini لا يطابق مخطط بيانات الكتاب: {exc}") from exc
        return ExtractionResult(data=data, raw=raw, usage=_usage(getattr(response, "usage_metadata", None)))
