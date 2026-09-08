from __future__ import annotations

import io
import zipfile
from xml.etree import ElementTree

from fastapi.testclient import TestClient

from organizer.main import create_app
def test_browser_api_persists_settings_and_manual_books(tmp_path):
    app = create_app(tmp_path)
    client = TestClient(app)

    response = client.put("/api/settings", json={
        "api_key": "new-key",
        "prompt": "استخرج البيانات المطلوبة",
        "model": "gemini-3.5-flash-lite",
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
    assert bootstrap["models"][0] == {
        "value": "gemini-3.5-flash-lite",
        "label": "Gemini 3.5 Flash Lite",
        "requestsPerMinute": 15,
        "safeRequestsPerMinute": 14,
        "dailyRequests": 500,
    }
    assert bootstrap["books"][0]["title"] == "الأصل"
    assert bootstrap["books"][0]["publication_year"] == "٢٠٢٥"
    assert bootstrap["books"][0]["edition_number"] == "٣"
    assert bootstrap["books"][0]["volume_number"] == "٢"
    assert "api_key" not in bootstrap["settings"]


def test_settings_reject_models_without_documented_quotas(tmp_path):
    client = TestClient(create_app(tmp_path))

    response = client.put("/api/settings", json={
        "api_key": "new-key",
        "prompt": "استخرج البيانات المطلوبة",
        "model": "gemini-unknown",
    })

    assert response.status_code == 400
    assert "لا توجد حدود طلبات موثقة" in response.json()["detail"]


def test_saved_api_key_survives_a_new_app_instance(tmp_path):
    first = TestClient(create_app(tmp_path))
    first.put("/api/settings", json={
        "api_key": "persistent-key",
        "prompt": "تعليمات",
        "model": "gemini-3.5-flash-lite",
    })

    second = TestClient(create_app(tmp_path))
    bootstrap = second.get("/api/bootstrap").json()

    assert bootstrap["settings"]["keySaved"] is True
    assert "persistent-key" not in second.get("/api/bootstrap").text


def test_selected_books_export_as_a_word_document(tmp_path):
    client = TestClient(create_app(tmp_path))
    first = client.post(
        "/api/books/manual",
        json={"title": "كتاب أول", "author": "مؤلف", "topic": "فقه"},
    ).json()
    second = client.post(
        "/api/books/manual",
        json={"title": "كتاب ثان", "publisher": "ناشر"},
    ).json()

    response = client.post("/api/books/export", json={"book_ids": [second["id"], first["id"]]})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    with zipfile.ZipFile(io.BytesIO(response.content)) as document:
        xml = document.read("word/document.xml").decode("utf-8")
        for name in document.namelist():
            if name.endswith((".xml", ".rels")):
                ElementTree.fromstring(document.read(name))
    assert "كتاب أول" in xml
    assert "كتاب ثان" in xml
    assert xml.index("كتاب ثان") < xml.index("كتاب أول")
    assert xml.count("<w:tbl>") == 1
    assert xml.count("كتاب أول") == 1
    assert xml.count("كتاب ثان") == 1
    assert xml.count("<w:tr>") == 3
    assert "اسم الملف" not in xml
    assert "المسار الكامل" not in xml
    assert "نسبة الثقة" not in xml
    assert "الصفحات المفحوصة" not in xml
    assert "حالة الفهرسة" not in xml
    assert "الموضوع" not in xml
    assert "فقه" not in xml
    assert "المحقق" not in xml
    assert "—" not in xml
    assert '<w:bidiVisual/>' in xml
    assert '<w:bidi/>' in xml
    assert '<w:rtl/>' in xml
    assert '<w:pgSz w:w="16838" w:h="11906" w:orient="landscape"/>' in xml


def test_selected_books_can_move_to_another_topic(tmp_path):
    client = TestClient(create_app(tmp_path))
    first = client.post("/api/books/manual", json={"title": "كتاب أول", "topic": "تاريخ"}).json()
    second = client.post("/api/books/manual", json={"title": "كتاب ثان", "topic": "فقه"}).json()

    response = client.post("/api/books/move", json={
        "book_ids": [first["id"], second["id"], first["id"]],
        "topic": "  حديث  ",
    })

    assert response.status_code == 200
    assert response.json() == {"affected": 2}
    books = client.get("/api/bootstrap").json()["books"]
    assert {book["topic"] for book in books} == {"حديث"}


def test_book_folder_endpoint_reveals_the_real_path(tmp_path, monkeypatch):
    pdf = tmp_path / "original.pdf"
    pdf.write_bytes(b"not needed for a manual book")
    client = TestClient(create_app(tmp_path / "data"))
    book = client.post("/api/books/manual", json={"title": "كتاب", "path": str(pdf)}).json()
    revealed = []
    monkeypatch.setattr("organizer.main.reveal_in_file_manager", revealed.append)

    response = client.post(f"/api/books/{book['id']}/folder")

    assert response.status_code == 200
    assert revealed == [pdf]
    assert book["fullPath"] == str(pdf)


def test_imported_pdf_is_referenced_at_its_original_path_without_copying(tmp_path, monkeypatch):
    source_folder = tmp_path / "library"
    source_folder.mkdir()
    pdf = source_folder / "original.pdf"
    pdf.write_bytes(b"%PDF-original-book")
    data_root = tmp_path / "app-data"
    client = TestClient(create_app(data_root))
    monkeypatch.setattr("organizer.main.inspect_pdf", lambda _data: 12)

    response = client.post("/api/imports/file", json={"path": str(pdf)})

    assert response.status_code == 201
    assert response.json()["fullPath"] == str(pdf)
    assert list((data_root / "books").rglob("*.pdf")) == []


def test_local_picker_returns_paths_without_copying_files(tmp_path, monkeypatch):
    folder = tmp_path / "library"
    folder.mkdir()
    pdf = folder / "book.pdf"
    pdf.write_bytes(b"%PDF-original")
    data_root = tmp_path / "app-data"
    client = TestClient(create_app(data_root))
    monkeypatch.setattr("organizer.main.pick_local_pdf", lambda: pdf)
    monkeypatch.setattr("organizer.main.pick_local_folder", lambda: folder)

    selected_file = client.post("/api/local-picker/file")
    selected_folder = client.post("/api/local-picker/folder")

    assert selected_file.status_code == 200
    assert selected_file.json() == {"path": str(pdf)}
    assert selected_folder.status_code == 200
    assert selected_folder.json() == {"path": str(folder)}
    assert client.post("/api/uploads", content=b"%PDF-copy").status_code == 404
    assert list((data_root / "books").rglob("*.pdf")) == []


def test_folder_import_discovers_and_references_original_pdfs(tmp_path, monkeypatch):
    root = tmp_path / "library"
    child = root / "child"
    deeper = child / "deeper"
    deeper.mkdir(parents=True)
    first = root / "a.pdf"
    skipped_by_limit = root / "b.pdf"
    second = child / "c.PDF"
    too_deep = deeper / "d.pdf"
    for path in (first, skipped_by_limit, second, too_deep):
        path.write_bytes(b"%PDF-test")
    (root / "notes.txt").write_text("not a book", encoding="utf-8")
    data_root = tmp_path / "app-data"
    client = TestClient(create_app(data_root))
    monkeypatch.setattr("organizer.main.inspect_pdf", lambda _path: 7)
    payload = {
        "path": str(root),
        "include_subfolders": True,
        "max_depth": 1,
        "per_folder_limit": 1,
    }

    preview = client.post("/api/imports/folder/preview", json=payload)
    imported = client.post("/api/imports/folder", json=payload)

    assert preview.status_code == 200
    assert preview.json() == {"path": str(root), "count": 2, "folderCount": 2}
    assert imported.status_code == 201
    assert imported.json()["count"] == 2
    books = client.get("/api/bootstrap").json()["books"]
    assert {book["fullPath"] for book in books} == {str(first), str(second)}
    assert all(path.startswith(str(root)) for path in (book["fullPath"] for book in books))
    assert list((data_root / "books").rglob("*.pdf")) == []
