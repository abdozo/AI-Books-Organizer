from __future__ import annotations

from organizer import system


def test_windows_reveal_keeps_explorer_switch_separate_from_spaced_path(
    tmp_path, monkeypatch
):
    folder = tmp_path / "Arabic Books"
    folder.mkdir()
    pdf = folder / "book.pdf"
    pdf.write_bytes(b"%PDF")
    launched = []
    monkeypatch.setattr(system, "Path", lambda value: value)
    monkeypatch.setattr(system.sys, "platform", "win32")
    monkeypatch.setattr(system.os, "name", "nt")
    monkeypatch.setattr(system.subprocess, "Popen", launched.append)

    system.reveal_in_file_manager(pdf)

    assert launched == [["explorer", "/select,", str(pdf.resolve())]]
