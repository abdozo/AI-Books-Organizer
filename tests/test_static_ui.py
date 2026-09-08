from __future__ import annotations

from pathlib import Path


APP_JS = Path(__file__).resolve().parents[1] / "static" / "app.js"
APP_HTML = Path(__file__).resolve().parents[1] / "ai-books-organizer.html"
STATIC_DIR = Path(__file__).resolve().parents[1] / "static"


def test_favicon_assets_are_linked_and_present():
    source = APP_HTML.read_text(encoding="utf-8")

    assert 'href="/static/favicon-32.png"' in source
    assert 'href="/static/app-icon-512.png"' in source
    assert 'href="/static/apple-touch-icon.png"' in source
    assert (STATIC_DIR / "favicon-32.png").is_file()
    assert (STATIC_DIR / "app-icon-512.png").is_file()
    assert (STATIC_DIR / "apple-touch-icon.png").is_file()


def test_gemini_models_and_safety_limits_come_from_the_server():
    source = APP_JS.read_text(encoding="utf-8")

    assert '<select class="input" id="modelInput"' in source
    assert "state.models = data.models" in source
    assert "state.models.map((model)" in source
    assert "model.safeRequestsPerMinute" in source
    assert "model.dailyRequests" in source


def test_publication_fields_are_editable_without_archive_routes():
    source = APP_JS.read_text(encoding="utf-8")

    assert '["publication_year", "سنة النشر"]' in source
    assert '["edition_number", "رقم الطبعة"]' in source
    assert '["volume_number", "رقم المجلد"]' in source
    assert "publication_year:" not in source.split("const entityTypes =", 1)[1].split("};", 1)[0]


def test_scan_queue_links_to_books_and_can_open_all_pages():
    source = APP_JS.read_text(encoding="utf-8")

    assert 'class="queue-book-link"' in source
    assert 'href="${bookRouteHref(entry.book_id)}"' in source
    assert 'id="openAllScanBooks"' in source
    assert 'window.open(bookRouteHref(bookId), "_blank", "noopener")' in source


def test_books_can_be_selected_and_exported_to_word():
    source = APP_JS.read_text(encoding="utf-8")

    assert 'id="selectPageBooks"' in source
    assert 'data-select-book="${book.id}"' in source
    assert 'fetch("/api/books/export"' in source
    assert 'link.download = "فهرس-الكتب.docx"' in source


def test_selected_or_single_books_can_move_to_another_topic():
    source = APP_JS.read_text(encoding="utf-8")
    html = APP_HTML.read_text(encoding="utf-8")

    assert 'id="moveBooks"' in source
    assert 'id="moveBookTopic"' in source
    assert 'api("/api/books/move"' in source
    assert 'id="moveBooksModal"' in html
    assert 'list="moveTopicOptions"' in html


def test_selected_or_single_books_can_be_deleted_after_confirmation():
    source = APP_JS.read_text(encoding="utf-8")
    html = APP_HTML.read_text(encoding="utf-8")

    assert 'id="deleteBooks"' in source
    assert 'id="deleteBook"' in source
    assert 'api("/api/books"' in source
    assert 'method: "DELETE"' in source
    assert 'id="deleteBooksModal"' in html
    assert 'role="alertdialog"' in html
    assert 'id="confirmDeleteBooksBtn"' in html
    assert "لن يُحذف ملف PDF الأصلي من جهازك" in html
    assert "سجل الكتاب" not in source


def test_folder_scan_has_depth_and_queue_limits_with_resume():
    html = APP_HTML.read_text(encoding="utf-8")
    source = APP_JS.read_text(encoding="utf-8")
    importer_source = (APP_HTML.parents[0] / "organizer" / "importer.py").read_text(encoding="utf-8")

    assert 'id="includeSubfolders"' in html
    assert 'id="folderMaxDepth"' in html
    assert 'id="folderPdfLimit"' in html
    assert 'id="folderScanItemLimit"' in html
    assert 'max="490"' in html
    assert 'value="490"' in html
    assert 'api("/api/imports/folder/preview"' in source
    assert "include_subfolders: options.includeSubfolders" in source
    assert "scan_item_limit: options.scanItemLimit" in source
    assert "preview.skippedCount" in source
    assert "depth >= max_depth" in importer_source
    assert "available[:min(per_folder_limit, remaining)]" in importer_source
    assert "candidate) in excluded" in importer_source


def test_local_picker_references_original_pdfs_without_browser_uploads():
    html = APP_HTML.read_text(encoding="utf-8")
    source = APP_JS.read_text(encoding="utf-8")
    main_source = (APP_HTML.parents[0] / "organizer" / "main.py").read_text(encoding="utf-8")
    database_source = (APP_HTML.parents[0] / "organizer" / "database.py").read_text(encoding="utf-8")

    assert 'type="file"' not in html
    assert "webkitdirectory" not in html
    assert 'api("/api/local-picker/file"' in source
    assert 'api("/api/local-picker/folder"' in source
    assert 'importAndScan("/api/imports/file"' in source
    assert 'importAndScan("/api/imports/folder"' in source
    assert "/api/uploads" not in source
    assert "/api/uploads" not in main_source
    assert "create_uploaded_book" not in database_source


def test_scan_ui_reports_rate_limit_buffer_and_book_path_actions():
    source = APP_JS.read_text(encoding="utf-8")

    assert "scan.bufferUntil" in source
    assert "انتظار حصة API" in source
    assert 'scan?.rateLimitWindow === "day"' in source
    assert "حد الأمان" in source
    assert 'id="openBookFolder"' in source
    assert 'id="changeBookPath"' in source
    assert 'api(`/api/books/${book.id}/path`' in source
    assert "book.fullPath" in source


def test_book_filters_open_from_search_toolbar_above_table():
    source = APP_JS.read_text(encoding="utf-8")

    tools_at = source.index('class="library-tools"')
    filters_at = source.index('id="bookFilters"', tools_at)
    table_at = source.index('class="table-card"', filters_at)

    assert tools_at < filters_at < table_at
    assert 'id="toggleFilters"' in source
    assert 'aria-controls="bookFilters"' in source
    assert 'class="filter-menu"' in source


def test_search_mode_can_switch_between_flexible_terms_and_phrase_matching():
    source = APP_JS.read_text(encoding="utf-8")

    assert 'flexible: "بحث مرن"' in source
    assert 'phrase: "مطابقة العبارة"' in source
    assert 'id="bookSearchMode"' in source
    assert 'id="entitySearchMode"' in source
    assert 'localStorage.setItem(searchModeStorageKey, state.searchMode)' in source
    assert 'id="searchModeInput"' not in source
    assert 'search_mode:' not in source
    assert 'function lightArabicStem(token)' in source
    assert 'function arabicRootKey(token)' in source
    assert 'function searchScore(query, weightedValues, mode = "flexible")' in source
    assert 'mode === "phrase"' in source
    assert 'matchedTerms * 20' in source
    assert '>الأكثر صلة</option>' in source
