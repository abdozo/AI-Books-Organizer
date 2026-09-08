from __future__ import annotations

import io
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .models import BOOK_REPLY_SCHEMA, SYSTEM_INSTRUCTION, BookExtraction


MODEL_PATTERN = re.compile(r"gemini-[A-Za-z0-9._-]+")


class GeminiFailure(RuntimeError):
    pass


def inspect_pdf(data: bytes) -> int:
    try:
        import pypdfium2 as pdfium
    except ImportError as exc:
        raise RuntimeError("حزمة pypdfium2 غير مثبتة. ثبّت متطلبات التطبيق أولًا") from exc
    try:
        with pdfium.PdfDocument(io.BytesIO(data)) as pdf:
            count = len(pdf)
    except Exception as exc:
        raise ValueError(f"تعذر فتح ملف PDF: {exc}") from exc
    if count < 1:
        raise ValueError("ملف PDF خالٍ من الصفحات")
    if count > 5000:
        raise ValueError("الحد الأقصى 5000 صفحة للكتاب")
    return count


def render_page(path: Path, page_number: int, dpi: int = 160) -> bytes:
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
                    output = io.BytesIO()
                    image.save(output, "JPEG", quality=88, optimize=True)
                    return output.getvalue()
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
    page_number: int,
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
        except ImportError as exc:
            raise RuntimeError("حزمة Google Gen AI غير مثبتة. ثبّت متطلبات التطبيق أولًا") from exc
        self.client = genai.Client(api_key=self.api_key)

    def extract(self, image_bytes: bytes, *, model: str, prompt: str) -> ExtractionResult:
        if not MODEL_PATTERN.fullmatch(model):
            raise ValueError("اسم نموذج Gemini غير صالح")
        try:
            from google.genai import types

            image = types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg")
            config = types.GenerateContentConfig(
                system_instruction=SYSTEM_INSTRUCTION,
                temperature=0,
                max_output_tokens=4096,
                response_mime_type="application/json",
                response_json_schema=BOOK_REPLY_SCHEMA,
            )
            response = self.client.models.generate_content(
                model=model,
                contents=[prompt, image],
                config=config,
            )
        except Exception as exc:
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
