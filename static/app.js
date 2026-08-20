/**
 * CRF Extraction Prototype — frontend
 */

const state = {
  jobId: null,
  job: null,
  currentPage: 1,
  zoom: 1,
  /** fieldKey -> { value, confidence, edited } across all pages */
  fields: {},
  /** fieldKey -> page_no (first occurrence) for scroll */
  fieldPage: {},
  pollTimer: null,
  lastPdfFile: null,
};

const $ = (id) => document.getElementById(id);

const dropView = $("dropView");
const resultView = $("resultView");
const overlay = $("overlay");
const progressBlock = $("progressBlock");
const errorBlock = $("errorBlock");
const dropzone = $("dropzone");
const fileInput = $("fileInput");

function show(el) {
  el.classList.remove("hidden");
}
function hide(el) {
  el.classList.add("hidden");
}

function setOverlayProgress(title, detail) {
  show(overlay);
  show(progressBlock);
  hide(errorBlock);
  $("progressTitle").textContent = title;
  $("progressDetail").textContent = detail || "";
}

function setOverlayError(message) {
  show(overlay);
  hide(progressBlock);
  show(errorBlock);
  $("errorMessage").textContent = message;
}

function clearOverlay() {
  hide(overlay);
}

/* ---------- Upload ---------- */

function acceptFile(file) {
  if (!file) return;
  if (!file.name.toLowerCase().endsWith(".pdf") && file.type !== "application/pdf") {
    setOverlayError("Please upload a PDF file.");
    return;
  }
  state.lastPdfFile = file;
  startExtract(file);
}

async function startExtract(file) {
  hide(resultView);
  show(dropView);
  setOverlayProgress("Uploading…", file.name);

  const form = new FormData();
  form.append("file", file);

  try {
    const res = await fetch("/api/extract", { method: "POST", body: form });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || `Upload failed (${res.status})`);
    }
    const data = await res.json();
    state.jobId = data.job_id;
    setOverlayProgress("Queued…", "Waiting for extraction");
    startPolling();
  } catch (e) {
    setOverlayError(String(e.message || e));
  }
}

function startPolling() {
  stopPolling();
  pollOnce();
  state.pollTimer = setInterval(pollOnce, 800);
}

function stopPolling() {
  if (state.pollTimer) {
    clearInterval(state.pollTimer);
    state.pollTimer = null;
  }
}

async function pollOnce() {
  if (!state.jobId) return;
  try {
    const res = await fetch(`/api/jobs/${state.jobId}`);
    if (!res.ok) throw new Error(`Status ${res.status}`);
    const job = await res.json();
    state.job = job;

    if (job.status === "converting") {
      setOverlayProgress("Converting PDF…", "Rendering pages at 300 DPI");
    } else if (job.status === "extracting" || job.status === "queued") {
      const cur = job.current_page || 1;
      const tot = job.total_pages || 3;
      setOverlayProgress(
        `Extracting page ${cur} of ${tot}…`,
        "Each page is sent separately to Gemini"
      );
    } else if (job.status === "validating") {
      setOverlayProgress("Validating…", "Running Python rules");
    } else if (job.status === "error") {
      stopPolling();
      setOverlayError(job.error || "Unknown error");
    } else if (job.status === "done") {
      stopPolling();
      clearOverlay();
      loadJobIntoState(job);
      renderAll();
    }
  } catch (e) {
    stopPolling();
    setOverlayError(String(e.message || e));
  }
}

function loadJobIntoState(job) {
  state.fields = {};
  state.fieldPage = {};
  state.currentPage = 1;
  state.zoom = 1;

  for (const page of job.pages || []) {
    for (const [key, item] of Object.entries(page.fields || {})) {
      if (!(key in state.fields)) {
        state.fields[key] = {
          value: item.value,
          confidence: item.confidence || "low",
          edited: false,
        };
        state.fieldPage[key] = page.page_no;
      } else {
        // Keep page-specific copies for header mismatches via synthetic keys
        const pkey = `__p${page.page_no}_${key}`;
        state.fields[pkey] = {
          value: item.value,
          confidence: item.confidence || "low",
          edited: false,
        };
        // Also store page-local under page fields when rendering that page
      }
    }
  }

  // Per-page field overrides for rendering (header fields can differ)
  state.pageFields = {};
  for (const page of job.pages || []) {
    state.pageFields[page.page_no] = {};
    for (const [key, item] of Object.entries(page.fields || {})) {
      state.pageFields[page.page_no][key] = {
        value: item.value,
        confidence: item.confidence || "low",
        edited: false,
      };
    }
  }

  hide(dropView);
  show(resultView);
  show($("headerMeta"));
  $("btnExport").disabled = false;
  $("btnRerun").disabled = false;
  $("fileName").textContent = job.filename || "";
  updateCounts();
}

function updateCounts() {
  let low = 0;
  const seen = new Set();
  for (const page of Object.values(state.pageFields || {})) {
    for (const [key, f] of Object.entries(page)) {
      const id = `${key}`;
      if (seen.has(id)) continue;
      // count each page-field occurrence for low confidence
    }
  }
  // Count unique low-confidence across all page fields
  low = 0;
  for (const page of Object.values(state.pageFields || {})) {
    for (const f of Object.values(page)) {
      if (!f.edited && f.confidence === "low") low += 1;
    }
  }

  const vals = (state.job && state.job.validations) || [];
  const failed = vals.filter((v) => v.severity === "error").length;

  $("lowCount").textContent = `${low} low confidence`;
  $("ruleCount").textContent = `${failed} failed rules`;
}

/* ---------- Render ---------- */

function renderAll() {
  renderTabs();
  renderImage();
  renderValidations();
  renderFields();
  applyZoom();
}

function renderTabs() {
  const tabs = $("pageTabs");
  tabs.innerHTML = "";
  const pages = (state.job && state.job.pages) || [];
  const count = Math.max(pages.length, 3);
  for (let i = 1; i <= count; i++) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "page-tab" + (i === state.currentPage ? " active" : "");
    btn.textContent = String(i);
    btn.addEventListener("click", () => {
      state.currentPage = i;
      renderAll();
    });
    tabs.appendChild(btn);
  }
}

function renderImage() {
  const img = $("pageImage");
  if (!state.jobId) return;
  img.src = `/api/jobs/${state.jobId}/image/${state.currentPage}?t=${Date.now()}`;
}

function applyZoom() {
  const img = $("pageImage");
  img.style.transform = `scale(${state.zoom})`;
  $("zoomLabel").textContent = `${Math.round(state.zoom * 100)}%`;
}

function renderValidations() {
  const list = $("validationList");
  const vals = (state.job && state.job.validations) || [];
  if (!vals.length) {
    list.innerHTML = `<p class="muted">No issues</p>`;
    return;
  }
  // errors first
  const sorted = [...vals].sort((a, b) => {
    if (a.severity === b.severity) return 0;
    return a.severity === "error" ? -1 : 1;
  });
  list.innerHTML = "";
  for (const v of sorted) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = `val-item ${v.severity}`;
    btn.textContent = v.message;
    btn.title = (v.affected_fields || []).join(", ");
    btn.addEventListener("click", () => {
      const key = (v.affected_fields || [])[0];
      if (!key) return;
      // Switch to page containing field
      const page = findPageForField(key);
      if (page && page !== state.currentPage) {
        state.currentPage = page;
        renderAll();
      }
      requestAnimationFrame(() => {
        const el = document.querySelector(`[data-field-key="${CSS.escape(key)}"]`);
        if (el) {
          el.scrollIntoView({ behavior: "smooth", block: "center" });
          el.classList.add("highlight");
          setTimeout(() => el.classList.remove("highlight"), 1200);
        }
      });
    });
    list.appendChild(btn);
  }
}

function findPageForField(key) {
  const pages = (state.job && state.job.pages) || [];
  for (const p of pages) {
    if (p.schema_fields && p.schema_fields.some((f) => f.key === key)) {
      return p.page_no;
    }
    if (p.fields && key in p.fields) return p.page_no;
  }
  // Prefer current page schema
  const schema = state.job && state.job.schema;
  if (schema) {
    for (const [pn, meta] of Object.entries(schema)) {
      if ((meta.fields || []).some((f) => f.key === key)) return Number(pn);
    }
  }
  return state.currentPage;
}

function renderFields() {
  const root = $("fieldsRoot");
  root.innerHTML = "";

  const page = (state.job.pages || []).find((p) => p.page_no === state.currentPage);
  let schemaFields = (page && page.schema_fields) || [];
  if (!schemaFields.length && state.job.schema) {
    schemaFields = (state.job.schema[String(state.currentPage)] || {}).fields || [];
  }

  const pageData = (state.pageFields && state.pageFields[state.currentPage]) || {};

  // Group by section
  const groups = new Map();
  for (const f of schemaFields) {
    const sec = f.section || "Fields";
    if (!groups.has(sec)) groups.set(sec, []);
    groups.get(sec).push(f);
  }

  for (const [section, fields] of groups) {
    const secEl = document.createElement("div");
    secEl.className = "section";
    secEl.innerHTML = `<h4>${escapeHtml(section)}</h4>`;

    for (const f of fields) {
      const data = pageData[f.key] || { value: null, confidence: "low", edited: false };
      const row = document.createElement("div");
      const confClass = data.edited ? "edited" : `confidence-${data.confidence || "low"}`;
      row.className = `field-row ${confClass}`;
      row.dataset.fieldKey = f.key;
      row.id = `field-${state.currentPage}-${f.key}`;

      const label = document.createElement("div");
      label.className = "field-label";
      label.innerHTML =
        escapeHtml(f.label) +
        (data.edited ? `<span class="pill-edited">edited</span>` : "");

      const inputWrap = document.createElement("div");
      const input = buildInput(f, data);
      input.addEventListener("change", () => onFieldEdit(f.key, input));
      input.addEventListener("input", () => {
        // mark lightly without rebuilding whole form on every keystroke for text
        if (f.type === "text" || f.type === "digits" || f.type === "number" || f.type === "date") {
          markEdited(f.key, coerceValue(f, input));
          // update pill without full re-render
          if (!label.querySelector(".pill-edited")) {
            label.insertAdjacentHTML("beforeend", `<span class="pill-edited">edited</span>`);
          }
          row.classList.remove("confidence-high", "confidence-medium", "confidence-low");
          row.classList.add("edited");
          updateCounts();
        }
      });
      inputWrap.appendChild(input);

      row.appendChild(label);
      row.appendChild(inputWrap);
      secEl.appendChild(row);
    }
    root.appendChild(secEl);
  }
}

function buildInput(field, data) {
  const val = data.value;
  const display =
    val === null || val === undefined ? "" : String(val);

  if (field.type === "enum" || field.type === "yes_no") {
    const sel = document.createElement("select");
    const empty = document.createElement("option");
    empty.value = "";
    empty.textContent = "—";
    sel.appendChild(empty);
    for (const opt of field.options || []) {
      const o = document.createElement("option");
      o.value = opt;
      o.textContent = opt;
      if (String(val) === String(opt)) o.selected = true;
      sel.appendChild(o);
    }
    // Handle UNCLEAR / MULTIPLE
    if (val === "UNCLEAR" || val === "MULTIPLE") {
      const o = document.createElement("option");
      o.value = String(val);
      o.textContent = String(val);
      o.selected = true;
      sel.appendChild(o);
    }
    return sel;
  }

  if (field.type === "bool") {
    const sel = document.createElement("select");
    for (const [v, label] of [
      ["", "—"],
      ["true", "Yes / Signed"],
      ["false", "No"],
    ]) {
      const o = document.createElement("option");
      o.value = v;
      o.textContent = label;
      if (val === true && v === "true") o.selected = true;
      else if (val === false && v === "false") o.selected = true;
      else if ((val === null || val === undefined) && v === "") o.selected = true;
      sel.appendChild(o);
    }
    if (val === "UNCLEAR" || val === "MULTIPLE") {
      const o = document.createElement("option");
      o.value = String(val);
      o.textContent = String(val);
      o.selected = true;
      sel.appendChild(o);
    }
    return sel;
  }

  const input = document.createElement("input");
  if (field.type === "number") {
    input.type = "number";
    input.step = "any";
  } else if (field.type === "date") {
    input.type = "text";
    input.placeholder = "YYYY-MM-DD";
  } else if (field.type === "digits") {
    input.type = "text";
    input.inputMode = "numeric";
  } else {
    input.type = "text";
  }
  input.value = display;
  return input;
}

function coerceValue(field, input) {
  if (field.type === "bool") {
    if (input.value === "") return null;
    if (input.value === "true") return true;
    if (input.value === "false") return false;
    return input.value;
  }
  if (field.type === "number") {
    if (input.value === "") return null;
    const n = Number(input.value);
    return Number.isNaN(n) ? input.value : n;
  }
  if (field.type === "enum" || field.type === "yes_no") {
    return input.value === "" ? null : input.value;
  }
  return input.value === "" ? null : input.value;
}

function markEdited(key, value) {
  const page = state.currentPage;
  if (!state.pageFields[page]) state.pageFields[page] = {};
  const prev = state.pageFields[page][key] || { confidence: "low" };
  state.pageFields[page][key] = {
    value,
    confidence: prev.confidence,
    edited: true,
  };
}

function onFieldEdit(key, input) {
  const page = (state.job.pages || []).find((p) => p.page_no === state.currentPage);
  const schemaFields = (page && page.schema_fields) || [];
  const field = schemaFields.find((f) => f.key === key) || { type: "text", key };
  markEdited(key, coerceValue(field, input));
  updateCounts();
  // Refresh validation against edited data (client-side soft re-export validate via API optional)
  // Keep local validations from job; user can re-export for fresh validations
}

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

/* ---------- Export / Rerun ---------- */

async function exportJson() {
  if (!state.jobId) return;
  const pages = [];
  for (const p of state.job.pages || []) {
    const pf = state.pageFields[p.page_no] || {};
    const fields = {};
    for (const [k, item] of Object.entries(pf)) {
      fields[k] = {
        value: item.value,
        confidence: item.edited ? "high" : item.confidence,
        edited: !!item.edited,
      };
    }
    pages.push({ page_no: p.page_no, fields });
  }

  // Flat map for convenience
  const flat = {};
  for (const p of pages) {
    for (const [k, item] of Object.entries(p.fields)) {
      flat[k] = item;
    }
  }

  const res = await fetch(`/api/jobs/${state.jobId}/export`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ fields: flat, pages }),
  });
  if (!res.ok) {
    alert("Export failed");
    return;
  }
  const data = await res.json();
  // Update validations from export
  if (state.job) {
    state.job.validations = data.validations || [];
    renderValidations();
    updateCounts();
  }
  const blob = new Blob([JSON.stringify(data, null, 2)], {
    type: "application/json",
  });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = (state.job.filename || "crf").replace(/\.pdf$/i, "") + "_extracted.json";
  a.click();
  URL.revokeObjectURL(a.href);
}

async function rerun() {
  if (!state.jobId) return;
  setOverlayProgress("Re-running…", "Restarting extraction");
  try {
    const res = await fetch(`/api/jobs/${state.jobId}/rerun`, { method: "POST" });
    if (!res.ok) throw new Error(`Rerun failed (${res.status})`);
    startPolling();
  } catch (e) {
    setOverlayError(String(e.message || e));
  }
}

/* ---------- Events ---------- */

$("btnBrowse").addEventListener("click", () => fileInput.click());
fileInput.addEventListener("change", () => {
  if (fileInput.files && fileInput.files[0]) acceptFile(fileInput.files[0]);
  fileInput.value = "";
});

["dragenter", "dragover"].forEach((ev) => {
  dropzone.addEventListener(ev, (e) => {
    e.preventDefault();
    dropzone.classList.add("dragover");
  });
});
["dragleave", "drop"].forEach((ev) => {
  dropzone.addEventListener(ev, (e) => {
    e.preventDefault();
    dropzone.classList.remove("dragover");
  });
});
dropzone.addEventListener("drop", (e) => {
  const file = e.dataTransfer.files && e.dataTransfer.files[0];
  acceptFile(file);
});

$("zoomIn").addEventListener("click", () => {
  state.zoom = Math.min(3, state.zoom + 0.15);
  applyZoom();
});
$("zoomOut").addEventListener("click", () => {
  state.zoom = Math.max(0.4, state.zoom - 0.15);
  applyZoom();
});
$("zoomReset").addEventListener("click", () => {
  state.zoom = 1;
  applyZoom();
});

$("btnExport").addEventListener("click", exportJson);
$("btnRerun").addEventListener("click", rerun);

$("btnRetry").addEventListener("click", () => {
  if (state.lastPdfFile) startExtract(state.lastPdfFile);
  else if (state.jobId) rerun();
  else clearOverlay();
});
$("btnBack").addEventListener("click", () => {
  clearOverlay();
  stopPolling();
  hide(resultView);
  show(dropView);
  hide($("headerMeta"));
  $("btnExport").disabled = true;
  $("btnRerun").disabled = true;
});
