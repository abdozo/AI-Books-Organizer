from __future__ import annotations

from fastapi.testclient import TestClient

from organizer.main import create_app
def test_browser_api_persists_settings_and_manual_books(tmp_path):
    app = create_app(tmp_path)
    client = TestClient(app)

    response = client.put("/api/settings", json={
        "api_key": "new-key",
        "prompt": "استخرج البيانات المطلوبة",
        "model": "gemini-test",
    })
    assert response.status_code == 200
    assert app.state.library.setting("api_key") == "new-key"

    created = client.post("/api/books/manual", json={
        "title": "الأصل",
        "author": "المؤلف",
        "editor": "",
        "publisher": "الناشر",
        "publication_year": "٢٠٢٥",
        "edition_number": "٣",
        "volume_number": "٢",
        "topic": "الفقه",
        "path": "",
    })
    assert created.status_code == 201

    bootstrap = client.get("/api/bootstrap").json()
    assert bootstrap["settings"]["keySaved"] is True
    assert bootstrap["settings"]["prompt"] == "استخرج البيانات المطلوبة"
    assert bootstrap["books"][0]["title"] == "الأصل"
    assert bootstrap["books"][0]["publication_year"] == "٢٠٢٥"
    assert bootstrap["books"][0]["edition_number"] == "٣"
    assert bootstrap["books"][0]["volume_number"] == "٢"
    assert "api_key" not in bootstrap["settings"]


def test_saved_api_key_survives_a_new_app_instance(tmp_path):
    first = TestClient(create_app(tmp_path))
    first.put("/api/settings", json={
        "api_key": "persistent-key",
        "prompt": "تعليمات",
        "model": "gemini-test",
    })

    second = TestClient(create_app(tmp_path))
    bootstrap = second.get("/api/bootstrap").json()

    assert bootstrap["settings"]["keySaved"] is True
    assert "persistent-key" not in second.get("/api/bootstrap").text
