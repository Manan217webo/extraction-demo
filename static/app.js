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
  pdfScroll: $("pdf-scroll"),
  originalEmpty: $("original-empty"),
  regionToggle: $("region-toggle"),
  regionToggleWrap: $("region-toggle-wrap"),
  regionCount: $("region-count"),

  stepper: $("stepper"),
  vbActions: $("vb-actions"),
  toolsExtract: $("tools-extract"),
  toHeader: $("to-header"),
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
  formChange: $("form-change"),
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
  cronos: null,
  step: "extract",
  pdf: null,
  header: null,
  forms: [],
  payload: null,
  selectedKey: null,
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

  // Original document, straight from the file the reviewer picked. It is rendered
  // page by page rather than handed to the browser's PDF plugin, because the
  // mapping stage needs to draw over it.
  showOriginal();

  resetMapping();
  if (state.mappingReady) enableStep("header");
  setStep("extract");
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
  if (state.pdf) {
    state.pdf.destroy();
    state.pdf = null;
  }
  resetMapping();
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
};
const HARD_ISSUES = new Set([
  "not_a_number", "not_an_allowed_option", "required_field_empty",
  "below_expected_range", "above_expected_range",
]);

/* ------------------------------------------------------------- original pane */

function showOriginal() {
  if (state.pdf) {
    state.pdf.destroy();
    state.pdf = null;
  }
  if (!state.file) {
    els.originalEmpty.classList.remove("hidden");
    return;
  }
  els.originalEmpty.classList.add("hidden");
  state.pdf = new PdfView(els.pdfScroll, { onSelect: (key) => selectField(key, "pdf") });
  state.file
    .arrayBuffer()
    .then((bytes) => state.pdf.load(bytes))
    .catch((error) => {
      console.warn("original document could not be rendered", error);
      els.originalEmpty.textContent =
        "We couldn't display the original file in this browser.";
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
  state.header = null;
  state.payload = null;
  state.forms = [];
  state.selectedKey = null;
  els.headerGroups.innerHTML = "";
  els.formSections.innerHTML = "";
  els.formList.innerHTML = "";
  els.formBody.classList.add("hidden");
  els.formPicker.classList.remove("hidden");
  setHighlights([]);
  [...els.stepper.children].forEach((button) => {
    button.disabled = button.dataset.step !== "extract";
    button.classList.remove("done");
  });
}

function enableStep(step) {
  const button = els.stepper.querySelector(`button[data-step="${step}"]`);
  if (button) button.disabled = false;
}

function setStep(step) {
  state.step = step;
  [...els.stepper.children].forEach((button) => {
    const own = button.dataset.step;
    button.classList.toggle("active", own === step);
    button.classList.toggle(
      "done",
      (own === "extract" && step !== "extract") || (own === "header" && step === "form")
    );
  });

  els.panelExtract.classList.toggle("hidden", step !== "extract");
  els.panelHeader.classList.toggle("hidden", step !== "header");
  els.panelForm.classList.toggle("hidden", step !== "form");

  els.toolsExtract.classList.toggle("hidden", step !== "extract");
  els.copyBtn.classList.toggle("hidden", step !== "extract");
  els.downloadBtn.parentElement.classList.toggle("hidden", step !== "extract");
  els.toHeader.classList.toggle("hidden", step !== "extract");

  els.extractedHeading.textContent =
    step === "extract" ? "Extracted text"
      : step === "header" ? "Document header"
        : "Cronos CRF";
  els.extractedNote.textContent =
    step === "extract" ? "Always check against the original"
      : "Red boxes show what was used";

  // Only the values on show should be boxed on the page.
  if (step === "header") setHighlights(state.header ? state.header.highlights : []);
  else if (step === "form") setHighlights(state.payload ? state.payload.highlights : []);
  else setHighlights([]);

  if (step !== "extract") setLayout(els.stage.dataset.layout === "extracted" ? "split" : els.stage.dataset.layout);
  els.extractedScroll.scrollTop = 0;
}

function busy(text) {
  els.panelBusyText.textContent = text;
  els.panelBusy.classList.remove("hidden");
}

function idle() {
  els.panelBusy.classList.add("hidden");
}

function inlineNotice(container, text, kind = "warn") {
  const notice = document.createElement("div");
  notice.className = `notice-inline ${kind === "warn" ? "" : kind}`.trim();
  notice.textContent = text;
  container.prepend(notice);
}

/* -------------------------------------------------------------- field inputs */

function fieldControl(field) {
  const value = field.value === null || field.value === undefined ? "" : String(field.value);

  if (field.type === "radio" && (field.options || []).length <= 4) {
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

  const input = document.createElement("input");
  input.type =
    field.type === "date" ? "date" : field.type === "time" ? "time"
      : field.type === "number" ? "number" : "text";
  if (field.type === "number") input.step = "any";
  input.value = value;
  input.placeholder = "Not recorded";
  return { node: input, read: () => input.value.trim() || null };
}

function renderField(field) {
  const row = document.createElement("div");
  row.className = "field";
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
  if (source.anchored) {
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
  (field.issues || []).forEach((code) => {
    const issue = document.createElement("span");
    issue.className = `issue${HARD_ISSUES.has(code) ? " hard" : ""}`;
    issue.textContent = ISSUE_TEXT[code] || code.replace(/_/g, " ");
    meta.appendChild(issue);
  });
  if (source.evidence) {
    const evidence = document.createElement("span");
    evidence.className = "evidence";
    evidence.textContent = `“${source.evidence}”`;
    meta.appendChild(evidence);
  }
  if (meta.childNodes.length) wrap.appendChild(meta);

  const commit = () => {
    const next = control.read();
    if (String(next === null ? "" : next) === String(field.value === null ? "" : field.value)) {
      return;
    }
    field.value = next;
    field.status = field.status === "empty" || field.value === null ? "manual" : "edited";
    field.confidence = null;
    field.source = { ...(field.source || {}), anchored: false, rects: [], match: "edited" };
    row.classList.remove("is-empty", "is-flagged");
    refreshHighlights();
  };
  control.node.addEventListener("change", commit);
  control.node.addEventListener("blur", commit, true);
  row.addEventListener("focusin", () => selectField(field.key, "form", { scroll: true }));

  row.append(label, wrap);
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

/* ------------------------------------------------------- selection linking */

function selectField(key, origin, { scroll = true } = {}) {
  state.selectedKey = key;
  document.querySelectorAll(".field.is-selected").forEach((row) =>
    row.classList.remove("is-selected")
  );
  const row = els.extractedScroll.querySelector(`.field[data-key="${cssEscape(key)}"]`);
  if (row) {
    row.classList.add("is-selected");
    if (origin === "pdf") row.scrollIntoView({ block: "center", behavior: "smooth" });
  }
  if (state.pdf) state.pdf.focus(key, { scroll: scroll && origin !== "pdf" });
}

function cssEscape(value) {
  return window.CSS && CSS.escape ? CSS.escape(value) : String(value).replace(/"/g, '\\"');
}

function collectHighlights(container) {
  const out = [];
  const walk = (fields, groupName, instance) => {
    (fields || []).forEach((field) => {
      const source = field.source || {};
      if (!source.anchored || !(source.rects || []).length) return;
      out.push({
        key: field.key,
        label: field.label,
        value: field.value,
        group: groupName,
        instance,
        issues: field.issues || [],
        page: source.page,
        rects: source.rects,
      });
    });
  };
  (container.groups || []).forEach((group) => walk(group.fields, group.name, null));
  (container.sections || []).forEach((section) => {
    walk(section.fields, section.name, null);
    (section.groups || []).forEach((group) =>
      (group.instances || []).forEach((instance) =>
        walk(instance.fields, `${section.name} — ${group.label}`, instance.instance)
      )
    );
  });
  return out;
}

function refreshHighlights() {
  if (state.step === "header" && state.header) {
    state.header.highlights = collectHighlights(state.header.header);
    setHighlights(state.header.highlights);
  } else if (state.step === "form" && state.payload) {
    state.payload.highlights = collectHighlights(state.payload.form).concat(
      collectHighlights(state.payload.header)
    );
    setHighlights(state.payload.highlights);
  }
}

/* ------------------------------------------------------------ step 2: header */

async function loadHeader() {
  if (!state.result || !state.result.job_id) return;
  setStep("header");
  if (state.header) return;

  busy("Reading the document header… this can take a minute on a long document.");
  try {
    const data = await api(`/api/documents/${state.result.job_id}/header`, { method: "POST" });
    state.header = data;
    renderHeader(data);
    enableStep("form");
  } catch (error) {
    els.headerGroups.innerHTML = "";
    inlineNotice(els.headerGroups, error.message);
    els.headerConfirm.disabled = true;
  } finally {
    idle();
  }
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
    const filled = group.fields.filter((field) => field.value !== null).length;
    els.headerGroups.appendChild(
      renderFieldGroup(group.name, group.fields, `${filled} of ${group.fields.length} read`)
    );
  });
  setHighlights(collectHighlights(data.header));
  state.header.highlights = collectHighlights(data.header);
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

/* -------------------------------------------------------- step 3: cronos CRF */

async function chooseForm() {
  setStep("form");
  els.formBody.classList.add("hidden");
  els.formPicker.classList.remove("hidden");
  if (state.forms.length) return;

  busy("Asking Cronos which forms apply…");
  const protocol = confirmedHeader().protocol_no;
  try {
    const data = await api(
      `/api/cronos/forms${protocol ? `?protocol_no=${encodeURIComponent(protocol)}` : ""}`
    );
    state.forms = data.forms || [];
    state.cronos = data;
    renderFormList(data);
  } catch (error) {
    els.formList.innerHTML = "";
    inlineNotice(els.formList, error.message);
  } finally {
    idle();
  }
}

function renderFormList(data) {
  const protocol = confirmedHeader().protocol_no;
  els.pickerNote.textContent = data.live
    ? `Forms Cronos lists${protocol ? ` for ${protocol}` : ""}.`
    : `Sample forms from the built-in connector${protocol ? ` for ${protocol}` : ""} — ` +
      "Cronos itself isn't connected yet.";

  els.formList.innerHTML = "";
  if (!data.forms.length) {
    inlineNotice(els.formList, "Cronos has no forms for this protocol.");
    return;
  }
  data.forms.forEach((form) => {
    const card = document.createElement("button");
    card.type = "button";
    card.className = "form-card";

    const title = document.createElement("strong");
    title.textContent = `${form.form_name} · ${form.form_id}`;
    const description = document.createElement("p");
    description.textContent = form.form_description || "";

    const chips = document.createElement("div");
    chips.className = "chips";
    [
      `Version ${form.form_version}`,
      `${form.section_count} sections`,
      `${form.field_count} fields`,
      ...(form.sections || []).map((section) => section.name),
    ].forEach((text) => {
      const chip = document.createElement("span");
      chip.textContent = text;
      chips.appendChild(chip);
    });

    card.append(title, description, chips);
    card.addEventListener("click", () => mapToForm(form.form_id));
    els.formList.appendChild(card);
  });
}

async function mapToForm(formId) {
  busy("Reading the document against the form… this can take a minute on a long document.");
  try {
    const payload = await api(`/api/documents/${state.result.job_id}/map`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ form_id: formId, header: confirmedHeader() }),
    });
    state.payload = payload;
    renderForm(payload);
    els.formPicker.classList.add("hidden");
    els.formBody.classList.remove("hidden");
    // Asserted rather than assumed: this is reachable from any step.
    setStep("form");
    els.extractedScroll.scrollTop = 0;
  } catch (error) {
    inlineNotice(els.formList, error.message);
  } finally {
    idle();
  }
}

function renderForm(payload) {
  const form = payload.form;
  els.formEyebrow.textContent = state.cronos && state.cronos.live
    ? "Cronos form" : "Cronos form · sample connector";
  els.formTitle.textContent = `${form.form_name} · ${form.form_id} v${form.form_version}`;
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
  if (!payload.anchoring) {
    inlineNotice(
      els.formSections,
      "This document was read in a mode that doesn't record page positions, so mapped " +
        "values can't be boxed on the original. Choose High or Maximum Accuracy to see them.",
      "info"
    );
  }
  (form.sections || []).forEach((section) => {
    const card = document.createElement("section");
    card.className = "crf-section";

    const head = document.createElement("header");
    const title = document.createElement("h4");
    title.textContent = section.name;
    head.appendChild(title);
    if (section.description) {
      const note = document.createElement("p");
      note.textContent = section.description;
      head.appendChild(note);
    }
    card.appendChild(head);

    if ((section.fields || []).length) {
      card.appendChild(renderFieldGroup(null, section.fields));
    }

    (section.groups || []).forEach((group) => {
      const groupHead = document.createElement("div");
      groupHead.className = "crf-group-head";
      const label = document.createElement("span");
      label.textContent = group.label;
      const count = document.createElement("span");
      count.textContent = `${(group.instances || []).length} recorded`;
      groupHead.append(label, count);
      card.appendChild(groupHead);

      if (!(group.instances || []).length) {
        const empty = document.createElement("p");
        empty.className = "crf-empty";
        empty.textContent = `No ${group.row_label.toLowerCase()} rows were found on the document.`;
        card.appendChild(empty);
        return;
      }
      group.instances.forEach((instance) => {
        const block = document.createElement("div");
        block.className = "crf-instance";
        const heading = document.createElement("h5");
        heading.textContent = `${group.row_label} ${instance.instance}`;
        block.appendChild(heading);
        block.appendChild(renderFieldGroup(null, instance.fields));
        card.appendChild(block);
      });
    });

    els.formSections.appendChild(card);
  });

  payload.highlights = collectHighlights(payload.form).concat(collectHighlights(payload.header));
  setHighlights(payload.highlights);
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

async function sendToCronos() {
  if (!state.payload) return;
  const button = els.sendCronos;
  const original = button.textContent;
  button.disabled = true;
  button.textContent = "Sending…";
  try {
    const result = await api("/api/cronos/submissions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ form_id: state.payload.form.form_id, payload: state.payload }),
    });
    button.textContent = result.live ? "Sent to Cronos" : "Accepted (sample connector)";
    inlineNotice(
      els.formSections,
      result.message ||
        `Cronos accepted ${result.fields_received} field${result.fields_received === 1 ? "" : "s"}.`,
      "info"
    );
    els.extractedScroll.scrollTop = 0;
    setTimeout(() => {
      button.textContent = original;
      button.disabled = false;
    }, 2600);
  } catch (error) {
    inlineNotice(els.formSections, error.message);
    els.extractedScroll.scrollTop = 0;
    button.textContent = original;
    button.disabled = false;
  }
}

/* ------------------------------------------------------------------- wiring */

function wireMapping() {
  els.toHeader.addEventListener("click", loadHeader);
  els.headerConfirm.addEventListener("click", chooseForm);
  els.formChange.addEventListener("click", chooseForm);
  els.dlCrfPdf.addEventListener("click", () => exportCrf("pdf"));
  els.dlCrfJson.addEventListener("click", () => exportCrf("json"));
  els.sendCronos.addEventListener("click", sendToCronos);

  els.regionToggle.addEventListener("change", () => {
    if (state.pdf) state.pdf.setVisible(els.regionToggle.checked);
  });

  els.stepper.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-step]");
    if (!button || button.disabled) return;
    if (button.dataset.step === "header") loadHeader();
    else setStep(button.dataset.step);
  });

  els.panelHeader.addEventListener("click", (event) => {
    const button = event.target.closest("[data-goto]");
    if (button) setStep(button.dataset.goto);
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
  wireMapping();
  try {
    const session = await api("/api/session");
    state.modes = session.modes || [];
    const recommended = state.modes.find((m) => m.recommended);
    state.modeId = recommended ? recommended.id : (state.modes[0] || {}).id;
    state.ready = Boolean(session.ready);
    state.mappingReady = Boolean((session.mapping || {}).ready);
    state.cronos = (session.mapping || {}).cronos || null;
    els.maxMb.textContent = String(session.max_file_mb || 50);
    if (!state.mappingReady) {
      els.toHeader.disabled = true;
      els.toHeader.title =
        "CRF mapping isn't configured yet. Please contact your administrator.";
    }

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
