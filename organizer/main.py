from __future__ import annotations

import os
import signal
import threading
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from .database import ENTITY_FIELDS, Library
from .exporter import build_books_docx
from .gemini import inspect_pdf
from .importer import discover_pdfs, local_pdf
from .models import (
    BookDeleteInput,
    BookExportInput,
    BookInput,
    BookMoveInput,
    DEFAULT_PROMPT,
    EntityChange,
    LocalFileInput,
    LocalFolderInput,
    ScanInput,
    SettingsInput,
)
from .rate_limits import model_options, quota_for
from .scanner import ScanManager
from .secrets import DatabaseSecretStore, SecretStore
from .system import pick_local_folder, pick_local_pdf, reveal_in_file_manager


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
            "models": model_options(),
            "libraryLabel": "المكتبة المحلية",
        }

    @app.put("/api/settings")
    def save_settings(payload: SettingsInput) -> dict[str, Any]:
        quota_for(payload.model)
        if payload.api_key:
            secrets.save(payload.api_key)
        elif not secrets.exists():
            raise ValueError("أدخل مفتاح Gemini API قبل الحفظ")
        library.save_settings(payload.prompt, payload.model)
        return {"saved": True, "keySaved": True}

    @app.post("/api/local-picker/file", include_in_schema=False)
    def choose_local_file() -> dict[str, str]:
        path = pick_local_pdf()
        return {"path": str(path) if path else ""}

    @app.post("/api/local-picker/folder", include_in_schema=False)
    def choose_local_folder() -> dict[str, str]:
        path = pick_local_folder()
        return {"path": str(path) if path else ""}

    @app.post("/api/imports/file", status_code=201)
    def import_file(payload: LocalFileInput) -> dict[str, Any]:
        path = local_pdf(payload.path)
        return library.create_referenced_book(path, inspect_pdf(path))

    def folder_paths(payload: LocalFolderInput) -> tuple[Path, list[Path], int]:
        return discover_pdfs(
            payload.path,
            include_subfolders=payload.include_subfolders,
            max_depth=payload.max_depth,
            per_folder_limit=payload.per_folder_limit,
        )

    @app.post("/api/imports/folder/preview")
    def preview_folder(payload: LocalFolderInput) -> dict[str, Any]:
        root, paths, folder_count = folder_paths(payload)
        return {"path": str(root), "count": len(paths), "folderCount": folder_count}

    @app.post("/api/imports/folder", status_code=201)
    def import_folder(payload: LocalFolderInput) -> dict[str, Any]:
        root, paths, folder_count = folder_paths(payload)
        entries = [(path, inspect_pdf(path)) for path in paths]
        book_ids = library.create_referenced_books(entries)
        return {
            "path": str(root),
            "count": len(book_ids),
            "folderCount": folder_count,
            "bookIds": book_ids,
        }

    @app.post("/api/books/manual", status_code=201)
    def create_manual(payload: BookInput) -> dict[str, Any]:
        return library.create_manual_book(payload.model_dump())

    @app.post("/api/books/move")
    def move_books(payload: BookMoveInput) -> dict[str, int]:
        affected = library.move_books_to_topic(payload.book_ids, payload.topic)
        return {"affected": affected}

    @app.delete("/api/books")
    def delete_books(payload: BookDeleteInput) -> dict[str, int]:
        deleted = library.delete_books(payload.book_ids)
        return {"deleted": deleted}

    @app.patch("/api/books/{book_id}")
    def update_book(book_id: str, payload: BookInput) -> dict[str, Any]:
        return library.update_book(book_id, payload.model_dump())

    @app.get("/api/books/{book_id}/pdf", include_in_schema=False)
    def open_pdf(book_id: str) -> FileResponse:
        path = library.source_path(book_id)
        return FileResponse(path, media_type="application/pdf", filename=path.name, content_disposition_type="inline")

    @app.post("/api/books/{book_id}/folder", include_in_schema=False)
    def open_book_folder(book_id: str) -> dict[str, bool]:
        try:
            path = library.book_file_path(book_id)
            reveal_in_file_manager(path)
        except (FileNotFoundError, OSError) as exc:
            raise ValueError(f"تعذر فتح مجلد الكتاب: {exc}") from exc
        return {"opened": True}

    @app.post("/api/books/export", include_in_schema=False)
    def export_books(payload: BookExportInput) -> Response:
        books = [library.get_book(book_id) for book_id in payload.book_ids]
        document = build_books_docx(books)
        return Response(
            document,
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            headers={"Content-Disposition": "attachment; filename=books-catalog.docx"},
        )

    @app.post("/api/scans", status_code=202)
    def create_scan(payload: ScanInput) -> dict[str, str]:
        active = library.latest_scan()
        if active and active["state"] in {"queued", "running"}:
            raise ValueError("يوجد فحص جارٍ بالفعل. أوقفه قبل بدء فحص جديد")
        model = library.setting("model")
        quota_for(model)
        scan_id = library.create_scan(payload.book_ids, payload.max_pages, model=model)
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
