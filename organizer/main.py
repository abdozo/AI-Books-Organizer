from __future__ import annotations

import os
import signal
import threading
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .database import ENTITY_FIELDS, Library
from .gemini import inspect_pdf
from .models import DEFAULT_PROMPT, BookInput, EntityChange, ScanInput, SettingsInput
from .scanner import ScanManager
from .secrets import DatabaseSecretStore, SecretStore


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def create_app(data_root: Path | None = None, secret_store: SecretStore | None = None) -> FastAPI:
    root = Path(data_root or os.environ.get("AI_BOOKS_DATA_DIR", PROJECT_ROOT / "data"))
    library = Library(root)
    secrets = secret_store or DatabaseSecretStore(library)
    scans = ScanManager(library, secrets)

    app = FastAPI(title="منظم الكتب", version="1.0.0")
    instance_id = os.environ.get("AI_BOOKS_INSTANCE_ID", "")
    app.state.library = library
    app.state.secrets = secrets
    app.state.scans = scans
    app.mount("/static", StaticFiles(directory=PROJECT_ROOT / "static"), name="static")

    @app.exception_handler(KeyError)
    async def missing_handler(_request: Request, exc: KeyError) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(exc).strip("'")})

    @app.exception_handler(ValueError)
    async def value_handler(_request: Request, exc: ValueError) -> JSONResponse:
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.get("/", include_in_schema=False)
    def home() -> FileResponse:
        return FileResponse(PROJECT_ROOT / "ai-books-organizer.html")

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "version": app.version, "instanceId": instance_id}

    @app.post("/api/runtime/shutdown", include_in_schema=False)
    def shutdown(request: Request) -> dict[str, bool]:
        supplied_id = request.headers.get("x-instance-id", "")
        if not instance_id or supplied_id != instance_id:
            raise HTTPException(status_code=403, detail="تعذر التحقق من نسخة الخادم")
        threading.Timer(0.25, lambda: os.kill(os.getpid(), signal.SIGTERM)).start()
        return {"ok": True}

    @app.get("/api/bootstrap")
    def bootstrap() -> dict[str, Any]:
        key_saved = secrets.exists()
        return {
            "books": library.list_books(),
            "scan": library.latest_scan(),
            "settings": {
                "prompt": library.setting("prompt"),
                "defaultPrompt": DEFAULT_PROMPT,
                "model": library.setting("model"),
                "keySaved": key_saved,
            },
            "libraryLabel": "المكتبة المحلية",
        }

    @app.put("/api/settings")
    def save_settings(payload: SettingsInput) -> dict[str, Any]:
        if payload.api_key:
            secrets.save(payload.api_key)
        elif not secrets.exists():
            raise ValueError("أدخل مفتاح Gemini API قبل الحفظ")
        library.save_settings(payload.prompt, payload.model)
        return {"saved": True, "keySaved": True}

    @app.post("/api/uploads", status_code=201)
    async def upload_pdf(request: Request) -> dict[str, Any]:
        encoded_name = request.headers.get("x-file-name", "")
        if not encoded_name:
            raise ValueError("اسم الملف مطلوب")
        content_length = int(request.headers.get("content-length", "0") or 0)
        if content_length > 500 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="حجم ملف PDF يتجاوز 500 ميجابايت")
        data = await request.body()
        page_count = inspect_pdf(data)
        file_name = unquote(encoded_name)
        source_label = unquote(request.headers.get("x-relative-path", encoded_name))
        return library.create_uploaded_book(file_name, data, source_label, page_count)

    @app.post("/api/books/manual", status_code=201)
    def create_manual(payload: BookInput) -> dict[str, Any]:
        return library.create_manual_book(payload.model_dump())

    @app.patch("/api/books/{book_id}")
    def update_book(book_id: str, payload: BookInput) -> dict[str, Any]:
        return library.update_book(book_id, payload.model_dump())

    @app.get("/api/books/{book_id}/pdf", include_in_schema=False)
    def open_pdf(book_id: str) -> FileResponse:
        path = library.source_path(book_id)
        return FileResponse(path, media_type="application/pdf", filename=path.name, content_disposition_type="inline")

    @app.post("/api/scans", status_code=202)
    def create_scan(payload: ScanInput) -> dict[str, str]:
        active = library.latest_scan()
        if active and active["state"] in {"queued", "running"}:
            raise ValueError("يوجد فحص جارٍ بالفعل. أوقفه قبل بدء فحص جديد")
        scan_id = library.create_scan(payload.book_ids, payload.max_pages)
        scans.start(scan_id)
        return {"id": scan_id}

    @app.post("/api/scans/{scan_id}/{action}")
    def control_scan(scan_id: str, action: str) -> dict[str, bool]:
        commands = {
            "pause": scans.pause,
            "resume": scans.resume,
            "cancel": scans.cancel,
            "skip": scans.skip,
        }
        command = commands.get(action)
        if command is None:
            raise HTTPException(status_code=404, detail="إجراء الفحص غير موجود")
        command(scan_id)
        return {"ok": True}

    @app.post("/api/entities/{field}/change")
    def change_entities(field: str, payload: EntityChange) -> dict[str, int]:
        if field not in ENTITY_FIELDS:
            raise ValueError("حقل الفهرس غير صالح")
        affected = library.change_entities(field, payload.names, payload.canonical)
        return {"affected": affected}

    return app


app = create_app()
