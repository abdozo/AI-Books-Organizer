from __future__ import annotations

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WINDOWS_DIR = PROJECT_ROOT / "Windows"
MACOS_DIR = PROJECT_ROOT / "macOS"


def test_platform_launchers_are_kept_in_separate_directories():
    assert not list(PROJECT_ROOT.glob("*.bat"))
    assert not list(PROJECT_ROOT.glob("*.command"))
    assert {path.name for path in WINDOWS_DIR.glob("*.bat")} == {
        "Start-Books-Organizer.bat",
        "Stop-Books-Organizer.bat",
        "Restart-Books-Organizer.bat",
        "Update.bat",
    }
    assert {path.name for path in MACOS_DIR.glob("*.command")} == {
        "Start-Books-Organizer.command",
        "Stop-Books-Organizer.command",
        "Restart-Books-Organizer.command",
    }


def test_platform_launchers_change_to_the_project_root():
    for launcher in WINDOWS_DIR.glob("*.bat"):
        assert 'cd /d "%~dp0.."' in launcher.read_text(encoding="utf-8")

    for launcher in MACOS_DIR.glob("*.command"):
        assert 'cd "${0:A:h}/.."' in launcher.read_text(encoding="utf-8")


def test_windows_updater_stops_updates_main_and_starts_in_order():
    source = (WINDOWS_DIR / "Update.bat").read_text(encoding="utf-8")

    stop_at = source.index('call "%~dp0Stop-Books-Organizer.bat"')
    checkout_at = source.index("git checkout main")
    pull_at = source.index("git pull --ff-only origin main")
    start_at = source.index('call "%~dp0Start-Books-Organizer.bat"')

    assert stop_at < checkout_at < pull_at < start_at


def test_windows_updater_checks_git_requirements_and_each_command_result():
    source = (WINDOWS_DIR / "Update.bat").read_text(encoding="utf-8")

    assert 'if not exist ".git" goto not_a_git_clone' in source
    assert "where git >nul 2>&1" in source
    assert source.count("if errorlevel 1 goto") >= 5
