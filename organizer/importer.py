from __future__ import annotations

import os
from pathlib import Path


def local_pdf(path_value: str) -> Path:
    path = Path(path_value).expanduser()
    if not path.is_absolute():
        raise ValueError("يجب اختيار ملف PDF من جهازك")
    try:
        path = path.resolve(strict=True)
    except OSError as exc:
        raise ValueError("ملف PDF غير موجود على القرص") from exc
    if not path.is_file() or path.suffix.lower() != ".pdf":
        raise ValueError("اختر ملف PDF صالحًا")
    if path.stat().st_size > 500 * 1024 * 1024:
        raise ValueError("حجم ملف PDF يتجاوز 500 ميجابايت")
    return path


def discover_pdfs(
    path_value: str,
    *,
    include_subfolders: bool,
    max_depth: int,
    per_folder_limit: int,
) -> tuple[Path, list[Path], int]:
    root = Path(path_value).expanduser()
    if not root.is_absolute():
        raise ValueError("يجب اختيار مجلد من جهازك")
    try:
        root = root.resolve(strict=True)
    except OSError as exc:
        raise ValueError("مجلد الكتب غير موجود على القرص") from exc
    if not root.is_dir():
        raise ValueError("المسار المختار ليس مجلدًا")

    selected: list[Path] = []
    folder_count = 0
    for current_value, directories, names in os.walk(root, followlinks=False):
        current = Path(current_value)
        depth = len(current.relative_to(root).parts)
        if not include_subfolders or depth >= max_depth:
            directories[:] = []
        directories.sort(key=str.casefold)
        pdf_names = sorted(
            (name for name in names if Path(name).suffix.lower() == ".pdf"),
            key=str.casefold,
        )
        chosen = pdf_names[:per_folder_limit]
        if chosen:
            folder_count += 1
            selected.extend(current / name for name in chosen)
    return root, selected, folder_count
