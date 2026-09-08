(() => {
  "use strict";

  const $ = (selector) => document.querySelector(selector);
  const $$ = (selector) => [...document.querySelectorAll(selector)];
  const esc = (value) => String(value ?? "").replace(/[&<>"]/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;",
  })[char]);

  const fields = [
    ["title", "العنوان"],
    ["author", "المؤلف"],
    ["editor", "المحقق"],
    ["publisher", "دار النشر"],
    ["publication_year", "سنة النشر"],
    ["edition_number", "رقم الطبعة"],
    ["volume_number", "رقم المجلد"],
    ["topic", "الموضوع"],
  ];
  const entityTypes = {
    authors: { key: "author", title: "المؤلفون", singular: "المؤلف" },
    topics: { key: "topic", title: "الموضوعات", singular: "الموضوع" },
    publishers: { key: "publisher", title: "دور النشر", singular: "دار النشر" },
    editors: { key: "editor", title: "المحققون", singular: "المحقق" },
  };
  const detailEntityRoutes = {
    author: "authors", editor: "editors", publisher: "publishers", topic: "topics",
  };
  const searchModes = {
    flexible: "بحث مرن",
    phrase: "مطابقة العبارة",
  };
  const searchModeStorageKey = "ai-books-organizer.search-mode";
  const arabicSearchStopWords = new Set([
    "في", "من", "الي", "على", "عن", "مع", "او", "ثم", "هذا", "هذه", "ذلك", "تلك",
    "الذي", "التي", "الذين", "هو", "هي",
  ]);

  function normalizeSearchText(value) {
    return String(value ?? "")
      .normalize("NFKC")
      .replace(/[\u0610-\u061a\u064b-\u065f\u0670\u06d6-\u06ed]/g, "")
      .replace(/ـ/g, "")
      .replace(/[أإآٱ]/g, "ا")
      .replace(/ؤ/g, "و")
      .replace(/ئ/g, "ي")
      .replace(/ى/g, "ي")
      .replace(/ة/g, "ه")
      .toLowerCase()
      .replace(/\s+/g, " ")
      .trim();
  }

  function searchTokens(value) {
    return normalizeSearchText(value).match(/[\p{L}\p{N}]+/gu) || [];
  }

  function lightArabicStem(token) {
    if (!/[\u0600-\u06ff]/.test(token)) return token;
    let stem = token;
    const prefix = ["وال", "فال", "بال", "كال", "لل", "ال"].find((item) => stem.startsWith(item) && stem.length - item.length >= 3);
    if (prefix) stem = stem.slice(prefix.length);
    for (let pass = 0; pass < 2; pass += 1) {
      const suffix = ["يات", "كما", "هما", "اتهم", "ات", "ون", "ين", "ان", "اء", "ها", "هم", "هن", "نا", "ه", "ي"].find((item) => stem.endsWith(item) && stem.length - item.length >= 3);
      if (!suffix) break;
      stem = stem.slice(0, -suffix.length);
    }
    return stem;
  }

  function arabicRootKey(token) {
    let root = lightArabicStem(token);
    const derivedPrefix = ["مست", "است"].find((item) => root.startsWith(item) && root.length - item.length >= 3);
    if (derivedPrefix) root = root.slice(derivedPrefix.length);
    else if (/^[مت]/.test(root) && root.length > 4) root = root.slice(1);
    const withoutWeakLetters = root.replace(/[اوي]/g, "");
    return withoutWeakLetters.length >= 3 ? withoutWeakLetters : root;
  }

  function editDistance(left, right) {
    const previous = Array.from({ length: right.length + 1 }, (_, index) => index);
    for (let row = 1; row <= left.length; row += 1) {
      let diagonal = previous[0];
      previous[0] = row;
      for (let column = 1; column <= right.length; column += 1) {
        const above = previous[column];
        previous[column] = Math.min(
          previous[column] + 1,
          previous[column - 1] + 1,
          diagonal + (left[row - 1] === right[column - 1] ? 0 : 1),
        );
        diagonal = above;
      }
    }
    return previous[right.length];
  }

  function tokenMatchScore(queryToken, textToken) {
    if (queryToken === textToken) return 12;
    const queryStem = lightArabicStem(queryToken);
    const textStem = lightArabicStem(textToken);
    if (queryStem === textStem) return 10;
    if (arabicRootKey(queryStem) === arabicRootKey(textStem)) return 8;
    if (Math.min(queryStem.length, textStem.length) >= 3
      && (queryStem.includes(textStem) || textStem.includes(queryStem))) return 6;
    const longest = Math.max(queryStem.length, textStem.length);
    if (longest >= 4) {
      const distance = editDistance(queryStem, textStem);
      if (distance === 1) return 4;
      if (longest >= 7 && distance === 2) return 2;
    }
    return 0;
  }

  function searchScore(query, weightedValues, mode = "flexible") {
    const normalizedQuery = normalizeSearchText(query);
    if (!normalizedQuery) return 1;
    const fieldsToSearch = weightedValues
      .map(({ value, weight }) => ({ text: normalizeSearchText(value), weight }))
      .filter(({ text }) => text);
    if (mode === "phrase") {
      return fieldsToSearch.reduce((best, field) => {
        if (!field.text.includes(normalizedQuery)) return best;
        return Math.max(best, field.weight * (field.text.startsWith(normalizedQuery) ? 4 : 3));
      }, 0);
    }
    let queryTokens = searchTokens(normalizedQuery);
    if (queryTokens.length > 1) {
      const meaningfulTokens = queryTokens.filter((token) => !arabicSearchStopWords.has(token));
      if (meaningfulTokens.length) queryTokens = meaningfulTokens;
    }
    let score = 0;
    let matchedTerms = 0;
    queryTokens.forEach((queryToken) => {
      let best = 0;
      fieldsToSearch.forEach((field) => {
        searchTokens(field.text).forEach((textToken) => {
          best = Math.max(best, tokenMatchScore(queryToken, textToken) * field.weight);
        });
      });
      if (best) {
        matchedTerms += 1;
        score += best;
      }
    });
    if (!matchedTerms) return 0;
    const coverageBonus = matchedTerms * 20;
    const phraseBonus = fieldsToSearch.some((field) => field.text.includes(normalizedQuery)) ? 12 : 0;
    return score + coverageBonus + phraseBonus;
  }

  function savedSearchMode() {
    try {
      const saved = localStorage.getItem(searchModeStorageKey);
      return searchModes[saved] ? saved : "flexible";
    } catch (_error) {
      return "flexible";
    }
  }

  function searchModeOptions(selectedMode) {
    return Object.entries(searchModes)
      .map(([value, label]) => `<option value="${value}" ${selectedMode === value ? "selected" : ""}>${label}</option>`)
      .join("");
  }

  function updateSearchMode(value) {
    state.searchMode = searchModes[value] ? value : "flexible";
    try {
      localStorage.setItem(searchModeStorageKey, state.searchMode);
    } catch (_error) {
      // The mode still applies until the page closes when browser storage is unavailable.
    }
  }

  const state = {
    books: [],
    scan: null,
    models: [],
    settings: { prompt: "", model: "gemini-3.5-flash-lite", keySaved: false },
    searchMode: savedSearchMode(),
    libraryLabel: "المكتبة المحلية",
    route: "books",
    query: "",
    statuses: new Set(),
    filtersOpen: false,
    selectedBooks: new Set(),
    movingBookIds: [],
    deletingBookIds: [],
    topic: "all",
    author: "all",
    publisher: "all",
    sort: "relevance",
    page: 1,
    pageSize: 8,
    editingId: null,
    rescanId: null,
    entityType: "authors",
    entityQuery: "",
    entityPage: 1,
    entityPageSize: 10,
    selectedEntities: new Set(),
    entityAction: "",
    entityMode: "merge",
    importing: false,
  };
  let pendingFolder = "";
  let pendingFileCount = 0;
  let folderPreviewSequence = 0;
  let pollTimer = null;

  async function api(path, options = {}) {
    const response = await fetch(path, options);
    const contentType = response.headers.get("content-type") || "";
    const payload = contentType.includes("application/json") ? await response.json() : null;
    if (!response.ok) {
      throw new Error(payload?.detail || `تعذر تنفيذ الطلب (${response.status})`);
    }
    return payload;
  }

  async function refreshData(renderPage = true) {
    const data = await api("/api/bootstrap");
    state.books = data.books;
    const bookIds = new Set(state.books.map((book) => book.id));
    state.selectedBooks = new Set([...state.selectedBooks].filter((id) => bookIds.has(id)));
    state.scan = data.scan;
    state.models = data.models;
    state.settings = data.settings;
    state.libraryLabel = data.libraryLabel;
    if (renderPage) render();
    schedulePoll();
  }

  function schedulePoll() {
    clearTimeout(pollTimer);
    if (state.scan && ["queued", "running"].includes(state.scan.state)) {
      pollTimer = setTimeout(async () => {
        try {
          await refreshData(state.route === "scan" || state.route.startsWith("book/"));
          if (state.route !== "scan" && !state.route.startsWith("book/")) renderChrome();
        } catch (error) {
          toast("تعذر تحديث الفحص", error.message, "error");
          schedulePoll();
        }
      }, 900);
    }
  }

  function go(route) {
    location.hash = `#/${route}`;
  }

  function bookRouteHref(bookId) {
    return `#/book/${encodeURIComponent(bookId)}`;
  }

  function parseRoute() {
    const value = location.hash.replace(/^#\//, "") || "books";
    const root = value.split("/")[0];
    if (entityTypes[root] && state.entityType !== root) {
      state.entityType = root;
      state.entityQuery = "";
      state.entityPage = 1;
      state.entityAction = "";
      state.selectedEntities.clear();
    }
    state.route = value;
    render();
  }

  function currentBook() {
    const id = state.route.split("/")[1];
    return state.books.find((book) => book.id === id);
  }

  function statusLabel(status) {
    return {
      complete: "مكتمل",
      review: "يحتاج مراجعة",
      failed: "تعذر الفحص",
      current: "قيد الفحص",
      waiting: "ينتظر",
      skipped: "تم التخطي",
    }[status] || status;
  }

  function statusClass(status) {
    if (status === "complete") return "complete";
    if (["review", "waiting", "current"].includes(status)) return "review";
    return "failed";
  }

  function listValues(key) {
    return [...new Set(state.books.map((book) => book[key]).filter(Boolean))]
      .sort((a, b) => a.localeCompare(b, "ar"));
  }

  function filteredBooks() {
    const fieldWeights = { title: 3, author: 2, editor: 2, topic: 2, publisher: 1.5 };
    const books = state.books.map((book, index) => ({
      book,
      index,
      score: searchScore(state.query, [
        ...fields.map(([key]) => ({ value: book[key], weight: fieldWeights[key] || 1 })),
        { value: book.file, weight: 1 },
      ], state.searchMode),
    })).filter(({ book, score }) => {
      return score > 0
        && (!state.statuses.size || state.statuses.has(statusClass(book.status)))
        && (state.topic === "all" || book.topic === state.topic)
        && (state.author === "all" || book.author === state.author)
        && (state.publisher === "all" || book.publisher === state.publisher);
    });
    if (state.sort === "relevance" && state.query.trim()) books.sort((a, b) => b.score - a.score || a.index - b.index);
    if (state.sort === "title") books.sort((a, b) => a.book.title.localeCompare(b.book.title, "ar"));
    if (state.sort === "author") books.sort((a, b) => a.book.author.localeCompare(b.book.author, "ar"));
    return books.map(({ book }) => book);
  }

  function render() {
    renderChrome();
    const root = state.route.split("/")[0];
    if (state.route === "books") renderBooks();
    else if (state.route === "scan") renderScan();
    else if (state.route === "settings") renderSettings();
    else if (entityTypes[root]) {
      state.entityType = root;
      state.route.includes("/") ? renderEntityDetail() : renderEntities();
    } else if (state.route.startsWith("book/")) renderDetail();
    else go("books");
  }

  function renderChrome() {
    const scan = state.scan;
    const running = scan && ["queued", "running"].includes(scan.state);
    const done = scan?.items?.filter((item) => ["complete", "review", "failed", "skipped"].includes(item.state)).length || 0;
    const root = state.route.split("/")[0];
    $("#booksBadge").textContent = state.books.length;
    $("#scanBadge").textContent = running ? Math.max(0, scan.items.length - done) : 0;
    $("#scanBadge").classList.toggle("live", Boolean(running));
    $("#scanIndicator").classList.toggle("idle", !running);
    const bufferSeconds = running ? Math.max(0, Math.ceil(((scan.bufferUntil || 0) - Date.now()) / 1000)) : 0;
    $("#scanIndicatorText").textContent = running
      ? (scan.paused ? "الفحص متوقف مؤقتًا" : bufferSeconds
        ? `انتظار حصة API، ${bufferSeconds} ث`
        : `فحص ${Math.min(done + 1, scan.items.length)} من ${scan.items.length}`)
      : "لا يوجد فحص جارٍ";
    $("#sidebarPath").textContent = state.libraryLabel;
    $$("[data-route]").forEach((button) => {
      button.classList.toggle("active", root === button.dataset.route || (button.dataset.route === "books" && root === "book"));
    });
    $("#crumbs").textContent = state.route === "books" ? "الكتب"
      : state.route === "scan" ? "الفحص"
        : state.route === "settings" ? "الإعدادات"
          : entityTypes[root] ? entityTypes[root].title : "الكتب / تفاصيل الكتاب";
  }

  function bookRow(book, selectable = false) {
    const publication = [
      book.publication_year && `السنة: ${book.publication_year}`,
      book.edition_number && `الطبعة: ${book.edition_number}`,
      book.volume_number && `المجلد: ${book.volume_number}`,
    ].filter(Boolean);
    return `<tr data-book-id="${book.id}">
      <td><div class="book-name">${selectable ? `<input class="book-check" type="checkbox" data-select-book="${book.id}" ${state.selectedBooks.has(book.id) ? "checked" : ""} aria-label="تحديد ${esc(book.title || book.file)}">` : ""}<span class="cover">${esc((book.title || book.file || "ك").slice(0, 1))}</span><div><strong>${esc(book.title || "بلا عنوان")}</strong><small>${esc(book.file)}</small></div></div></td>
      <td class="${book.author ? "" : "missing"}">${esc(book.author || "بدون")}</td>
      <td class="${book.editor ? "" : "missing"}">${esc(book.editor || "بدون")}</td>
      <td class="${book.publisher ? "" : "missing"}">${esc(book.publisher || "بدون")}</td>
      <td class="${publication.length ? "publication-meta" : "missing"}">${publication.length ? publication.map((value) => `<small>${esc(value)}</small>`).join("") : "لم يُعثر عليها"}</td>
      <td class="${book.topic ? "" : "missing"}">${esc(book.topic || "بدون")}</td>
      <td><span class="status ${statusClass(book.status)}">${statusLabel(book.status)}</span></td>
      <td><button class="row-menu" data-row-menu="${book.id}" aria-label="تعديل ${esc(book.title)}">⋯</button></td>
    </tr>`;
  }

  function renderBooks() {
    const keepSearch = document.activeElement?.id === "bookSearch";
    const cursor = keepSearch ? document.activeElement.selectionStart : 0;
    const all = filteredBooks();
    const pages = Math.max(1, Math.ceil(all.length / state.pageSize));
    if (state.page > pages) state.page = pages;
    const rows = all.slice((state.page - 1) * state.pageSize, state.page * state.pageSize);
    const pageSelected = rows.length > 0 && rows.every((book) => state.selectedBooks.has(book.id));
    const filterCount = state.statuses.size
      + [state.topic, state.author, state.publisher].filter((value) => value !== "all").length;
    $("#routeHost").innerHTML = `
      <div class="page-head"><div><h1>الكتب</h1><p>${state.books.length} كتابًا في المكتبة المحلية</p></div><div class="actions"><button class="btn selection-btn" id="moveBooks" ${state.selectedBooks.size ? "" : "disabled"}>نقل إلى موضوع${state.selectedBooks.size ? ` (${state.selectedBooks.size})` : ""}</button><button class="btn btn-danger selection-btn" id="deleteBooks" ${state.selectedBooks.size ? "" : "disabled"}>حذف المحدد${state.selectedBooks.size ? ` (${state.selectedBooks.size})` : ""}</button><button class="btn export-btn" id="exportBooks" ${state.selectedBooks.size ? "" : "disabled"}>تصدير المحدد إلى Word${state.selectedBooks.size ? ` (${state.selectedBooks.size})` : ""}</button><button class="btn" data-action="add-file">إضافة ملف PDF</button><button class="btn" data-action="manual">إضافة كتاب يدويًا</button><button class="btn btn-primary" data-action="folder">فحص مجلد</button></div></div>
      <div class="library-layout"><div class="books-panel"><div class="library-tools"><div class="search-tools"><label class="search"><svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="7"/><path d="m20 20-4-4"/></svg><input id="bookSearch" value="${esc(state.query)}" placeholder="ابحث في الكتب"></label><select class="search-mode-select" id="bookSearchMode" aria-label="طريقة البحث" title="اختر بين البحث بالكلمات والصيغ القريبة أو مطابقة العبارة">${searchModeOptions(state.searchMode)}</select></div><button class="btn filter-toggle ${filterCount ? "active" : ""}" id="toggleFilters" type="button" aria-expanded="${state.filtersOpen}" aria-controls="bookFilters"><svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M4 6h16M7 12h10m-7 6h4"/></svg><span>تصفية</span>${filterCount ? `<span class="filter-count">${filterCount}</span>` : ""}<span class="filter-chevron" aria-hidden="true">⌄</span></button><select class="sort" id="sortBooks" aria-label="ترتيب الكتب"><option value="relevance" ${state.sort === "relevance" ? "selected" : ""}>الأكثر صلة</option><option value="recent" ${state.sort === "recent" ? "selected" : ""}>آخر تحديث</option><option value="title" ${state.sort === "title" ? "selected" : ""}>العنوان</option><option value="author" ${state.sort === "author" ? "selected" : ""}>المؤلف</option></select></div>
          <section class="filter-menu" id="bookFilters" aria-label="خيارات تصفية الكتب" ${state.filtersOpen ? "" : "hidden"}>
            <div class="filter-group"><label>الحالة</label><div class="status-filters">${[["complete", "مكتمل"], ["review", "يحتاج مراجعة"], ["failed", "تعذر الفحص"]].map(([value, label]) => `<label class="check"><input type="checkbox" data-status="${value}" ${state.statuses.has(value) ? "checked" : ""}><span>${label}</span><em>${state.books.filter((book) => statusClass(book.status) === value).length}</em></label>`).join("")}</div></div>
            <div class="filter-group"><label for="filterTopic">الموضوع</label><select class="select" id="filterTopic"><option value="all">كل الموضوعات</option>${listValues("topic").map((value) => `<option ${state.topic === value ? "selected" : ""}>${esc(value)}</option>`).join("")}</select></div>
            <div class="filter-group"><label for="filterAuthor">المؤلف</label><select class="select" id="filterAuthor"><option value="all">كل المؤلفين</option>${listValues("author").map((value) => `<option ${state.author === value ? "selected" : ""}>${esc(value)}</option>`).join("")}</select></div>
            <div class="filter-group"><label for="filterPublisher">دار النشر</label><select class="select" id="filterPublisher"><option value="all">كل دور النشر</option>${listValues("publisher").map((value) => `<option ${state.publisher === value ? "selected" : ""}>${esc(value)}</option>`).join("")}</select></div>
            <button class="btn filter-clear" data-action="clear-filters" type="button" ${filterCount ? "" : "disabled"}>مسح التصفية</button>
          </section>
          <div class="table-card"><table><thead><tr><th><label><input class="book-select-all" id="selectPageBooks" type="checkbox" ${pageSelected ? "checked" : ""} ${rows.length ? "" : "disabled"} aria-label="تحديد الكتب الظاهرة"> الكتاب</label></th><th>المؤلف</th><th>المحقق</th><th>دار النشر</th><th>بيانات النشر</th><th>الموضوع</th><th>الحالة</th><th></th></tr></thead><tbody>${rows.length ? rows.map((book) => bookRow(book, true)).join("") : `<tr><td class="empty" colspan="8">${state.books.length ? "لا توجد كتب مطابقة" : "المكتبة فارغة. أضف ملف PDF أو اختر مجلدًا للبدء."}</td></tr>`}</tbody></table></div>
          <div class="pagination"><span>عرض ${all.length ? ((state.page - 1) * state.pageSize) + 1 : 0} إلى ${Math.min(state.page * state.pageSize, all.length)} من ${all.length}</span><div class="pages"><button class="page-btn" data-page="prev" ${state.page === 1 ? "disabled" : ""}>‹</button>${Array.from({ length: pages }, (_, index) => `<button class="page-btn ${state.page === index + 1 ? "active" : ""}" data-page="${index + 1}">${index + 1}</button>`).join("")}<button class="page-btn" data-page="next" ${state.page === pages ? "disabled" : ""}>›</button></div></div>
        </div></div>`;
    bindBooks();
    if (keepSearch) {
      const input = $("#bookSearch");
      input.focus();
      input.setSelectionRange(cursor, cursor);
    }
  }

  function bindBooks() {
    $("#toggleFilters").onclick = () => {
      state.filtersOpen = !state.filtersOpen;
      $("#bookFilters").hidden = !state.filtersOpen;
      $("#toggleFilters").setAttribute("aria-expanded", String(state.filtersOpen));
    };
    $("#bookSearch").oninput = (event) => { state.query = event.target.value; state.page = 1; renderBooks(); };
    $("#bookSearchMode").onchange = (event) => { updateSearchMode(event.target.value); state.page = 1; renderBooks(); };
    $("#sortBooks").onchange = (event) => { state.sort = event.target.value; renderBooks(); };
    $("#filterTopic").onchange = (event) => { state.topic = event.target.value; state.page = 1; renderBooks(); };
    $("#filterAuthor").onchange = (event) => { state.author = event.target.value; state.page = 1; renderBooks(); };
    $("#filterPublisher").onchange = (event) => { state.publisher = event.target.value; state.page = 1; renderBooks(); };
    $$("[data-status]").forEach((control) => control.onchange = () => {
      control.checked ? state.statuses.add(control.dataset.status) : state.statuses.delete(control.dataset.status);
      state.page = 1;
      renderBooks();
    });
    $("#selectPageBooks").onchange = (event) => {
      const pageBooks = filteredBooks().slice((state.page - 1) * state.pageSize, state.page * state.pageSize);
      pageBooks.forEach((book) => event.target.checked
        ? state.selectedBooks.add(book.id)
        : state.selectedBooks.delete(book.id));
      renderBooks();
    };
    $$("[data-select-book]").forEach((control) => {
      control.onclick = (event) => event.stopPropagation();
      control.onchange = () => {
        control.checked ? state.selectedBooks.add(control.dataset.selectBook) : state.selectedBooks.delete(control.dataset.selectBook);
        renderBooks();
      };
    });
    $("#moveBooks").onclick = () => openMoveBooks([...state.selectedBooks]);
    $("#deleteBooks").onclick = () => openDeleteBooks([...state.selectedBooks]);
    $("#exportBooks").onclick = exportSelectedBooks;
    $$("[data-book-id]").forEach((row) => row.onclick = () => go(`book/${row.dataset.bookId}`));
    $$("[data-row-menu]").forEach((button) => button.onclick = (event) => {
      event.stopPropagation();
      openEdit(button.dataset.rowMenu);
    });
    $$("[data-page]").forEach((button) => button.onclick = () => {
      if (button.dataset.page === "prev") state.page -= 1;
      else if (button.dataset.page === "next") state.page += 1;
      else state.page = Number(button.dataset.page);
      renderBooks();
    });
    $$("[data-action]").forEach((button) => button.onclick = () => handleAction(button.dataset.action));
  }

  function scanActivityLabel(page, maxPages) {
    if (page < maxPages) return `أجهّز الصفحة ${page} من ${maxPages} للطلب الموحّد`;
    const pageLabel = maxPages === 1 ? "صفحة واحدة" : `${maxPages} صفحات`;
    return `أحلل ${pageLabel} في طلب Gemini واحد`;
  }

  function formatWait(seconds) {
    if (seconds >= 3600) {
      const hours = Math.floor(seconds / 3600);
      const minutes = Math.ceil((seconds % 3600) / 60);
      return minutes ? `${hours} ساعة و${minutes} دقيقة` : `${hours} ساعة`;
    }
    if (seconds >= 60) return `${Math.ceil(seconds / 60)} دقيقة`;
    return `${seconds} ثانية`;
  }

  function renderScan() {
    const scan = state.scan;
    const running = scan && ["queued", "running"].includes(scan.state);
    const item = running ? scan.items[scan.currentIndex] : null;
    const finished = scan?.items?.filter((entry) => ["complete", "review", "failed", "skipped"].includes(entry.state)).length || 0;
    const targetPages = item ? Math.min(scan.maxPages, state.books.find((book) => book.id === item.book_id)?.pageCount || scan.maxPages) : 1;
    const bookPercent = item ? Math.min(100, Math.round(scan.currentPage / Math.max(targetPages, 1) * 100)) : 0;
    const overallPercent = scan?.items?.length ? Math.min(100, Math.round((finished + (item ? bookPercent / 100 : 0)) / scan.items.length * 100)) : 0;
    const bufferSeconds = running ? Math.max(0, Math.ceil(((scan.bufferUntil || 0) - Date.now()) / 1000)) : 0;
    const buffering = Boolean(bufferSeconds && !scan.paused);
    const quota = state.models.find((model) => model.value === scan?.model);
    const bufferMessage = scan?.rateLimitWindow === "day"
      ? `بلغ الفحص الحد اليومي، ${scan.rateLimitRpd || quota?.dailyRequests || 0} طلبًا. سيستأنف تلقائيًا بعد خروج أقدم طلب من نافذة 24 ساعة.`
      : `بلغ الفحص حد الأمان، ${scan?.rateLimitRpm || quota?.safeRequestsPerMinute || 1} من أصل ${quota?.requestsPerMinute || "الحد المنشور"} طلبات في الدقيقة. سيستأنف تلقائيًا عند توفر طلب جديد.`;
    const heading = running ? (scan.paused ? "الفحص متوقف مؤقتًا" : buffering ? "انتظار حصة API" : "الكتاب الحالي")
      : scan?.state === "cancelled" ? "أُوقف الفحص"
        : scan?.state === "failed" ? "تعذر إكمال الفحص"
          : scan?.items?.length ? "اكتمل الفحص" : "لا يوجد فحص جارٍ";
    $("#routeHost").innerHTML = `
      <div class="page-head"><div><h1>الفحص</h1><p>${esc(state.libraryLabel)}</p></div><div class="actions"><button class="btn" data-action="add-file">إضافة ملف PDF</button><button class="btn btn-primary" data-action="folder">فحص مجلد</button></div></div>
      <div class="scan-grid"><section class="card"><div class="card-head"><h2>${heading}</h2><small>${finished} مكتمل من ${scan?.items?.length || 0}</small></div><div class="current-scan">${item ? `
        <div class="scan-file"><div class="pdf-icon">PDF</div><div><strong>${esc(item.file)}</strong><small>الكتاب ${scan.currentIndex + 1} من ${scan.items.length}، تجهيز الصفحة ${scan.currentPage || 1} من ${targetPages}</small></div></div>
        <div class="scan-activity ${scan.paused ? "paused" : buffering ? "buffering" : ""}" role="status" aria-live="polite"><span class="activity-dot" aria-hidden="true"></span><div><small>ما يجري الآن</small><strong>${scan.paused ? `توقفت عند الصفحة ${scan.currentPage}` : buffering ? `استكمال الفحص بعد ${formatWait(bufferSeconds)}` : scanActivityLabel(scan.currentPage || 1, targetPages)}</strong><p>${scan.paused ? "لن ينتقل الفحص إلى صفحة أخرى حتى تضغط على استكمال." : buffering ? bufferMessage : "تُرسل الصفحات كصور مستقلة مرتبة داخل طلب واحد لهذا الكتاب."}</p></div></div>
        <div class="scan-progresses"><div class="progress-block"><div class="progress-meta"><span>تقدم الكتاب الحالي</span><strong>${bookPercent}%</strong></div><div class="progress"><span style="width:${bookPercent}%"></span></div></div><div class="progress-block"><div class="progress-meta"><span>تقدم الطابور كله</span><strong>${overallPercent}%</strong></div><div class="progress"><span style="width:${overallPercent}%"></span></div></div></div>
        <div class="extracted">${fields.map(([key, label]) => `<div class="${item[key] ? "" : "active"}"><span>${label}</span><strong class="${item[key] ? "" : "waiting"}">${esc(item[key] || (scan.paused ? "متوقف مؤقتًا" : buffering ? "بانتظار حصة API" : "جارٍ البحث"))}</strong></div>`).join("")}</div>
        <div class="scan-controls"><button class="btn btn-primary" id="pauseScan">${scan.paused ? "استكمال الفحص" : "إيقاف مؤقت"}</button><button class="btn" id="skipScan">تخطي هذا الكتاب</button><button class="btn btn-danger" id="cancelScan">إنهاء الفحص</button></div>` : `
        <div class="empty scan-empty"><strong>${heading}</strong><span>${scan?.error ? esc(scan.error) : "اختر ملف PDF أو مجلدًا لبدء استخراج بيانات الكتب."}</span><button class="btn btn-primary" data-action="folder">اختيار مجلد</button></div>`}</div></section>
        <aside class="card"><div class="card-head"><h2>طابور الفحص</h2><div class="card-head-tools"><small>${scan?.items?.length || 0} كتب</small>${scan?.items?.length ? '<button class="link-btn open-all-books" id="openAllScanBooks" type="button">فتح الكل</button>' : ""}</div></div><div class="queue-list">${scan?.items?.length ? scan.items.map((entry, index) => { const current = running && index === scan.currentIndex; const bookName = entry.title || entry.file || "كتاب بلا عنوان"; return `<div class="queue-row ${current ? "current" : ""}"><span class="queue-num">${index + 1}</span><div class="queue-book"><a class="queue-book-link" href="${bookRouteHref(entry.book_id)}" aria-label="فتح صفحة الكتاب: ${esc(bookName)}" title="افتح الصفحة، أو استخدم زر الفأرة الأوسط لفتحها في تبويب جديد">${esc(bookName)}</a>${entry.title && entry.file && entry.title !== entry.file ? `<small class="queue-file">${esc(entry.file)}</small>` : ""}<small>${current && scan.paused ? "متوقف مؤقتًا" : statusLabel(current ? "current" : entry.state)}</small>${entry.error ? `<small class="missing">${esc(entry.error)}</small>` : ""}</div><span class="queue-state ${statusClass(current ? "current" : entry.state)}"></span></div>`; }).join("") : "<div class=\"empty\">الطابور فارغ</div>"}</div></aside></div>`;
    $$("[data-action]").forEach((button) => button.onclick = () => handleAction(button.dataset.action));
    if (scan?.items?.length) {
      $("#openAllScanBooks").onclick = () => {
        const bookIds = [...new Set(scan.items.map((entry) => entry.book_id))];
        bookIds.forEach((bookId) => window.open(bookRouteHref(bookId), "_blank", "noopener"));
        toast("فتح صفحات الكتب", `طُلب فتح ${bookIds.length} صفحة. إذا منع المتصفح بعضها، اسمح بالنوافذ المنبثقة لهذا التطبيق.`);
      };
    }
    if (item) {
      $("#pauseScan").onclick = () => controlScan(scan.paused ? "resume" : "pause");
      $("#skipScan").onclick = () => controlScan("skip");
      $("#cancelScan").onclick = async () => {
        if (confirm("سيُوقف الفحص الحالي وتبقى الكتب المكتملة محفوظة. هل تريد المتابعة؟")) await controlScan("cancel");
      };
    }
  }

  async function controlScan(action) {
    try {
      await api(`/api/scans/${state.scan.id}/${action}`, { method: "POST" });
      await refreshData();
    } catch (error) {
      toast("تعذر تنفيذ الإجراء", error.message, "error");
    }
  }

  function detailMetadataValue(book, key, label) {
    const value = book[key];
    if (!value) return '<strong class="missing">بدون</strong>';
    const route = detailEntityRoutes[key];
    return route ? `<a class="metadata-link" href="#/${route}/${encodeURIComponent(value)}" aria-label="فتح صفحة ${esc(label)}: ${esc(value)}">${esc(value)}</a>` : `<strong>${esc(value)}</strong>`;
  }

  function renderDetail() {
    const book = currentBook();
    if (!book) { go("books"); return; }
    $("#routeHost").innerHTML = `
      <div class="detail-head"><button class="btn btn-icon back" id="backBooks">→</button><div><h1>${esc(book.title)}</h1><p>${esc(book.file)}</p></div><span class="status ${statusClass(book.status)}">${statusLabel(book.status)}</span><div class="actions"><button class="btn" id="openPdf" ${book.pageCount ? "" : "disabled"}>فتح ملف PDF</button><button class="btn" id="openBookFolder" ${book.fullPath ? "" : "disabled"}>فتح المجلد</button><button class="btn" id="changeBookPath">تغيير مسار الملف</button><button class="btn" id="moveBookTopic">نقل إلى موضوع</button><button class="btn" id="editBook">تعديل البيانات</button><button class="btn btn-danger" id="deleteBook">حذف الكتاب</button><button class="btn btn-primary" id="rescanBook" ${book.pageCount ? "" : "disabled"}>إعادة توليد البيانات</button></div></div>
      <div class="detail-layout"><section class="card metadata"><div class="metadata-grid">${fields.map(([key, label]) => `<div class="meta-row"><span>${label}</span>${detailMetadataValue(book, key, label)}</div>`).join("")}<div class="meta-row"><span>الثقة</span><strong>${book.confidence}%</strong></div><div class="meta-row"><span>الصفحات المفحوصة</span><strong>${book.pagesChecked} من ${book.maxPages}</strong></div></div><div class="path-box"><strong>المسار الكامل على الجهاز</strong><br>${esc(book.fullPath || "لا يوجد ملف مرتبط بهذا الكتاب")}</div>${book.error ? `<div class="impact missing">${esc(book.error)}</div>` : ""}</section><aside class="card pdf-preview"><div class="pdf-page"><span>صفحة العنوان</span><strong>${esc(book.title)}</strong><span>${esc(book.author || "المؤلف غير معروف")}</span></div></aside></div>`;
    $("#backBooks").onclick = () => go("books");
    $("#openPdf").onclick = () => window.open(`/api/books/${book.id}/pdf`, "_blank", "noopener");
    $("#openBookFolder").onclick = async () => {
      try {
        await api(`/api/books/${book.id}/folder`, { method: "POST" });
      } catch (error) {
        toast("تعذر فتح المجلد", error.message, "error");
      }
    };
    $("#changeBookPath").onclick = async () => {
      const button = $("#changeBookPath");
      button.disabled = true;
      try {
        const selected = await api("/api/local-picker/file", { method: "POST" });
        if (!selected.path) return;
        await api(`/api/books/${book.id}/path`, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ path: selected.path }),
        });
        await refreshData();
        toast("تغير مسار الملف", selected.path);
      } catch (error) {
        toast("تعذر تغيير المسار", error.message, "error");
      } finally {
        const currentButton = $("#changeBookPath");
        if (currentButton) currentButton.disabled = false;
      }
    };
    $("#moveBookTopic").onclick = () => openMoveBooks([book.id]);
    $("#editBook").onclick = () => openEdit(book.id);
    $("#deleteBook").onclick = () => openDeleteBooks([book.id]);
    $("#rescanBook").onclick = () => openRescan(book.id);
  }

  function entityConfig() { return entityTypes[state.entityType]; }
  function entityRows() {
    const config = entityConfig();
    const map = new Map();
    state.books.forEach((book) => {
      const name = book[config.key];
      if (!name) return;
      if (!map.has(name)) map.set(name, []);
      map.get(name).push(book);
    });
    return [...map.entries()].map(([name, books], index) => ({
      name,
      books,
      index,
      score: searchScore(state.entityQuery, [{ value: name, weight: 1 }], state.searchMode),
    }))
      .filter((row) => row.score > 0)
      .sort((a, b) => state.entityQuery.trim()
        ? b.score - a.score || a.index - b.index
        : a.name.localeCompare(b.name, "ar"));
  }

  function renderEntities() {
    const keepSearch = document.activeElement?.id === "entitySearch";
    const cursor = keepSearch ? document.activeElement.selectionStart : 0;
    const config = entityConfig();
    const allRows = entityRows();
    const pages = Math.max(1, Math.ceil(allRows.length / state.entityPageSize));
    if (state.entityPage > pages) state.entityPage = pages;
    const rows = allRows.slice((state.entityPage - 1) * state.entityPageSize, state.entityPage * state.entityPageSize);
    const allSelected = rows.length && rows.every((row) => state.selectedEntities.has(row.name));
    $("#routeHost").innerHTML = `
      <div class="page-head"><div><h1>${config.title}</h1><p>${allRows.length} اسمًا ${state.entityQuery.trim() ? "مرتبة حسب الصلة" : "مرتبة أبجديًا"}</p></div></div>
      <div class="bulkbar"><label class="select-all"><input id="selectAllEntities" type="checkbox" ${allSelected ? "checked" : ""} aria-label="تحديد الصفحة"></label><div class="search-tools"><label class="search"><svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="7"/><path d="m20 20-4-4"/></svg><input id="entitySearch" value="${esc(state.entityQuery)}" placeholder="ابحث في ${config.title}"></label><select class="search-mode-select" id="entitySearchMode" aria-label="طريقة البحث" title="اختر بين البحث بالكلمات والصيغ القريبة أو مطابقة العبارة">${searchModeOptions(state.searchMode)}</select></div><select class="select" id="entityAction"><option value="">اختر إجراء</option><option value="merge" ${state.entityAction === "merge" ? "selected" : ""}>دمج المحدد</option><option value="rename" ${state.entityAction === "rename" ? "selected" : ""}>إعادة تسمية</option><option value="delete" ${state.entityAction === "delete" ? "selected" : ""}>حذف</option></select><button class="btn btn-primary" id="applyEntityAction" ${state.selectedEntities.size ? "" : "disabled"}>تنفيذ</button><span class="selected-count">${state.selectedEntities.size ? `${state.selectedEntities.size} محدد` : "لم تحدد شيئًا"}</span></div>
      <div class="table-card"><table><thead><tr><th style="width:38px"></th><th>${config.singular}</th><th>عدد الكتب</th><th>كتب مرتبطة</th><th></th></tr></thead><tbody>${rows.length ? rows.map((row) => { const encoded = encodeURIComponent(row.name); return `<tr><td><input class="entity-check" type="checkbox" data-entity="${encoded}" ${state.selectedEntities.has(row.name) ? "checked" : ""}></td><td><button class="link-btn entity-name" data-view-entity="${encoded}">${esc(row.name)}</button></td><td>${row.books.length}</td><td class="book-examples">${esc(row.books.slice(0, 3).map((book) => book.title).join("، "))}${row.books.length > 3 ? "…" : ""}</td><td><button class="link-btn" data-view-entity="${encoded}">فتح الصفحة</button></td></tr>`; }).join("") : '<tr><td class="empty" colspan="5">لا توجد نتائج مطابقة</td></tr>'}</tbody></table></div>
      <div class="pagination"><span>عرض ${allRows.length ? ((state.entityPage - 1) * state.entityPageSize) + 1 : 0} إلى ${Math.min(state.entityPage * state.entityPageSize, allRows.length)} من ${allRows.length}</span><div class="pages"><button class="page-btn" data-entity-page="prev" ${state.entityPage === 1 ? "disabled" : ""}>‹</button>${Array.from({ length: pages }, (_, index) => `<button class="page-btn ${state.entityPage === index + 1 ? "active" : ""}" data-entity-page="${index + 1}">${index + 1}</button>`).join("")}<button class="page-btn" data-entity-page="next" ${state.entityPage === pages ? "disabled" : ""}>›</button></div></div>`;
    bindEntities(rows);
    if (keepSearch) {
      const input = $("#entitySearch");
      input.focus();
      input.setSelectionRange(cursor, cursor);
    }
  }

  function bindEntities(pageRows) {
    $("#entitySearch").oninput = (event) => { state.entityQuery = event.target.value; state.entityPage = 1; state.selectedEntities.clear(); renderEntities(); };
    $("#entitySearchMode").onchange = (event) => { updateSearchMode(event.target.value); state.entityPage = 1; state.selectedEntities.clear(); renderEntities(); };
    $("#entityAction").onchange = (event) => { state.entityAction = event.target.value; };
    $("#selectAllEntities").onchange = (event) => {
      const names = pageRows.map((row) => row.name);
      event.target.checked ? names.forEach((name) => state.selectedEntities.add(name)) : names.forEach((name) => state.selectedEntities.delete(name));
      renderEntities();
    };
    $$("[data-entity]").forEach((box) => box.onchange = () => {
      const name = decodeURIComponent(box.dataset.entity);
      box.checked ? state.selectedEntities.add(name) : state.selectedEntities.delete(name);
      renderEntities();
    });
    $$("[data-view-entity]").forEach((button) => button.onclick = () => go(`${state.entityType}/${button.dataset.viewEntity}`));
    $$("[data-entity-page]").forEach((button) => button.onclick = () => {
      if (button.dataset.entityPage === "prev") state.entityPage -= 1;
      else if (button.dataset.entityPage === "next") state.entityPage += 1;
      else state.entityPage = Number(button.dataset.entityPage);
      renderEntities();
    });
    $("#applyEntityAction").onclick = applyEntityAction;
  }

  function renderEntityDetail() {
    const config = entityConfig();
    const name = decodeURIComponent(state.route.split("/").slice(1).join("/"));
    const books = state.books.filter((book) => book[config.key] === name);
    if (!books.length) { go(state.entityType); return; }
    $("#routeHost").innerHTML = `
      <div class="detail-head"><button class="btn btn-icon back" id="backEntities">←</button><div><h1>${esc(name)}</h1><p>${books.length} كتاب</p></div><div class="actions"><button class="btn" id="renameEntity">إعادة تسمية</button><button class="btn btn-danger" id="deleteEntity">حذف</button></div></div>
      <div class="table-card"><table><thead><tr><th>الكتاب</th><th>المؤلف</th><th>المحقق</th><th>دار النشر</th><th>بيانات النشر</th><th>الموضوع</th><th>الحالة</th><th></th></tr></thead><tbody>${books.map(bookRow).join("")}</tbody></table></div>`;
    $("#backEntities").onclick = () => go(state.entityType);
    $$("[data-book-id]").forEach((row) => row.onclick = () => go(`book/${row.dataset.bookId}`));
    $("#renameEntity").onclick = () => { state.selectedEntities = new Set([name]); openEntityMerge("rename"); };
    $("#deleteEntity").onclick = () => { state.selectedEntities = new Set([name]); deleteEntities(); };
  }

  function renderSettings() {
    const modelOptions = state.models.map((model) => {
      const selected = model.value === state.settings.model ? " selected" : "";
      return `<option value="${model.value}"${selected}>${model.label} (حد المزوّد ${model.requestsPerMinute}/دقيقة، التطبيق ${model.safeRequestsPerMinute}/دقيقة، ${model.dailyRequests}/يوم)</option>`;
    }).join("");
    $("#routeHost").innerHTML = `
      <div class="page-head"><div><h1>الإعدادات</h1><p>مفتاح Gemini والنموذج والتعليمات المستخدمة في فحص الكتب</p></div><div class="actions"><button class="btn btn-primary" id="saveSettings">حفظ الإعدادات</button></div></div>
      <div class="settings-layout"><section class="card settings-card"><h2>Gemini API</h2><p>يحفظ التطبيق المفتاح داخل قاعدة SQLite المحلية على هذا الكمبيوتر.</p><div class="form-field"><label for="apiKeyInput">API Key</label><div class="secret-field"><input class="input" id="apiKeyInput" type="password" autocomplete="new-password" placeholder="${state.settings.keySaved ? "المفتاح محفوظ. اتركه فارغًا للاحتفاظ به" : "أدخل المفتاح"}"><button class="btn btn-icon" id="toggleApiKey" type="button" aria-label="إظهار المفتاح"><svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/></svg></button></div></div><div class="form-field" style="margin-top:14px"><label for="modelInput">نموذج Gemini</label><select class="input" id="modelInput" dir="ltr" aria-describedby="modelInputHint">${modelOptions}</select><div class="hint" id="modelInputHint">يترك التطبيق طلبًا واحدًا احتياطيًا تحت حد الدقيقة، ويحسب كل طلب صفحة داخل نافذتين متحركتين مدتهما 60 ثانية و24 ساعة.</div></div><div class="key-status ${state.settings.keySaved ? "saved" : ""}">${state.settings.keySaved ? "المفتاح محفوظ في قاعدة البيانات" : "لم يُحفظ مفتاح بعد"}</div></section>
      <section class="card prompt-card"><div class="prompt-head"><h2>Prompt استخراج بيانات الكتاب</h2><button class="btn btn-small" id="resetPrompt">استعادة النص الافتراضي</button></div><div class="prompt-body"><div class="variables"><span>إدراج متغير</span>${["{{file_name}}", "{{page_number}}", "{{max_pages}}", "{{previous_results}}", "{{missing_fields}}"].map((token) => `<button class="variable-btn" data-token="${token}">${token}</button>`).join("")}</div><textarea class="prompt-editor" id="promptInput" spellcheck="false">${esc(state.settings.prompt)}</textarea><div class="prompt-foot"><span>تُستبدل المتغيرات قبل الطلب. مخطط JSON ثابت داخل الخادم ولا يظهر هنا.</span><span id="promptCount">${state.settings.prompt.length} حرف</span></div></div></section></div>`;
    bindSettings();
  }

  function bindSettings() {
    const key = $("#apiKeyInput");
    const prompt = $("#promptInput");
    $("#toggleApiKey").onclick = () => {
      key.type = key.type === "password" ? "text" : "password";
      $("#toggleApiKey").setAttribute("aria-label", key.type === "password" ? "إظهار المفتاح" : "إخفاء المفتاح");
    };
    prompt.oninput = () => { $("#promptCount").textContent = `${prompt.value.length} حرف`; };
    $$("[data-token]").forEach((button) => button.onclick = () => {
      prompt.focus();
      prompt.setRangeText(button.dataset.token, prompt.selectionStart, prompt.selectionEnd, "end");
      prompt.dispatchEvent(new Event("input"));
    });
    $("#resetPrompt").onclick = () => {
      prompt.value = state.settings.defaultPrompt;
      prompt.dispatchEvent(new Event("input"));
      prompt.focus();
    };
    $("#saveSettings").onclick = async () => {
      const payload = { api_key: key.value.trim(), prompt: prompt.value.trim(), model: $("#modelInput").value.trim() };
      if (!payload.api_key && !state.settings.keySaved) return toast("مفتاح API مطلوب", "أدخل مفتاح Gemini قبل الحفظ", "error");
      if (!payload.prompt) return toast("الـPrompt مطلوب", "أدخل تعليمات استخراج البيانات", "error");
      try {
        await api("/api/settings", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
        await refreshData();
        toast("حُفظت الإعدادات", "ستُستخدم في عمليات الفحص القادمة");
      } catch (error) {
        toast("تعذر حفظ الإعدادات", error.message, "error");
      }
    };
  }

  function applyEntityAction() {
    const count = state.selectedEntities.size;
    const action = state.entityAction;
    if (!action) return toast("اختر إجراءً", "حدد الدمج أو إعادة التسمية أو الحذف", "error");
    if (action === "merge" && count < 2) return toast("اختر اسمين على الأقل", "الدمج يحتاج إلى اسمين أو أكثر", "error");
    if (action === "rename" && count !== 1) return toast("اختر اسمًا واحدًا", "إعادة التسمية تعمل على اسم واحد", "error");
    if (action === "delete") return deleteEntities();
    openEntityMerge(action);
  }

  function openEntityMerge(mode) {
    state.entityMode = mode;
    const config = entityConfig();
    const selected = [...state.selectedEntities];
    const counts = new Map(entityRows().map((row) => [row.name, row.books.length]));
    const suggested = [...selected].sort((a, b) => (counts.get(b) || 0) - (counts.get(a) || 0))[0] || "";
    $("#entityMergeTitle").textContent = mode === "merge" ? `دمج ${config.title}` : `إعادة تسمية ${config.singular}`;
    $("#entityMergeList").innerHTML = selected.map((name) => `<span class="merge-chip">${esc(name)} · ${counts.get(name) || 0} كتاب</span>`).join("");
    $("#canonicalEntityName").value = suggested;
    const affected = state.books.filter((book) => selected.includes(book[config.key])).length;
    $("#mergeImpact").textContent = mode === "merge" ? `ستصبح الكتب البالغ عددها ${affected} تحت اسم واحد.` : `سيظهر الاسم الجديد في ${affected} كتاب.`;
    openModal("entityMergeModal");
  }

  async function saveEntityMerge() {
    const canonical = $("#canonicalEntityName").value.trim();
    if (!canonical) return toast("الاسم مطلوب", "اكتب الاسم الصحيح قبل الحفظ", "error");
    const wasDetail = state.route.includes("/");
    try {
      const result = await api(`/api/entities/${entityConfig().key}/change`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ names: [...state.selectedEntities], canonical }),
      });
      state.selectedEntities.clear();
      state.entityAction = "";
      closeModal("entityMergeModal");
      await refreshData(false);
      wasDetail ? go(state.entityType) : render();
      toast("حُفظ التغيير", `${result.affected} كتاب تحت اسم ${canonical}`);
    } catch (error) {
      toast("تعذر حفظ التغيير", error.message, "error");
    }
  }

  async function deleteEntities() {
    const config = entityConfig();
    const names = [...state.selectedEntities];
    const affected = state.books.filter((book) => names.includes(book[config.key])).length;
    const wasDetail = state.route.includes("/");
    if (!confirm(`سيُحذف ${config.singular} من ${affected} كتاب. هل تريد المتابعة؟`)) return;
    try {
      await api(`/api/entities/${config.key}/change`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ names, canonical: null }),
      });
      state.selectedEntities.clear();
      state.entityAction = "";
      await refreshData(false);
      wasDetail ? go(state.entityType) : render();
      toast("تم الحذف", `${affected} كتاب يحتاج مراجعة`);
    } catch (error) {
      toast("تعذر الحذف", error.message, "error");
    }
  }

  function openMoveBooks(bookIds) {
    const books = [...new Set(bookIds)]
      .map((id) => state.books.find((book) => book.id === id))
      .filter(Boolean);
    if (!books.length) return;
    state.movingBookIds = books.map((book) => book.id);
    $("#moveBooksTitle").textContent = books.length === 1 ? "نقل الكتاب إلى موضوع" : "نقل الكتب إلى موضوع";
    $("#moveTopicInput").value = "";
    $("#moveTopicOptions").innerHTML = listValues("topic")
      .map((topic) => `<option value="${esc(topic)}"></option>`)
      .join("");
    const currentTopics = [...new Set(books.map((book) => book.topic).filter(Boolean))];
    $("#moveBooksImpact").textContent = books.length === 1
      ? `سيُنقل «${books[0].title}» من ${currentTopics[0] || "دون موضوع"}.`
      : `سيُنقل ${books.length} من الكتب${currentTopics.length ? ` من ${currentTopics.length} موضوع` : " التي لا موضوع لها"}.`;
    openModal("moveBooksModal");
    $("#moveTopicInput").focus();
  }

  async function saveBookMove() {
    const topic = $("#moveTopicInput").value.trim();
    if (!topic) return toast("الموضوع مطلوب", "اختر موضوعًا موجودًا أو اكتب موضوعًا جديدًا", "error");
    try {
      const result = await api("/api/books/move", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ book_ids: state.movingBookIds, topic }),
      });
      state.movingBookIds.forEach((id) => state.selectedBooks.delete(id));
      closeModal("moveBooksModal");
      await refreshData();
      toast(
        result.affected ? "تم نقل الكتب" : "لا يوجد تغيير",
        result.affected ? `${result.affected} كتاب إلى موضوع ${topic}` : `الكتب موجودة بالفعل في موضوع ${topic}`,
      );
    } catch (error) {
      toast("تعذر نقل الكتب", error.message, "error");
    }
  }

  function openDeleteBooks(bookIds) {
    const books = [...new Set(bookIds)]
      .map((id) => state.books.find((book) => book.id === id))
      .filter(Boolean);
    if (!books.length) return;
    state.deletingBookIds = books.map((book) => book.id);
    $("#deleteBooksTitle").textContent = books.length === 1 ? "حذف الكتاب" : "حذف الكتب المحددة";
    $("#deleteBooksImpact").textContent = books.length === 1
      ? `سيُحذف «${books[0].title}» وبيانات فهرسته من المكتبة. لن يظهر بعد ذلك في صفحات المؤلف أو الموضوع أو بقية الفهارس.`
      : `سيُحذف ${books.length} من الكتب وبيانات فهرستها من المكتبة. ستُحدّث صفحات المؤلفين والموضوعات وبقية الفهارس تلقائيًا.`;
    $("#confirmDeleteBooksBtn").textContent = books.length === 1 ? "حذف الكتاب" : "حذف الكتب";
    openModal("deleteBooksModal");
  }

  async function deleteBooks() {
    const bookIds = [...state.deletingBookIds];
    if (!bookIds.length) return;
    const button = $("#confirmDeleteBooksBtn");
    button.disabled = true;
    button.textContent = "جارٍ الحذف";
    try {
      const result = await api("/api/books", {
        method: "DELETE", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ book_ids: bookIds }),
      });
      const deletingCurrentBook = state.route.startsWith("book/")
        && bookIds.includes(state.route.split("/")[1]);
      bookIds.forEach((id) => state.selectedBooks.delete(id));
      state.deletingBookIds = [];
      closeModal("deleteBooksModal");
      await refreshData(false);
      if (deletingCurrentBook) {
        state.route = "books";
        history.replaceState(null, "", "#/books");
      }
      render();
      toast(
        result.deleted === 1 ? "تم حذف الكتاب" : "تم حذف الكتب",
        result.deleted === 1 ? "حُذفت بياناته من المكتبة" : `حُذف ${result.deleted} من الكتب من المكتبة`,
      );
    } catch (error) {
      toast("تعذر حذف الكتب", error.message, "error");
    } finally {
      button.disabled = false;
      if (state.deletingBookIds.length) {
        button.textContent = state.deletingBookIds.length === 1 ? "حذف الكتاب" : "حذف الكتب";
      }
    }
  }

  async function exportSelectedBooks() {
    const bookIds = [...state.selectedBooks];
    if (!bookIds.length) return;
    const button = $("#exportBooks");
    button.disabled = true;
    button.textContent = "أُجهز ملف Word";
    try {
      const response = await fetch("/api/books/export", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ book_ids: bookIds }),
      });
      if (!response.ok) {
        const payload = await response.json().catch(() => null);
        throw new Error(payload?.detail || `تعذر التصدير (${response.status})`);
      }
      const url = URL.createObjectURL(await response.blob());
      const link = document.createElement("a");
      link.href = url;
      link.download = "فهرس-الكتب.docx";
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(url);
      toast("اكتمل التصدير", `يتضمن ملف Word عدد ${bookIds.length} من الكتب`);
    } catch (error) {
      toast("تعذر تصدير الكتب", error.message, "error");
    } finally {
      renderBooks();
    }
  }

  function handleAction(action) {
    if (action === "folder") openFolder();
    if (action === "add-file") chooseSingleFile();
    if (action === "manual") openManual();
    if (action === "clear-filters") {
      state.statuses.clear();
      state.topic = state.author = state.publisher = "all";
      state.page = 1;
      renderBooks();
    }
  }

  function openModal(id) { $(`#${id}`).classList.add("open"); }
  function closeModal(id) { $(`#${id}`).classList.remove("open"); }
  function openFolder() {
    pendingFolder = "";
    pendingFileCount = 0;
    folderPreviewSequence += 1;
    $("#pendingFolderPath").textContent = "لم يُحدد مجلد";
    $("#folderSelectionSummary").textContent = "اختر مجلدًا لعرض عدد الملفات التي ستُفحص.";
    $("#startFolderScanBtn").disabled = true;
    openModal("folderModal");
  }
  async function chooseFolder(path) {
    pendingFolder = path;
    $("#pendingFolderPath").textContent = path;
    await updateFolderSelection();
  }
  function folderOptions() {
    const includeSubfolders = $("#includeSubfolders").checked;
    return {
      includeSubfolders,
      maxDepth: includeSubfolders ? Number($("#folderMaxDepth").value) : 0,
      perFolderLimit: Number($("#folderPdfLimit").value),
      scanItemLimit: Number($("#folderScanItemLimit").value),
    };
  }
  function folderPayload() {
    const options = folderOptions();
    return {
      path: pendingFolder,
      include_subfolders: options.includeSubfolders,
      max_depth: options.includeSubfolders ? options.maxDepth : 1,
      per_folder_limit: options.perFolderLimit,
      scan_item_limit: options.scanItemLimit,
    };
  }
  async function updateFolderSelection() {
    const previewSequence = ++folderPreviewSequence;
    $("#folderMaxDepth").disabled = !$("#includeSubfolders").checked;
    const options = folderOptions();
    const validDepth = !options.includeSubfolders || (Number.isInteger(options.maxDepth) && options.maxDepth >= 1 && options.maxDepth <= 50);
    const validLimit = Number.isInteger(options.perFolderLimit) && options.perFolderLimit >= 1 && options.perFolderLimit <= 100;
    const validScanItemLimit = Number.isInteger(options.scanItemLimit) && options.scanItemLimit >= 1 && options.scanItemLimit <= 490;
    pendingFileCount = 0;
    $("#startFolderScanBtn").disabled = true;
    if (!pendingFolder) return;
    if (!validDepth || !validLimit || !validScanItemLimit) {
      $("#folderSelectionSummary").textContent = "راجع عمق المجلدات وحدود الملفات وطابور الفحص.";
      return;
    }
    $("#folderSelectionSummary").textContent = "جارٍ فحص محتويات المجلد...";
    try {
      const preview = await api("/api/imports/folder/preview", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(folderPayload()),
      });
      if (previewSequence !== folderPreviewSequence) return;
      pendingFolder = preview.path;
      pendingFileCount = preview.count;
      $("#pendingFolderPath").textContent = preview.path;
      $("#startFolderScanBtn").disabled = !pendingFileCount;
      const skippedText = preview.skippedCount ? ` سيتجاوز التطبيق ${preview.skippedCount} ملفًا مسجلًا من فحص سابق.` : "";
      $("#folderSelectionSummary").textContent = pendingFileCount
        ? `سيُضاف ${pendingFileCount} سطرًا إلى طابور الفحص داخل ${preview.folderCount} مجلد.${skippedText}`
        : `لا توجد ملفات PDF جديدة مطابقة لهذه الخيارات.${skippedText}`;
    } catch (error) {
      if (previewSequence !== folderPreviewSequence) return;
      $("#folderSelectionSummary").textContent = error.message;
    }
  }
  function readMaxPages(id) {
    const input = $(`#${id}`);
    const value = Number(input.value);
    if (!Number.isInteger(value) || value < 1 || value > 100) {
      input.setAttribute("aria-invalid", "true");
      input.focus();
      toast("عدد الصفحات غير صحيح", "اكتب رقمًا صحيحًا من 1 إلى 100", "error");
      return null;
    }
    input.removeAttribute("aria-invalid");
    return value;
  }
  function readFolderNumber(id, min, max, message) {
    const input = $(`#${id}`);
    const value = Number(input.value);
    if (!Number.isInteger(value) || value < min || value > max) {
      input.setAttribute("aria-invalid", "true");
      input.focus();
      toast("القيمة غير صحيحة", message, "error");
      return null;
    }
    input.removeAttribute("aria-invalid");
    return value;
  }

  async function importAndScan(importPath, payload, maxPages, folderLabel) {
    if (!state.settings.keySaved) {
      toast("أدخل مفتاح Gemini أولًا", "احفظ المفتاح من صفحة الإعدادات ثم أعد اختيار الكتب", "error");
      go("settings");
      return;
    }
    if (state.scan && ["queued", "running"].includes(state.scan.state)) {
      toast("يوجد فحص جارٍ", "انتظر اكتماله أو أوقفه قبل إضافة طابور جديد", "error");
      go("scan");
      return;
    }
    state.importing = true;
    go("scan");
    $("#routeHost").innerHTML = '<div class="card"><div class="empty scan-empty"><strong>جارٍ تجهيز الكتب</strong><span>تُستخدم الملفات من مساراتها الأصلية.</span></div></div>';
    try {
      const imported = await api(importPath, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const bookIds = imported.bookIds || [imported.id].filter(Boolean);
      if (!bookIds.length) throw new Error("لا توجد ملفات PDF مطابقة لهذه الخيارات");
      state.libraryLabel = folderLabel || "المكتبة المحلية";
      await api("/api/scans", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ book_ids: bookIds, max_pages: maxPages }),
      });
      await refreshData();
      toast("بدأ الفحص", `${bookIds.length} كتاب في الطابور`);
    } catch (error) {
      await refreshData();
      toast("تعذر بدء الفحص", error.message, "error");
    } finally {
      state.importing = false;
    }
  }

  async function chooseSingleFile() {
    try {
      const selected = await api("/api/local-picker/file", { method: "POST" });
      if (!selected.path) return;
      await importAndScan("/api/imports/file", { path: selected.path }, 5, selected.path);
    } catch (error) {
      toast("تعذر اختيار الملف", error.message, "error");
    }
  }

  function openManual() {
    $("#manualFields").innerHTML = formFields({
      title: "", author: "", editor: "", publisher: "", publication_year: "",
      edition_number: "", volume_number: "", topic: "", path: "",
    }, true);
    openModal("manualModal");
  }
  function formFields(book, includePath = false) {
    return fields.map(([key, label]) => `<div class="form-field"><label>${label}</label><input class="input" data-field="${key}" value="${esc(book[key] || "")}"></div>`).join("")
      + (includePath ? `<div class="form-field full"><label>مسار مرجعي لملف PDF</label><input class="input" data-field="path" value="${esc(book.path || "")}" placeholder="اختياري"></div>` : "");
  }
  function openEdit(id) {
    const book = state.books.find((entry) => entry.id === id);
    if (!book) return;
    state.editingId = id;
    $("#editFields").innerHTML = formFields(book);
    openModal("editModal");
  }
  function openRescan(id) {
    const book = state.books.find((entry) => entry.id === id);
    if (!book) return;
    state.rescanId = id;
    $("#rescanMaxPages").value = book.maxPages || 5;
    openModal("rescanModal");
  }
  function collect(container) {
    const data = {};
    container.querySelectorAll("[data-field]").forEach((input) => { data[input.dataset.field] = input.value.trim(); });
    return data;
  }
  function toast(title, message, kind = "") {
    $(".toast")?.remove();
    const node = document.createElement("div");
    node.className = `toast ${kind}`;
    node.innerHTML = `<strong>${esc(title)}</strong><span>${esc(message)}</span>`;
    $("#toastHost").appendChild(node);
    setTimeout(() => node.remove(), 5000);
  }

  function bindStaticControls() {
    $$("[data-route]").forEach((button) => button.onclick = () => go(button.dataset.route));
    $("#scanIndicator").onclick = () => go("scan");
    $("#sidebarFolder").onclick = openFolder;
    $("#pickFolderBtn").onclick = async () => {
      const button = $("#pickFolderBtn");
      button.disabled = true;
      try {
        const selected = await api("/api/local-picker/folder", { method: "POST" });
        if (selected.path) await chooseFolder(selected.path);
      } catch (error) {
        toast("تعذر اختيار المجلد", error.message, "error");
      } finally {
        button.disabled = false;
      }
    };
    $("#recentFolderBtn").hidden = true;
    $("#includeSubfolders").onchange = updateFolderSelection;
    $("#folderMaxDepth").oninput = updateFolderSelection;
    $("#folderPdfLimit").oninput = updateFolderSelection;
    $("#folderScanItemLimit").oninput = updateFolderSelection;
    $("#startFolderScanBtn").onclick = async () => {
      const maxPages = readMaxPages("folderMaxPages");
      if (maxPages === null) return;
      if ($("#includeSubfolders").checked && readFolderNumber("folderMaxDepth", 1, 50, "اكتب عمقًا صحيحًا من 1 إلى 50") === null) return;
      if (readFolderNumber("folderPdfLimit", 1, 100, "اكتب عدد ملفات صحيحًا من 1 إلى 100") === null) return;
      if (readFolderNumber("folderScanItemLimit", 1, 490, "اكتب عدد أسطر صحيحًا من 1 إلى 490") === null) return;
      if (!pendingFileCount) return toast("لا توجد ملفات للفحص", "غيّر خيارات المجلد أو اختر مجلدًا آخر", "error");
      const payload = folderPayload();
      closeModal("folderModal");
      await importAndScan("/api/imports/folder", payload, maxPages, pendingFolder);
    };
    $("#saveManualBtn").onclick = async () => {
      const data = collect($("#manualFields"));
      if (!data.title) return toast("العنوان مطلوب", "اكتب عنوان الكتاب", "error");
      try {
        await api("/api/books/manual", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data) });
        closeModal("manualModal");
        await refreshData(false);
        state.page = 1;
        go("books");
        render();
        toast("أُضيف الكتاب", data.title);
      } catch (error) {
        toast("تعذر إضافة الكتاب", error.message, "error");
      }
    };
    $("#saveEditBtn").onclick = async () => {
      const data = collect($("#editFields"));
      if (!data.title) return toast("العنوان مطلوب", "اكتب عنوان الكتاب", "error");
      try {
        await api(`/api/books/${state.editingId}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ...data, path: "" }) });
        closeModal("editModal");
        await refreshData();
        toast("حُفظت البيانات", data.title);
      } catch (error) {
        toast("تعذر حفظ البيانات", error.message, "error");
      }
    };
    $("#confirmRescanBtn").onclick = async () => {
      const maxPages = readMaxPages("rescanMaxPages");
      if (maxPages === null) return;
      closeModal("rescanModal");
      try {
        await api("/api/scans", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ book_ids: [state.rescanId], max_pages: maxPages }) });
        go("scan");
        await refreshData();
      } catch (error) {
        toast("تعذر بدء إعادة الفحص", error.message, "error");
      }
    };
    $("#saveEntityMergeBtn").onclick = saveEntityMerge;
    $("#saveBookMoveBtn").onclick = saveBookMove;
    $("#confirmDeleteBooksBtn").onclick = deleteBooks;
    $$("[data-close]").forEach((button) => button.onclick = () => closeModal(button.dataset.close));
    $$(".modal-backdrop").forEach((modal) => modal.onclick = (event) => { if (event.target === modal) closeModal(modal.id); });
    window.addEventListener("hashchange", parseRoute);
  }

  async function initialize() {
    bindStaticControls();
    $("#routeHost").innerHTML = '<div class="card"><div class="empty">جارٍ فتح المكتبة…</div></div>';
    try {
      await refreshData(false);
      if (!location.hash) location.hash = "#/books";
      else parseRoute();
    } catch (error) {
      $("#routeHost").innerHTML = `<div class="card"><div class="empty missing">تعذر فتح المكتبة: ${esc(error.message)}</div></div>`;
    }
  }

  initialize();
})();
