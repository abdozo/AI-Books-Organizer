from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


_TK_PICKER = r"""
import sys
import tkinter as tk
from tkinter import filedialog

root = tk.Tk()
root.withdraw()
root.attributes("-topmost", True)
root.update()
if sys.argv[1] == "folder":
    selected = filedialog.askdirectory(parent=root, title="اختر مجلد الكتب", mustexist=True)
else:
    selected = filedialog.askopenfilename(
        parent=root,
        title="اختر ملف PDF",
        filetypes=[("PDF", "*.pdf")],
    )
root.destroy()
if selected:
    print(selected)
"""


def _pick_local_path(kind: str) -> Path | None:
    """Open a native picker in a child process and return the selected local path."""
    if kind not in {"file", "folder"}:
        raise ValueError("نوع اختيار المسار غير صالح")
    try:
        result = subprocess.run(
            [sys.executable, "-c", _TK_PICKER, kind],
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise OSError("تعذر فتح نافذة اختيار الملفات") from exc
    if result.returncode != 0:
        raise OSError("تعذر فتح نافذة اختيار الملفات")
    selected = next((line for line in reversed(result.stdout.splitlines()) if line), "")
    return Path(selected) if selected else None


def pick_local_pdf() -> Path | None:
    return _pick_local_path("file")


def pick_local_folder() -> Path | None:
    return _pick_local_path("folder")


def reveal_in_file_manager(path: Path) -> None:
    """Open the containing folder and select the book when the OS supports it."""
    target = Path(path).resolve()
    if not target.exists():
        raise FileNotFoundError("ملف الكتاب غير موجود على القرص")
    if sys.platform == "darwin":
        subprocess.Popen(["open", "-R", str(target)])
    elif os.name == "nt":
        subprocess.Popen(["explorer", f"/select,{target}"])
    else:
        folder = target if target.is_dir() else target.parent
        subprocess.Popen(["xdg-open", str(folder)])
