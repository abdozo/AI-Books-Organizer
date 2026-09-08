from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .models import DEFAULT_PROMPT, PREVIOUS_DEFAULT_PROMPTS


ENTITY_FIELDS = {"author", "editor", "publisher", "topic"}
DEFAULT_MODEL = "gemini-3.5-flash-lite"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


SCHEMA = """
CREATE TABLE IF NOT EXISTS books (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  author TEXT NOT NULL DEFAULT '',
  editor TEXT NOT NULL DEFAULT '',
  publisher TEXT NOT NULL DEFAULT '',
  publication_year TEXT NOT NULL DEFAULT '',
  edition_number TEXT NOT NULL DEFAULT '',
  volume_number TEXT NOT NULL DEFAULT '',
  topic TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'review',
  file_name TEXT NOT NULL DEFAULT '',
  stored_path TEXT NOT NULL DEFAULT '',
  source_label TEXT NOT NULL DEFAULT '',
  page_count INTEGER NOT NULL DEFAULT 0,
  pages_checked INTEGER NOT NULL DEFAULT 0,
  max_pages INTEGER NOT NULL DEFAULT 0,
  confidence INTEGER NOT NULL DEFAULT 0,
  attempts INTEGER NOT NULL DEFAULT 0,
  error TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS book_history (
  id TEXT PRIMARY KEY,
  book_id TEXT NOT NULL REFERENCES books(id) ON DELETE CASCADE,
  event TEXT NOT NULL,
  attempt INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS scans (
  id TEXT PRIMARY KEY,
  state TEXT NOT NULL,
  max_pages INTEGER NOT NULL,
  model TEXT NOT NULL DEFAULT '',
  current_index INTEGER NOT NULL DEFAULT 0,
  current_page INTEGER NOT NULL DEFAULT 0,
  buffer_until INTEGER NOT NULL DEFAULT 0,
  rate_limit_rpm INTEGER NOT NULL DEFAULT 0,
  rate_limit_rpd INTEGER NOT NULL DEFAULT 0,
  rate_limit_window TEXT NOT NULL DEFAULT '',
  paused INTEGER NOT NULL DEFAULT 0,
  cancel_requested INTEGER NOT NULL DEFAULT 0,
  skip_requested INTEGER NOT NULL DEFAULT 0,
  error TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS scan_items (
  id TEXT PRIMARY KEY,
  scan_id TEXT NOT NULL REFERENCES scans(id) ON DELETE CASCADE,
  book_id TEXT NOT NULL REFERENCES books(id) ON DELETE CASCADE,
  position INTEGER NOT NULL,
  state TEXT NOT NULL DEFAULT 'waiting',
  UNIQUE(scan_id, position)
);
CREATE TABLE IF NOT EXISTS api_request_reservations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  model TEXT NOT NULL,
  reserved_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS scan_file_history (
  path TEXT PRIMARY KEY,
  folder_path TEXT NOT NULL DEFAULT '',
  book_id TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS books_updated ON books(updated_at DESC);
CREATE INDEX IF NOT EXISTS scan_items_scan ON scan_items(scan_id, position);
CREATE INDEX IF NOT EXISTS scan_file_history_folder ON scan_file_history(folder_path);
CREATE INDEX IF NOT EXISTS api_requests_model_time
ON api_request_reservations(model, reserved_at);
"""


class Library:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / "library.sqlite3"
        self._migrate()

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.db_path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA journal_mode=WAL")
        return db

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        db = self.connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _migrate(self) -> None:
        with self.connect() as db:
            db.executescript(SCHEMA)
            book_columns = {row[1] for row in db.execute("PRAGMA table_info(books)")}
            for column in ("publication_year", "edition_number", "volume_number"):
                if column not in book_columns:
                    db.execute(f"ALTER TABLE books ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")
            scan_columns = {row[1] for row in db.execute("PRAGMA table_info(scans)")}
            scan_migrations = {
                "model": "TEXT NOT NULL DEFAULT ''",
                "buffer_until": "INTEGER NOT NULL DEFAULT 0",
                "rate_limit_rpm": "INTEGER NOT NULL DEFAULT 0",
                "rate_limit_rpd": "INTEGER NOT NULL DEFAULT 0",
                "rate_limit_window": "TEXT NOT NULL DEFAULT ''",
            }
            for column, definition in scan_migrations.items():
                if column not in scan_columns:
                    db.execute(f"ALTER TABLE scans ADD COLUMN {column} {definition}")
            db.execute("INSERT OR IGNORE INTO settings VALUES ('prompt', ?)", (DEFAULT_PROMPT,))
            for previous_prompt in PREVIOUS_DEFAULT_PROMPTS:
                db.execute(
                    "UPDATE settings SET value=? WHERE key='prompt' AND value=?",
                    (DEFAULT_PROMPT, previous_prompt),
                )
            db.execute("INSERT OR IGNORE INTO settings VALUES ('model', ?)", (DEFAULT_MODEL,))
            now = utc_now()
            db.execute(
                """INSERT OR IGNORE INTO scan_file_history(path,folder_path,book_id,created_at)
                   SELECT stored_path,'',id,? FROM books WHERE stored_path<>''""",
                (now,),
            )
            db.execute(
                """INSERT OR IGNORE INTO scan_file_history(path,folder_path,book_id,created_at)
                   SELECT source_label,'',id,? FROM books WHERE source_label<>''""",
                (now,),
            )
            db.execute(
                "UPDATE scans SET state='interrupted', error='أُغلق التطبيق قبل اكتمال الفحص', updated_at=? WHERE state IN ('running','queued')",
                (now,),
            )
            db.execute(
                "UPDATE books SET status='review', error='توقف الفحص قبل اكتماله', updated_at=? WHERE status IN ('current','waiting')",
                (now,),
            )

    def setting(self, key: str, default: str = "") -> str:
        with self.connect() as db:
            row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return str(row[0]) if row else default

    def save_settings(self, prompt: str, model: str) -> None:
        with self.transaction() as db:
            db.executemany(
                "INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (("prompt", prompt), ("model", model)),
            )

    def save_api_key(self, api_key: str) -> None:
        value = api_key.strip()
        if not value:
            raise ValueError("مفتاح Gemini فارغ")
        with self.transaction() as db:
            db.execute(
                "INSERT INTO settings(key,value) VALUES ('api_key',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (value,),
            )

    def _history(self, db: sqlite3.Connection, book_id: str, event: str, attempt: int = 0) -> None:
        db.execute(
            "INSERT INTO book_history VALUES (?,?,?,?,?)",
            (uuid.uuid4().hex, book_id, event, attempt, utc_now()),
        )

    def create_manual_book(self, values: dict[str, str]) -> dict[str, Any]:
        book_id = uuid.uuid4().hex
        now = utc_now()
        path = values.get("path", "")
        file_name = Path(path).name if path else f"{values['title']}.pdf"
        with self.transaction() as db:
            db.execute(
                """INSERT INTO books
                   (id,title,author,editor,publisher,publication_year,edition_number,volume_number,topic,
                    status,file_name,source_label,confidence,created_at,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,'complete',?,?,100,?,?)""",
                (
                    book_id,
                    values["title"],
                    values.get("author", ""),
                    values.get("editor", ""),
                    values.get("publisher", ""),
                    values.get("publication_year", ""),
                    values.get("edition_number", ""),
                    values.get("volume_number", ""),
                    values.get("topic", ""),
                    file_name,
                    path,
                    now,
                    now,
                ),
            )
            self._history(db, book_id, "أُضيف يدويًا")
        return self.get_book(book_id)

    def processed_source_paths(self) -> set[str]:
        with self.connect() as db:
            rows = db.execute(
                """SELECT path FROM scan_file_history
                   UNION SELECT stored_path FROM books WHERE stored_path<>''
                   UNION SELECT source_label FROM books WHERE source_label<>''"""
            ).fetchall()
        return {
            str(Path(row[0]).expanduser().resolve())
            for row in rows
            if row[0] and Path(row[0]).expanduser().is_absolute()
        }

    def create_referenced_books(
        self,
        entries: list[tuple[Path, int]],
        *,
        folder_root: Path | None = None,
        skip_processed: bool = False,
    ) -> list[str]:
        if not entries:
            return []
        now = utc_now()
        book_ids: list[str] = []
        with self.transaction() as db:
            for path, page_count in entries:
                normalized_path = str(path.expanduser().resolve())
                if skip_processed:
                    exists = db.execute(
                        """SELECT 1 FROM scan_file_history WHERE path=?
                           UNION SELECT 1 FROM books WHERE stored_path=? OR source_label=?
                           LIMIT 1""",
                        (normalized_path, normalized_path, normalized_path),
                    ).fetchone()
                    if exists:
                        continue
                book_id = uuid.uuid4().hex
                book_ids.append(book_id)
                db.execute(
                    """INSERT INTO books
                       (id,title,status,file_name,stored_path,source_label,page_count,created_at,updated_at)
                       VALUES (?,?,'waiting',?,?,?,?,?,?)""",
                    (
                        book_id, path.stem, path.name, normalized_path,
                        normalized_path, page_count, now, now,
                    ),
                )
                db.execute(
                    """INSERT OR IGNORE INTO scan_file_history
                       (path,folder_path,book_id,created_at) VALUES (?,?,?,?)""",
                    (
                        normalized_path,
                        str(folder_root.resolve()) if folder_root else "",
                        book_id,
                        now,
                    ),
                )
                self._history(db, book_id, "أُضيف إلى المكتبة وينتظر الفحص")
        return book_ids

    def create_referenced_book(self, path: Path, page_count: int) -> dict[str, Any]:
        book_id = self.create_referenced_books([(path, page_count)])[0]
        return self.get_book(book_id)

    def _book_dict(self, row: sqlite3.Row, history: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        book = dict(row)
        book["file"] = book.pop("file_name")
        source_label = book.pop("source_label")
        stored_path = book.get("stored_path", "")
        source_path = Path(source_label).expanduser() if source_label else None
        managed_path = Path(stored_path).resolve() if stored_path else None
        source_is_real = bool(source_path and source_path.is_absolute() and source_path.exists())
        book["path"] = source_label or stored_path
        book["fullPath"] = str(source_path if source_is_real else managed_path or "")
        book["pagesChecked"] = book.pop("pages_checked")
        book["maxPages"] = book.pop("max_pages")
        book["pageCount"] = book.pop("page_count")
        book["updated"] = book.pop("updated_at")
        book["created"] = book.pop("created_at")
        book.pop("stored_path", None)
        if history is not None:
            book["history"] = history
        return book

    def list_books(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM books ORDER BY updated_at DESC").fetchall()
        return [self._book_dict(row) for row in rows]

    def get_book(self, book_id: str) -> dict[str, Any]:
        with self.connect() as db:
            row = db.execute("SELECT * FROM books WHERE id=?", (book_id,)).fetchone()
            if row is None:
                raise KeyError("الكتاب غير موجود")
            history = [
                {"date": item["created_at"], "text": item["event"], "attempt": item["attempt"]}
                for item in db.execute(
                    "SELECT event,attempt,created_at FROM book_history WHERE book_id=? ORDER BY created_at DESC, rowid DESC",
                    (book_id,),
                )
            ]
        return self._book_dict(row, history)

    def source_path(self, book_id: str) -> Path:
        with self.connect() as db:
            row = db.execute("SELECT stored_path FROM books WHERE id=?", (book_id,)).fetchone()
        if row is None:
            raise KeyError("الكتاب غير موجود")
        if not row[0]:
            raise ValueError("لا يوجد ملف PDF محفوظ لهذا الكتاب")
        path = Path(row[0])
        if not path.is_file():
            raise FileNotFoundError("ملف PDF غير موجود على القرص")
        return path

    def book_file_path(self, book_id: str) -> Path:
        """Return the original PDF path, with legacy managed copies as fallback."""
        with self.connect() as db:
            row = db.execute(
                "SELECT source_label,stored_path FROM books WHERE id=?", (book_id,)
            ).fetchone()
        if row is None:
            raise KeyError("الكتاب غير موجود")
        source = Path(row["source_label"]).expanduser() if row["source_label"] else None
        if source and source.is_absolute() and source.exists():
            return source
        stored = Path(row["stored_path"]).resolve() if row["stored_path"] else None
        if stored and stored.exists():
            return stored
        raise FileNotFoundError("ملف الكتاب غير موجود على القرص")

    def update_book(self, book_id: str, values: dict[str, str]) -> dict[str, Any]:
        now = utc_now()
        with self.transaction() as db:
            row = db.execute("SELECT attempts FROM books WHERE id=?", (book_id,)).fetchone()
            if row is None:
                raise KeyError("الكتاب غير موجود")
            db.execute(
                """UPDATE books SET title=?,author=?,editor=?,publisher=?,publication_year=?,
                   edition_number=?,volume_number=?,topic=?,
                   status='complete',confidence=100,error='',updated_at=? WHERE id=?""",
                (
                    values["title"], values.get("author", ""), values.get("editor", ""),
                    values.get("publisher", ""), values.get("publication_year", ""),
                    values.get("edition_number", ""), values.get("volume_number", ""),
                    values.get("topic", ""), now, book_id,
                ),
            )
            self._history(db, book_id, "عُدلت البيانات يدويًا", row["attempts"])
        return self.get_book(book_id)

    def update_book_path(self, book_id: str, path: Path, page_count: int) -> dict[str, Any]:
        normalized_path = str(path.expanduser().resolve())
        now = utc_now()
        with self.transaction() as db:
            row = db.execute(
                "SELECT attempts,stored_path,source_label FROM books WHERE id=?",
                (book_id,),
            ).fetchone()
            if row is None:
                raise KeyError("الكتاب غير موجود")
            active = db.execute(
                """SELECT 1 FROM scan_items si
                   JOIN scans s ON s.id=si.scan_id
                   WHERE si.book_id=? AND s.state IN ('queued','running') LIMIT 1""",
                (book_id,),
            ).fetchone()
            if active:
                raise ValueError("لا يمكن تغيير مسار كتاب موجود في فحص جارٍ. أوقف الفحص أولًا")
            previous = row["source_label"] or row["stored_path"]
            if previous == normalized_path:
                return self.get_book(book_id)
            previous_path = Path(previous).expanduser() if previous else None
            if previous_path and previous_path.is_absolute():
                db.execute(
                    """INSERT OR IGNORE INTO scan_file_history
                       (path,folder_path,book_id,created_at) VALUES (?,?,?,?)""",
                    (str(previous_path.resolve()), "", book_id, now),
                )
            db.execute(
                """UPDATE books SET file_name=?,stored_path=?,source_label=?,page_count=?,
                   error='',updated_at=? WHERE id=?""",
                (path.name, normalized_path, normalized_path, page_count, now, book_id),
            )
            db.execute(
                """INSERT OR IGNORE INTO scan_file_history
                   (path,folder_path,book_id,created_at) VALUES (?,?,?,?)""",
                (normalized_path, "", book_id, now),
            )
            self._history(db, book_id, f"تغير مسار ملف PDF إلى {normalized_path}", row["attempts"])
        return self.get_book(book_id)

    def move_books_to_topic(self, book_ids: list[str], topic: str) -> int:
        unique_ids = list(dict.fromkeys(book_id.strip() for book_id in book_ids if book_id.strip()))
        topic = topic.strip()
        if not unique_ids:
            raise ValueError("اختر كتابًا واحدًا على الأقل")
        if not topic:
            raise ValueError("اختر الموضوع الذي ستُنقل إليه الكتب")
        placeholders = ",".join("?" for _ in unique_ids)
        now = utc_now()
        with self.transaction() as db:
            rows = db.execute(
                f"SELECT id,topic,attempts FROM books WHERE id IN ({placeholders})",
                unique_ids,
            ).fetchall()
            if len(rows) != len(unique_ids):
                raise KeyError("تعذر العثور على أحد الكتب")
            changed = [row for row in rows if row["topic"] != topic]
            for row in changed:
                db.execute(
                    "UPDATE books SET topic=?,updated_at=? WHERE id=?",
                    (topic, now, row["id"]),
                )
                previous = row["topic"]
                event = (
                    f"نُقل من موضوع {previous} إلى {topic}"
                    if previous
                    else f"نُقل إلى موضوع {topic}"
                )
                self._history(db, row["id"], event, row["attempts"])
        return len(changed)

    def delete_books(self, book_ids: list[str]) -> int:
        unique_ids = list(dict.fromkeys(book_id.strip() for book_id in book_ids if book_id.strip()))
        if not unique_ids:
            raise ValueError("اختر كتابًا واحدًا على الأقل")
        placeholders = ",".join("?" for _ in unique_ids)
        with self.transaction() as db:
            rows = db.execute(
                f"SELECT id FROM books WHERE id IN ({placeholders})",
                unique_ids,
            ).fetchall()
            if len(rows) != len(unique_ids):
                raise KeyError("تعذر العثور على أحد الكتب")
            active = db.execute(
                f"""SELECT 1 FROM scan_items si
                    JOIN scans s ON s.id=si.scan_id
                    WHERE si.book_id IN ({placeholders})
                      AND s.state IN ('queued','running')
                    LIMIT 1""",
                unique_ids,
            ).fetchone()
            if active:
                raise ValueError("لا يمكن حذف كتاب موجود في فحص جارٍ. أوقف الفحص أولًا")
            cursor = db.execute(
                f"DELETE FROM books WHERE id IN ({placeholders})",
                unique_ids,
            )
        return cursor.rowcount

    def change_entities(self, field: str, names: list[str], canonical: str | None) -> int:
        if field not in ENTITY_FIELDS:
            raise ValueError("حقل الفهرس غير صالح")
        placeholders = ",".join("?" for _ in names)
        now = utc_now()
        replacement = canonical.strip() if canonical is not None else ""
        with self.transaction() as db:
            rows = db.execute(
                f"SELECT id,attempts,status FROM books WHERE {field} IN ({placeholders})", names
            ).fetchall()
            for row in rows:
                status = row["status"] if replacement else "review"
                db.execute(
                    f"UPDATE books SET {field}=?,status=?,updated_at=? WHERE id=?",
                    (replacement, status, now, row["id"]),
                )
                event = f"عُدّل حقل {field} إلى {replacement}" if replacement else f"حُذف حقل {field}"
                self._history(db, row["id"], event, row["attempts"])
        return len(rows)

    def create_scan(self, book_ids: list[str], max_pages: int, model: str = "") -> str:
        unique_ids = list(dict.fromkeys(book_ids))
        scan_id = uuid.uuid4().hex
        now = utc_now()
        with self.transaction() as db:
            rows = db.execute(
                f"SELECT id,stored_path FROM books WHERE id IN ({','.join('?' for _ in unique_ids)})",
                unique_ids,
            ).fetchall()
            found = {row["id"]: row["stored_path"] for row in rows}
            if len(found) != len(unique_ids):
                raise KeyError("تعذر العثور على أحد الكتب")
            if any(not found[book_id] for book_id in unique_ids):
                raise ValueError("لا يمكن فحص كتاب أُضيف يدويًا من دون ملف PDF محفوظ")
            db.execute(
                "INSERT INTO scans(id,state,max_pages,model,created_at,updated_at) VALUES (?,'queued',?,?,?,?)",
                (scan_id, max_pages, model, now, now),
            )
            for position, book_id in enumerate(unique_ids):
                db.execute(
                    "INSERT INTO scan_items VALUES (?,?,?,?,'waiting')",
                    (uuid.uuid4().hex, scan_id, book_id, position),
                )
                db.execute(
                    "UPDATE books SET status='waiting',max_pages=?,error='',updated_at=? WHERE id=?",
                    (max_pages, now, book_id),
                )
        return scan_id

    def reserve_api_request(
        self,
        model: str,
        *,
        reserved_at: float,
        minute_seconds: float,
        day_seconds: float,
        minute_limit: int,
        day_limit: int,
    ) -> tuple[float, str, int, int, int | None]:
        """Atomically reserve an API request across app processes and restarts."""
        if minute_limit < 1 or day_limit < 1:
            raise ValueError("حدود طلبات النموذج غير صالحة")
        minute_cutoff = reserved_at - minute_seconds
        day_cutoff = reserved_at - day_seconds
        with self.transaction() as db:
            db.execute(
                "DELETE FROM api_request_reservations WHERE reserved_at<=?",
                (day_cutoff,),
            )
            request_times = [
                float(row[0])
                for row in db.execute(
                    """SELECT reserved_at FROM api_request_reservations
                       WHERE model=? AND reserved_at>? ORDER BY reserved_at""",
                    (model, day_cutoff),
                )
            ]
            minute_times = [timestamp for timestamp in request_times if timestamp > minute_cutoff]
            minute_wait = (
                max(0.0, minute_times[0] + minute_seconds - reserved_at)
                if len(minute_times) >= minute_limit
                else 0.0
            )
            day_wait = (
                max(0.0, request_times[0] + day_seconds - reserved_at)
                if len(request_times) >= day_limit
                else 0.0
            )
            wait_seconds = max(minute_wait, day_wait)
            if wait_seconds > 0:
                window = "day" if day_wait >= minute_wait else "minute"
                return wait_seconds, window, len(minute_times), len(request_times), None
            cursor = db.execute(
                "INSERT INTO api_request_reservations(model,reserved_at) VALUES (?,?)",
                (model, reserved_at),
            )
            return 0.0, "", len(minute_times) + 1, len(request_times) + 1, int(cursor.lastrowid)

    def complete_api_request(self, reservation_id: int, *, completed_at: float) -> None:
        """Keep a request counted until at least the time its response completed."""
        with self.transaction() as db:
            db.execute(
                """UPDATE api_request_reservations
                   SET reserved_at=MAX(reserved_at, ?) WHERE id=?""",
                (completed_at, reservation_id),
            )

    def latest_scan(self) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM scans ORDER BY created_at DESC, rowid DESC LIMIT 1").fetchone()
            if row is None:
                return None
            items = [dict(item) for item in db.execute(
                """SELECT si.position,si.state,b.id book_id,b.file_name file,b.title,b.author,b.editor,
                          b.publisher,b.publication_year,b.edition_number,b.volume_number,b.topic,b.error
                   FROM scan_items si JOIN books b ON b.id=si.book_id
                   WHERE si.scan_id=? ORDER BY si.position""",
                (row["id"],),
            )]
        result = dict(row)
        result["maxPages"] = result.pop("max_pages")
        result["currentIndex"] = result.pop("current_index")
        result["currentPage"] = result.pop("current_page")
        result["bufferUntil"] = result.pop("buffer_until")
        result["rateLimitRpm"] = result.pop("rate_limit_rpm")
        result["rateLimitRpd"] = result.pop("rate_limit_rpd")
        result["rateLimitWindow"] = result.pop("rate_limit_window")
        result["cancelRequested"] = bool(result.pop("cancel_requested"))
        result["skipRequested"] = bool(result.pop("skip_requested"))
        result["paused"] = bool(result["paused"])
        result["items"] = items
        return result

    def scan(self, scan_id: str) -> dict[str, Any]:
        latest = self.latest_scan()
        if latest is None or latest["id"] != scan_id:
            with self.connect() as db:
                row = db.execute("SELECT * FROM scans WHERE id=?", (scan_id,)).fetchone()
            if row is None:
                raise KeyError("مهمة الفحص غير موجودة")
        return latest if latest and latest["id"] == scan_id else dict(row)

    def scan_items(self, scan_id: str) -> list[dict[str, Any]]:
        with self.connect() as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM scan_items WHERE scan_id=? ORDER BY position", (scan_id,)
            )]

    def update_scan(self, scan_id: str, **fields: Any) -> None:
        allowed = {
            "state", "current_index", "current_page", "paused", "cancel_requested",
            "skip_requested", "error", "buffer_until", "rate_limit_rpm",
            "rate_limit_rpd", "rate_limit_window",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"حقول فحص غير صالحة: {unknown}")
        fields["updated_at"] = utc_now()
        with self.transaction() as db:
            db.execute(
                f"UPDATE scans SET {','.join(f'{key}=?' for key in fields)} WHERE id=?",
                (*fields.values(), scan_id),
            )

    def update_scan_item(self, item_id: str, state: str) -> None:
        with self.transaction() as db:
            db.execute("UPDATE scan_items SET state=? WHERE id=?", (state, item_id))

    def update_scan_result(
        self,
        book_id: str,
        *,
        values: dict[str, str],
        pages_checked: int,
        max_pages: int,
        confidence: int,
        final: bool,
        error: str = "",
    ) -> None:
        now = utc_now()
        with self.transaction() as db:
            row = db.execute("SELECT attempts FROM books WHERE id=?", (book_id,)).fetchone()
            if row is None:
                raise KeyError("الكتاب غير موجود")
            attempt = row["attempts"] + (1 if final else 0)
            required = all(values.get(field, "").strip() for field in ("title", "author", "publisher", "topic"))
            if error:
                status = "failed"
            elif final:
                status = "complete" if required and confidence >= 70 else "review"
            else:
                status = "current"
            db.execute(
                """UPDATE books SET title=?,author=?,editor=?,publisher=?,publication_year=?,
                   edition_number=?,volume_number=?,topic=?,status=?,
                   pages_checked=?,max_pages=?,confidence=?,attempts=?,error=?,updated_at=? WHERE id=?""",
                (
                    values.get("title", ""), values.get("author", ""), values.get("editor", ""),
                    values.get("publisher", ""), values.get("publication_year", ""),
                    values.get("edition_number", ""), values.get("volume_number", ""),
                    values.get("topic", ""), status, pages_checked,
                    max_pages, confidence, attempt, error, now, book_id,
                ),
            )
            if final:
                event = "تعذرت قراءة الصفحات" if error else (
                    "اكتمل استخراج البيانات" if status == "complete" else "اكتملت بيانات جزئية وتحتاج مراجعة"
                )
                self._history(db, book_id, event, attempt)

    def mark_book_skipped(self, book_id: str, max_pages: int) -> None:
        with self.transaction() as db:
            row = db.execute("SELECT attempts FROM books WHERE id=?", (book_id,)).fetchone()
            if row is None:
                return
            attempt = row["attempts"] + 1
            db.execute(
                "UPDATE books SET status='failed',max_pages=?,attempts=?,error='تخطى المستخدم هذا الكتاب',updated_at=? WHERE id=?",
                (max_pages, attempt, utc_now(), book_id),
            )
            self._history(db, book_id, "تخطى المستخدم هذا الكتاب", attempt)
