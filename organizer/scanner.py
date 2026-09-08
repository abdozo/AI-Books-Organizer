from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

from .database import Library
from .gemini import MAX_INLINE_IMAGE_BYTES, GeminiCataloguer, render_page, render_prompt
from .rate_limits import PersistentRateLimiter
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
        self._rate_limiter = PersistentRateLimiter(library)

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

    def _wait_for_api_slot(self, scan_id: str, model: str) -> bool:
        """Wait for this model's rolling minute or day quota."""
        while True:
            reservation = self._rate_limiter.reserve(model)
            delay = reservation.wait_seconds
            limit_fields = {
                "rate_limit_rpm": reservation.safe_requests_per_minute,
                "rate_limit_rpd": reservation.requests_per_day,
            }
            if delay <= 0:
                self.library.update_scan(
                    scan_id,
                    buffer_until=0,
                    rate_limit_window="",
                    **limit_fields,
                )
                return True
            deadline = time.monotonic() + delay
            self.library.update_scan(
                scan_id,
                buffer_until=int((time.time() + delay) * 1000),
                rate_limit_window=reservation.window,
                **limit_fields,
            )
            while time.monotonic() < deadline:
                control = self._control(scan_id)
                if control["cancelRequested"] or control["skipRequested"]:
                    self.library.update_scan(scan_id, buffer_until=0, rate_limit_window="")
                    return False
                if control["paused"] and not self._wait_if_paused(scan_id):
                    self.library.update_scan(scan_id, buffer_until=0, rate_limit_window="")
                    return False
                time.sleep(min(0.5, max(0.0, deadline - time.monotonic())))

    def _run(self, scan_id: str) -> None:
        with self._execution_lock:
            try:
                self.library.update_scan(scan_id, state="running", error="")
                key = self.secrets.get()
                client = GeminiCataloguer(key)
                prompt_template = self.library.setting("prompt")
                items = self.library.scan_items(scan_id)
                scan = self._control(scan_id)
                model = scan.get("model") or self.library.setting("model")
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
                    self.library.update_scan(
                        scan_id,
                        state="complete",
                        paused=0,
                        current_page=0,
                        buffer_until=0,
                        rate_limit_window="",
                    )
            except Exception as exc:
                message = str(exc)
                self.library.update_scan(
                    scan_id,
                    state="failed",
                    error=message,
                    paused=0,
                    buffer_until=0,
                    rate_limit_window="",
                )
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
        values = {field: book.get(field, "") for field in CATALOG_FIELDS}
        scores = {field: 0 for field in CATALOG_FIELDS}
        try:
            path: Path = self.library.source_path(book_id)
            page_limit = min(max_pages, book["pageCount"])
            images: list[bytes] = []
            image_budget = MAX_INLINE_IMAGE_BYTES // page_limit
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
                images.append(
                    render_page(
                        path,
                        page_number,
                        max_bytes=image_budget,
                    )
                )

            previous = {"values": values, "confidence": scores}
            missing_fields = [field for field in CATALOG_FIELDS if not values[field]]
            page_range = "1" if page_limit == 1 else f"1 إلى {page_limit}"
            prompt = render_prompt(
                prompt_template,
                file_name=book["file"],
                page_number=page_range,
                max_pages=max_pages,
                previous=previous,
                missing_fields=missing_fields,
            )
            if not self._wait_for_api_slot(scan_id, model):
                control = self._control(scan_id)
                if control["skipRequested"]:
                    self.library.mark_book_skipped(book_id, max_pages)
                    self.library.update_scan(scan_id, skip_requested=0)
                    return "skipped"
                return "waiting"
            result = client.extract(images, model=model, prompt=prompt)
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
                pages_checked=page_limit,
                max_pages=max_pages,
                confidence=confidence,
                final=True,
            )
            final_book = self.library.get_book(book_id)
            return final_book["status"]
        except Exception as exc:
            file_label = book.get("file") or book.get("title") or book_id
            message = f'تعذر فحص "{file_label}": {exc}'
            confidence = overall_confidence(values, scores)
            self.library.update_scan_result(
                book_id,
                values=values,
                pages_checked=self._control(scan_id)["currentPage"],
                max_pages=max_pages,
                confidence=confidence,
                final=True,
                error=message,
            )
            return "failed"

    def pause(self, scan_id: str) -> None:
        self.library.update_scan(scan_id, paused=1)

    def resume(self, scan_id: str) -> None:
        self.library.update_scan(scan_id, paused=0)
        self.start(scan_id)

    def cancel(self, scan_id: str) -> None:
        self.library.update_scan(
            scan_id,
            cancel_requested=1,
            paused=0,
            buffer_until=0,
            rate_limit_window="",
        )

    def skip(self, scan_id: str) -> None:
        self.library.update_scan(
            scan_id,
            skip_requested=1,
            paused=0,
            buffer_until=0,
            rate_limit_window="",
        )
