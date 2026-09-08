from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

from .database import Library
from .gemini import GeminiCataloguer, render_page, render_prompt
from .secrets import SecretStore


CATALOG_FIELDS = (
    "title", "author", "editor", "publisher", "publication_year",
    "edition_number", "volume_number", "topic",
)


def overall_confidence(values: dict[str, str], scores: dict[str, int]) -> int:
    populated_scores = [scores[field] for field in CATALOG_FIELDS if values[field]]
    return round(sum(populated_scores) / len(populated_scores)) if populated_scores else 0


class ScanManager:
    """Run one persisted scan queue at a time without blocking HTTP requests."""

    def __init__(self, library: Library, secrets: SecretStore):
        self.library = library
        self.secrets = secrets
        self._execution_lock = threading.Lock()
        self._threads: dict[str, threading.Thread] = {}

    def start(self, scan_id: str) -> None:
        thread = self._threads.get(scan_id)
        if thread and thread.is_alive():
            return
        thread = threading.Thread(target=self._run, args=(scan_id,), daemon=True, name=f"scan-{scan_id[:8]}")
        self._threads[scan_id] = thread
        thread.start()

    def _control(self, scan_id: str) -> dict[str, Any]:
        scan = self.library.latest_scan()
        if scan is None or scan["id"] != scan_id:
            raise KeyError("مهمة الفحص غير موجودة")
        return scan

    def _wait_if_paused(self, scan_id: str) -> bool:
        while True:
            scan = self._control(scan_id)
            if scan["cancelRequested"]:
                return False
            if not scan["paused"]:
                return True
            time.sleep(0.25)

    def _run(self, scan_id: str) -> None:
        with self._execution_lock:
            try:
                self.library.update_scan(scan_id, state="running", error="")
                key = self.secrets.get()
                client = GeminiCataloguer(key)
                prompt_template = self.library.setting("prompt")
                model = self.library.setting("model")
                items = self.library.scan_items(scan_id)
                scan = self._control(scan_id)
                max_pages = scan["maxPages"]
                for index, item in enumerate(items):
                    control = self._control(scan_id)
                    if control["cancelRequested"]:
                        self.library.update_scan(scan_id, state="cancelled")
                        return
                    if item["state"] in {"complete", "review", "failed", "skipped"}:
                        continue
                    if not self._wait_if_paused(scan_id):
                        self.library.update_scan(scan_id, state="cancelled")
                        return
                    self.library.update_scan(scan_id, current_index=index, current_page=1, skip_requested=0)
                    self.library.update_scan_item(item["id"], "current")
                    outcome = self._scan_book(
                        scan_id,
                        item["book_id"],
                        max_pages=max_pages,
                        prompt_template=prompt_template,
                        model=model,
                        client=client,
                    )
                    self.library.update_scan_item(item["id"], outcome)
                if self._control(scan_id)["cancelRequested"]:
                    self.library.update_scan(scan_id, state="cancelled")
                else:
                    self.library.update_scan(scan_id, state="complete", paused=0, current_page=0)
            except Exception as exc:
                message = str(exc)
                self.library.update_scan(scan_id, state="failed", error=message, paused=0)
                scan = self.library.latest_scan()
                if scan and scan["id"] == scan_id and scan["items"]:
                    index = min(scan["currentIndex"], len(scan["items"]) - 1)
                    current = scan["items"][index]
                    if current["state"] in {"waiting", "current"}:
                        values = {field: current.get(field, "") for field in CATALOG_FIELDS}
                        self.library.update_scan_result(
                            current["book_id"], values=values, pages_checked=scan["currentPage"],
                            max_pages=scan["maxPages"], confidence=0, final=True, error=message,
                        )

    def _scan_book(
        self,
        scan_id: str,
        book_id: str,
        *,
        max_pages: int,
        prompt_template: str,
        model: str,
        client: GeminiCataloguer,
    ) -> str:
        book = self.library.get_book(book_id)
        path: Path = self.library.source_path(book_id)
        page_limit = min(max_pages, book["pageCount"])
        values = {field: book.get(field, "") for field in CATALOG_FIELDS}
        scores = {field: 0 for field in CATALOG_FIELDS}
        try:
            for page_number in range(1, page_limit + 1):
                if not self._wait_if_paused(scan_id):
                    return "waiting"
                control = self._control(scan_id)
                if control["cancelRequested"]:
                    return "waiting"
                if control["skipRequested"]:
                    self.library.mark_book_skipped(book_id, max_pages)
                    self.library.update_scan(scan_id, skip_requested=0)
                    return "skipped"
                self.library.update_scan(scan_id, current_page=page_number)
                previous = {"values": values, "confidence": scores}
                prompt = render_prompt(
                    prompt_template,
                    file_name=book["file"],
                    page_number=page_number,
                    max_pages=max_pages,
                    previous=previous,
                )
                result = client.extract(render_page(path, page_number), model=model, prompt=prompt)
                response = result.data
                for field in CATALOG_FIELDS:
                    candidate = getattr(response, field)
                    candidate_score = getattr(response.confidence, field)
                    if candidate and (not values[field] or candidate_score >= scores[field]):
                        values[field] = candidate
                        scores[field] = candidate_score
                confidence = overall_confidence(values, scores)
                self.library.update_scan_result(
                    book_id,
                    values=values,
                    pages_checked=page_number,
                    max_pages=max_pages,
                    confidence=confidence,
                    final=False,
                )
                all_fields_ready = all(values[field] for field in CATALOG_FIELDS)
                if all_fields_ready:
                    break
            confidence = overall_confidence(values, scores)
            self.library.update_scan_result(
                book_id,
                values=values,
                pages_checked=page_number,
                max_pages=max_pages,
                confidence=confidence,
                final=True,
            )
            final_book = self.library.get_book(book_id)
            return final_book["status"]
        except Exception as exc:
            confidence = overall_confidence(values, scores)
            self.library.update_scan_result(
                book_id,
                values=values,
                pages_checked=self._control(scan_id)["currentPage"],
                max_pages=max_pages,
                confidence=confidence,
                final=True,
                error=str(exc),
            )
            return "failed"

    def pause(self, scan_id: str) -> None:
        self.library.update_scan(scan_id, paused=1)

    def resume(self, scan_id: str) -> None:
        self.library.update_scan(scan_id, paused=0)
        self.start(scan_id)

    def cancel(self, scan_id: str) -> None:
        self.library.update_scan(scan_id, cancel_requested=1, paused=0)

    def skip(self, scan_id: str) -> None:
        self.library.update_scan(scan_id, skip_requested=1, paused=0)
