from __future__ import annotations

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_windows_updater_stops_updates_main_and_starts_in_order():
    source = (PROJECT_ROOT / "Update.bat").read_text(encoding="utf-8")

    stop_at = source.index("call Stop-Books-Organizer.bat")
    checkout_at = source.index("git checkout main")
    pull_at = source.index("git pull --ff-only origin main")
    start_at = source.index("call Start-Books-Organizer.bat")

    assert stop_at < checkout_at < pull_at < start_at


def test_windows_updater_checks_git_requirements_and_each_command_result():
    source = (PROJECT_ROOT / "Update.bat").read_text(encoding="utf-8")

    assert 'if not exist ".git" goto not_a_git_clone' in source
    assert "where git >nul 2>&1" in source
    assert source.count("if errorlevel 1 goto") >= 5
