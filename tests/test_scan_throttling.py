"""Exercise request timing through the real queue and SQLite limiter, without HTTP."""
from types import SimpleNamespace

import pytest

from organizer.database import Library
from organizer.gemini import GeminiRateLimit
from organizer.rate_limits import MODEL_QUOTAS, PersistentRateLimiter
from organizer.scanner import ScanManager


class Clock:
    def __init__(self):
        self.now = 100_000.0
        self.on_sleep = lambda: None

    def sleep(self, seconds):
        self.now += seconds
        self.on_sleep()


@pytest.fixture
def queue(tmp_path, monkeypatch):
    clock = Clock()
    monkeypatch.setattr("organizer.scanner.time.sleep", clock.sleep)
    monkeypatch.setattr("organizer.scanner.time.time", lambda: clock.now)
    monkeypatch.setattr("organizer.scanner.time.monotonic", lambda: clock.now)
    monkeypatch.setattr("organizer.scanner.render_page", lambda *_args, **_kwargs: b"page")
    library = Library(tmp_path / "data")
    books = []
    for index in range(35):
        path = tmp_path / f"book-{index}.pdf"
        path.write_bytes(b"%PDF-test")
        books.append(library.create_referenced_book(path, 1)["id"])
    manager = ScanManager(library, SimpleNamespace(get=lambda: "test-key"))
    manager._rate_limiter = PersistentRateLimiter(library, clock=lambda: clock.now)
    calls = []

    def extract(*_args, **_kwargs):
        calls.append(clock.now)
        # A local response failure keeps the fixture small and must still count.
        raise RuntimeError("invalid response")

    client = SimpleNamespace(extract=extract)
    monkeypatch.setattr("organizer.scanner.GeminiCataloguer", lambda _key: client)
    return SimpleNamespace(
        clock=clock, library=library, books=books, manager=manager,
        calls=calls, client=client,
    )


@pytest.mark.parametrize("model", MODEL_QUOTAS)
def test_all_model_limits_hold_for_actual_dispatches(queue, model):
    q = queue
    quota = MODEL_QUOTAS[model]
    books = q.books[:min(len(q.books), quota.requests_per_day)]
    scan_id = q.library.create_scan(books, 1, model=model)
    q.manager._run(scan_id)

    assert len(q.calls) == len(books)
    for sent_at in q.calls:
        assert sum(sent_at - 60 < stamp <= sent_at for stamp in q.calls) <= quota.safe_requests_per_minute
        assert sum(sent_at - 86400 < stamp <= sent_at for stamp in q.calls) <= quota.requests_per_day


@pytest.mark.parametrize("action", ["pause", "cancel", "skip"])
def test_controls_during_last_page_render_prevent_dispatch(queue, monkeypatch, action):
    q = queue
    scan_id = q.library.create_scan(q.books[:1], 1, model="gemini-3.8-flash")
    started_at = q.clock.now

    def render(*_args, **_kwargs):
        getattr(q.manager, action)(scan_id)
        return b"page"

    monkeypatch.setattr("organizer.scanner.render_page", render)
    if action == "pause":
        def resume_later():
            if q.clock.now >= started_at + 10:
                q.library.update_scan(scan_id, paused=0)
        q.clock.on_sleep = resume_later
    q.manager._run(scan_id)

    assert q.calls == ([started_at + 10] if action == "pause" else [])


@pytest.mark.parametrize("action", ["skip", "cancel_restart"])
def test_provider_stop_period_survives_skip_and_cancel_restart(queue, action):
    q = queue
    model = "gemini-3.8-flash"
    scan_id = q.library.create_scan(q.books[:2], 1, model=model)
    started_at = q.clock.now

    def extract(*_args, **_kwargs):
        q.calls.append(q.clock.now)
        if len(q.calls) == 1:
            raise GeminiRateLimit(quota_type="rpm", retry_after_seconds=90)
        raise RuntimeError("invalid response")

    q.client.extract = extract

    def interrupt_wait():
        if len(q.calls) == 1:
            if action == "skip":
                q.manager.skip(scan_id)
            else:
                q.manager.cancel(scan_id)
            q.clock.on_sleep = lambda: None

    q.clock.on_sleep = interrupt_wait
    q.manager._run(scan_id)
    if action == "cancel_restart":
        library = Library(q.library.root)
        manager = ScanManager(library, SimpleNamespace(get=lambda: "test-key"))
        manager._rate_limiter = PersistentRateLimiter(library, clock=lambda: q.clock.now)
        new_scan = library.create_scan(q.books[1:2], 1, model=model)
        manager._run(new_scan)

    assert len(q.calls) == 2
    assert q.calls[1] >= started_at + 91


def test_daily_stop_is_observed_by_the_queue(queue):
    q = queue
    model = "gemini-3.8-flash"
    started_at = q.clock.now
    # A full daily allowance, with the oldest request expiring in two seconds.
    with q.library.transaction() as db:
        db.executemany(
            "INSERT INTO api_request_reservations(model,reserved_at) VALUES (?,?)",
            [(model, started_at - 86400 + 2 + index * 61) for index in range(20)],
        )
    scan_id = q.library.create_scan(q.books[:1], 1, model=model)
    q.manager._run(scan_id)

    assert q.calls == [started_at + 2]


def test_pause_resume_during_rpm_wait_does_not_end_it_early(queue):
    q = queue
    model = "gemini-3.8-flash"
    started_at = q.clock.now
    scan_id = q.library.create_scan(q.books[:5], 1, model=model)
    paused = False

    def control_wait():
        nonlocal paused
        if not paused:
            q.manager.pause(scan_id)
            paused = True
        if q.clock.now >= started_at + 10:
            q.library.update_scan(scan_id, paused=0)

    q.clock.on_sleep = control_wait
    q.manager._run(scan_id)

    assert q.calls == [started_at] * 4 + [started_at + 60]


def test_pause_after_reserving_does_not_send_with_an_expired_slot(queue, monkeypatch):
    q = queue
    model = "gemini-3.8-flash"
    started_at = q.clock.now
    scan_id = q.library.create_scan(q.books[:1], 1, model=model)
    reserve = q.manager._wait_for_api_slot
    reservations = []

    def pause_after_first_slot(*args):
        slot = reserve(*args)
        reservations.append(slot)
        if len(reservations) == 1:
            q.manager.pause(scan_id)
        return slot

    def resume_later():
        if q.clock.now >= started_at + 120:
            q.library.update_scan(scan_id, paused=0)

    monkeypatch.setattr(q.manager, "_wait_for_api_slot", pause_after_first_slot)
    q.clock.on_sleep = resume_later
    q.manager._run(scan_id)

    assert q.calls == [started_at + 120]
    assert len(reservations) == 2
    assert reservations[0].reservation_id != reservations[1].reservation_id
