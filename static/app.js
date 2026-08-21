/* Webo Healthtech — Document Extraction (browser) */

const $ = (id) => document.getElementById(id);

const els = {
  workspace: $("workspace"),
  setupError: $("setup-error"),
  maxMb: $("max-mb"),
  fileInput: $("file-input"),
  dropzone: $("dropzone"),
  fileCard: $("file-card"),
  fileName: $("file-name"),
  fileDetail: $("file-detail"),
  clearFile: $("clear-file"),
  modes: $("modes"),
  runFile: $("run-file"),
  runMode: $("run-mode"),
  runCost: $("run-cost"),
  runBtn: $("run-btn"),
  errorCard: $("error-card"),
  errorCopy: $("error-copy"),
  errorRetry: $("error-retry"),

  creditsChip: $("credits-chip"),
  creditArc: $("credit-arc"),
  creditsRemaining: $("credits-remaining"),
  popRemaining: $("pop-remaining"),
  popUsed: $("pop-used"),
  popTotal: $("pop-total"),
  popRenews: $("pop-renews"),

  processing: $("processing"),
  processingTitle: $("processing-title"),
  processingFile: $("processing-file"),
  processingFoot: $("processing-foot"),
  elapsed: $("elapsed"),
  stages: $("stages"),

  viewer: $("viewer"),
  viewerBack: $("viewer-back"),
  resultTitle: $("result-title"),
  resultChips: $("result-chips"),
  stage: $("viewer-stage"),
  originalFrame: $("original-frame"),
  originalEmpty: $("original-empty"),
  extractedScroll: $("extracted-scroll"),
  extractedNote: $("extracted-note"),
  docFormatted: $("doc-formatted"),
  docMarkdown: $("doc-markdown"),
  docPlain: $("doc-plain"),
  layoutToggle: $("layout-toggle"),
  formatToggle: $("format-toggle"),
  searchInput: $("search-input"),
  searchCount: $("search-count"),
  searchPrev: $("search-prev"),
  searchNext: $("search-next"),
  zoomIn: $("zoom-in"),
  zoomOut: $("zoom-out"),
  zoomLevel: $("zoom-level"),
  copyBtn: $("copy-btn"),
  downloadBtn: $("download-btn"),
  downloadMenu: $("download-menu"),
};

const state = {
  modes: [],
  modeId: "high",
  file: null,
  pages: null,
  result: null,
  objectUrl: null,
  pollTimer: null,
  tickTimer: null,
  startedAt: 0,
  zoom: 1,
  matches: [],
  matchIndex: 0,
  ready: true,
};

const STAGE_ORDER = ["upload", "queue", "read", "finish"];
const RING = 97.4; // circumference of the credit ring

marked.setOptions({ gfm: true, breaks: true });

/* ----------------------------------------------------------------- helpers */

const fmt = (n) => Number(n || 0).toLocaleString("en-US");

function formatBytes(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function activeMode() {
  return state.modes.find((m) => m.id === state.modeId) || state.modes[0] || null;
}

async function api(path, options) {
  const response = await fetch(path, options);
  let data = {};
  try {
    data = await response.json();
  } catch {
    /* non-JSON responses fall through to the generic message below */
  }
  if (!response.ok) {
    const detail = Array.isArray(data.detail)
      ? data.detail.map((d) => d.msg || "").join(" ")
      : data.detail;
    throw new Error(detail || "Something went wrong. Please try again.");
  }
  return data;
}

/* ----------------------------------------------------------------- credits */

function renderCredits(credits) {
  if (!credits || !credits.available) {
    els.creditsChip.classList.add("is-empty");
    return;
  }
  els.creditsChip.classList.remove("is-empty", "is-loading");

  const { remaining = 0, total = 0, used = 0, renews_on: renews } = credits;
  const ratio = total > 0 ? Math.max(0, Math.min(1, remaining / total)) : 0;

  els.creditArc.style.strokeDashoffset = String(RING * (1 - ratio));
  els.creditsChip.classList.toggle("low", ratio <= 0.25 && ratio > 0.1);
  els.creditsChip.classList.toggle("critical", ratio <= 0.1);

  countTo(els.creditsRemaining, remaining);
  els.popRemaining.textContent = fmt(remaining);
  els.popUsed.textContent = fmt(used);
  els.popTotal.textContent = fmt(total);
  els.popRenews.textContent = renews
    ? `Allowance renews ${new Date(renews).toLocaleDateString(undefined, {
        day: "numeric",
        month: "short",
        year: "numeric",
      })}`
    : "";
}

function countTo(el, target) {
  const from = Number(String(el.textContent).replace(/[^0-9]/g, "")) || 0;
  if (from === target) {
    el.textContent = fmt(target);
    return;
  }
  const started = performance.now();
  const step = (now) => {
    const t = Math.min(1, (now - started) / 700);
    const eased = 1 - Math.pow(1 - t, 3);
    el.textContent = fmt(Math.round(from + (target - from) * eased));
    if (t < 1) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}

async function refreshCredits(force) {
  try {
    renderCredits(await api(`/api/credits${force ? "?refresh=true" : ""}`));
  } catch {
    /* the balance is informational — never block the workflow on it */
  }
}

/* ------------------------------------------------------------------- modes */

function renderModes() {
  els.modes.innerHTML = "";
  state.modes.forEach((mode) => {
    const card = document.createElement("button");
    card.type = "button";
    card.className = "mode" + (mode.id === state.modeId ? " selected" : "");
    card.setAttribute("role", "radio");
    card.setAttribute("aria-checked", String(mode.id === state.modeId));
    card.dataset.mode = mode.id;

    const bars = [1, 2, 3, 4]
      .map((n) => `<i class="${n <= mode.accuracy ? "on" : ""}"></i>`)
      .join("");

    card.innerHTML = `
      ${mode.recommended ? '<span class="mode-badge">Recommended</span>' : ""}
      <span class="mode-name">${mode.name}</span>
      <span class="mode-tag">${mode.tagline}</span>
      <p class="mode-desc">${mode.description}</p>
      <div class="mode-best"><b>Best for</b>${mode.best_for}</div>
      <div class="mode-foot">
        <span class="mode-cost">${mode.credits_per_page} <span>credits/page</span></span>
        <span class="accuracy" title="Reading depth">${bars}</span>
      </div>
      <div class="mode-speed">Usually ${mode.speed.toLowerCase()}</div>
    `;
    card.addEventListener("click", () => {
      state.modeId = mode.id;
      renderModes();
      updateRunBar();
    });
    els.modes.appendChild(card);
  });
}

/* -------------------------------------------------------------------- file */

async function countPdfPages(file) {
  try {
    const bytes = new Uint8Array(await file.arrayBuffer());
    const text = new TextDecoder("latin1").decode(bytes);
    const counts = [...text.matchAll(/\/Type\s*\/Pages\b[\s\S]{0,400}?\/Count\s+(\d+)/g)].map((m) =>
      parseInt(m[1], 10)
    );
    if (counts.length) return Math.max(...counts);
    const found = text.match(/\/Type\s*\/Page(?![s/\w])/g);
    return found ? found.length : null;
  } catch {
    return null;
  }
}

async function setFile(file) {
  hideError();
  if (!file) {
    state.file = null;
    state.pages = null;
    els.fileCard.classList.add("hidden");
    els.dropzone.classList.remove("hidden");
    updateRunBar();
    return;
  }
  if (!file.name.toLowerCase().endsWith(".pdf") && file.type !== "application/pdf") {
    showError("That file isn't a PDF. Please choose a PDF document.");
    return;
  }

  state.file = file;
  state.pages = null;
  els.fileName.textContent = file.name;
  els.fileDetail.textContent = `${formatBytes(file.size)} · checking pages…`;
  els.fileCard.classList.remove("hidden");
  els.dropzone.classList.add("hidden");
  updateRunBar();

  state.pages = await countPdfPages(file);
  els.fileDetail.textContent = state.pages
    ? `${formatBytes(file.size)} · ${state.pages} page${state.pages === 1 ? "" : "s"}`
    : formatBytes(file.size);
  updateRunBar();
}

function updateRunBar() {
  const mode = activeMode();
  els.runFile.textContent = state.file ? state.file.name : "No file selected";
  els.runMode.textContent = mode ? mode.name : "—";

  if (!mode) {
    els.runCost.textContent = "—";
  } else if (state.pages) {
    els.runCost.textContent = `about ${fmt(state.pages * mode.credits_per_page)} credits`;
  } else {
    els.runCost.textContent = `${mode.credits_per_page} credits per page`;
  }
  els.runBtn.disabled = !state.file || !state.ready;
}

function showError(message) {
  els.errorCopy.textContent = message;
  els.errorCard.classList.remove("hidden");
  els.errorCard.scrollIntoView({ behavior: "smooth", block: "center" });
}

function hideError() {
  els.errorCard.classList.add("hidden");
}

/* -------------------------------------------------------------- processing */

function setStage(name) {
  const target = STAGE_ORDER.indexOf(name);
  [...els.stages.children].forEach((li, index) => {
    li.classList.toggle("done", index < target);
    li.classList.toggle("active", index === target);
  });
}

function startProcessing() {
  const mode = activeMode();
  els.processing.classList.remove("hidden");
  els.processingFile.textContent = state.file ? state.file.name : "";
  els.processingTitle.textContent = "Reading your document";
  els.processingFoot.textContent = mode
    ? `${mode.name} usually takes ${mode.speed.toLowerCase()}. You can leave this open.`
    : "";
  setStage("upload");

  state.startedAt = Date.now();
  els.elapsed.textContent = "0:00";
  clearInterval(state.tickTimer);
  state.tickTimer = setInterval(() => {
    const seconds = Math.floor((Date.now() - state.startedAt) / 1000);
    els.elapsed.textContent = `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
  }, 500);
}

function stopProcessing() {
  clearInterval(state.tickTimer);
  clearInterval(state.pollTimer);
  state.tickTimer = null;
  state.pollTimer = null;
  els.processing.classList.add("hidden");
}

/* ----------------------------------------------------------------- extract */

async function runExtraction() {
  if (!state.file) return;
  hideError();
  els.runBtn.disabled = true;
  startProcessing();

  const body = new FormData();
  body.append("file", state.file);
  body.append("mode", state.modeId);

  try {
    const job = await api("/api/documents", { method: "POST", body });
    setStage("queue");
    if (!state.pages && job.page_count) {
      state.pages = job.page_count;
    }

    const deadline = Date.now() + 10 * 60 * 1000;
    if (await pollOnce(job.job_id)) return;

    state.pollTimer = setInterval(async () => {
      try {
        if (Date.now() > deadline) throw new Error("This is taking longer than expected. Please try again.");
        await pollOnce(job.job_id);
      } catch (err) {
        stopProcessing();
        showError(err.message);
        els.runBtn.disabled = false;
      }
    }, 2000);
  } catch (err) {
    stopProcessing();
    showError(err.message);
    els.runBtn.disabled = false;
  }
}

async function pollOnce(jobId) {
  const data = await api(`/api/documents/${encodeURIComponent(jobId)}`);
  const status = (data.status || "").toUpperCase();

  if (status === "RUNNING") setStage("read");
  if (status === "COMPLETED") {
    setStage("finish");
    [...els.stages.children].forEach((li) => {
      li.classList.add("done");
      li.classList.remove("active");
    });
    // Let the completed checklist register before swapping to the result.
    await new Promise((resolve) => setTimeout(resolve, 420));
    stopProcessing();
    showResult(data);
    refreshCredits(true);
    els.runBtn.disabled = false;
    return true;
  }
  if (status === "FAILED" || status === "CANCELLED") {
    throw new Error(data.error || "We couldn't read this document. Please try a different file.");
  }
  return false;
}

/* ------------------------------------------------------------------ viewer */

function renderMarkdown(markdown) {
  return DOMPurify.sanitize(marked.parse(markdown || ""), { USE_PROFILES: { html: true } });
}

function decorate(root) {
  root.querySelectorAll("table").forEach((table) => {
    if (table.parentElement && table.parentElement.classList.contains("table-wrap")) return;
    const wrap = document.createElement("div");
    wrap.className = "table-wrap";
    table.replaceWith(wrap);
    wrap.appendChild(table);
  });
  root.querySelectorAll("a[href]").forEach((link) => {
    link.target = "_blank";
    link.rel = "noreferrer";
  });
  root.querySelectorAll("img").forEach((img) => {
    img.loading = "lazy";
  });
}

function showResult(data) {
  state.result = data;
  const mode = state.modes.find((m) => m.id === data.mode);
  const markdown = data.markdown || data.text || "";
  const plain = data.text || data.markdown || "";

  els.resultTitle.textContent = data.filename || "Extracted document";
  els.resultChips.innerHTML = "";
  const chips = [
    data.page_count ? `${data.page_count} page${data.page_count === 1 ? "" : "s"}` : null,
    mode ? mode.name : null,
    data.credits_used != null ? `${fmt(Math.round(data.credits_used))} credits used` : null,
    `${Math.max(1, Math.round((Date.now() - state.startedAt) / 1000))}s`,
  ].filter(Boolean);
  chips.forEach((text, index) => {
    const chip = document.createElement("span");
    chip.textContent = text;
    if (index === 0) chip.className = "good";
    els.resultChips.appendChild(chip);
  });
  els.extractedNote.textContent = "Always check against the original";

  // Formatted view: one sheet of paper per source page.
  els.docFormatted.innerHTML = "";
  const pages = data.pages && data.pages.length ? data.pages : null;
  if (pages) {
    pages.forEach((page) => {
      const sheet = document.createElement("article");
      sheet.className = "sheet";
      const label = document.createElement("span");
      label.className = "sheet-num";
      label.textContent = `Page ${page.page_number}`;
      const body = document.createElement("div");
      body.className = "md";
      body.innerHTML = renderMarkdown(page.content || "_This page was blank._");
      decorate(body);
      sheet.append(label, body);
      els.docFormatted.appendChild(sheet);
    });
  } else {
    const sheet = document.createElement("article");
    sheet.className = "sheet";
    const body = document.createElement("div");
    body.className = "md";
    body.innerHTML = renderMarkdown(markdown || "_Nothing could be extracted from this file._");
    decorate(body);
    sheet.appendChild(body);
    els.docFormatted.appendChild(sheet);
  }

  els.docMarkdown.textContent = markdown || "No markdown was produced for this mode.";
  els.docPlain.textContent = plain || "No text was extracted.";

  // Original document, straight from the file the reviewer picked.
  if (state.objectUrl) URL.revokeObjectURL(state.objectUrl);
  if (state.file) {
    state.objectUrl = URL.createObjectURL(state.file);
    els.originalFrame.src = `${state.objectUrl}#view=FitH`;
    els.originalFrame.classList.remove("hidden");
    els.originalEmpty.classList.add("hidden");
  } else {
    els.originalFrame.removeAttribute("src");
    els.originalFrame.classList.add("hidden");
    els.originalEmpty.classList.remove("hidden");
  }

  setFormat("formatted");
  setLayout("split");
  clearSearch();
  els.viewer.classList.remove("hidden");
  document.body.classList.add("viewing");
  els.extractedScroll.scrollTop = 0;
  // Tables and images settle a frame later; pin the reader back to the first page.
  requestAnimationFrame(() => {
    els.extractedScroll.scrollTop = 0;
  });
}

function closeViewer() {
  els.viewer.classList.add("hidden");
  document.body.classList.remove("viewing");
  if (state.objectUrl) {
    URL.revokeObjectURL(state.objectUrl);
    state.objectUrl = null;
  }
  els.originalFrame.removeAttribute("src");
  els.fileInput.value = "";
  setFile(null);
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function setLayout(layout) {
  els.stage.dataset.layout = layout;
  [...els.layoutToggle.children].forEach((btn) =>
    btn.classList.toggle("active", btn.dataset.layout === layout)
  );
}

function setFormat(format) {
  els.docFormatted.classList.toggle("hidden", format !== "formatted");
  els.docMarkdown.classList.toggle("hidden", format !== "markdown");
  els.docPlain.classList.toggle("hidden", format !== "plain");
  [...els.formatToggle.children].forEach((btn) =>
    btn.classList.toggle("active", btn.dataset.format === format)
  );
  els.extractedScroll.scrollTop = 0;
  if (els.searchInput.value) runSearch(els.searchInput.value);
}

function visibleDoc() {
  if (!els.docFormatted.classList.contains("hidden")) return els.docFormatted;
  if (!els.docMarkdown.classList.contains("hidden")) return els.docMarkdown;
  return els.docPlain;
}

/* ------------------------------------------------------------------ search */

function clearMarks(root) {
  root.querySelectorAll("mark").forEach((mark) => {
    const parent = mark.parentNode;
    parent.replaceChild(document.createTextNode(mark.textContent), mark);
    parent.normalize();
  });
}

function clearSearch() {
  els.searchInput.value = "";
  [els.docFormatted, els.docMarkdown, els.docPlain].forEach(clearMarks);
  state.matches = [];
  state.matchIndex = 0;
  els.searchCount.textContent = "";
  els.searchPrev.disabled = true;
  els.searchNext.disabled = true;
}

function runSearch(query) {
  const root = visibleDoc();
  clearMarks(root);
  state.matches = [];
  state.matchIndex = 0;

  const needle = query.trim().toLowerCase();
  if (needle.length < 2) {
    els.searchCount.textContent = "";
    els.searchPrev.disabled = true;
    els.searchNext.disabled = true;
    return;
  }

  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
    acceptNode: (node) =>
      node.nodeValue && node.nodeValue.trim()
        ? NodeFilter.FILTER_ACCEPT
        : NodeFilter.FILTER_REJECT,
  });
  const nodes = [];
  while (walker.nextNode()) nodes.push(walker.currentNode);

  nodes.forEach((node) => {
    const text = node.nodeValue;
    const lower = text.toLowerCase();
    if (!lower.includes(needle)) return;

    const fragment = document.createDocumentFragment();
    let from = 0;
    let index = lower.indexOf(needle);
    while (index !== -1) {
      fragment.appendChild(document.createTextNode(text.slice(from, index)));
      const mark = document.createElement("mark");
      mark.textContent = text.slice(index, index + needle.length);
      fragment.appendChild(mark);
      state.matches.push(mark);
      from = index + needle.length;
      index = lower.indexOf(needle, from);
    }
    fragment.appendChild(document.createTextNode(text.slice(from)));
    node.parentNode.replaceChild(fragment, node);
  });

  const has = state.matches.length > 0;
  els.searchPrev.disabled = !has;
  els.searchNext.disabled = !has;
  if (has) gotoMatch(0);
  else els.searchCount.textContent = "no matches";
}

function gotoMatch(index) {
  if (!state.matches.length) return;
  const count = state.matches.length;
  state.matchIndex = ((index % count) + count) % count;
  state.matches.forEach((mark, i) => mark.classList.toggle("current", i === state.matchIndex));
  state.matches[state.matchIndex].scrollIntoView({ block: "center", behavior: "smooth" });
  els.searchCount.textContent = `${state.matchIndex + 1} of ${count}`;
}

/* ----------------------------------------------------------------- exports */

function saveBlob(blob, filename) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function stem(name) {
  return (name || "document").replace(/\.pdf$/i, "");
}

async function download(kind) {
  const result = state.result;
  if (!result) return;
  const name = stem(result.filename);
  const markdown = result.markdown || result.text || "";

  if (kind === "markdown") {
    saveBlob(new Blob([markdown], { type: "text/markdown" }), `${name}.md`);
  } else if (kind === "text") {
    saveBlob(new Blob([result.text || markdown], { type: "text/plain" }), `${name}.txt`);
  } else if (kind === "print") {
    setFormat("formatted");
    setLayout("extracted");
    setTimeout(() => window.print(), 120);
  } else if (kind === "word") {
    const label = els.downloadBtn.innerHTML;
    els.downloadBtn.textContent = "Preparing…";
    els.downloadBtn.disabled = true;
    try {
      const response = await fetch("/api/exports/word", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ filename: name, markdown }),
      });
      if (!response.ok) throw new Error("We couldn't build the Word file.");
      saveBlob(await response.blob(), `${name}.docx`);
    } catch (err) {
      alert(err.message);
    } finally {
      els.downloadBtn.innerHTML = label;
      els.downloadBtn.disabled = false;
    }
  }
}

/* ------------------------------------------------------------------- wiring */

els.dropzone.addEventListener("dragover", (event) => {
  event.preventDefault();
  els.dropzone.classList.add("drag");
});
els.dropzone.addEventListener("dragleave", () => els.dropzone.classList.remove("drag"));
els.dropzone.addEventListener("drop", (event) => {
  event.preventDefault();
  els.dropzone.classList.remove("drag");
  const file = event.dataTransfer.files[0];
  if (file) setFile(file);
});
els.fileInput.addEventListener("change", () => {
  if (els.fileInput.files[0]) setFile(els.fileInput.files[0]);
});
els.clearFile.addEventListener("click", () => {
  els.fileInput.value = "";
  setFile(null);
  els.fileInput.click();
});

els.runBtn.addEventListener("click", runExtraction);
els.errorRetry.addEventListener("click", () => {
  hideError();
  runExtraction();
});

els.viewerBack.addEventListener("click", closeViewer);
els.layoutToggle.addEventListener("click", (event) => {
  const btn = event.target.closest("button[data-layout]");
  if (btn) setLayout(btn.dataset.layout);
});
els.formatToggle.addEventListener("click", (event) => {
  const btn = event.target.closest("button[data-format]");
  if (btn) setFormat(btn.dataset.format);
});

let searchTimer = null;
els.searchInput.addEventListener("input", () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => runSearch(els.searchInput.value), 220);
});
els.searchInput.addEventListener("keydown", (event) => {
  if (event.key === "Enter") {
    event.preventDefault();
    gotoMatch(state.matchIndex + (event.shiftKey ? -1 : 1));
  }
});
els.searchPrev.addEventListener("click", () => gotoMatch(state.matchIndex - 1));
els.searchNext.addEventListener("click", () => gotoMatch(state.matchIndex + 1));

function setZoom(value) {
  state.zoom = Math.min(1.6, Math.max(0.8, Math.round(value * 10) / 10));
  document.documentElement.style.setProperty("--doc-zoom", String(state.zoom));
  els.zoomLevel.textContent = `${Math.round(state.zoom * 100)}%`;
}
els.zoomIn.addEventListener("click", () => setZoom(state.zoom + 0.1));
els.zoomOut.addEventListener("click", () => setZoom(state.zoom - 0.1));

els.copyBtn.addEventListener("click", async () => {
  const result = state.result;
  if (!result) return;
  try {
    await navigator.clipboard.writeText(result.text || result.markdown || "");
    els.copyBtn.textContent = "Copied";
    setTimeout(() => (els.copyBtn.textContent = "Copy text"), 1400);
  } catch {
    els.copyBtn.textContent = "Press Ctrl+C";
    setTimeout(() => (els.copyBtn.textContent = "Copy text"), 1800);
  }
});

els.downloadBtn.addEventListener("click", (event) => {
  event.stopPropagation();
  els.downloadMenu.classList.toggle("hidden");
});
els.downloadMenu.addEventListener("click", (event) => {
  const btn = event.target.closest("button[data-dl]");
  if (!btn) return;
  els.downloadMenu.classList.add("hidden");
  download(btn.dataset.dl);
});
document.addEventListener("click", () => els.downloadMenu.classList.add("hidden"));
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") els.downloadMenu.classList.add("hidden");
  if ((event.metaKey || event.ctrlKey) && event.key === "f" && document.body.classList.contains("viewing")) {
    event.preventDefault();
    els.searchInput.focus();
    els.searchInput.select();
  }
});

/* -------------------------------------------------------------------- boot */

(async function boot() {
  setZoom(1);
  try {
    const session = await api("/api/session");
    state.modes = session.modes || [];
    const recommended = state.modes.find((m) => m.recommended);
    state.modeId = recommended ? recommended.id : (state.modes[0] || {}).id;
    state.ready = Boolean(session.ready);
    els.maxMb.textContent = String(session.max_file_mb || 50);

    renderModes();
    updateRunBar();
    renderCredits(session.credits);

    if (!state.ready) {
      els.setupError.textContent =
        "The extraction service isn't configured yet. Please contact your administrator before running a document.";
      els.setupError.classList.remove("hidden");
    }
  } catch {
    els.setupError.textContent =
      "We couldn't reach the extraction service. Check that the server is running and refresh this page.";
    els.setupError.classList.remove("hidden");
  }
})();
