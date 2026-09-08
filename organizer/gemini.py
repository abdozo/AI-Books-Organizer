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
    if count > 5000:
        raise ValueError(
            f"أبلغ قارئ PDF عن {count:,} صفحة في بنية الملف، "
            "بينما حد الأمان 5,000 صفحة"
        )
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
