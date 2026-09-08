from __future__ import annotations

from organizer.runtime_revision import application_revision


def test_application_revision_changes_with_server_source(tmp_path):
    organizer = tmp_path / "organizer"
    static = tmp_path / "static"
    organizer.mkdir()
    static.mkdir()
    source = organizer / "main.py"
    source.write_text("VERSION = 1\n", encoding="utf-8")
    first = application_revision(tmp_path)

    source.write_text("VERSION = 2\n", encoding="utf-8")

    assert application_revision(tmp_path) != first
