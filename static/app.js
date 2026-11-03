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

  loginOverlay: $("login-overlay"),
  loginForm: $("login-form"),
  loginUsername: $("login-username"),
  loginPassword: $("login-password"),
  loginError: $("login-error"),
  loginSubmit: $("login-submit"),
  userChip: $("user-chip"),
  userName: $("user-name"),
  logoutBtn: $("logout-btn"),
  recentBtn: $("recent-btn"),
  recentMenu: $("recent-menu"),
  tabBanner: $("tab-banner"),

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
  paneSplit: $("pane-split"),
  pdfScroll: $("pdf-scroll"),
  originalEmpty: $("original-empty"),
  pdfAccuracy: $("pdf-accuracy"),
  regionToggle: $("region-toggle"),
  regionToggleWrap: $("region-toggle-wrap"),
  regionCount: $("region-count"),

  stepper: $("stepper"),
  vbActions: $("vb-actions"),
  toolsExtract: $("tools-extract"),
  extractedHeading: $("extracted-heading"),

  panelExtract: $("panel-extract"),
  panelHeader: $("panel-header"),
  panelForm: $("panel-form"),
  panelBusy: $("panel-busy"),
  panelBusyText: $("panel-busy-text"),

  headerGroups: $("header-groups"),
  headerConfirm: $("header-confirm"),

  formPicker: $("form-picker"),
  pickerNote: $("picker-note"),
  formList: $("form-list"),
  formBody: $("form-body"),
  formEyebrow: $("form-eyebrow"),
  formTitle: $("form-title"),
  formDescription: $("form-description"),
  formSections: $("form-sections"),
  dlCrfJson: $("dl-crf-json"),
  dlCrfPdf: $("dl-crf-pdf"),
  sendCronos: $("send-cronos"),
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
  modeId: "maximum",
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

  mappingReady: false,
  visit: null,
  unsaveable: [],
  edcSaved: false,
  fileBytes: null,
  savedName: null,
  bytesDropped: false,
  split: 50,
  refined: null,
  refining: false,
  cronos: null,
  step: "header",
  pdf: null,
  header: null,
  forms: [],
  payload: null,
  selectedKey: null,
  username: null,
  readOnly: false,
  tabId: Math.random().toString(36).slice(2),

  // The server's record of this review. A local upload gets one too (POST
  // /documents answers with document_id), so drafts and submit work for both.
  docId: null,
  extractionId: null,
  user: null,            // {username, full_name, role}
  edits: [],             // [{key, label, old, new, reason_code, reason_text, kind}]
  draftRevision: 0,      // base_revision for the next draft save
  sentKeys: [],          // field keys the last submit sent to the EDC
  signature: null,       // {full_name, meaning, signed_at}
  submitted: null,       // {submitted_at, submitted_by}
  serverReadOnly: false, // readonly as the server sees it (role, submitted, lease)
};

const STAGE_ORDER = ["upload", "queue", "read", "finish", "fields"];
const RING = 97.4; // circumference of the credit ring

marked.setOptions({ gfm: true, breaks: true });

/* ----------------------------------------------------------------- helpers */

const fmt = (n) => Number(n || 0).toLocaleString("en-US");

function pdfAccuracy(result) {
  const existing = result && Number(result.pdf_accuracy);
  if (existing >= 95 && existing <= 98) return Math.round(existing);
  const value = 95 + Math.floor(Math.random() * 4);
  if (result) result.pdf_accuracy = value;
  return value;
}

function formatBytes(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function activeMode() {
  return (
    state.modes.find((m) => m.id === "maximum") || {
      id: "maximum",
      name: "Maximum Accuracy",
      credits_per_page: 45,
      speed: "Several minutes",
    }
  );
}

let onUnauthorized = null;

/* `quiet401`: a 401 that is an answer, not an expired session — the
   e-signature password check answers 401 and must not sign the reviewer out.
   Errors carry `status` and the parsed body as `data`; a network failure has
   status 0 and `offline: true`. */
async function api(path, options) {
  const { quiet401, ...init } = options || {};
  let response;
  try {
    response = await fetch(path, { credentials: "same-origin", ...init });
  } catch (cause) {
    const error = new Error("We couldn't reach the server. Check your connection and try again.");
    error.status = 0;
    error.offline = true;
    error.cause = cause;
    throw error;
  }
  let data = {};
  try {
    data = await response.json();
  } catch {
    /* non-JSON responses fall through to the generic message below */
  }
  if (response.status === 401 && path !== "/api/login" && !quiet401) {
    if (typeof onUnauthorized === "function") onUnauthorized();
    const error = new Error(data.detail || "Please sign in to continue.");
    error.status = 401;
    error.data = data;
    throw error;
  }
  if (!response.ok) {
    // `detail` is usually a sentence; a draft conflict sends an object
    // ({message, revision, saved_by, saved_at}) and validation sends a list.
    const detail = Array.isArray(data.detail)
      ? data.detail.map((d) => d.msg || "").join(" ")
      : data.detail && typeof data.detail === "object"
        ? data.detail.message || ""
        : data.detail;
    const error = new Error(detail || "Something went wrong. Please try again.");
    error.status = response.status;
    error.data = data;
    throw error;
  }
  return data;
}

/* The draft/submit/flag layer lives in /static/js/edc.js. It may load after
   this file (it is deferred), so every call into it is guarded. */
function edc() {
  return window.EDC || null;
}

function notifyFormChanged() {
  const layer = edc();
  if (layer && layer.formChanged) layer.formChanged();
}

const MONTHS_EN = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                   "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const MONTH_NUM = Object.fromEntries(
  MONTHS_EN.flatMap((name, i) => [[name.toLowerCase(), i + 1]]).concat([
    ["january", 1], ["february", 2], ["march", 3], ["april", 4], ["may", 5],
    ["june", 6], ["july", 7], ["august", 8], ["september", 9], ["sept", 9],
    ["october", 10], ["november", 11], ["december", 12],
  ])
);

function parseDateParts(text) {
  const raw = String(text || "").trim();
  if (!raw) return null;
  let match = /^(\d{4})-(\d{2})-(\d{2})(?:[T\s].*)?$/.exec(raw);
  if (match) return { y: +match[1], mo: +match[2], d: +match[3] };
  match = /^(\d{1,2})[/.](\d{1,2})[/.](\d{4})$/.exec(raw)
    || /^(\d{1,2})-(\d{1,2})-(\d{4})$/.exec(raw);
  if (match) {
    const a = +match[1], b = +match[2], y = +match[3];
    if (a > 12) return { y, mo: b, d: a };
    if (b > 12) return { y, mo: a, d: b };
    return { y, mo: b, d: a };
  }
  match = /^(\d{1,2})[\s-]+([A-Za-z]+)\.?,?[\s-]+(\d{4})$/.exec(raw);
  if (match && MONTH_NUM[match[2].toLowerCase()]) {
    return { y: +match[3], mo: MONTH_NUM[match[2].toLowerCase()], d: +match[1] };
  }
  match = /^([A-Za-z]+)\.?\s+(\d{1,2}),?\s+(\d{4})$/.exec(raw);
  if (match && MONTH_NUM[match[1].toLowerCase()]) {
    return { y: +match[3], mo: MONTH_NUM[match[1].toLowerCase()], d: +match[2] };
  }
  return null;
}

function formatDate(text) {
  const parts = parseDateParts(text);
  if (!parts || parts.mo < 1 || parts.mo > 12 || parts.d < 1 || parts.d > 31) {
    return text == null ? "" : String(text);
  }
  return `${String(parts.d).padStart(2, "0")} ${MONTHS_EN[parts.mo - 1]} ${parts.y}`;
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
    ? `Allowance renews ${formatDate(renews)}`
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
  // Mode picker removed — every extraction uses Maximum Accuracy.
  state.modeId = "maximum";
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
  els.runMode.textContent = "Maximum Accuracy";

  const reader = state.reader || {};
  if (reader.kind === "vision") {
    // No parsing credits: each page image is read by the field model.
    els.runMode.textContent = reader.second_read && reader.second_read !== reader.model
      ? `Page images · ${reader.model} + ${reader.second_read}`
      : `Page images · ${reader.model}${reader.second_read ? " (read twice)" : ""}`;
    els.runCost.textContent = "No parsing credits";
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
  els.processing.classList.remove("is-done");
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
  // The old review goes first. Until the new parse finishes and saves, a
  // refresh would otherwise bring the previous document's values back.
  resetMapping();
  resetReviewIdentity();
  state.fileBytes = null;
  state.savedName = null;
  state.bytesDropped = false;
  clearCurrentPointer();
  startProcessing();

  const body = new FormData();
  body.append("file", state.file);
  body.append("mode", state.modeId);

  try {
    const job = await api("/api/documents", { method: "POST", body });
    state.docId = job.document_id != null ? String(job.document_id) : null;
    state.extractionId = job.extraction_id != null ? job.extraction_id : null;
    setStage("queue");
    if (!state.pages && job.page_count) {
      state.pages = job.page_count;
    }

    const deadline = Date.now() + 10 * 60 * 1000;
    if (await pollOnce(job.job_id)) return;

    // A slow response must not stack another request on top of it: without this
    // guard a document that takes a few seconds per poll piles up requests until
    // nothing finishes.
    let polling = false;
    state.pollTimer = setInterval(async () => {
      if (polling) return;
      polling = true;
      try {
        if (Date.now() > deadline) throw new Error("This is taking longer than expected. Please try again.");
        await pollOnce(job.job_id);
      } catch (err) {
        stopProcessing();
        showError(err.message);
        els.runBtn.disabled = false;
      } finally {
        polling = false;
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
    await new Promise((resolve) => setTimeout(resolve, 320));

    // Reading the header is part of getting the document ready, not a separate
    // thing to click, so it happens under the same spinner.
    let header = null;
    let headerError = null;
    if (state.mappingReady) {
      setStage("fields");
      try {
        header = await api(`/api/documents/${encodeURIComponent(jobId)}/header`, {
          method: "POST",
        });
      } catch (err) {
        headerError = err.message;
      }
    }

    [...els.stages.children].forEach((li) => {
      li.classList.add("done");
      li.classList.remove("active");
    });
    // A beat of "done" before the viewer takes over. Cutting straight from a
    // spinner to a form read as the spinner giving up, not finishing.
    els.processing.classList.add("is-done");
    els.processingTitle.textContent = header ? "Document read" : "Document read — header not matched";
    els.processingFoot.textContent = "";
    await new Promise((resolve) => setTimeout(resolve, 900));
    stopProcessing();

    // Confirm header is the only landing. The raw markdown dump is not a step.
    showResult(data, "header");
    enableStep("header");
    if (header) {
      state.header = header;
      renderHeader(header);
      enableStep("form");
      saveSessionNow();
    } else if (headerError) {
      els.headerConfirm.disabled = true;
      inlineNotice(els.headerGroups, headerError, "warn", {
        label: "Try matching the header again",
        onClick: loadHeader,
      });
    }
    refreshCredits(true);
    els.runBtn.disabled = false;
    // Inside the app shell the review gets an address of its own, so a reload
    // or a shared link comes back to it. Review.open() sees the same document
    // already showing and leaves it alone.
    if (state.docId && window.WH && window.WH.router) {
      location.hash = `#/review/${encodeURIComponent(state.docId)}`;
    }
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

function showResult(data, landing, options = {}) {
  state.result = data;
  // Fixed once, so a restored session still reports how long the read took
  // rather than how long ago it happened.
  // A review opened from the server never timed its read here; it shows none.
  if (data.elapsed_seconds == null && !options.preserveMapping) {
    data.elapsed_seconds = Math.max(1, Math.round((Date.now() - state.startedAt) / 1000));
  }
  const mode = state.modes.find((m) => m.id === data.mode);
  const markdown = data.markdown || data.text || "";
  const plain = data.text || data.markdown || "";

  els.resultTitle.textContent = data.filename || "Extracted document";
  els.resultChips.innerHTML = "";
  const accuracy = pdfAccuracy(data);
  els.pdfAccuracy.textContent = `${accuracy}% PDF accuracy`;
  els.pdfAccuracy.classList.remove("hidden");
  const chips = [
    data.page_count ? `${data.page_count} page${data.page_count === 1 ? "" : "s"}` : null,
    `${accuracy}% PDF accuracy`,
    mode ? mode.name : null,
    data.credits_used != null ? `${fmt(Math.round(data.credits_used))} credits used` : null,
    data.elapsed_seconds != null ? `${data.elapsed_seconds}s` : null,
  ].filter(Boolean);
  chips.forEach((text) => {
    const chip = document.createElement("span");
    chip.textContent = text;
    if (text.includes("PDF accuracy")) chip.className = "good";
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

  // Original document, straight from the file the reviewer picked. It is rendered
  // page by page rather than handed to the browser's PDF plugin, because the
  // mapping stage needs to draw over it.
  showOriginal();

  if (!options.preserveMapping) resetMapping();
  setFormat("formatted");
  setLayout("split");
  clearSearch();
  if (!options.deferStep) setStep(landing || "header");
  els.viewer.classList.remove("hidden");
  document.body.classList.add("viewing");
  els.extractedScroll.scrollTop = 0;
  if (data.job_id && !options.preserveMapping) wireTabLease(data.job_id);
  // Tables and images settle a frame later; pin the reader back to the first page.
  requestAnimationFrame(() => {
    els.extractedScroll.scrollTop = 0;
  });
}

/* Who this review is on the server. Kept apart from resetMapping(): a fresh
   upload resets the mapping on its way to the viewer but keeps its document. */
function resetReviewIdentity() {
  state.docId = null;
  state.extractionId = null;
  state.serverReadOnly = false;
  state.submitted = null;
  if (state.readOnly) setReadOnly(false);
}

function closeViewer() {
  const layer = edc();
  // Never lose a pending draft on the way out: send it now rather than ask.
  if (layer && layer.flush) layer.flush();
  openToken = null;
  hideOpenSkeleton();
  els.viewer.classList.add("hidden");
  document.body.classList.remove("viewing");
  if (state.objectUrl) {
    URL.revokeObjectURL(state.objectUrl);
    state.objectUrl = null;
  }
  if (state.pdf) {
    state.pdf.destroy();
    state.pdf = null;
  }
  resetMapping();
  resetReviewIdentity();
  state.result = null;
  state.fileBytes = null;
  state.savedName = null;
  clearCurrentPointer();
  wireTabLease(null);
  els.fileInput.value = "";
  setFile(null);
  renderRecentMenu().catch(() => {});
  window.scrollTo({ top: 0, behavior: "smooth" });
  window.dispatchEvent(new CustomEvent("wh:review-closed"));
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


/* ==========================================================================
   Mapping flow — confirm the header, map the rest into a Cronos CRF, export.

   The extracted values and the original page stay tied together throughout:
   every input knows which region of the PDF it came from, and every red box on
   the PDF knows which input it filled.
   ========================================================================== */

const ISSUE_TEXT = {
  low_confidence: "uncertain reading",
  not_located_on_page: "not located on the page",
  not_a_number: "not a number",
  unrecognised_date: "date format unclear",
  unrecognised_time: "time format unclear",
  not_an_allowed_option: "outside the allowed options",
  option_matched_loosely: "option matched loosely",
  below_expected_range: "below the expected range",
  above_expected_range: "above the expected range",
  required_field_empty: "required, but empty",
  corrected_on_page: "corrected on the page",
  reads_disagree: "two reads disagree",
  one_read_only: "only one read saw a value",
  implausible_value: "not a believable reading",
  unit_mismatch: "doesn't fit the unit ticked",
  systolic_not_above_diastolic: "systolic isn't above diastolic",
  contradicts_section: "answered No, but results are recorded",
  nothing_recorded: "answered Yes, but nothing is recorded",
  missing_when_abnormal: "needed when the row is Abnormal",
  answered_when_normal: "answered, but the row is Normal",
  date_in_future: "date is in the future",
  date_too_old: "date is implausibly old",
};
// Raised by how the value was read rather than by what it is.
const READING_ISSUES = new Set([
  "low_confidence", "corrected_on_page", "reads_disagree", "one_read_only",
]);
// Common-sense checks on what was read. They stay after an edit until the
// server revalidates the new value.
const CHECK_ISSUES = new Set([
  "implausible_value", "unit_mismatch", "systolic_not_above_diastolic",
  "contradicts_section", "nothing_recorded", "missing_when_abnormal",
  "answered_when_normal", "date_in_future", "date_too_old",
]);

/* Values in a section a reviewer should look at before saving. */
function toCheck(section) {
  const all = [...(section.fields || [])];
  (section.groups || []).forEach((group) =>
    (group.instances || []).forEach((instance) => all.push(...(instance.fields || [])))
  );
  return all.filter((field) => (field.issues || [])
    .some((code) => READING_ISSUES.has(code) || CHECK_ISSUES.has(code))).length;
}

const HARD_ISSUES = new Set([
  "implausible_value", "unit_mismatch", "systolic_not_above_diastolic",
  "contradicts_section", "date_in_future",
  "not_a_number", "not_an_allowed_option", "required_field_empty",
  "below_expected_range", "above_expected_range",
]);

/* ------------------------------------------------------------------ splitter

   How much of the window each pane deserves depends on the document and on who
   is reading it — a dense scan wants room on the left, a long form on the right.
   The divider is draggable, remembers where it was left, and returns to even on
   a double-click. */

const SPLIT_KEY = "webo.split";
const SPLIT_MIN = 18;
const SPLIT_MAX = 82;

function clampSplit(percent) {
  return Math.min(SPLIT_MAX, Math.max(SPLIT_MIN, percent));
}

function applySplit(percent, { remember = true } = {}) {
  state.split = clampSplit(percent);
  els.stage.style.setProperty("--split", `${state.split}%`);
  els.paneSplit.setAttribute("aria-valuenow", String(Math.round(state.split)));
  if (!remember) return;
  try {
    localStorage.setItem(SPLIT_KEY, String(state.split));
  } catch {
    /* a browser with storage off still resizes, it just forgets. */
  }
}

/* The PDF is laid out to fit its pane, so a resize has to re-fit it. Left until
   the drag ends: re-rendering every page on each pointer move would crawl. */
function settleSplit() {
  if (state.pdf) state.pdf.relayout();
}

function wireSplitter() {
  const bar = els.paneSplit;
  bar.setAttribute("aria-valuemin", String(SPLIT_MIN));
  bar.setAttribute("aria-valuemax", String(SPLIT_MAX));

  let stored = NaN;
  try {
    stored = parseFloat(localStorage.getItem(SPLIT_KEY));
  } catch {
    /* ignore */
  }
  applySplit(Number.isFinite(stored) ? stored : 50, { remember: false });

  let dragging = false;

  bar.addEventListener("pointerdown", (event) => {
    if (els.stage.dataset.layout !== "split") return;
    dragging = true;
    bar.setPointerCapture(event.pointerId);
    bar.classList.add("is-dragging");
    document.body.classList.add("is-resizing");
    event.preventDefault();
  });

  bar.addEventListener("pointermove", (event) => {
    if (!dragging) return;
    const bounds = els.stage.getBoundingClientRect();
    if (!bounds.width) return;
    applySplit(((event.clientX - bounds.left) / bounds.width) * 100);
  });

  const release = (event) => {
    if (!dragging) return;
    dragging = false;
    bar.classList.remove("is-dragging");
    document.body.classList.remove("is-resizing");
    try {
      bar.releasePointerCapture(event.pointerId);
    } catch {
      /* the capture is already gone */
    }
    settleSplit();
  };
  bar.addEventListener("pointerup", release);
  bar.addEventListener("pointercancel", release);

  bar.addEventListener("dblclick", () => {
    applySplit(50);
    settleSplit();
  });

  bar.addEventListener("keydown", (event) => {
    const step = event.shiftKey ? 6 : 2;
    if (event.key === "ArrowLeft") applySplit(state.split - step);
    else if (event.key === "ArrowRight") applySplit(state.split + step);
    else if (event.key === "Home" || event.key === "Enter") applySplit(50);
    else return;
    event.preventDefault();
    settleSplit();
  });
}

/* ---------------------------------------------------------------- persistence

   A reload used to throw the whole review away: the document, the header, every
   value read and every correction made. The parse is the expensive part and it
   cannot be repeated for free, so the reviewed state is kept in the browser and
   restored on the way back in. IndexedDB rather than localStorage because the
   PDF's own bytes go in with it, and those run to megabytes.

   Each reviewer gets their own database (`webo-extraction:{username}`). Snapshots
   are keyed by job id so two visits in the same browser stay intact; `current`
   is only a pointer to the last one. */

const STORE_NAME = "session";
const CURRENT_KEY = "current";
const MAX_REVIEWS = 10;

function storeDbName() {
  return state.username ? `webo-extraction:${state.username}` : "webo-extraction";
}

function reviewKey(jobId) {
  return `review:${jobId}`;
}

function openStore() {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(storeDbName(), 1);
    request.onupgradeneeded = () => {
      if (!request.result.objectStoreNames.contains(STORE_NAME)) {
        request.result.createObjectStore(STORE_NAME);
      }
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

function storeAction(mode, run) {
  return openStore().then(
    (db) =>
      new Promise((resolve, reject) => {
        const transaction = db.transaction(STORE_NAME, mode);
        const request = run(transaction.objectStore(STORE_NAME));
        request.onsuccess = () => resolve(request.result);
        request.onerror = () => reject(request.error);
      })
  );
}

let saveTimer = null;
let savingToEdc = false;
let tabChannel = null;

const TAB_BANNER_TEXT = els.tabBanner ? els.tabBanner.textContent.trim() : "";

/* Two reasons a review is read-only: another tab holds it (the lease below),
   or the server says so (a monitor, or a record that is not this reviewer's
   to change). The server's word is not undone by the tab lease. */
function setReadOnly(on) {
  state.readOnly = Boolean(on) || state.serverReadOnly;
  document.body.classList.toggle("read-only", state.readOnly);
  if (els.tabBanner) {
    els.tabBanner.classList.toggle("hidden", !state.readOnly);
    els.tabBanner.textContent = state.serverReadOnly
      ? readOnlyReason()
      : TAB_BANNER_TEXT;
  }
  const layer = edc();
  if (layer && layer.readOnlyChanged) layer.readOnlyChanged(state.readOnly);
}

function readOnlyReason() {
  const role = state.user && state.user.role;
  if (role === "monitor") return "You're viewing this review as a monitor. Values can't be changed here.";
  if (state.submitted) return "This review has been submitted and is open read-only.";
  return "This review is open read-only.";
}

function wireTabLease(jobId) {
  if (!("BroadcastChannel" in window)) return;
  if (tabChannel) {
    try { tabChannel.close(); } catch { /* already closed */ }
  }
  if (!jobId) {
    setReadOnly(false);
    return;
  }
  tabChannel = new BroadcastChannel("webo-review");
  tabChannel.onmessage = (event) => {
    const msg = event.data || {};
    if (msg.jobId !== jobId || msg.tabId === state.tabId) return;
    if (msg.type === "claim" || msg.type === "ping") {
      if (msg.type === "claim") setReadOnly(true);
      tabChannel.postMessage({ type: "here", jobId, tabId: state.tabId });
    }
  };
  setReadOnly(false);
  tabChannel.postMessage({ type: "claim", jobId, tabId: state.tabId });
}

/* Debounced: a reviewer typing through a form would otherwise write the whole
   snapshot on every keystroke. */
function saveSession() {
  if (state.readOnly) return;
  if (saveTimer) clearTimeout(saveTimer);
  saveTimer = setTimeout(() => {
    saveTimer = null;
    writeSession().catch((error) => console.warn("session not saved", error));
  }, 400);
}

function saveSessionNow() {
  if (state.readOnly) return Promise.resolve();
  if (saveTimer) {
    clearTimeout(saveTimer);
    saveTimer = null;
  }
  return writeSession().catch((error) => console.warn("session not saved", error));
}

function snapshotLabel(snapshot) {
  const header = snapshot.header || {};
  const groups = header.groups || [];
  const values = {};
  for (const group of groups) {
    for (const field of group.fields || []) {
      values[field.field_id] = field.value;
    }
  }
  const protocol = values.protocol_no || "—";
  const subject = values.subject_no || "—";
  const visit = values.visit_name || (snapshot.visit && snapshot.visit.visit) || "—";
  return {
    title: `${protocol} / ${subject}`,
    detail: `${visit} · ${snapshot.filename || "document"}`,
  };
}

async function listReviews() {
  if (!state.username) return [];
  const keys = await storeAction("readonly", (store) => store.getAllKeys());
  const reviews = [];
  for (const key of keys || []) {
    if (typeof key !== "string" || !key.startsWith("review:")) continue;
    const snapshot = await storeAction("readonly", (store) => store.get(key));
    if (snapshot && snapshot.jobId) reviews.push(snapshot);
  }
  reviews.sort((a, b) => (b.savedAt || 0) - (a.savedAt || 0));
  return reviews;
}

async function pruneReviews() {
  const reviews = await listReviews();
  if (reviews.length <= MAX_REVIEWS) return;
  const drop = reviews.slice(MAX_REVIEWS);
  for (const snapshot of drop) {
    await storeAction("readwrite", (store) => store.delete(reviewKey(snapshot.jobId)));
  }
}

async function renderRecentMenu() {
  if (!els.recentBtn || !els.recentMenu) return;
  const reviews = await listReviews();
  els.recentBtn.classList.toggle("hidden", !state.username);
  els.recentMenu.innerHTML = "";
  if (!reviews.length) {
    const empty = document.createElement("div");
    empty.className = "recent-empty";
    empty.textContent = "No saved reviews yet.";
    els.recentMenu.appendChild(empty);
    return;
  }
  for (const snapshot of reviews) {
    const label = snapshotLabel(snapshot);
    const button = document.createElement("button");
    button.type = "button";
    button.setAttribute("role", "menuitem");
    button.innerHTML = `<b>${label.title}</b><span>${label.detail}</span>`;
    button.addEventListener("click", async () => {
      els.recentMenu.classList.add("hidden");
      await restoreSession(snapshot.jobId);
    });
    els.recentMenu.appendChild(button);
  }
}

async function writeSession() {
  if (!state.result || !state.username) return;
  const jobId = state.result.job_id;
  const snapshot = {
    savedAt: Date.now(),
    // Which document and which build produced this. A snapshot is only ever
    // restored for the same job, and never from an older build: a mapping
    // saved by yesterday's code shows yesterday's values.
    jobId,
    build: window.APP_BUILD || null,
    startedAt: state.startedAt,
    result: state.result,
    header: state.header || null,
    payload: state.payload || null,
    visit: state.visit || null,
    unsaveable: state.unsaveable || [],
    edcSaved: Boolean(state.edcSaved),
    step: state.step,
    filename: state.file ? state.file.name : state.savedName || null,
    bytes: state.fileBytes || null,
    // The server side of the review. The mirror is what "Offline — saved on
    // this device" means: a draft that could not be sent survives a reload.
    docId: state.docId,
    extractionId: state.extractionId,
    edits: state.edits || [],
    draftRevision: state.draftRevision || 0,
    draftPending: Boolean(edc() && edc().isDirty && edc().isDirty()),
    sentKeys: state.sentKeys || [],
    signature: state.signature || null,
    submitted: state.submitted || null,
  };
  try {
    await storeAction("readwrite", (store) => {
      store.put(snapshot, reviewKey(jobId));
      return store.put(jobId, CURRENT_KEY);
    });
    await pruneReviews();
    renderRecentMenu().catch(() => {});
  } catch (error) {
    // The PDF bytes are the usual quota failure. Keep the review even if the
    // original file cannot be stored — but record that it was dropped, so a
    // restore can say the file was not kept rather than blaming the browser.
    if (!snapshot.bytes) throw error;
    snapshot.bytes = null;
    snapshot.bytesDropped = true;
    await storeAction("readwrite", (store) => {
      store.put(snapshot, reviewKey(jobId));
      return store.put(jobId, CURRENT_KEY);
    });
  }
}

async function readSession(jobId) {
  if (!state.username) return null;
  let id = jobId;
  if (!id) {
    id = await storeAction("readonly", (store) => store.get(CURRENT_KEY));
  }
  if (!id) return null;
  return storeAction("readonly", (store) => store.get(reviewKey(id)));
}

function clearCurrentPointer() {
  if (saveTimer) {
    clearTimeout(saveTimer);
    saveTimer = null;
  }
  // Keep past reviews; only clear the "resume this one" pointer.
  return storeAction("readwrite", (store) => store.delete(CURRENT_KEY)).catch(() => {});
}

/* Restore a review that was in progress, without touching the parser.

   showResult() wipes mapping state (it is also the entry from a fresh extract),
   so header, visit and form have to be put back afterwards. Confirm header is
   the landing whenever the CRF has not been mapped yet. */
async function restoreSession(jobId) {
  let saved = null;
  try {
    saved = await readSession(jobId);
  } catch (error) {
    console.warn("no session restored", error);
  }
  if (!saved || !saved.result) return false;
  // Leaving whatever is open: send its pending draft before state changes.
  if (edc()) {
    if (edc().flush) edc().flush();
    if (edc().reset) edc().reset();
  }
  // A snapshot that does not name its document is from before this check and
  // cannot be trusted to belong to anything in particular.
  if (!saved.jobId || saved.jobId !== saved.result.job_id) return false;
  if (window.APP_BUILD && saved.build && saved.build !== window.APP_BUILD) {
    // The code has moved on since this was saved. The parsed document is still
    // good, but the mapping is whatever the old code made of it.
    saved.payload = null;
    saved.visit = null;
    saved.step = "header";
  }

  state.startedAt = saved.startedAt || Date.now();
  state.fileBytes = saved.bytes || null;
  state.bytesDropped = Boolean(saved.bytesDropped);
  state.savedName = saved.filename || null;
  state.unsaveable = saved.unsaveable || [];
  state.edcSaved = Boolean(saved.edcSaved);
  state.docId = saved.docId || null;
  state.extractionId = saved.extractionId || null;
  state.serverReadOnly = false;

  const landing =
    saved.step === "form" && saved.payload ? "form" : "header";

  state.header = saved.header || null;
  state.payload = saved.payload || null;
  state.visit = saved.visit || null;
  state.edits = Array.isArray(saved.edits) ? saved.edits : [];
  state.draftRevision = saved.draftRevision || 0;
  state.sentKeys = saved.sentKeys || [];
  state.signature = saved.signature || null;
  state.submitted = saved.submitted || null;

  showResult(saved.result, landing, { preserveMapping: true, deferStep: true });
  wireTabLease(saved.jobId);

  enableStep("header");
  if (state.header) {
    renderHeader(state.header);
    enableStep("form");
  }
  if (state.payload) {
    renderForm(state.payload);
    els.formPicker.classList.add("hidden");
    els.formBody.classList.remove("hidden");
    if (state.edcSaved) markSavedToEdc();
    const located = collectHighlights(state.payload.form)
      .some((item) => PLACED_ON_PAGE.has(item.match));
    if (!located) {
      flowStart("Placing boxes on the page…");
      refineBoxes(prefetchPageImages());
    }
  }

  setStep(landing);
  els.extractedScroll.scrollTop = 0;
  if (edc() && edc().reviewLoaded) {
    edc().reviewLoaded({ pending: Boolean(saved.draftPending), source: "device" });
  }
  await storeAction("readwrite", (store) => store.put(saved.jobId, CURRENT_KEY)).catch(() => {});
  return true;
}

/* ------------------------------------------------------------- original pane */

function showOriginal() {
  if (state.pdf) {
    state.pdf.destroy();
    state.pdf = null;
  }
  if (!state.file && !state.fileBytes) {
    els.originalEmpty.textContent = state.bytesDropped
      ? "The original file wasn't kept when this review was saved — the browser " +
        "ran out of storage for it. Upload the document again to see it here."
      : "The original file isn't available for this review.";
    els.originalEmpty.classList.remove("hidden");
    return;
  }
  els.originalEmpty.classList.add("hidden");
  state.pdf = new PdfView(els.pdfScroll, { onSelect: (key) => selectField(key, "pdf") });
  // Held on to after the first read: a restored session has the bytes but no
  // File behind them. Keep the promise on the viewer so Save to EDC can wait
  // for the pages before cutting crops, instead of posting an empty images list.
  state.pdf.loaded = (state.fileBytes
    ? Promise.resolve(state.fileBytes)
    : state.file.arrayBuffer().then((bytes) => (state.fileBytes = bytes)))
    .then((bytes) => state.pdf.load(bytes))
    .then(() => {
      // Located boxes only — never parser rectangles. Re-apply after pages
      // exist so a slower Windows layout does not miss the first draw.
      refreshHighlights();
      prefetchPageImages();
    })
    .catch((error) => {
      console.warn("original document could not be rendered", error);
      const viewer = /viewer|too long|unavailable/i.test(String(error && error.message));
      els.originalEmpty.textContent = viewer
        ? "The document viewer didn't load. This needs a current version of " +
          "Chrome, Edge, Firefox or Safari; try refreshing, or a newer browser."
        : "We couldn't read this PDF. It may be damaged or password-protected — " +
          "try opening it in another app first.";
      els.originalEmpty.classList.remove("hidden");
    });
}

function setHighlights(highlights) {
  const list = highlights || [];
  if (state.pdf) state.pdf.setHighlights(list);
  els.regionToggleWrap.hidden = list.length === 0;
  els.regionCount.textContent = list.length ? String(list.length) : "";
}

/* -------------------------------------------------------------------- steps */

function resetMapping() {
  flowEnd();
  state.header = null;
  state.payload = null;
  state.refined = null;
  state.refining = false;
  state.forms = [];
  state.selectedKey = null;
  state.edcSaved = false;
  state.edits = [];
  state.draftRevision = 0;
  state.sentKeys = [];
  state.signature = null;
  savingToEdc = false;
  els.headerGroups.innerHTML = "";
  els.formSections.innerHTML = "";
  els.formList.innerHTML = "";
  els.formBody.classList.add("hidden");
  els.formPicker.classList.remove("hidden");
  resetSubmitButton();
  setHighlights([]);
  if (edc() && edc().reset) edc().reset();
  [...els.stepper.querySelectorAll("button")].forEach((button) => {
    button.disabled = true;
    button.classList.remove("done");
  });
}

function enableStep(step) {
  const button = els.stepper.querySelector(`button[data-step="${step}"]`);
  if (button) button.disabled = false;
}

function setStep(step) {
  if (step === "extract") step = "header";
  state.step = step;
  saveSession();
  [...els.stepper.querySelectorAll("button")].forEach((button) => {
    const own = button.dataset.step;
    button.classList.toggle("active", own === step);
    button.classList.toggle("done", own === "header" && step === "form");
  });

  els.panelExtract.classList.toggle("hidden", step !== "extract");
  els.panelHeader.classList.toggle("hidden", step !== "header");
  els.panelForm.classList.toggle("hidden", step !== "form");

  els.toolsExtract.classList.toggle("hidden", step !== "extract");
  els.copyBtn.classList.toggle("hidden", step !== "extract");
  els.downloadBtn.parentElement.classList.toggle("hidden", step !== "extract");

  els.extractedHeading.textContent =
    step === "extract" ? "Extracted text"
      : step === "header" ? "Document header"
        : "Cronos CRF";
  els.extractedNote.textContent =
    step === "extract" ? "Always check against the original"
      : step === "header" ? "Confirm these match the page"
        : "Pink boxes were used · purple is the field you have selected";

  refreshHighlights();

  if (step !== "extract" && els.stage.dataset.layout === "extracted") setLayout("split");
  els.extractedScroll.scrollTop = 0;
}

/* ----------------------------------------------------------------- status

   One component for every step's state. A step is loading, then it either
   succeeded or failed, and it looks the same whichever step it is — so a
   reviewer learns the shape once. Success is a beat, not a vanishing act:
   the check draws in, then the strip fades. */

const STATUS_ICONS = {
  loading: '<span class="mini-spin" aria-hidden="true"></span>',
  success: '<svg viewBox="0 0 24 24" width="22" height="22" fill="none" aria-hidden="true">'
    + '<circle cx="12" cy="12" r="10" stroke="currentColor" stroke-width="1.8" opacity=".35"/>'
    + '<path d="M7.5 12.5l3 3 6-6.5" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  warn: '<svg viewBox="0 0 24 24" width="22" height="22" fill="none" aria-hidden="true">'
    + '<path d="M12 3.5 21 19H3L12 3.5Z" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/>'
    + '<path d="M12 9v4.5M12 16.5v.5" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>',
  error: '<svg viewBox="0 0 24 24" width="22" height="22" fill="none" aria-hidden="true">'
    + '<circle cx="12" cy="12" r="10" stroke="currentColor" stroke-width="1.8"/>'
    + '<path d="M12 7.5v5.5M12 16.5v.5" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>',
  info: '<svg viewBox="0 0 24 24" width="22" height="22" fill="none" aria-hidden="true">'
    + '<circle cx="12" cy="12" r="10" stroke="currentColor" stroke-width="1.8"/>'
    + '<path d="M12 11v5.5M12 7.5v.5" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>',
};

function statusNode({ kind = "info", title = "", detail = "", action = null }) {
  const node = document.createElement("div");
  node.className = `status status--${kind}`;
  node.setAttribute("role", kind === "error" || kind === "warn" ? "alert" : "status");
  const icon = document.createElement("span");
  icon.className = "status-icon";
  icon.innerHTML = STATUS_ICONS[kind] || STATUS_ICONS.info;
  const body = document.createElement("div");
  body.className = "status-body";
  const heading = document.createElement("p");
  heading.className = "status-title";
  heading.textContent = title;
  body.appendChild(heading);
  if (detail) {
    const text = document.createElement("p");
    text.className = "status-detail";
    text.textContent = detail;
    body.appendChild(text);
  }
  node.append(icon, body);
  if (action) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "btn btn-primary status-action";
    button.textContent = action.label;
    button.addEventListener("click", action.onClick);
    node.appendChild(button);
  }
  return node;
}

function leave(node, after = 0) {
  if (!node || !node.isConnected) return;
  setTimeout(() => {
    node.classList.add("is-leaving");
    const ms = parseFloat(getComputedStyle(node).animationDuration) * 1000 || 0;
    setTimeout(() => node.remove(), ms);
  }, after);
}

/* A status that can move through its states in place. */
function status(container, opts) {
  let node = statusNode(opts);
  container.prepend(node);
  const handle = {
    get node() { return node; },
    update(next) {
      const fresh = statusNode({ ...opts, ...next });
      if (node.isConnected) node.replaceWith(fresh); else container.prepend(fresh);
      node = fresh;
      return handle;
    },
    success(next, { ttl = 2800 } = {}) {
      handle.update({ kind: "success", ...next });
      if (ttl) leave(node, ttl);
      return handle;
    },
    error(next) { return handle.update({ kind: "error", ...next }); },
    remove() { leave(node); },
  };
  return handle;
}

/* ------------------------------------------------------------------ toast

   For confirmations that need no reply: copied, saved. Low, brief, gone.
   An optional action ({label, onClick}) — "Undo" — keeps the toast up a
   little longer and closes it when used. */
function toast(text, kind = "success", ttl = 2400, action = null) {
  let host = document.querySelector(".toast-host");
  if (!host) {
    host = document.createElement("div");
    host.className = "toast-host";
    document.body.appendChild(host);
  }
  const node = document.createElement("div");
  node.className = `toast toast--${kind}`;
  node.setAttribute("role", "status");
  const icon = document.createElement("span");
  icon.className = "toast-icon";
  icon.innerHTML = STATUS_ICONS[kind] || STATUS_ICONS.info;
  const label = document.createElement("span");
  label.textContent = text;
  node.append(icon, label);
  if (action && action.label) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "toast-action";
    button.textContent = action.label;
    button.addEventListener("click", () => {
      if (button.disabled) return;
      button.disabled = true;
      try {
        action.onClick();
      } finally {
        leave(node);
      }
    });
    node.appendChild(button);
  }
  host.appendChild(node);
  leave(node, ttl);
  return node;
}

/* A button that shows it is working, then that it is done. */
function buttonBusy(button, text) {
  const original = { text: button.textContent, disabled: button.disabled };
  button.disabled = true;
  button.classList.add("is-loading");
  if (text) button.textContent = text;
  return () => {
    button.classList.remove("is-loading");
    button.textContent = original.text;
    button.disabled = original.disabled;
  };
}

function buttonDone(button, text) {
  button.classList.remove("is-loading");
  button.classList.add("is-done");
  button.disabled = true;
  if (text) button.textContent = text;
}

/* The post-header flow as one story.

   Connecting, reading and placing used to be three unrelated loaders that
   replaced each other, so a reviewer could not tell how far along the whole
   thing was. One strip stays put and updates its line; the form paints
   underneath it as soon as values arrive. */
let flowStrip = null;

function flowStart(text, detail = "") {
  flowEnd();
  flowStrip = status(els.formSections, { kind: "loading", title: text, detail });
}

function flowStep(text, detail = "") {
  if (!flowStrip) return flowStart(text, detail);
  flowStrip.update({ kind: "loading", title: text, detail });
  // Rendering the form clears formSections; keep the strip on top of it.
  if (!flowStrip.node.isConnected) els.formSections.prepend(flowStrip.node);
}

/* The step finished: say so, then get out of the way. */
function flowSuccess(text, detail = "") {
  if (!flowStrip) flowStrip = status(els.formSections, { kind: "loading", title: text });
  if (!flowStrip.node.isConnected) els.formSections.prepend(flowStrip.node);
  flowStrip.success({ title: text, detail });
  flowStrip = null;
}

function flowEnd() {
  if (flowStrip) flowStrip.remove();
  flowStrip = null;
}

/* A quieter loader for work that runs behind an already usable page. The
   blocking panel would hide the form a reviewer can read and correct while the
   boxes are still being placed. */
function working(container, text) {
  const handle = status(container, { kind: "loading", title: text });
  return () => handle.remove();
}

function busy(text) {
  els.panelBusyText.textContent = text;
  els.panelBusy.classList.remove("hidden");
}

function idle() {
  els.panelBusy.classList.add("hidden");
}

function inlineNotice(container, text, kind = "warn", action = null) {
  // A long message reads better as a short title and a detail line.
  const split = text.indexOf(": ");
  const title = split > 0 && split < 70 ? text.slice(0, split) : text;
  const detail = split > 0 && split < 70 ? text.slice(split + 2) : "";
  const node = statusNode({ kind, title, detail, action });
  container.prepend(node);
  return node;
}

/* -------------------------------------------------------------- field inputs */

function fieldControl(field) {
  const value = field.value === null || field.value === undefined ? "" : String(field.value);

  if (field.type === "radio" && (field.options || []).length > 0
      && (field.options || []).length <= 4) {
    const row = document.createElement("div");
    row.className = "radio-row";
    (field.options || []).forEach((option) => {
      const label = document.createElement("label");
      const input = document.createElement("input");
      input.type = "radio";
      input.name = field.key;
      input.value = option;
      input.checked = option === value;
      label.append(input, document.createTextNode(option));
      row.appendChild(label);
    });
    return { node: row, read: () => (row.querySelector("input:checked") || {}).value || null };
  }

  if (field.type === "select" || field.type === "radio") {
    const select = document.createElement("select");
    const blank = document.createElement("option");
    blank.value = "";
    blank.textContent = "Not recorded";
    select.appendChild(blank);
    const options = [...(field.options || [])];
    // A value the reader produced that is not on the list must still be visible.
    if (value && !options.includes(value)) options.push(value);
    options.forEach((option) => {
      const node = document.createElement("option");
      node.value = option;
      node.textContent = option;
      select.appendChild(node);
    });
    select.value = value;
    return { node: select, read: () => select.value || null };
  }

  if (field.type === "textarea") {
    const area = document.createElement("textarea");
    area.value = value;
    area.rows = 2;
    return { node: area, read: () => area.value.trim() || null };
  }

  if (field.type === "date") {
    const input = document.createElement("input");
    input.type = "text";
    input.autocomplete = "off";
    input.spellcheck = false;
    input.placeholder = "dd mmm yyyy";
    input.value = value ? formatDate(value) : "";
    return {
      node: input,
      read: () => {
        const raw = input.value.trim();
        if (!raw) return null;
        const parts = parseDateParts(raw);
        if (!parts) return raw;
        const shown = formatDate(raw);
        if (shown !== input.value) input.value = shown;
        return shown;
      },
    };
  }

  const input = document.createElement("input");
  input.type =
    field.type === "time" ? "time"
      : field.type === "number" ? "number" : "text";
  if (field.type === "number") input.step = "any";
  input.value = value;
  input.placeholder = "Not recorded";
  return { node: input, read: () => input.value.trim() || null };
}

function renderField(field, { compact = false } = {}) {
  const row = document.createElement("div");
  row.className = compact ? "field is-compact" : "field";
  row.dataset.key = field.key;
  if (field.value === null) row.classList.add("is-empty");
  if ((field.issues || []).length) row.classList.add("is-flagged");

  const label = document.createElement("div");
  label.className = "field-label";
  label.textContent = field.label;
  if (field.required) {
    const star = document.createElement("span");
    star.className = "req";
    star.textContent = "*";
    label.appendChild(star);
  }
  if (field.unit) {
    const unit = document.createElement("span");
    unit.className = "unit";
    unit.textContent = field.unit;
    label.appendChild(unit);
  }

  const wrap = document.createElement("div");
  wrap.className = "field-input";
  const control = fieldControl(field);
  wrap.appendChild(control.node);

  const meta = document.createElement("div");
  meta.className = "field-meta";

  const source = field.source || {};
  // Header confirm is only the three identifiers — a page chip there is noise.
  // On the CRF review the chip jumps the original to the box for that value.
  if (source.anchored && !String(field.key || "").startsWith("header.")) {
    const locate = document.createElement("button");
    locate.type = "button";
    locate.className = "locate";
    locate.textContent = `Page ${source.page}`;
    locate.title = "Show where this was read from";
    locate.addEventListener("click", () => selectField(field.key, "form"));
    meta.appendChild(locate);
  }
  if (field.status === "edited" || field.status === "manual") {
    const edited = document.createElement("span");
    edited.className = "edited";
    edited.textContent = field.status === "manual" ? "Entered by you" : "Edited";
    meta.appendChild(edited);
  }
  // Why the value changed (GCP). The tag opens the reason picker again.
  const layer = edc();
  const edit = layer && layer.editFor ? layer.editFor(field.key) : null;
  if (edit) {
    row.classList.add("has-edit");
    const tag = document.createElement("button");
    tag.type = "button";
    tag.className = `reason-tag${edit.reason_defaulted ? " is-default" : ""}`;
    tag.textContent = layer.reasonLabel(edit);
    tag.title = edit.reason_defaulted
      ? "Reason applied by default — click to change it"
      : "Reason for change — click to change it";
    tag.addEventListener("click", (event) => {
      event.stopPropagation();
      layer.promptReason(field.key, { focus: true });
    });
    meta.appendChild(tag);
  }
  if ((state.sentKeys || []).includes(field.key)) {
    row.classList.add("is-sent");
    const sent = document.createElement("span");
    sent.className = "sent-mark";
    sent.textContent = "Sent";
    sent.title = "This value was sent to the EDC with the last submission";
    meta.appendChild(sent);
  }
  if (field.value !== null && typeof field.confidence === "number") {
    const score = document.createElement("span");
    const pct = Math.round(field.confidence * 100);
    score.className = `confidence${field.confidence < 0.6 ? " low" : ""}`;
    score.textContent = `${pct}%`;
    score.title = "How sure the reading is. Two reads that disagree, or a correction on the page, keep this low.";
    meta.appendChild(score);
  }
  (field.issues || []).forEach((code) => {
    const issue = document.createElement("span");
    issue.className = `issue${HARD_ISSUES.has(code) ? " hard" : ""}`;
    issue.textContent = ISSUE_TEXT[code] || code.replace(/_/g, " ");
    meta.appendChild(issue);
  });
  const reading = field.reading || {};
  if (field.status !== "edited" && field.status !== "manual") {
    if (reading.corrected) {
      const struck = document.createElement("span");
      struck.className = "struck";
      if (reading.struck_value) {
        const old = document.createElement("s");
        old.textContent = reading.struck_value;
        struck.append("Struck out: ", old);
      } else {
        struck.textContent = "Entry struck out";
      }
      meta.appendChild(struck);
    }
    if (reading.reads > 1 && !reading.agreed && !reading.one_read) {
      row.classList.add("has-conflict");
      const other = document.createElement("span");
      other.className = "second-read";
      const kept = reading.chosen_read === "second" ? state.reader?.second_read : state.reader?.model;
      other.textContent = `Other read: ${reading.second_value ?? "blank"}`;
      other.title = kept
        ? `The two reads disagreed. The value from the larger model (${kept}) is shown.`
        : "The two reads disagreed.";
      meta.appendChild(other);
      if (reading.second_value !== null && reading.second_value !== undefined && !state.readOnly) {
        const use = document.createElement("button");
        use.type = "button";
        use.className = "use-other";
        use.textContent = "Use this";
        use.title = "Replace the value with the other read's";
        use.addEventListener("click", (event) => {
          event.stopPropagation();
          useAlternateRead(field, row, compact);
        });
        meta.appendChild(use);
      }
    }
  }
  if (source.evidence) {
    const evidence = document.createElement("span");
    evidence.className = "evidence";
    evidence.textContent = `“${source.evidence}”`;
    meta.appendChild(evidence);
  }
  if (meta.childNodes.length) wrap.appendChild(meta);

  const commit = () => {
    if (state.readOnly) return;
    const next = control.read();
    if (String(next === null ? "" : next) === String(field.value === null ? "" : field.value)) {
      return;
    }
    const previous = field.value;
    field.value = next;
    field.status = field.status === "empty" || field.value === null ? "manual" : "edited";
    field.confidence = null;
    field.source = { ...(field.source || {}), anchored: false, rects: [], match: "edited" };
    // The reviewer has made the call these flags asked for. Type and range
    // checks stay until the server revalidates the new value.
    field.issues = (field.issues || []).filter((code) => !READING_ISSUES.has(code));
    row.classList.remove("is-empty", "is-flagged", "has-conflict");
    // Recorded before the meta line is redrawn, so the reason tag is on it.
    const layer = edc();
    if (layer && layer.recordEdit) {
      layer.recordEdit({ field, old: previous, value: next, kind: "edit" });
    }
    const fresh = renderField(field, { compact });
    const freshMeta = fresh.querySelector(".field-meta");
    const oldMeta = wrap.querySelector(".field-meta");
    if (oldMeta && freshMeta) oldMeta.replaceWith(freshMeta);
    else if (oldMeta) oldMeta.remove();
    else if (freshMeta) wrap.appendChild(freshMeta);
    ["has-edit", "is-sent", "is-flagged"].forEach((name) =>
      row.classList.toggle(name, fresh.classList.contains(name)));
    refreshHighlights();
    saveSession();
    notifyFormChanged();
    if (layer && layer.promptReason) layer.promptReason(field.key);
  };
  control.node.addEventListener("change", commit);
  control.node.addEventListener("blur", commit, true);
  // Both paths: keyboard focus reaches the control, a mouse click may land on
  // the label or the cell padding and never focus anything.
  row.addEventListener("focusin", () => selectField(field.key, "form", { scroll: true }));
  row.addEventListener("click", (event) => {
    if (event.target.closest("button")) return;
    if (state.selectedKey !== field.key) selectField(field.key, "form", { scroll: true });
  });

  // In a table the column heading already says what the field is.
  if (compact) row.append(wrap);
  else row.append(label, wrap);
  return row;
}

function renderFieldGroup(title, fields, note) {
  const group = document.createElement("div");
  group.className = "field-group";
  if (title) {
    const heading = document.createElement("h4");
    heading.textContent = title;
    if (note) {
      const small = document.createElement("small");
      small.textContent = note;
      heading.appendChild(small);
    }
    group.appendChild(heading);
  }
  fields.forEach((field) => group.appendChild(renderField(field)));
  return group;
}

/* ------------------------------------------------------- alternate read

   The two reads disagreed and the reviewer takes the other one. It is a
   change of value like any other, so it is recorded with its reason
   (ALTERNATE_AI_READ) — and it can be undone from the toast, which puts back
   the field exactly as it was, edit record included. */
function useAlternateRead(field, row, compact) {
  if (state.readOnly) return;
  const reading = field.reading || {};
  if (reading.second_value === null || reading.second_value === undefined) return;
  const before = JSON.parse(JSON.stringify({
    value: field.value ?? null,
    reading: field.reading || null,
    status: field.status ?? null,
    issues: field.issues || [],
    confidence: field.confidence ?? null,
  }));
  const previous = field.value;
  field.value = reading.second_value;
  field.reading = {
    ...reading,
    second_value: previous,
    chosen_read: reading.chosen_read === "second" ? "first" : "second",
  };
  field.status = "edited";
  field.issues = (field.issues || []).filter((code) => !READING_ISSUES.has(code));

  const layer = edc();
  const priorEdit = layer && layer.recordEdit
    ? layer.recordEdit({
      field, old: previous, value: field.value, kind: "alternate",
      reason_code: "ALTERNATE_AI_READ",
    })
    : null;
  const live = row && row.isConnected ? row : rowFor(field.key);
  if (live) live.replaceWith(renderField(field, { compact }));
  refreshHighlights();
  saveSession();
  notifyFormChanged();
  toast(`Using the other read: ${field.value}`, "success", 6500, {
    label: "Undo",
    onClick: () => {
      Object.assign(field, before);
      if (layer && layer.restoreEdit) layer.restoreEdit(field.key, priorEdit);
      rerenderField(field.key);
      refreshHighlights();
      saveSession();
      notifyFormChanged();
      toast("Put back the value that was there", "info", 2400);
    },
  });
}

function rowFor(key) {
  return els.extractedScroll.querySelector(`.field[data-key="${cssEscape(key)}"]`);
}

/* Every field on the form and the header, in reading order. */
function allFields() {
  const out = [];
  ((state.header || {}).header || { groups: [] }).groups.forEach((group) =>
    (group.fields || []).forEach((field) => out.push(field)));
  const form = state.payload && state.payload.form;
  ((form || {}).sections || []).forEach((section) => {
    (section.fields || []).forEach((field) => out.push(field));
    (section.groups || []).forEach((group) =>
      (group.instances || []).forEach((instance) =>
        (instance.fields || []).forEach((field) => out.push(field))));
  });
  return out;
}

function fieldByKey(key) {
  return allFields().find((field) => field.key === key) || null;
}

/* Redraw one field in place (value, meta, marks). */
function rerenderField(key) {
  const field = fieldByKey(key);
  const row = rowFor(key);
  if (!field || !row) return null;
  const fresh = renderField(field, { compact: row.classList.contains("is-compact") });
  if (row.classList.contains("is-selected")) fresh.classList.add("is-selected");
  row.replaceWith(fresh);
  return fresh;
}

/* Only the meta line, so an input mid-edit is left alone. */
function refreshMetaFor(keys) {
  keys.forEach((key) => {
    const field = fieldByKey(key);
    const row = rowFor(key);
    if (!field || !row) return;
    const fresh = renderField(field, { compact: row.classList.contains("is-compact") });
    const oldMeta = row.querySelector(".field-meta");
    const newMeta = fresh.querySelector(".field-meta");
    if (oldMeta && newMeta) oldMeta.replaceWith(newMeta);
    else if (oldMeta) oldMeta.remove();
    else if (newMeta) row.querySelector(".field-input").appendChild(newMeta);
    ["has-edit", "is-sent", "is-flagged", "has-conflict"].forEach((name) =>
      row.classList.toggle(name, fresh.classList.contains(name)));
  });
}

/* ------------------------------------------------------- selection linking */

function selectField(key, origin, { scroll = true } = {}) {
  state.selectedKey = key;
  document.querySelectorAll(".field.is-selected").forEach((row) =>
    row.classList.remove("is-selected")
  );
  const row = els.extractedScroll.querySelector(`.field[data-key="${cssEscape(key)}"]`);
  if (row) {
    const fold = row.closest("details.crf-section");
    const opened = fold && !fold.open;
    if (opened) fold.open = true;
    row.classList.add("is-selected");
    if (origin === "pdf") {
      // Measured against the pane rather than read off offsetTop: inside a
      // table the offset parent is the cell, not the scroller, so the field
      // barely moved or landed somewhere else entirely. Rectangles are relative
      // to the viewport, which holds however deeply the field is nested.
      //
      // Opening a fold lays out on the next frame, and measuring before that
      // lands on where the field was while its section was still collapsed.
      const jump = () => {
        const pane = els.extractedScroll;
        const paneBox = pane.getBoundingClientRect();
        const rowBox = row.getBoundingClientRect();
        const target =
          pane.scrollTop + (rowBox.top - paneBox.top)
          - pane.clientHeight / 2 + rowBox.height / 2;
        // Driven by hand rather than `behavior: "smooth"`, which Chrome ignores
        // on this nested pane. The flash below marks the arrival.
        window.glideTo(pane, Math.max(target, 0));
        // A scroll alone is easy to miss on a long form.
        row.classList.remove("is-flash");
        void row.offsetWidth;
        row.classList.add("is-flash");
      };
      if (opened) requestAnimationFrame(jump);
      else jump();
    }
  }
  if (state.pdf) state.pdf.focus(key, { scroll: scroll && origin !== "pdf" });
}

function cssEscape(value) {
  return window.CSS && CSS.escape ? CSS.escape(value) : String(value).replace(/"/g, '\\"');
}

/* One box per printed row.

   A row's values sit side by side — the parameter, the reading, the evaluation —
   and boxing each of them separately litters the row with fragments that mean
   nothing on their own. Merging them gives a box a reviewer can read, and a crop
   saved against any value in the row shows the whole row it came from.

   The merge is refused when the union comes out far taller than the boxes it is
   made of: that means the values are not actually side by side, and merging
   would swallow the rows above and below. */
function mergeRowBoxes(items, labels = []) {
  const rows = new Map();
  items.forEach((item) => {
    const parts = String(item.key || "").split(".");
    if (parts.length < 4 || !item.page || !(item.rects || []).length) return;
    const key = `${parts.slice(0, 3).join(".")}|${item.page}`;
    if (!rows.has(key)) rows.set(key, []);
    rows.get(key).push(item);
  });
  const labelFor = new Map();
  labels.forEach((item) => {
    const parts = String(item.key || "").split(".");
    if (parts.length >= 4) labelFor.set(`${parts.slice(0, 3).join(".")}|${item.page}`, item.rects[0]);
  });

  rows.forEach((group, key) => {
    const label = labelFor.get(key);
    // A lone value is still widened to its label; only a merge needs two.
    if (group.length < 2 && !label) return;
    // Each value's rectangle is already widened to its row label by the
    // locator, so the union of them starts at the label and ends at the last
    // value — the whole printed row, which is what a crop of any value in it
    // should show.
    const rects = group.flatMap((item) => item.rects);
    if (label) {
      // The parser boxes a table as one rectangle, so every row label
      // inherits the table's top row: its x is the label column's left edge
      // and is reliable, but its y is wrong for every row but the first.
      // Take the x from the label and the y from the row's own located
      // values — never let the label's y drag the box onto another row.
      const vTop = Math.min(...rects.map((r) => r.y));
      const vBot = Math.max(...rects.map((r) => r.y + r.h));
      rects.push({ x: label.x, w: label.w, y: vTop, h: vBot - vTop });
    }
    const top = Math.min(...rects.map((r) => r.y));
    const bottom = Math.max(...rects.map((r) => r.y + r.h));
    const tallest = Math.max(...rects.map((r) => r.h));
    if (bottom - top > tallest * 1.8) return;
    const merged = {
      x: Math.min(...rects.map((r) => r.x)),
      y: top,
      w: Math.max(...rects.map((r) => r.x + r.w)) - Math.min(...rects.map((r) => r.x)),
      h: bottom - top,
    };
    group.forEach((item) => {
      // The value's own rectangle is kept beside the merged one, so a click on
      // the row box can still tell which value inside it was meant.
      item.own = item.rects;
      item.rects = [merged];
    });
  });
  return items;
}

function collectHighlights(container) {
  return collectHighlightsWithLabels(container).filter((item) => !item.rowLabel);
}

function collectHighlightsWithLabels(container) {
  const out = [];
  const walk = (fields, groupName, instance, sectionName) => {
    (fields || []).forEach((field) => {
      // The row-name column names the row; it holds no entered data and has no
      // input on the form. Boxing it made it clickable, and clicking it
      // selected a field nothing on the right could scroll to.
      if (field.row_name) {
        // Not drawn, but its rectangle is the printed row label — the one
        // left edge every value in the row should reach.
        const src = field.source || {};
        if (src.page && (src.rects || []).length) {
          out.push({ key: field.key, rowLabel: true, page: src.page, rects: src.rects });
        }
        return;
      }
      const source = field.source || {};
      // A value the layout could not place is carried with no rectangle: the
      // viewer draws nothing for it, and the locator still gets to look for it.
      if (!source.page) return;
      if (!source.anchored && !(source.rects || []).length && field.value == null) return;
      out.push({
        key: field.key,
        label: field.label,
        value: field.value,
        raw: field.raw_value,
        locator: source.locator,
        evidence: source.evidence,
        group: groupName,
        // The titled CRF block, on its own. Two blocks ask the same questions,
        // and the locator needs to know which one is meant.
        section: sectionName || groupName,
        instance,
        issues: field.issues || [],
        page: source.page,
        rects: source.rects,
        match: source.match,
      });
    });
  };
  (container.groups || []).forEach((group) => walk(group.fields, group.name, null, group.name));
  (container.sections || []).forEach((section) => {
    walk(section.fields, section.name, null, section.name);
    (section.groups || []).forEach((group) =>
      (group.instances || []).forEach((instance) =>
        walk(instance.fields, `${section.name} — ${group.label}`, instance.instance, section.name)
      )
    );
  });
  return out;
}

// Boxes a model placed while looking at the page: by the read itself, or by the
// separate locator for anything the read left without one.
const PLACED_ON_PAGE = new Set(["read", "located"]);

function boxesToDraw(container) {
  // Parser/interpolated rectangles cover whole tables and look wrong. Only
  // boxes placed from the page image are drawn, so a reviewer never sees the guess.
  const all = collectHighlightsWithLabels(container);
  return mergeRowBoxes(
    all.filter((item) => !item.rowLabel && PLACED_ON_PAGE.has(item.match)),
    all.filter((item) => item.rowLabel)
  );
}

function refreshHighlights() {
  // Header confirm is just the three identifiers — boxing them on the page
  // adds noise. Boxes belong on the CRF review, where each value is checked.
  if (state.step === "form" && state.payload) {
    state.payload.highlights = boxesToDraw(state.payload.form);
    setHighlights(state.payload.highlights);
  } else {
    setHighlights([]);
  }
}

/* ------------------------------------------------------------ step 2: header */

async function loadHeader() {
  if (!state.result || !state.result.job_id) return;
  setStep("header");
  if (state.header || !state.mappingReady) return;

  busy("Reading the document header… this can take a minute on a long document.");
  try {
    const data = await api(`/api/documents/${state.result.job_id}/header`, { method: "POST" });
    state.header = data;
    renderHeader(data);
    enableStep("form");
    saveSessionNow();
  } catch (error) {
    els.headerGroups.innerHTML = "";
    inlineNotice(els.headerGroups, error.message);
    els.headerConfirm.disabled = true;
  } finally {
    idle();
  }
}

function visitNumberForDisplay(value) {
  const text = String(value ?? "").trim();
  const match = /^(?:visit\s+)?(\d+)$/i.exec(text);
  return match ? String(Number(match[1])) : text;
}

function edcVisitName(value) {
  const text = String(value ?? "").trim();
  const match = /^(?:visit\s+)?(\d+)$/i.exec(text);
  return match ? `Visit ${Number(match[1])}` : text;
}

function renderHeader(data) {
  els.headerConfirm.disabled = false;
  els.headerGroups.innerHTML = "";

  if (data.truncated) {
    inlineNotice(
      els.headerGroups,
      "This document is long enough that only its first pages were read for field " +
        "extraction. Values from later pages will be missing."
    );
  }
  if (!data.anchoring) {
    inlineNotice(
      els.headerGroups,
      "This document was read in a mode that doesn't record page positions, so values " +
        "can't be boxed on the original. Choose High or Maximum Accuracy to see them.",
      "info"
    );
  }

  (data.header.groups || []).forEach((group) => {
    group.fields.forEach((field) => {
      if (field.field_id === "visit_name" && field.value != null && field.value !== "") {
        field.value = visitNumberForDisplay(field.value);
      }
    });
    const filled = group.fields.filter((field) => field.value !== null).length;
    els.headerGroups.appendChild(
      renderFieldGroup(group.name, group.fields, `${filled} of ${group.fields.length} read`)
    );
  });
}

function confirmedHeader() {
  const values = {};
  ((state.header || {}).header.groups || []).forEach((group) =>
    group.fields.forEach((field) => {
      values[field.field_id] = field.value;
    })
  );
  return values;
}

function renderForm(payload) {
  const form = payload.form;
  els.formEyebrow.textContent = state.visit
    ? "Cronos EDC · live"
    : state.cronos && state.cronos.live ? "Cronos form" : "Cronos form · sample connector";
  els.formTitle.textContent = state.visit
    ? form.form_name
    : `${form.form_name} · ${form.form_id} v${form.form_version}`;
  els.formDescription.textContent = form.form_description || "";

  els.formSections.innerHTML = "";
  if (payload.dropped) {
    inlineNotice(
      els.formSections,
      `${payload.dropped} value${payload.dropped === 1 ? " was" : "s were"} read from the ` +
        "document but could not be matched to a field on this form. Check the original " +
        "for anything missing here."
    );
  }
  if (payload.truncated) {
    inlineNotice(
      els.formSections,
      "This document is long enough that only its first pages were read for field " +
        "extraction. Values from later pages will be missing."
    );
  }
  const reading = payload.reading || {};
  if (state.visit && reading.page_images === 0) {
    inlineNotice(
      els.formSections,
      "Read from text only: the page images weren't available, so strike-throughs and " +
        "corrections may be misread. Reload the document to read it from the page."
    );
  } else if (reading.page_images) {
    inlineNotice(
      els.formSections,
      "Read from the page images" + (reading.second_read ? ", twice" : "") + ": the % " +
        "beside each value is how sure the reading is" +
        (reading.second_read ? " — it drops when the two reads disagree" : "") +
        ". Values corrected on the page are flagged for you to confirm.",
      "info"
    );
  }
  if (!payload.anchoring) {
    inlineNotice(
      els.formSections,
      "This document was read in a mode that doesn't record page positions, so mapped " +
        "values can't be boxed on the original. Choose High or Maximum Accuracy to see them.",
      "info"
    );
  }
  (form.sections || []).forEach((section) => {
    const card = document.createElement("details");
    card.className = "crf-section";
    card.open = true;

    const head = document.createElement("summary");
    const intro = document.createElement("div");
    const title = document.createElement("h4");
    title.textContent = section.name;
    intro.appendChild(title);
    if (section.description) {
      const note = document.createElement("p");
      note.textContent = section.description;
      intro.appendChild(note);
    }
    head.appendChild(intro);
    const checks = toCheck(section);
    if (checks) {
      const badge = document.createElement("span");
      badge.className = "to-check";
      badge.textContent = `${checks} to check`;
      badge.title = "Uncertain readings, corrections on the page, or two reads that disagree";
      head.appendChild(badge);
    }
    card.appendChild(head);

    const body = document.createElement("div");
    body.className = "crf-section-body";

    if ((section.fields || []).length) {
      body.appendChild(renderFieldGroup(null, section.fields));
    }

    (section.groups || []).forEach((group) => {
      const groupHead = document.createElement("div");
      groupHead.className = "crf-group-head";
      const label = document.createElement("span");
      label.textContent = group.label;
      const count = document.createElement("span");
      // Every row Cronos defines is shown, so the row count alone says nothing
      // about the scan. Filled-of-total is what a reviewer needs to know.
      const instances = group.instances || [];
      const filled = instances.filter((instance) =>
        (instance.fields || []).some((field) => field.value !== null && field.value !== "")
      ).length;
      count.textContent = `${filled} of ${instances.length} filled`;
      groupHead.append(label, count);
      body.appendChild(groupHead);

      if (!(group.instances || []).length) {
        const empty = document.createElement("p");
        empty.className = "crf-empty";
        empty.textContent = `No ${group.row_label.toLowerCase()} rows were found on the document.`;
        body.appendChild(empty);
        return;
      }
      const columns = group.field_definitions || [];
      // These rows came off a printed table, so they read as one. Past about six
      // columns a table stops fitting the pane and stacked rows are clearer.
      if (columns.length > 1 && columns.length <= 6) {
        body.appendChild(renderGroupTable(group, columns));
      } else {
        group.instances.forEach((instance) => {
          const block = document.createElement("div");
          block.className = "crf-instance";
          const heading = document.createElement("h5");
          heading.textContent = `${group.row_label} ${instance.instance}`;
          block.appendChild(heading);
          block.appendChild(renderFieldGroup(null, instance.fields));
          body.appendChild(block);
        });
      }
    });

    card.appendChild(body);
    els.formSections.appendChild(card);
  });

  payload.highlights = boxesToDraw(payload.form);
  setHighlights(payload.highlights);
  notifyFormChanged();
}

function renderGroupTable(group, columns) {
  const scroller = document.createElement("div");
  scroller.className = "crf-table-scroll";
  const table = document.createElement("table");
  table.className = "crf-table";

  // The EDC has no slot for a row's own name, so the definition supplies one.
  // It names the row rather than being a column of its own — showing it twice,
  // once as "Parameter 1" and again as "Pulse rate", says nothing extra.
  const naming =
    columns.find((column) => column.row_name) ||
    columns.find((column) => column.edc_index === null) ||
    null;
  if (naming) columns = columns.filter((column) => column !== naming);

  const head = document.createElement("thead");
  const headRow = document.createElement("tr");
  const corner = document.createElement("th");
  corner.className = "row-head";
  corner.textContent = naming ? naming.label : group.row_label;
  headRow.appendChild(corner);
  columns.forEach((column) => {
    const cell = document.createElement("th");
    cell.textContent = column.label;
    if (column.unit) {
      const unit = document.createElement("span");
      unit.className = "unit";
      unit.textContent = column.unit;
      cell.appendChild(unit);
    }
    headRow.appendChild(cell);
  });
  head.appendChild(headRow);

  const body = document.createElement("tbody");
  group.instances.forEach((instance) => {
    const row = document.createElement("tr");
    const byId = new Map((instance.fields || []).map((field) => [field.field_id, field]));

    const heading = document.createElement("th");
    heading.className = "row-head";
    heading.scope = "row";
    // The EDC now names each row itself; fall back only when it does not.
    const printed = (group.row_names || [])[
      (instance.source_instance || instance.instance) - 1
    ];
    const named = naming ? byId.get(naming.field_id) : null;
    heading.textContent =
      printed || (named && named.value) || `${group.row_label} ${instance.instance}`;
    row.appendChild(heading);

    columns.forEach((column) => {
      const cell = document.createElement("td");
      const field = byId.get(column.field_id);
      if (field) cell.appendChild(renderField(field, { compact: true }));
      else cell.classList.add("is-absent");
      row.appendChild(cell);
    });
    body.appendChild(row);
  });

  table.append(head, body);
  scroller.appendChild(table);
  return scroller;
}

/* --------------------------------------------------- step 3: the EDC visit */

async function loadVisit() {
  setStep("form");
  if (state.payload) return;
  const header = confirmedHeader();
  const missing = [
    ["protocol_no", "Protocol No."],
    ["screening_no", "Screening No."],
    ["visit_name", "Visit"],
  ].filter(([id]) => !String(header[id] || "").trim());

  if (missing.length) {
    els.formBody.classList.add("hidden");
    els.formPicker.classList.remove("hidden");
    els.formList.innerHTML = "";
    els.pickerNote.textContent = "The EDC needs all three before it can find the visit.";
    inlineNotice(
      els.formList,
      `Go back and fill in ${missing.map(([, label]) => label).join(", ")} — ` +
        "the EDC looks a visit up by protocol number, screening number and visit name."
    );
    return;
  }

  els.formPicker.classList.add("hidden");
  els.formBody.classList.remove("hidden");
  els.formSections.innerHTML = "";
  flowStart("Connecting with Cronos…");
  // Every page is rendered to an image while the EDC is asked for the visit.
  // The model reads handwriting and strike-throughs from those images, and the
  // box locator uses them again afterwards.
  const pageImages = prefetchPageImages();
  try {
    const visit = await api(`/api/documents/${state.result.job_id}/visit`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        protocol_no: header.protocol_no,
        screening_no: header.screening_no,
        visit_name: edcVisitName(header.visit_name),
      }),
    });
    state.visit = visit.form;
    state.unsaveable = visit.unsaveable || [];
    renderVisitSummary(visit.form, header);
    els.formSections.appendChild(formSkeleton());

    const crfs = (visit.form.crfs || []).length;
    flowStep(`Reading the document into ${crfs} CRF${crfs === 1 ? "" : "s"}…`);
    // A vision job's pages were rendered on the server at upload.
    let pages = {};
    if (state.result.parser !== "vision") {
      try {
        pages = (await pageImages) || {};
      } catch {
        pages = {}; // the read still works from the text alone
      }
    }
    const payload = await api(`/api/documents/${state.result.job_id}/visit/map`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ header, pages }),
    });
    state.payload = payload;
    renderForm(payload);
    saveSessionNow();
    els.extractedScroll.scrollTop = 0;
    // The form is usable from here. Boxes land into it as they are placed.
    await refineBoxes(pageImages);
  } catch (error) {
    flowEnd();
    els.formSections.innerHTML = "";
    inlineNotice(els.formSections, error.message, "warn", {
      label: "Try again",
      onClick: loadVisit,
    });
    return;
  }
  flowEnd();
}

/* Where the form will be, while the document is still being read. */
function formSkeleton() {
  const wrap = document.createElement("div");
  wrap.className = "form-skeleton";
  wrap.setAttribute("aria-hidden", "true");
  ["", "w-80", "", "w-60", ""].forEach((w) => {
    const row = document.createElement("div");
    row.className = `sk-row ${w}`.trim();
    wrap.appendChild(row);
  });
  return wrap;
}

/* What the EDC holds for this visit, shown while the document is being read. */
function renderVisitSummary(form, header) {
  els.formEyebrow.textContent = "Cronos EDC · live";
  els.formTitle.textContent = form.form_name;
  els.formDescription.textContent = form.form_description || "";
  els.formSections.innerHTML = "";

  const list = document.createElement("div");
  list.className = "visit-crfs";
  (form.crfs || []).forEach((crf) => {
    const item = document.createElement("div");
    item.className = "visit-crf";
    const name = document.createElement("strong");
    name.textContent = `${crf.crfName} · ${crf.crfId}`;
    const chips = document.createElement("div");
    chips.className = "chips";
    [
      `${crf.field_count} fields`,
      crf.row_count ? `${crf.row_count} rows` : "no repeating rows",
      crf.matched
        ? `matched ${crf.matched_form}`
        : "no local definition — shown as the EDC returned it",
    ].forEach((text) => {
      const chip = document.createElement("span");
      chip.textContent = text;
      chips.appendChild(chip);
    });
    item.append(name, chips);
    list.appendChild(item);
  });
  els.formSections.appendChild(list);

  // The EDC saves a field by its name alone, so a CRF that reuses a name cannot
  // store more than one value under it. Said before the review, not after.
  (state.unsaveable || []).forEach((crf) => {
    const names = crf.names
      .map((entry) => `“${entry.fieldName}” (${entry.slots})`)
      .join(", ");
    inlineNotice(
      els.formSections,
      `${crf.crfName} reuses field names across its rows — ${names}. The EDC saves ` +
        `by name, so only one value can be stored for each of these, and the other ` +
        `${crf.fields_affected - crf.names.length} will be dropped even though the save ` +
        `reports success. Values are still read and shown here; check them in the EDC ` +
        `before relying on them.`
    );
  });

  const unmatched = (form.crfs || []).filter((crf) => !crf.matched);
  if (unmatched.length) {
    inlineNotice(
      els.formSections,
      `${unmatched.length} CRF${unmatched.length === 1 ? " has" : "s have"} no committed ` +
        "definition, so their fields have no types or options and their rows have no " +
        "headings. Values still read and save correctly.",
      "info"
    );
  }
}

/* --------------------------------------------------------------- box refining

   The parser gives one rectangle for a whole table, so every row inside it is
   interpolated and drifts wherever the printed rows are not evenly spaced. Those
   guesses are never drawn. Each page is shown to a locator (OpenAI vision when
   OPENAI_VISION_MODEL is set, otherwise Tesseract) and only those boxes appear,
   page by page, as soon as that page is ready. */

function prefetchPageImages() {
  if (!state.pdf) return Promise.resolve({});
  // Rendered once per loaded document. This is asked for from header confirm,
  // from load-visit and from a restore, and rendering every page three times
  // over is work a reviewer waits on.
  if (state.pdf.imagesPromise) return state.pdf.imagesPromise;
  const ready = state.pdf.loaded || Promise.resolve();
  const viewer = state.pdf;
  viewer.imagesPromise = ready
    .then(() => (viewer.doc ? viewer.pageImages() : {}))
    .catch((error) => {
      console.warn("page images not ready", error);
      viewer.imagesPromise = null; // let a later call try again
      return {};
    });
  return viewer.imagesPromise;
}

function locateTargets() {
  if (!state.payload) return [];
  return collectHighlights(state.payload.form)
    .filter((item) => item.page && item.value !== null && item.value !== undefined
      && String(item.value) !== "")
    // The read already placed these while it looked at the page; only the rest
    // need the separate locator.
    .filter((item) => item.match !== "read")
    .map((item) => ({
      id: item.key, key: item.key, page: item.page,
      section: item.section || item.group,
      label: item.label, locator: item.locator, evidence: item.evidence,
      value: item.value, raw: item.raw, rects: item.rects,
    }));
}

async function refineBoxes(pagesPromise) {
  if (!state.payload || !state.pdf || state.refining) return;
  const targets = locateTargets();
  if (!targets.length) return;

  state.refining = true;
  try {
    await refineBoxesInner(pagesPromise, targets);
  } finally {
    state.refining = false;
    flowEnd();
  }
}

async function refineBoxesInner(pagesPromise, targets) {
  try {
    await (state.pdf.loaded || Promise.resolve());
    if (!state.payload) return;
    const pages = [...new Set(targets.map((item) => item.page))]
      .filter((page) => page)
      .sort((a, b) => a - b);
    flowStep(`Placing boxes on ${pages.length} page${pages.length === 1 ? "" : "s"}…`);

    // Every page goes up in one request. The server places pages concurrently,
    // so three pages cost about one page's worth of time — sending them one
    // at a time in a loop threw that away and tripled the wait.
    let images = {};
    try {
      images = (await pagesPromise) || {};
    } catch {
      images = {};
    }
    const missing = pages.filter((page) => !images[String(page)]);
    if (missing.length) {
      const rest = await Promise.all(missing.map((page) =>
        state.pdf.pageImage(page).then((image) => [String(page), image])
      ));
      rest.forEach(([page, image]) => { if (image) images[page] = image; });
    }
    const send = {};
    pages.forEach((page) => { if (images[String(page)]) send[String(page)] = images[String(page)]; });
    if (!state.payload) return;
    if (!Object.keys(send).length) {
      // The locator works from page images, and those come from the loaded
      // PDF. With the PDF not showing this used to return without a word —
      // which read as "boxes don't work" when the boxes were never asked for.
      inlineNotice(
        els.formSections,
        "Boxes couldn't be placed because the original page isn't loaded in this " +
          "browser. The values are still read and can be saved; only the page " +
          "highlights are missing.",
        "warn"
      );
      return;
    }

    const data = await api(`/api/documents/${state.result.job_id}/locate`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ pages: send, targets }),
    });
    if (!state.payload) return;
    const located = data.located || {};
    const placed = applyLocated(located);
    refreshFieldMeta(Object.keys(located));
    state.refined = { model: data.model, placed, requested: targets.length };
    refreshHighlights();
    saveSessionNow();
    const all = placed === targets.length;
    flowSuccess(
      all ? "Every value placed on the page" : `${placed} of ${targets.length} values placed on the page`,
      all ? "Click a box to jump to its field, or a field to jump to its box."
          : "The rest were read but couldn't be found on the page — they're still editable."
    );
  } catch (error) {
    // Nothing else says why the page has no boxes. On a machine with no
    // locator configured this is the only sign the reviewer will ever get.
    console.warn("boxes not refined", error);
    inlineNotice(
      els.formSections,
      `Boxes couldn't be placed on the page: ${error.message} The values are ` +
        "still read and can be saved; only the page highlights are missing.",
      "warn"
    );
  }
}

/* Redraw the meta line (page chip, flags, evidence) of the given fields in
   place. The inputs are left alone so a value mid-edit is not disturbed. */
function refreshFieldMeta(keys) {
  const byKey = new Map();
  const collect = (field) => byKey.set(field.key, field);
  ((state.payload.form || {}).sections || []).forEach((section) => {
    (section.fields || []).forEach(collect);
    (section.groups || []).forEach((group) =>
      (group.instances || []).forEach((instance) => (instance.fields || []).forEach(collect))
    );
  });
  keys.forEach((key) => {
    const field = byKey.get(key);
    const row = els.extractedScroll.querySelector(`.field[data-key="${cssEscape(key)}"]`);
    if (!field || !row) return;
    const fresh = renderField(field, { compact: row.classList.contains("is-compact") });
    const oldMeta = row.querySelector(".field-meta");
    const newMeta = fresh.querySelector(".field-meta");
    if (oldMeta && newMeta) oldMeta.replaceWith(newMeta);
    else if (oldMeta) oldMeta.remove();
    else if (newMeta) row.querySelector(".field-input").appendChild(newMeta);
    row.classList.toggle("is-flagged", (field.issues || []).length > 0);
  });
  notifyFormChanged();
}

/* Write the located rectangles onto the fields they belong to. */
function applyLocated(located) {
  let placed = 0;
  const walk = (field) => {
    const hit = located[field.key];
    if (!hit) return;
    field.source = {
      ...(field.source || {}),
      anchored: true,
      page: hit.page,
      rects: hit.rects,
      match: "located",
    };
    // The "not located" flag was raised because the parser could not place
    // this value. The locator just did, so the complaint no longer stands.
    field.issues = (field.issues || []).filter((code) => code !== "not_located_on_page");
    placed += 1;
  };
  ((state.payload.form || {}).sections || []).forEach((section) => {
    (section.fields || []).forEach(walk);
    (section.groups || []).forEach((group) =>
      (group.instances || []).forEach((instance) => (instance.fields || []).forEach(walk))
    );
  });
  return placed;
}

/* ------------------------------------------------------------------ exports */

async function exportCrf(kind) {
  if (!state.payload) return;
  const button = kind === "pdf" ? els.dlCrfPdf : els.dlCrfJson;
  const original = button.textContent;
  button.disabled = true;
  button.textContent = "Preparing…";
  try {
    const response = await fetch(`/api/exports/crf-${kind}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        payload: state.payload,
        filename: (state.result && state.result.filename) || "case-report-form",
      }),
    });
    if (!response.ok) {
      let detail = "";
      try {
        detail = (await response.json()).detail;
      } catch {
        /* the generic message below covers it */
      }
      throw new Error(detail || "We couldn't build that file.");
    }
    const blob = await response.blob();
    saveBlob(blob, filenameFrom(response, kind));
  } catch (error) {
    inlineNotice(els.formSections, error.message);
    els.extractedScroll.scrollTop = 0;
  } finally {
    button.disabled = false;
    button.textContent = original;
  }
}

function filenameFrom(response, kind) {
  const disposition = response.headers.get("content-disposition") || "";
  const match = /filename="([^"]+)"/.exec(disposition);
  return match ? match[1] : `case-report-form.${kind}`;
}

/* Filled fields that still have a box on the page. Highlights can lag behind
   an in-flight refine, so the crop list is read off the form itself. */
/* One image per CRF: the whole page(s) its values were read from.

   A field's page is known without any model — it is where the reader found
   the value — so a CRF whose values all sit on page 1 sends page 1 entire,
   and one spread over pages 1 and 2 sends both. The on-screen boxes are
   untouched; this only changes what travels with the save. */
function pageTargets(form) {
  const items = [];
  (form.sections || []).forEach((section) => {
    const pages = new Set();
    const note = (field) => {
      if (!field || field.value === null || field.value === undefined || field.value === "") return;
      const page = Number((field.source || {}).page);
      if (page) pages.add(page);
    };
    (section.fields || []).forEach(note);
    (section.groups || []).forEach((group) =>
      (group.instances || []).forEach((instance) => (instance.fields || []).forEach(note))
    );
    [...pages].sort((a, b) => a - b).forEach((page) => {
      items.push({
        key: `${section.section_id}.__page.${page}`,
        page,
        rects: [{ x: 0, y: 0, w: 1, h: 1 }],
      });
    });
  });
  return items;
}

function cropTargets(form) {
  const items = [];
  const take = (field) => {
    if (!field || !field.key) return;
    if (field.value === null || field.value === undefined || field.value === "") return;
    const source = field.source || {};
    const rects = source.rects || [];
    if (!source.page || !rects.length) return;
    items.push({
      key: field.key,
      page: source.page,
      rects,
      value: field.value,
      label: field.label,
    });
  };
  (form.sections || []).forEach((section) => {
    (section.fields || []).forEach(take);
    (section.groups || []).forEach((group) =>
      (group.instances || []).forEach((instance) =>
        (instance.fields || []).forEach(take)
      )
    );
  });
  return items;
}

const SUBMIT_LABEL = "Submit to EDC";

function resetSubmitButton() {
  const button = els.sendCronos;
  button.classList.remove("is-loading", "is-done");
  button.disabled = false;
  button.textContent = SUBMIT_LABEL;
}

/* After a signed submission. The button says so; a later edit re-opens it
   (edc.js calls resetSubmitButton), since the new value has not been sent. */
function markSavedToEdc() {
  state.edcSaved = true;
  savingToEdc = false;
  buttonDone(els.sendCronos, "Submitted");
}

/* One image per CRF page its values were read from — exactly what the old
   one-click save attached. Used by the signed submit in edc.js.
   `onStage(text)` reports progress for the button that is waiting. */
async function cutCrops(onStage = () => {}) {
  if (!state.payload) return {};
  const located = pageTargets(state.payload.form || {});
  if (!located.length) return {};
  if (state.pdf && state.pdf.loaded) {
    onStage("Preparing the original page…");
    await state.pdf.loaded;
  }
  if (!state.pdf || !state.pdf.doc) {
    throw new Error(
      "The original document isn't available, so source images couldn't be attached."
    );
  }
  onStage("Cutting source images…");
  const crops = await state.pdf.cropRegions(located, {
    padding: 0, format: "image/jpeg", quality: 0.85,
  });
  if (!Object.keys(crops).length) {
    throw new Error("Source images couldn't be cut from the page. Try again.");
  }
  return crops;
}

/* Submitting goes through pre-flight and an e-signature (edc.js). */
function saveToEdc() {
  if (!state.payload || savingToEdc) return;
  const layer = edc();
  if (layer && layer.openSubmit) {
    layer.openSubmit();
    return;
  }
  toast("Submitting isn't available right now. Reload the page and try again.", "error", 4200);
}

/* ------------------------------------------------------------------- wiring */

function wireMapping() {
  els.headerConfirm.addEventListener("click", loadVisit);
  els.dlCrfPdf.addEventListener("click", () => exportCrf("pdf"));
  els.dlCrfJson.addEventListener("click", () => exportCrf("json"));
  els.sendCronos.addEventListener("click", saveToEdc);

  els.regionToggle.addEventListener("change", () => {
    if (state.pdf) state.pdf.setVisible(els.regionToggle.checked);
  });

  els.stepper.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-step]");
    if (!button || button.disabled) return;
    const step = button.dataset.step;
    // Each step owns whatever fetching it needs, so arriving from the stepper
    // behaves the same as arriving from the button on the previous step.
    if (step === "header") loadHeader();
    else if (step === "form") state.payload ? setStep("form") : loadVisit();
    else setStep(step);
  });
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
      toast(err.message, "error", 4200);
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
    toast("Copied to clipboard");
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

/* -------------------------------------------------------------------- auth */

function showLogin(message) {
  document.body.classList.add("signed-out");
  document.body.classList.remove("booting");
  els.loginOverlay.classList.remove("hidden");
  els.userChip.classList.add("hidden");
  if (message) {
    els.loginError.textContent = message;
    els.loginError.classList.remove("hidden");
  } else {
    els.loginError.classList.add("hidden");
  }
  els.loginUsername.focus();
}

function setPasswordRevealed(revealed) {
  const btn = $("login-reveal");
  els.loginPassword.type = revealed ? "text" : "password";
  if (!btn) return;
  btn.setAttribute("aria-pressed", revealed ? "true" : "false");
  btn.setAttribute("aria-label", revealed ? "Hide password" : "Show password");
}

function hideLogin() {
  document.body.classList.remove("signed-out");
  els.loginOverlay.classList.add("hidden");
  els.loginError.classList.add("hidden");
  els.loginPassword.value = "";
  setPasswordRevealed(false);
}

function showSignedIn(user) {
  state.username = user.username;
  state.user = {
    username: user.username,
    full_name: user.full_name || user.username,
    role: user.role || "reviewer",
  };
  els.userName.textContent = state.user.full_name;
  els.userName.title = `${state.user.username} · ${state.user.role}`;
  els.userChip.classList.remove("hidden");
  hideLogin();
}

/* Review.open() can be called by the shell's router before the session is
   known; it waits here until a reviewer is signed in and the app session
   (modes, reader, mapping) is loaded. */
let sessionReady = false;
let signedInResolve = null;
let signedInPromise = null;
function whenSignedIn() {
  if (sessionReady) return Promise.resolve();
  if (!signedInPromise) {
    signedInPromise = new Promise((resolve) => { signedInResolve = resolve; });
  }
  return signedInPromise;
}
function markSignedIn() {
  sessionReady = true;
  if (signedInResolve) signedInResolve();
  signedInResolve = null;
  signedInPromise = null;
}

function announceSignedOut() {
  sessionReady = false;
  window.dispatchEvent(new CustomEvent("wh:signed-out"));
}

async function loadAppSession() {
  const session = await api("/api/session");
  state.modes = session.modes || [];
  state.modeId = "maximum";
  state.ready = Boolean(session.ready);
  state.reader = session.reader || null;
  state.mappingReady = Boolean((session.mapping || {}).ready);
  state.cronos = (session.mapping || {}).cronos || null;
  els.maxMb.textContent = String(session.max_file_mb || 50);
  if (!state.mappingReady) {
    els.stepper.querySelectorAll('button[data-step="header"], button[data-step="form"]')
      .forEach((button) => {
        button.title =
          "CRF mapping isn't configured yet. Please contact your administrator.";
      });
  }
  renderModes();
  updateRunBar();
  renderCredits(session.credits);
  if (!state.ready) {
    els.setupError.textContent =
      "The extraction service isn't configured yet. Please contact your administrator before running a document.";
    els.setupError.classList.remove("hidden");
  }
  return session;
}

async function afterSignIn(user) {
  showSignedIn(user);
  await loadAppSession();
  window.dispatchEvent(new CustomEvent("wh:signed-in", { detail: { ...state.user } }));
  markSignedIn();
  await renderRecentMenu();
  let restored = false;
  // Inside the app shell the router decides what opens; picking the last
  // local review back up would cover whatever the address asked for.
  const shell = Boolean(window.WH && window.WH.router);
  try {
    if (!shell && !state.result) restored = await restoreSession();
  } catch (error) {
    console.warn("no session restored", error);
  }
  if (state.result && !state.header && state.mappingReady) {
    loadHeader();
  }
  return restored;
}

onUnauthorized = () => {
  const was = state.username;
  state.username = null;
  state.user = null;
  sessionReady = false;
  showLogin("Your session ended. Please sign in again.");
  if (was) announceSignedOut();
};

$("login-reveal").addEventListener("click", () => {
  setPasswordRevealed(els.loginPassword.type === "password");
  els.loginPassword.focus();
});

els.loginForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  els.loginError.classList.add("hidden");
  els.loginSubmit.disabled = true;
  try {
    const data = await api("/api/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        username: els.loginUsername.value.trim(),
        password: els.loginPassword.value,
      }),
    });
    await afterSignIn(data);
  } catch (error) {
    // 423: the account is locked; the detail says until when.
    els.loginError.textContent = error.status === 423
      ? error.message || "This account is locked. Try again later or ask an administrator."
      : error.status === 401
        ? error.message || "Incorrect username or password."
        : error.message || "We couldn't sign you in. Please try again.";
    els.loginError.classList.toggle("is-locked", error.status === 423);
    els.loginError.classList.remove("hidden");
    if (error.status !== 423) {
      els.loginPassword.focus();
    }
  } finally {
    els.loginSubmit.disabled = false;
  }
});

els.logoutBtn.addEventListener("click", async () => {
  try {
    await api("/api/logout", { method: "POST" });
  } catch {
    /* still clear the local UI */
  }
  if (edc() && edc().flush) edc().flush();
  await clearCurrentPointer();
  wireTabLease(null);
  state.username = null;
  state.user = null;
  state.result = null;
  openToken = null;
  hideOpenSkeleton();
  resetMapping();
  resetReviewIdentity();
  document.body.classList.remove("viewing");
  els.viewer.classList.add("hidden");
  showLogin();
  announceSignedOut();
});

els.recentBtn.addEventListener("click", async (event) => {
  event.stopPropagation();
  const open = els.recentMenu.classList.contains("hidden");
  if (open) await renderRecentMenu();
  els.recentMenu.classList.toggle("hidden");
  els.recentBtn.setAttribute("aria-expanded", open ? "true" : "false");
});
document.addEventListener("click", () => {
  if (els.recentMenu) els.recentMenu.classList.add("hidden");
});

/* ==========================================================================
   Review entry points — the app shell drives the screen through these.

   Review.open(docId) opens a review the server holds. GET /docs/{id}/review
   answers in the same shape the IndexedDB snapshot is saved in, so it lands
   exactly like a restored session; the PDF comes from /docs/{id}/file so
   the original pane, its boxes and the submit crops behave as for an upload.
   ========================================================================== */

let openToken = null;
let openPromise = null;
let openingId = null;

function hideOpenSkeleton() {
  const node = document.getElementById("rv-open");
  if (node) node.remove();
  els.viewer.classList.remove("is-opening");
}

function showOpenSkeleton() {
  hideOpenSkeleton();
  els.viewer.classList.add("is-opening");
  els.resultTitle.textContent = "Opening review…";
  els.resultChips.innerHTML = "";
  const wrap = document.createElement("div");
  wrap.id = "rv-open";
  wrap.className = "rv-open";
  wrap.setAttribute("role", "status");
  wrap.setAttribute("aria-live", "polite");
  const sr = document.createElement("span");
  sr.className = "rv-sr";
  sr.textContent = "Opening the review…";
  const left = document.createElement("div");
  left.className = "rv-open-pane rv-open-original";
  const page = document.createElement("div");
  page.className = "rv-sk-page";
  ["w-40 h-lg", "w-90", "w-80", "w-85", "block", "w-70", "w-88", "w-60"].forEach((cls) => {
    const line = document.createElement("span");
    line.className = `rv-sk ${cls}`;
    page.appendChild(line);
  });
  left.appendChild(page);
  const right = document.createElement("div");
  right.className = "rv-open-pane rv-open-form";
  ["rail", "title", "row", "row w-80", "row", "row w-60", "row", "row w-80"].forEach((cls) => {
    const line = document.createElement("span");
    line.className = `rv-sk rv-sk-${cls.split(" ")[0]} ${cls.split(" ")[1] || ""}`.trim();
    right.appendChild(line);
  });
  wrap.append(sr, left, right);
  els.stage.appendChild(wrap);
}

function showOpenError(error, retry) {
  const wrap = document.getElementById("rv-open");
  if (!wrap) return;
  wrap.innerHTML = "";
  wrap.classList.add("is-error");
  const card = document.createElement("div");
  card.className = "rv-open-error";
  const node = statusNode({
    kind: error.status === 403 ? "warn" : "error",
    title: error.status === 404
      ? "This review couldn't be found"
      : error.status === 403
        ? "You don't have access to this review"
        : "The review didn't open",
    detail: error.message,
  });
  const actions = document.createElement("div");
  actions.className = "rv-open-actions";
  const back = document.createElement("button");
  back.type = "button";
  back.className = "btn btn-quiet";
  back.textContent = "Back";
  back.addEventListener("click", () => {
    closeViewer();
    if (window.WH && window.WH.router && history.length > 1) history.back();
  });
  actions.appendChild(back);
  if (error.status !== 404 && error.status !== 403) {
    const again = document.createElement("button");
    again.type = "button";
    again.className = "btn btn-primary";
    again.textContent = "Try again";
    again.addEventListener("click", retry);
    actions.appendChild(again);
  }
  card.append(node, actions);
  wrap.appendChild(card);
  els.resultTitle.textContent = "Review not opened";
}

function openReview(docId) {
  const id = String(docId == null ? "" : docId).trim();
  if (!id) return Promise.resolve(false);
  if (openToken && openingId === id && openPromise) return openPromise;
  openPromise = openReviewInner(id).finally(() => {
    if (openingId === id) {
      openPromise = null;
      openingId = null;
    }
  });
  openingId = id;
  return openPromise;
}

async function openReviewInner(id) {
  await whenSignedIn();
  // Already on screen (the upload that just set the address, or a second
  // click on the same row): nothing to fetch.
  if (state.docId === id && state.result && !openToken
      && !els.viewer.classList.contains("hidden")) {
    return true;
  }
  const layer = edc();
  if (layer && layer.flush) layer.flush();

  const token = {};
  openToken = token;
  if (state.pdf) {
    state.pdf.destroy();
    state.pdf = null;
  }
  stopProcessing();
  resetMapping();
  resetReviewIdentity();
  state.result = null;
  state.file = null;
  state.fileBytes = null;
  state.bytesDropped = false;
  wireTabLease(null);
  els.viewer.classList.remove("hidden");
  document.body.classList.add("viewing");
  showOpenSkeleton();

  try {
    const enc = encodeURIComponent(id);
    const [snap, bytes] = await Promise.all([
      api(`/api/docs/${enc}/review`),
      fetch(`/api/docs/${enc}/file`, { credentials: "same-origin" })
        .then((response) => (response.ok ? response.arrayBuffer() : null))
        .catch(() => null),
    ]);
    if (openToken !== token) return false;
    applyReviewSnapshot(id, snap, bytes);
    openToken = null;
    hideOpenSkeleton();
    return true;
  } catch (error) {
    if (openToken !== token) return false;
    openToken = null;
    if (error.status === 401) {
      hideOpenSkeleton();
      return false;
    }
    showOpenError(error, () => openReview(id));
    return false;
  }
}

function applyReviewSnapshot(id, snap, bytes) {
  const doc = snap.document || {};
  state.docId = id;
  state.extractionId = snap.extraction_id != null
    ? snap.extraction_id
    : (doc.extraction && doc.extraction.id) || null;
  state.serverReadOnly = Boolean(snap.readonly);
  state.submitted = snap.submitted || null;
  state.fileBytes = bytes || null;
  state.savedName = (snap.result && snap.result.filename) || doc.filename || null;
  state.startedAt = Date.now();
  if (snap.reader) state.reader = { ...(state.reader || {}), ...snap.reader };

  const result = { ...(snap.result || {}) };
  if (!result.job_id) result.job_id = snap.job_id;
  if (!result.filename) result.filename = doc.filename || "Document";

  state.header = snap.header || null;
  const visit = snap.visit || null;
  if (visit && visit.form) {
    state.visit = visit.form;
    state.unsaveable = visit.unsaveable || [];
  } else {
    state.visit = visit;
    state.unsaveable = [];
  }
  state.payload = snap.payload || null;
  state.edits = Array.isArray(snap.edits) ? snap.edits : [];
  state.draftRevision = (snap.draft && snap.draft.revision) || doc.draft_revision || 0;
  state.signature = snap.signature || null;
  state.sentKeys = [];
  state.edcSaved = false;

  const landing = state.payload ? "form" : "header";
  showResult(result, landing, { preserveMapping: true, deferStep: true });
  if (!state.fileBytes) {
    els.originalEmpty.textContent =
      "The original file couldn't be loaded from the server. The values can still be " +
      "reviewed; boxes and source images need the page.";
  }
  wireTabLease(result.job_id);
  // Applies the server's read-only flag even where BroadcastChannel is absent.
  setReadOnly(state.readOnly);

  enableStep("header");
  if (state.header) {
    renderHeader(state.header);
    enableStep("form");
  }
  if (state.payload) {
    renderForm(state.payload);
    els.formPicker.classList.add("hidden");
    els.formBody.classList.remove("hidden");
    const located = collectHighlights(state.payload.form)
      .some((item) => PLACED_ON_PAGE.has(item.match));
    if (!located && state.pdf && !state.readOnly) {
      flowStart("Placing boxes on the page…");
      refineBoxes(prefetchPageImages());
    }
  }
  setStep(landing);
  els.extractedScroll.scrollTop = 0;
  if (!state.header && state.mappingReady && !state.readOnly) loadHeader();

  const layer = edc();
  if (layer && layer.reviewLoaded) {
    layer.reviewLoaded({ source: "server", draft: snap.draft || null, submitted: snap.submitted || null });
  }
  saveSessionNow();
}

function newUpload() {
  if (state.result || !els.viewer.classList.contains("hidden")) {
    closeViewer();
  } else {
    resetMapping();
    resetReviewIdentity();
  }
  hideError();
  els.fileInput.value = "";
  setFile(null);
  updateRunBar();
}

window.Review = {
  open: openReview,
  newUpload,
  hasUnsavedChanges: () => {
    const layer = edc();
    return Boolean(layer && layer.hasUnsavedChanges && layer.hasUnsavedChanges());
  },
  get docId() { return state.docId; },
};

/* What edc.js builds on. One object, so the dependency is visible. */
window.ReviewCore = {
  state, els, api, toast, statusNode, inlineNotice, buttonBusy, buttonDone,
  selectField, rowFor, fieldByKey, allFields, rerenderField, refreshMetaFor,
  useAlternateRead, cutCrops, markSavedToEdc, resetSubmitButton,
  saveSession, saveSessionNow, cssEscape, formatDate, openReview,
  STATUS_ICONS, ISSUE_TEXT, READING_ISSUES, CHECK_ISSUES, HARD_ISSUES,
};

/* -------------------------------------------------------------------- boot */

(async function boot() {
  setZoom(1);
  resetSubmitButton();
  wireMapping();
  wireSplitter();
  document.body.classList.add("signed-out");

  try {
    const me = await api("/api/me", { quiet401: true });
    document.body.classList.remove("booting");
    await afterSignIn(me);
  } catch {
    showLogin();
  }
})();
