/* Webo Healthtech — eSource review layer.

   Everything the review screen needs to be a GCP record rather than a form:

   - every change of value carries a reason (reason-for-change popover);
   - the work is kept as server drafts (autosave, Cmd/Ctrl-S, 409 handling),
     mirrored on this device while offline;
   - submitting goes through a pre-flight against the live EDC and an
     e-signature (21 CFR Part 11) instead of a one-click save;
   - a flag rail walks the reviewer through what needs a look.

   Built on window.ReviewCore (static/app.js). Classic script; publishes
   window.EDC. Every string a person or the server wrote goes in via
   textContent. */
(function () {
  "use strict";

  const C = window.ReviewCore;
  if (!C) {
    console.warn("edc.js: ReviewCore is missing — load /static/app.js first");
    return;
  }
  const { state, els, api, toast } = C;

  /* ------------------------------------------------------------ utilities */

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function button(text, className, onClick) {
    const node = el("button", className || "btn btn-quiet", text);
    node.type = "button";
    if (onClick) node.addEventListener("click", onClick);
    return node;
  }

  const blank = (v) => v === null || v === undefined || String(v).trim() === "";
  const sameValue = (a, b) => String(blank(a) ? "" : a).trim() === String(blank(b) ? "" : b).trim();
  const plural = (n, one, many) => `${n} ${n === 1 ? one : many || `${one}s`}`;
  const reduceMotion = () =>
    window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const isMac = /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent || "");

  function relTime(ms) {
    if (!ms) return "";
    const seconds = Math.max(0, Math.round((Date.now() - ms) / 1000));
    if (seconds < 5) return "just now";
    if (seconds < 60) return `${seconds}s ago`;
    const minutes = Math.round(seconds / 60);
    if (minutes < 60) return `${minutes} min ago`;
    const hours = Math.round(minutes / 60);
    if (hours < 24) return `${hours} h ago`;
    return C.formatDate(new Date(ms).toISOString().slice(0, 10));
  }

  function utcStamp(iso) {
    const d = new Date(iso || Date.now());
    if (Number.isNaN(d.getTime())) return String(iso || "");
    const iso8601 = d.toISOString();
    return `${C.formatDate(iso8601.slice(0, 10))} ${iso8601.slice(11, 16)} UTC`;
  }

  function shown(value) {
    return blank(value) ? "" : String(value);
  }

  function valueNode(value, className) {
    const node = el("span", `rv-val ${className || ""}`.trim());
    if (blank(value)) {
      node.classList.add("is-blank");
      node.textContent = "empty";
    } else {
      node.textContent = String(value);
    }
    return node;
  }

  function skeletonLines(widths, className) {
    const wrap = el("div", `rv-skel ${className || ""}`.trim());
    wrap.setAttribute("aria-hidden", "true");
    widths.forEach((w) => {
      const line = el("span", "rv-sk");
      line.style.width = `${w}%`;
      wrap.appendChild(line);
    });
    return wrap;
  }

  function shake(node) {
    node.classList.remove("rv-shake");
    void node.offsetWidth;
    node.classList.add("rv-shake");
  }

  function storageGet(key) {
    try { return localStorage.getItem(key); } catch { return null; }
  }
  function storageSet(key, value) {
    try { localStorage.setItem(key, value); } catch { /* per-viewer convenience only */ }
  }

  /* ----------------------------------------------------------------- meta */

  const FALLBACK_REASONS = [
    { code: "AI_MISREAD", label: "AI misread the source" },
    { code: "TRANSCRIPTION_ERROR", label: "Transcription error" },
    { code: "CORRECTED_PER_SOURCE", label: "Corrected per source" },
    { code: "ILLEGIBLE_SOURCE", label: "Illegible source" },
    { code: "ALTERNATE_AI_READ", label: "Used the other AI read" },
    { code: "UNIT_CONVERSION", label: "Unit conversion" },
    { code: "QUERY_RESPONSE", label: "Query response" },
    { code: "VALUE_REMOVED", label: "Value removed" },
    { code: "OTHER", label: "Other" },
  ];
  const FALLBACK_MEANINGS = [
    { code: "REVIEWED_ACCURATE", label: "I confirm these values were verified against the source document" },
    { code: "AUTHOR", label: "I am the author of these entries" },
  ];
  const SHORT_MEANING = { REVIEWED_ACCURATE: "Reviewed — accurate", AUTHOR: "Author" };

  const meta = { reasons: FALLBACK_REASONS, meanings: FALLBACK_MEANINGS, promise: null };

  function loadMeta(force) {
    if (meta.promise && !force) return meta.promise;
    meta.promise = api("/api/meta")
      .then((data) => {
        if (Array.isArray(data.reasons) && data.reasons.length) meta.reasons = data.reasons;
        if (Array.isArray(data.meanings) && data.meanings.length) meta.meanings = data.meanings;
        if (data.user && data.user.username) {
          state.user = { ...(state.user || {}), ...data.user };
        }
        return meta;
      })
      .catch(() => {
        meta.promise = null; // try again next time; the fallbacks carry on meanwhile
        return meta;
      });
    return meta.promise;
  }

  function reasonByCode(code) {
    return meta.reasons.find((r) => r.code === code) || null;
  }

  function reasonLabel(edit) {
    if (!edit || !edit.reason_code) return "Reason needed";
    if (edit.reason_code === "OTHER") return edit.reason_text ? `Other: ${edit.reason_text}` : "Other";
    const reason = reasonByCode(edit.reason_code);
    return reason ? reason.label : edit.reason_code.replace(/_/g, " ").toLowerCase();
  }

  function meaningShort(code) {
    if (SHORT_MEANING[code]) return SHORT_MEANING[code];
    // The server records the meaning's full wording on the signature; map it
    // back to its short name, or show the wording exactly as signed.
    const m = meta.meanings.find((x) => x.code === code || x.label === code);
    if (m && SHORT_MEANING[m.code]) return SHORT_MEANING[m.code];
    if (m) return m.label;
    return /^[A-Z_]+$/.test(String(code || "")) ? String(code).replace(/_/g, " ").toLowerCase() : String(code || "");
  }

  /* -------------------------------------------------------------- edits */

  function lastReasonKey() {
    return `wh.lastReason:${state.username || ""}`;
  }

  function lastReason() {
    try {
      const saved = JSON.parse(storageGet(lastReasonKey()) || "null");
      if (saved && saved.code && reasonByCode(saved.code)) return saved;
    } catch { /* fall through */ }
    const preferred = reasonByCode("AI_MISREAD")
      || meta.reasons.find((r) => !["ALTERNATE_AI_READ", "OTHER", "VALUE_REMOVED"].includes(r.code));
    return { code: preferred ? preferred.code : "OTHER", text: "" };
  }

  function rememberReason(code, text) {
    if (code === "ALTERNATE_AI_READ" || code === "VALUE_REMOVED") return;
    storageSet(lastReasonKey(), JSON.stringify({ code, text: code === "OTHER" ? text : "" }));
  }

  /* The reason a reviewer would most likely give, used for preselection and
     for Esc/blur. "Other" without text is never a default. */
  function suggestedReason(edit) {
    if (edit.kind === "alternate") return { code: "ALTERNATE_AI_READ", text: "" };
    if (blank(edit.new) && reasonByCode("VALUE_REMOVED")) return { code: "VALUE_REMOVED", text: "" };
    const last = lastReason();
    if (last.code === "OTHER" && !last.text) {
      const fallback = reasonByCode("AI_MISREAD") ? "AI_MISREAD" : meta.reasons[0].code;
      return { code: fallback, text: "" };
    }
    return last;
  }

  function editFor(key) {
    return (state.edits || []).find((e) => e.key === key) || null;
  }

  /* A field's label with its row, so "Result" reads as "Haemoglobin · Result". */
  function contextLabel(field) {
    const row = C.rowFor(field.key);
    const label = field.label || field.key;
    if (!row) return label;
    const tr = row.closest("tr");
    const head = tr && tr.querySelector("th.row-head");
    if (head && head.textContent.trim()) return `${head.textContent.trim()} · ${label}`;
    const block = row.closest(".crf-instance");
    const h5 = block && block.querySelector("h5");
    if (h5 && h5.textContent.trim()) return `${h5.textContent.trim()} · ${label}`;
    return label;
  }

  /* One record per field: `old` is the value before the reviewer first
     touched it, `new` the value now. A field changed back to where it
     started is no longer an edit. Returns the record it replaced (for undo). */
  function recordEdit({ field, old, value, kind = "edit", reason_code = null }) {
    if (!field || !field.key) return null;
    if (!Array.isArray(state.edits)) state.edits = [];
    const index = state.edits.findIndex((e) => e.key === field.key);
    const prior = index >= 0 ? { ...state.edits[index] } : null;
    const original = prior ? prior.old : (old === undefined ? null : old);

    if (sameValue(original, value)) {
      if (index >= 0) state.edits.splice(index, 1);
      if (popover && popover.key === field.key) closePopover("discard");
      afterChange();
      return prior;
    }

    const edit = {
      key: field.key,
      label: contextLabel(field),
      old: original === undefined ? null : original,
      new: value === undefined ? null : value,
      reason_code: null,
      reason_text: "",
      kind,
      at: new Date().toISOString(),
      reason_defaulted: true,
      reason_pending: true,
    };
    if (reason_code) {
      edit.reason_code = reason_code;
      edit.reason_defaulted = false;
      edit.reason_pending = false;
    } else {
      // A further change keeps the reason already given, preselected.
      const keep = prior && prior.kind === kind && !prior.reason_defaulted ? prior : null;
      const pick = keep ? { code: keep.reason_code, text: keep.reason_text } : suggestedReason(edit);
      edit.reason_code = pick.code;
      edit.reason_text = pick.text || "";
    }
    if (index >= 0) state.edits[index] = edit;
    else state.edits.push(edit);
    afterChange();
    return prior;
  }

  function restoreEdit(key, prior) {
    const index = (state.edits || []).findIndex((e) => e.key === key);
    if (index >= 0) state.edits.splice(index, 1);
    if (prior) {
      if (index >= 0) state.edits.splice(index, 0, prior);
      else state.edits.push(prior);
    }
    afterChange();
  }

  function unexplained() {
    return (state.edits || []).filter((e) => e.kind !== "alternate" && (e.reason_defaulted || e.reason_pending));
  }

  /* What the server gets: the contract's Edit shape and nothing else. */
  function cleanEdits() {
    return (state.edits || []).map((e) => ({
      key: e.key,
      label: e.label,
      old: e.old === undefined ? null : e.old,
      new: e.new === undefined ? null : e.new,
      reason_code: e.reason_code || "OTHER",
      reason_text: e.reason_code === "OTHER" ? (e.reason_text || "") : (e.reason_text || ""),
      kind: e.kind || "edit",
    }));
  }

  function afterChange() {
    markDirty();
    updateRail();
    // A value changed after a submission has not been sent: open Submit again.
    if (state.edcSaved) {
      state.edcSaved = false;
      C.resetSubmitButton();
    }
  }

  /* --------------------------------------------------- reason popover

     Opens beside the field that just changed, with the last-used reason
     preselected so Enter confirms. Esc, or clicking away, applies that
     default and leaves a dotted underline on the field's reason tag until
     the reviewer confirms one. */

  let popover = null;

  function promptReason(key, { focus = false } = {}) {
    if (state.readOnly) return;
    const edit = editFor(key);
    if (!edit) return;
    if (!focus && (edit.kind === "alternate" || !edit.reason_pending)) return;
    if (popover) closePopover(popover.key === key ? "discard" : "default");
    // Let focus land where the reviewer was going (Tab to the next field)
    // first, so it can be given back after the reason is confirmed.
    setTimeout(() => openPopover(key, focus), 0);
  }

  function openPopover(key, explicit) {
    const edit = editFor(key);
    const row = C.rowFor(key);
    if (!edit || !row || state.readOnly) return;
    if (popover) closePopover("default");

    const active = document.activeElement;
    const returnTo = active && active !== document.body ? active : null;

    const root = el("div", "rv-reason");
    root.setAttribute("role", "dialog");
    root.setAttribute("aria-label", "Reason for change");
    root.tabIndex = -1;

    const head = el("div", "rv-reason-head");
    head.appendChild(el("strong", "", `Why did ${edit.label} change?`));
    const delta = el("div", "rv-reason-delta");
    delta.append(valueNode(edit.old, "is-old"), el("span", "rv-arrow", "→"), valueNode(edit.new, "is-new"));
    head.appendChild(delta);

    const chips = el("div", "rv-chips");
    chips.setAttribute("role", "radiogroup");
    chips.setAttribute("aria-label", "Reason");
    const choices = meta.reasons.filter((r) =>
      r.code !== "ALTERNATE_AI_READ" || edit.kind === "alternate");
    let selected = edit.reason_code || suggestedReason(edit).code;
    if (!choices.some((r) => r.code === selected)) selected = choices[0] && choices[0].code;

    const other = el("input", "rv-other");
    other.type = "text";
    other.placeholder = "Say why (required)";
    other.maxLength = 500;
    other.value = edit.reason_code === "OTHER" ? edit.reason_text || "" : lastReason().text || "";
    other.setAttribute("aria-label", "Reason, in your words");
    const otherError = el("p", "rv-reason-error hidden", "Say why in a few words.");
    otherError.setAttribute("role", "alert");

    const chipButtons = choices.map((reason) => {
      const chip = button(reason.label, "rv-chip");
      chip.dataset.code = reason.code;
      chip.setAttribute("role", "radio");
      chip.addEventListener("click", () => {
        select(reason.code);
        if (reason.code === "OTHER") other.focus();
      });
      chips.appendChild(chip);
      return chip;
    });

    function select(code) {
      selected = code;
      chipButtons.forEach((chip) => {
        const on = chip.dataset.code === code;
        chip.setAttribute("aria-checked", on ? "true" : "false");
        chip.tabIndex = on ? 0 : -1;
        chip.classList.toggle("is-on", on);
      });
      other.classList.toggle("hidden", code !== "OTHER");
      otherError.classList.add("hidden");
      updateApplyAll();
      place();
    }

    const foot = el("div", "rv-reason-foot");
    const applyAll = button("", "rv-link");
    applyAll.addEventListener("click", () => confirm({ all: true }));
    const hint = el("span", "rv-hint");
    const ok = button("Confirm", "btn btn-primary rv-small", () => confirm());
    foot.append(applyAll, hint, ok);

    function updateApplyAll() {
      const others = unexplained().filter((e) => e.key !== key).length;
      applyAll.classList.toggle("hidden", others === 0);
      const label = (reasonByCode(selected) || {}).label || "this reason";
      applyAll.textContent = `Apply “${label}” to all ${others + 1} unexplained edits`;
      hint.textContent = "Enter to confirm · Esc uses the default";
    }

    root.append(head, chips, other, otherError, foot);
    document.body.appendChild(root);

    function confirm({ all = false } = {}) {
      const text = other.value.trim();
      if (selected === "OTHER" && !text) {
        otherError.classList.remove("hidden");
        other.classList.remove("hidden");
        shake(root);
        other.focus();
        return;
      }
      const targets = all ? [edit, ...unexplained().filter((e) => e.key !== key)] : [edit];
      targets.forEach((target) => {
        target.reason_code = selected;
        target.reason_text = selected === "OTHER" ? text : "";
        target.reason_defaulted = false;
        target.reason_pending = false;
      });
      rememberReason(selected, text);
      closePopover("confirmed");
      C.refreshMetaFor(targets.map((t) => t.key));
      markDirty();
      updateRail();
      if (all && targets.length > 1) {
        toast(`Reason applied to ${plural(targets.length, "edit")}`, "success", 2600);
      }
    }

    root.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopPropagation();
        closePopover("default");
        return;
      }
      if (event.key === "Enter" && !event.isComposing) {
        // Enter on a chip button selects-and-confirms; on the link it applies to all.
        if (event.target === applyAll) return;
        event.preventDefault();
        const chip = event.target.closest && event.target.closest(".rv-chip");
        if (chip) select(chip.dataset.code);
        if (chip && chip.dataset.code === "OTHER" && !other.value.trim()) {
          other.focus();
          return;
        }
        confirm();
        return;
      }
      if (["ArrowRight", "ArrowDown", "ArrowLeft", "ArrowUp"].includes(event.key)
          && event.target.classList && event.target.classList.contains("rv-chip")) {
        event.preventDefault();
        const i = chipButtons.findIndex((c) => c.dataset.code === selected);
        const step = event.key === "ArrowRight" || event.key === "ArrowDown" ? 1 : -1;
        const next = chipButtons[(i + step + chipButtons.length) % chipButtons.length];
        select(next.dataset.code);
        next.focus();
        return;
      }
      if (event.key === "Tab") {
        // Keep Tab inside while it is open; Esc or Enter leaves.
        const focusables = [...root.querySelectorAll("button:not(.hidden), input:not(.hidden)")]
          .filter((n) => n.tabIndex !== -1 && n.offsetParent !== null);
        if (!focusables.length) return;
        const first = focusables[0];
        const last = focusables[focusables.length - 1];
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
      }
    });
    // Safari does not focus a clicked button; keep focus inside so a click
    // on a chip is not read as "clicked away".
    root.addEventListener("mousedown", (event) => {
      if (!event.target.closest("input")) event.preventDefault();
    });
    root.addEventListener("focusout", (event) => {
      const next = event.relatedTarget;
      if (next && root.contains(next)) return;
      setTimeout(() => {
        if (popover && popover.root === root && !root.contains(document.activeElement)) {
          closePopover("default", { keepFocus: true });
        }
      }, 0);
    });

    function place() {
      const anchor = C.rowFor(key);
      if (!anchor) { closePopover("default", { keepFocus: true }); return; }
      const box = anchor.getBoundingClientRect();
      const pane = els.extractedScroll.getBoundingClientRect();
      if (box.bottom < pane.top || box.top > pane.bottom) {
        root.style.visibility = "hidden";
        return;
      }
      root.style.visibility = "";
      const width = Math.min(380, window.innerWidth - 24);
      root.style.width = `${width}px`;
      const input = anchor.querySelector(".field-input") || anchor;
      const ibox = input.getBoundingClientRect();
      let left = Math.max(12, Math.min(ibox.left, window.innerWidth - width - 12));
      const height = root.offsetHeight;
      let top = box.bottom + 6;
      if (top + height > window.innerHeight - 12) top = Math.max(12, box.top - height - 6);
      root.style.left = `${left}px`;
      root.style.top = `${top}px`;
    }

    const onScroll = () => place();
    els.extractedScroll.addEventListener("scroll", onScroll, { passive: true });
    window.addEventListener("resize", onScroll);

    popover = {
      key, root, returnTo, explicit,
      cleanup() {
        els.extractedScroll.removeEventListener("scroll", onScroll);
        window.removeEventListener("resize", onScroll);
      },
    };
    select(selected);
    place();
    const current = chipButtons.find((c) => c.dataset.code === selected);
    if (selected === "OTHER") other.focus();
    else if (current) current.focus({ preventScroll: true });
  }

  /* how: "confirmed" (already applied), "default" (apply the preselected
     reason, mark it unexplained), "discard" (the edit went away). */
  function closePopover(how, { keepFocus = false } = {}) {
    if (!popover) return;
    const current = popover;
    popover = null;
    current.cleanup();
    current.root.remove();
    const edit = editFor(current.key);
    if (edit && how === "default" && edit.reason_pending) {
      edit.reason_pending = false;
      edit.reason_defaulted = true;
      C.refreshMetaFor([current.key]);
      markDirty();
      updateRail();
    }
    if (!keepFocus) {
      const back = current.returnTo && current.returnTo.isConnected
        ? current.returnTo
        : (C.rowFor(current.key) || {}).querySelector
          ? C.rowFor(current.key).querySelector("input, select, textarea, button")
          : null;
      if (back) back.focus({ preventScroll: true });
    }
  }

  /* --------------------------------------------------------------- drafts */

  const draft = {
    status: "idle", // idle | dirty | saving | saved | offline | error | conflict
    dirty: false,
    timer: null,
    inflight: null,
    again: false,
    savedAt: 0,
    savedBy: null,
    error: "",
  };
  const AUTOSAVE_MS = 2000;

  function canDraft() {
    return Boolean(state.docId && state.payload && !state.readOnly);
  }

  function markDirty() {
    // Before the CRF is mapped there is no draft to save; the edits ride
    // along with the first one, and the device copy holds them meanwhile.
    if (!canDraft()) return;
    draft.dirty = true;
    if (draft.status !== "saving") setPill("dirty");
    schedule(AUTOSAVE_MS);
  }

  function schedule(ms) {
    clearTimeout(draft.timer);
    draft.timer = setTimeout(() => {
      draft.timer = null;
      saveDraft({ autosave: true });
    }, ms);
  }

  function payloadForServer() {
    const out = { ...state.payload };
    delete out.highlights; // redrawn from the form on open
    return out;
  }

  function saveDraft({ autosave = true, quietly = false } = {}) {
    clearTimeout(draft.timer);
    draft.timer = null;
    if (!canDraft()) {
      if (!autosave && !quietly) {
        toast(state.readOnly
          ? "This review is read-only, so there's nothing to save."
          : !state.docId
            ? "This review isn't on the server yet, so it's saved on this device only."
            : "There's nothing to save until the CRF is mapped.", "info", 3600);
      }
      return Promise.resolve(false);
    }
    if (draft.inflight) {
      draft.again = true;
      return draft.inflight;
    }
    if (!draft.dirty && autosave) return Promise.resolve(true);

    const docId = state.docId;
    const body = {
      payload: payloadForServer(),
      base_revision: state.draftRevision || 0,
      edits: cleanEdits(),
      autosave: Boolean(autosave),
    };
    draft.dirty = false;
    setPill("saving");

    draft.inflight = api(`/api/docs/${encodeURIComponent(docId)}/draft`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    })
      .then((res) => {
        if (state.docId !== docId) return true;
        if (res.revision != null) state.draftRevision = res.revision;
        draft.savedAt = Date.parse(res.saved_at) || Date.now();
        draft.savedBy = state.user ? state.user.full_name : null;
        draft.error = "";
        setPill(draft.dirty ? "dirty" : "saved");
        C.saveSession();
        if (!autosave && !quietly) toast("Draft saved", "success", 1800);
        return true;
      })
      .catch((error) => {
        if (state.docId !== docId) return false;
        draft.dirty = true;
        draft.error = error.message;
        C.saveSessionNow(); // the device copy is the fallback, whatever went wrong
        if (error.status === 409) {
          setPill("conflict");
          showConflict(error);
        } else if (error.offline || navigator.onLine === false) {
          setPill("offline");
        } else if (error.status === 422) {
          // Values changed after submission without a reason.
          setPill("error", "A reason is needed");
          toast(error.message, "error", 6000);
          const missing = unexplained()[0];
          if (missing) promptReason(missing.key, { focus: true });
        } else if (error.status === 401) {
          setPill("error", "Sign in to save");
        } else {
          setPill("error");
          if (!autosave && !quietly) toast(error.message, "error", 4200);
          schedule(20000); // a passing server hiccup should not need a click
        }
        return false;
      })
      .finally(() => {
        draft.inflight = null;
        if (draft.again) {
          draft.again = false;
          if (draft.dirty && state.docId === docId) schedule(300);
        }
      });
    return draft.inflight;
  }

  /* Send what is pending now — on the way out of a review. */
  function flush() {
    if (popover) closePopover("default", { keepFocus: true });
    if (draft.dirty && canDraft()) saveDraft({ autosave: true, quietly: true });
  }

  function isDirty() {
    return draft.dirty || Boolean(draft.inflight);
  }

  function hasUnsavedChanges() {
    return isDirty() || ["error", "offline", "conflict"].includes(draft.status);
  }

  /* ------------------------------------------------------------- conflict */

  function showConflict(error) {
    const info = (error.data && typeof error.data.detail === "object" && error.data.detail) || {};
    const modal = openLayer({ kind: "modal", title: "Someone saved a newer version of this draft" });
    const who = info.saved_by ? `${info.saved_by}` : "Another reviewer";
    const when = info.saved_at ? ` ${relTime(Date.parse(info.saved_at))}` : "";
    modal.body.appendChild(el("p", "rv-lede",
      `${who} saved revision ${info.revision != null ? info.revision : "—"}${when}, after the one ` +
      "your changes started from. Your changes are still here and on this device."));
    modal.body.appendChild(el("p", "rv-muted",
      "Reload theirs to see their version (your unsaved changes are dropped), or keep yours " +
      "to save it over theirs as the newest revision. Both are kept in the history."));
    const theirs = button("Reload theirs", "btn btn-quiet", () => {
      modal.close();
      draft.dirty = false;
      setPill("idle");
      const id = state.docId;
      state.docId = null; // forces a fresh open of the same document
      C.openReview(id);
    });
    const mine = button("Keep mine", "btn btn-primary", async () => {
      const done = C.buttonBusy(mine, "Saving…");
      let revision = info.revision;
      if (revision == null) {
        try {
          const res = await api(`/api/docs/${encodeURIComponent(state.docId)}`);
          revision = res.document && res.document.draft_revision;
        } catch { /* fall back to what we have */ }
      }
      if (revision != null) state.draftRevision = revision;
      draft.dirty = true;
      modal.close();
      done();
      saveDraft({ autosave: false });
    });
    modal.foot.append(theirs, mine);
    mine.focus();
  }

  /* --------------------------------------------------------- status pill */

  let pill = null;
  let pillTicker = null;
  let saveDraftBtn = null;
  let signatureLine = null;

  function ensureChrome() {
    if (!pill && els.vbActions) {
      pill = button("", "rv-pill hidden");
      pill.setAttribute("aria-live", "polite");
      pill.addEventListener("click", () => {
        if (draft.status === "error" || draft.status === "offline") saveDraft({ autosave: false });
        else if (draft.status === "conflict") saveDraft({ autosave: false });
        else openHistory();
      });
      els.vbActions.prepend(pill);
    }
    if (!signatureLine && els.resultChips && els.resultChips.parentElement) {
      signatureLine = el("div", "rv-signature hidden");
      els.resultChips.parentElement.appendChild(signatureLine);
    }
    if (!saveDraftBtn && els.sendCronos && els.sendCronos.parentElement) {
      saveDraftBtn = button("Save draft", "btn btn-quiet rv-save-draft", () => saveDraft({ autosave: false }));
      const kbd = el("kbd", "rv-kbd", isMac ? "⌘S" : "Ctrl S");
      saveDraftBtn.appendChild(kbd);
      saveDraftBtn.title = "Save a draft on the server. Changes also save by themselves.";
      els.sendCronos.parentElement.insertBefore(saveDraftBtn, els.sendCronos);
    }
    ensureRail();
  }

  function setPill(status, override) {
    draft.status = status;
    if (!pill) return;
    const visible = Boolean(state.docId) && Boolean(state.result);
    pill.classList.toggle("hidden", !visible);
    pill.className = `rv-pill rv-pill--${status}${visible ? "" : " hidden"}`;
    pill.textContent = "";
    pill.appendChild(el("span", "rv-pill-dot"));
    let text;
    let title = "Show the draft history";
    switch (status) {
      case "dirty": text = "Unsaved changes"; break;
      case "saving": text = "Saving…"; break;
      case "saved": text = `Draft saved · ${relTime(draft.savedAt)}`; break;
      case "offline":
        text = "Offline — saved on this device";
        title = "The server couldn't be reached. Your work is kept in this browser and is sent when you're back online. Click to try now.";
        break;
      case "error":
        text = override || "Draft not saved — retry";
        title = draft.error || "Click to try again";
        break;
      case "conflict":
        text = "Newer draft on the server";
        title = "Someone saved after you. Click to choose which version to keep.";
        break;
      default:
        text = draft.savedAt ? `Draft saved · ${relTime(draft.savedAt)}` : "No changes yet";
    }
    pill.appendChild(el("span", "", text));
    pill.title = title;
    clearInterval(pillTicker);
    if (status === "saved" || (status === "idle" && draft.savedAt)) {
      pillTicker = setInterval(() => {
        if (draft.status === "saved" || draft.status === "idle") setPill(draft.status);
      }, 5000);
    }
    syncButtons();
  }

  function syncButtons() {
    if (!saveDraftBtn) return;
    saveDraftBtn.disabled = !canDraft() || draft.status === "saving";
    saveDraftBtn.classList.toggle("hidden", !state.docId || state.readOnly);
    if (els.sendCronos && !els.sendCronos.classList.contains("is-loading")
        && !els.sendCronos.classList.contains("is-done")) {
      els.sendCronos.disabled = state.readOnly || !state.payload;
    }
  }

  function renderSignature() {
    if (!signatureLine) return;
    const sig = state.signature;
    const sub = state.submitted;
    signatureLine.textContent = "";
    if (!sig && !sub) {
      signatureLine.classList.add("hidden");
      return;
    }
    signatureLine.classList.remove("hidden");
    const icon = el("span", "rv-signature-icon");
    icon.setAttribute("aria-hidden", "true");
    icon.innerHTML = C.STATUS_ICONS.success; // fixed markup, no data
    signatureLine.appendChild(icon);
    const text = sig
      ? `Signed by ${sig.full_name || "—"} · ${meaningShort(sig.meaning)} · ${utcStamp(sig.signed_at)}`
      : `Submitted by ${sub.submitted_by || "—"} · ${utcStamp(sub.submitted_at)}`;
    signatureLine.appendChild(el("span", "", text));
  }

  /* -------------------------------------------------------------- flag rail */

  const rail = { root: null, filter: "flags", counts: {}, chips: {}, pos: null, prev: null, next: null };

  function ensureRail() {
    if (rail.root || !els.formBody) return;
    const root = el("div", "rv-rail");
    root.setAttribute("role", "toolbar");
    root.setAttribute("aria-label", "Fields to check");
    const group = el("div", "rv-rail-filters");
    [
      ["flags", "Flags", "Values the checks flagged"],
      ["conflicts", "Conflicts", "Fields where the two AI reads disagree"],
      ["edits", "Edits", "Values you changed"],
    ].forEach(([id, label, title]) => {
      const chip = button("", "rv-rail-chip");
      chip.dataset.filter = id;
      chip.title = title;
      chip.setAttribute("aria-pressed", "false");
      chip.append(el("span", "rv-rail-label", label), el("b", "rv-rail-count", "0"));
      chip.addEventListener("click", () => {
        rail.filter = id;
        updateRail();
        go(1, { fromStart: true });
      });
      group.appendChild(chip);
      rail.chips[id] = chip;
    });
    const nav = el("div", "rv-rail-nav");
    rail.prev = button("◂", "rv-rail-step", () => go(-1));
    rail.prev.setAttribute("aria-label", "Previous (press [)");
    rail.prev.title = "Previous — [";
    rail.pos = el("span", "rv-rail-pos", "–");
    rail.pos.setAttribute("aria-live", "polite");
    rail.next = button("▸", "rv-rail-step", () => go(1));
    rail.next.setAttribute("aria-label", "Next (press ])");
    rail.next.title = "Next — ]";
    nav.append(rail.prev, rail.pos, rail.next);
    const keys = el("span", "rv-rail-keys");
    keys.append(el("kbd", "", "["), el("kbd", "", "]"), document.createTextNode(" move · "),
      el("kbd", "", isMac ? "⌥↵" : "Alt ↵"), document.createTextNode(" use other read"));
    root.append(group, nav, keys);
    els.formBody.prepend(root);
    rail.root = root;
  }

  function classify() {
    const out = { flags: new Set(), conflicts: new Set(), edits: new Set() };
    const form = state.payload && state.payload.form;
    if (!form) return out;
    const visit = (field) => {
      if (!field || !field.key || field.row_name) return;
      if ((field.issues || []).length) out.flags.add(field.key);
      const r = field.reading || {};
      if (r.reads > 1 && !r.agreed && !r.one_read
          && field.status !== "edited" && field.status !== "manual") out.conflicts.add(field.key);
    };
    (form.sections || []).forEach((section) => {
      (section.fields || []).forEach(visit);
      (section.groups || []).forEach((group) =>
        (group.instances || []).forEach((instance) => (instance.fields || []).forEach(visit)));
    });
    (state.edits || []).forEach((e) => { if (!e.key.startsWith("header.")) out.edits.add(e.key); });
    return out;
  }

  function railKeys() {
    const sets = classify();
    const set = sets[rail.filter] || new Set();
    const rows = [...els.formSections.querySelectorAll(".field[data-key]")];
    return { sets, rows, keys: rows.map((r) => r.dataset.key).filter((k) => set.has(k)) };
  }

  function updateRail() {
    ensureRail();
    if (!rail.root) return;
    let { sets, keys } = railKeys();
    // Start on whatever there is to look at.
    if (!sets[rail.filter].size && rail.filter !== "edits") {
      const better = sets.flags.size ? "flags" : sets.conflicts.size ? "conflicts" : rail.filter;
      if (better !== rail.filter) {
        rail.filter = better;
        keys = railKeys().keys;
      }
    }
    Object.entries(rail.chips).forEach(([id, chip]) => {
      chip.querySelector(".rv-rail-count").textContent = String(sets[id].size);
      chip.setAttribute("aria-pressed", id === rail.filter ? "true" : "false");
      chip.classList.toggle("is-on", id === rail.filter);
      chip.classList.toggle("is-zero", sets[id].size === 0);
    });
    const index = keys.indexOf(state.selectedKey);
    const total = keys.length;
    rail.pos.textContent = total ? `${index >= 0 ? index + 1 : "–"} / ${total}` : "None";
    rail.prev.disabled = !total;
    rail.next.disabled = !total;
    rail.root.classList.toggle("is-clear", !sets.flags.size && !sets.conflicts.size);
  }

  function go(direction, { fromStart = false } = {}) {
    if (!state.payload) return;
    const { rows, keys } = railKeys();
    if (!keys.length) {
      toast(rail.filter === "edits" ? "No edits yet" : `No ${rail.filter} left to check`, "info", 1800);
      return;
    }
    const order = rows.map((r) => r.dataset.key);
    const here = fromStart ? -1 : order.indexOf(state.selectedKey);
    let target;
    if (direction > 0) {
      target = keys.find((k) => order.indexOf(k) > here) || keys[0];
    } else {
      const before = keys.filter((k) => order.indexOf(k) < (here < 0 ? order.length : here));
      target = before.length ? before[before.length - 1] : keys[keys.length - 1];
    }
    jumpTo(target);
  }

  /* Scroll the form to the field, select it, and pulse its box on the page. */
  function jumpTo(key) {
    C.selectField(key, "pdf");
    if (state.pdf) state.pdf.focus(key, { scroll: true });
    const row = C.rowFor(key);
    const control = row && row.querySelector("input:not([type=hidden]), select, textarea");
    if (control) control.focus({ preventScroll: true });
    updateRail();
  }

  /* ---------------------------------------------------- layers (sheet/modal) */

  const layers = [];

  function openLayer({ kind = "sheet", title = "", subtitle = "", onClose = null, className = "" }) {
    const returnTo = document.activeElement;
    const scrim = el("div", `rv-scrim rv-scrim--${kind}`);
    const panel = el("div", `rv-${kind} ${className}`.trim());
    panel.setAttribute("role", "dialog");
    panel.setAttribute("aria-modal", "true");
    const id = `rv-l-${Math.random().toString(36).slice(2, 8)}`;
    const head = el("div", `rv-${kind}-head`);
    const titles = el("div", "rv-titles");
    const h = el("h2", "", title);
    h.id = id;
    panel.setAttribute("aria-labelledby", id);
    titles.appendChild(h);
    const sub = el("p", "rv-sub", subtitle);
    if (!subtitle) sub.classList.add("hidden");
    titles.appendChild(sub);
    const x = button("", "rv-x");
    x.setAttribute("aria-label", "Close");
    x.innerHTML = '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" aria-hidden="true"><path d="M6 6l12 12M18 6 6 18" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>';
    head.append(titles, x);
    const body = el("div", `rv-${kind}-body`);
    const foot = el("div", `rv-${kind}-foot`);
    panel.append(head, body, foot);
    scrim.appendChild(panel);
    document.body.appendChild(scrim);
    requestAnimationFrame(() => scrim.classList.add("is-open"));

    const layer = {
      kind, scrim, panel, body, foot, head, title: h, sub,
      closed: false,
      close() {
        if (layer.closed) return;
        layer.closed = true;
        const i = layers.indexOf(layer);
        if (i >= 0) layers.splice(i, 1);
        scrim.classList.remove("is-open");
        scrim.classList.add("is-closing");
        setTimeout(() => scrim.remove(), reduceMotion() ? 0 : 220);
        if (onClose) onClose();
        if (returnTo && returnTo.isConnected && returnTo.focus) returnTo.focus({ preventScroll: true });
      },
      dismissable: true,
    };
    x.addEventListener("click", () => layer.dismissable && layer.close());
    scrim.addEventListener("mousedown", (event) => {
      if (event.target === scrim && layer.dismissable) layer.close();
    });
    panel.addEventListener("keydown", (event) => {
      if (event.key !== "Tab") return;
      const focusables = [...panel.querySelectorAll(
        "button:not([disabled]), input:not([disabled]), select, textarea, summary, [tabindex='0']"
      )].filter((n) => n.offsetParent !== null);
      if (!focusables.length) return;
      const first = focusables[0];
      const last = focusables[focusables.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    });
    layers.push(layer);
    setTimeout(() => { if (!panel.contains(document.activeElement)) x.focus({ preventScroll: true }); }, 30);
    return layer;
  }

  function topLayer() {
    return layers[layers.length - 1] || null;
  }

  function closeAllLayers() {
    [...layers].reverse().forEach((layer) => layer.close());
  }

  /* -------------------------------------------------------- submit: pre-flight */

  let sheet = null;
  let preflight = null;

  function subjectLine() {
    const values = {};
    ((state.header || {}).header || { groups: [] }).groups.forEach((group) =>
      (group.fields || []).forEach((field) => { values[field.field_id] = field.value; }));
    return [values.protocol_no, values.subject_no || values.screening_no, values.visit_name]
      .filter((v) => !blank(v)).join(" · ");
  }

  function openSubmit() {
    if (!state.payload) return;
    if (state.readOnly) {
      toast("This review is read-only.", "info", 2400);
      return;
    }
    if (!state.docId) {
      toast("This review isn't on the server, so it can't be submitted. Open it from the inbox.", "error", 4800);
      return;
    }
    if (popover) closePopover("default", { keepFocus: true });
    if (draft.dirty) saveDraft({ autosave: true, quietly: true });
    if (sheet) sheet.close();
    sheet = openLayer({
      kind: "sheet",
      title: "Submit to EDC",
      subtitle: subjectLine(),
      onClose: () => { sheet = null; },
      className: "rv-submit",
    });
    runPreflight();
  }

  function preflightSkeleton() {
    const wrap = el("div", "rv-pf-skel");
    wrap.setAttribute("aria-hidden", "true");
    wrap.appendChild(skeletonLines([46], "rv-sk-title"));
    const table = el("div", "rv-sk-table");
    for (let i = 0; i < 6; i += 1) table.appendChild(skeletonLines([30, 22, 22, 18], "rv-sk-row"));
    wrap.appendChild(table);
    wrap.appendChild(skeletonLines([38], "rv-sk-title"));
    return wrap;
  }

  async function runPreflight(notice) {
    if (!sheet) return;
    const layer = sheet;
    layer.body.textContent = "";
    layer.foot.textContent = "";
    const live = el("p", "rv-sr", "Checking the EDC…");
    live.setAttribute("role", "status");
    layer.body.append(live, preflightSkeleton());
    const cancel = button("Cancel", "btn btn-quiet", () => layer.close());
    const cont = button("Continue to sign", "btn btn-primary");
    cont.disabled = true;
    cont.classList.add("is-loading");
    cont.textContent = "Checking the EDC…";
    layer.foot.append(el("span", "rv-foot-note"), cancel, cont);

    let data;
    try {
      data = await api(`/api/docs/${encodeURIComponent(state.docId)}/preflight`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ payload: payloadForServer() }),
      });
    } catch (error) {
      if (layer.closed) return;
      layer.body.textContent = "";
      layer.body.appendChild(C.statusNode({
        kind: "error",
        title: error.status === 502 ? "The EDC couldn't be reached" : "The check didn't finish",
        detail: error.message,
        action: { label: "Try again", onClick: () => runPreflight() },
      }));
      cont.classList.remove("is-loading");
      cont.textContent = "Continue to sign";
      return;
    }
    if (layer.closed) return;
    preflight = {
      changes: data.changes || [],
      skipped: data.skipped || [],
      warnings: data.warnings || [],
      blocked: data.blocked || [],
      diff_sha: data.diff_sha || null,
    };
    renderPreflight(layer, preflight, notice, cont);
  }

  /* The edit (and so the reason) behind a change the EDC will receive. */
  function editForChange(change) {
    const edits = state.edits || [];
    const byValue = edits.filter((e) => sameValue(e.new, change.new));
    const exact = byValue.find((e) => {
      const field = C.fieldByKey(e.key);
      return e.label === change.label || (field && field.label === change.label)
        || String(e.label || "").endsWith(` · ${change.label}`);
    });
    return exact || (byValue.length === 1 ? byValue[0] : null);
  }

  /* Map changes back to field keys, for "Go to" and the Sent marks. */
  function keysForChanges(changes) {
    const used = new Set();
    const sections = ((state.payload && state.payload.form) || {}).sections || [];
    return changes.map((change) => {
      const edit = editForChange(change);
      if (edit && !used.has(edit.key)) { used.add(edit.key); return edit.key; }
      const crf = String(change.crfName || "").toLowerCase();
      const candidates = [];
      sections.forEach((section) => {
        const inCrf = crf && [section.name, section.section_id, section.crfName]
          .some((n) => String(n || "").toLowerCase() === crf);
        const take = (field) => {
          if (!field || !field.key || used.has(field.key)) return;
          if (field.label !== change.label && field.field_id !== change.fieldName) return;
          if (!sameValue(field.value, change.new)) return;
          candidates.push({ key: field.key, score: inCrf ? 0 : 1 });
        };
        (section.fields || []).forEach(take);
        (section.groups || []).forEach((g) =>
          (g.instances || []).forEach((i) => (i.fields || []).forEach(take)));
      });
      candidates.sort((a, b) => a.score - b.score);
      const hit = candidates[0];
      if (hit) used.add(hit.key);
      return hit ? hit.key : null;
    });
  }

  function goToField(key) {
    if (!key) return;
    closeAllLayers();
    setTimeout(() => jumpTo(key), reduceMotion() ? 0 : 120);
  }

  function renderPreflight(layer, pf, notice, cont) {
    layer.body.textContent = "";
    if (notice) layer.body.appendChild(C.statusNode({ kind: "warn", title: notice }));

    const n = pf.changes.length;
    const m = pf.skipped.length;

    if (pf.blocked.length) {
      const box = el("section", "rv-pf-block");
      box.appendChild(C.statusNode({
        kind: "error",
        title: `${plural(pf.blocked.length, "field")} must be fixed before this can be submitted`,
      }));
      const list = el("ul", "rv-pf-list");
      pf.blocked.forEach((item) => {
        const li = el("li");
        const text = el("div");
        text.append(el("strong", "", item.label || item.key), el("span", "rv-muted", item.reason || ""));
        li.append(text, button("Go to field", "btn btn-quiet rv-small", () => goToField(item.key)));
        list.appendChild(li);
      });
      box.appendChild(list);
      layer.body.appendChild(box);
    }

    pf.warnings.forEach((warning) => layer.body.appendChild(C.statusNode({ kind: "warn", title: String(warning) })));

    const keys = keysForChanges(pf.changes);
    pf.keys = keys;

    if (!n) {
      const empty = el("div", "rv-pf-empty");
      const icon = el("span", "rv-pf-empty-icon");
      icon.innerHTML = C.STATUS_ICONS.success;
      empty.append(icon,
        el("h3", "", "Everything already matches the EDC — nothing to send"),
        el("p", "rv-muted", m
          ? `All ${plural(m, "value")} on this form are already in the EDC. You can sign the review as done.`
          : "There are no values to send. You can sign the review as done."));
      layer.body.appendChild(empty);
    } else {
      const section = el("section", "rv-pf-send");
      section.appendChild(el("h3", "rv-pf-h", `Will be sent (${n})`));
      const scroller = el("div", "rv-table-wrap");
      const table = el("table", "rv-table");
      const thead = el("thead");
      const hr = el("tr");
      ["Field", "In the EDC now", "New", "Reason"].forEach((t) => hr.appendChild(el("th", "", t)));
      thead.appendChild(hr);
      const tbody = el("tbody");
      pf.changes.forEach((change, i) => {
        const tr = el("tr");
        const name = el("td", "rv-pf-field");
        const label = keys[i]
          ? button(change.label || change.fieldName || "—", "rv-link", () => goToField(keys[i]))
          : el("span", "", change.label || change.fieldName || "—");
        if (keys[i]) label.title = "Go to this field";
        name.append(label, el("small", "rv-muted", change.crfName || ""));
        const before = el("td");
        before.appendChild(valueNode(change.old, "is-old"));
        const after = el("td");
        after.appendChild(valueNode(change.new, "is-new"));
        const reason = el("td", "rv-pf-reason");
        const edit = editForChange(change);
        if (edit) {
          const tag = el("span", `rv-reason-chip${edit.reason_defaulted ? " is-default" : ""}`, reasonLabel(edit));
          if (edit.reason_defaulted) tag.title = "Applied by default";
          reason.appendChild(tag);
        } else {
          reason.appendChild(el("span", "rv-muted", "As read from the document"));
        }
        tr.append(name, before, after, reason);
        tbody.appendChild(tr);
      });
      table.append(thead, tbody);
      scroller.appendChild(table);
      section.appendChild(scroller);
      layer.body.appendChild(section);
    }

    if (m) {
      const details = el("details", "rv-pf-skipped");
      details.appendChild(el("summary", "", `Already in the EDC — not sent (${m})`));
      const list = el("ul", "rv-pf-plain");
      pf.skipped.forEach((item) => {
        const li = el("li");
        li.append(el("span", "rv-muted", item.crfName ? `${item.crfName} · ` : ""),
          el("span", "", item.label || ""), document.createTextNode(" "), valueNode(item.value));
        list.appendChild(li);
      });
      details.appendChild(list);
      layer.body.appendChild(details);
    }

    const pending = unexplained().length;
    if (n && pending) {
      const tip = el("p", "rv-pf-tip");
      tip.append(document.createTextNode(
        `${plural(pending, "edit")} ${pending === 1 ? "has" : "have"} a default reason. `));
      const review = button("Review reasons", "rv-link", () => {
        const first = unexplained()[0];
        closeAllLayers();
        setTimeout(() => { jumpTo(first.key); promptReason(first.key, { focus: true }); }, 140);
      });
      tip.appendChild(review);
      layer.body.appendChild(tip);
    }

    const note = layer.foot.querySelector(".rv-foot-note");
    if (note) note.textContent = n ? `${plural(n, "change")} · ${m} already match` : "";
    cont.classList.remove("is-loading");
    cont.disabled = pf.blocked.length > 0;
    cont.textContent = n ? "Continue to sign" : "Mark as reviewed";
    const fresh = cont.cloneNode(true); // drop any earlier handler
    cont.replaceWith(fresh);
    fresh.addEventListener("click", () => openSign(n ? "send" : "review"));
    if (!fresh.disabled) fresh.focus({ preventScroll: true });
  }

  /* ------------------------------------------------------- e-signature */

  function openSign(mode) {
    if (!preflight) return;
    const n = preflight.changes.length;
    const user = state.user || { username: state.username, full_name: state.username };
    const modal = openLayer({
      kind: "modal",
      title: mode === "send" ? "Sign and submit" : "Sign the review",
      className: "rv-sign",
    });
    const form = el("form", "rv-sign-form");
    form.noValidate = true;
    form.autocomplete = "off";

    form.appendChild(el("p", "rv-sign-statement",
      `I, ${user.full_name || user.username} (${user.username}), sign this record.`));

    const fieldset = el("fieldset", "rv-meanings");
    fieldset.appendChild(el("legend", "", "Meaning of this signature"));
    meta.meanings.forEach((meaning, i) => {
      const label = el("label", "rv-meaning");
      const radio = el("input");
      radio.type = "radio";
      radio.name = "rv-meaning";
      radio.value = meaning.code;
      radio.checked = meaning.code === "REVIEWED_ACCURATE" || (i === 0 && !meta.meanings.some((x) => x.code === "REVIEWED_ACCURATE"));
      const text = el("span");
      text.append(el("b", "", meaningShort(meaning.code)), el("small", "", meaning.label));
      label.append(radio, text);
      fieldset.appendChild(label);
    });
    form.appendChild(fieldset);

    const pwLabel = el("label", "rv-pw");
    pwLabel.appendChild(el("span", "", "Password"));
    const pw = el("input");
    pw.type = "password";
    pw.autocomplete = "current-password";
    pw.required = true;
    pwLabel.appendChild(pw);
    const caps = el("p", "rv-caps hidden", "Caps Lock is on");
    caps.setAttribute("role", "status");
    pwLabel.appendChild(caps);
    form.appendChild(pwLabel);

    const error = el("p", "rv-sign-error hidden");
    error.setAttribute("role", "alert");
    form.appendChild(error);

    form.appendChild(el("p", "rv-part11",
      "This is an electronic signature under 21 CFR Part 11. It is the legally binding " +
      "equivalent of your handwritten signature. Your name, the date and time (UTC) and the " +
      "meaning above are permanently linked to this record and recorded in the audit trail."));
    modal.body.appendChild(form);

    const cancel = button("Cancel", "btn btn-quiet", () => modal.close());
    const go = el("button", "btn btn-primary",
      mode === "send" ? `Sign and submit ${plural(n, "change")}` : "Sign as reviewed");
    go.type = "button";
    go.disabled = true;
    modal.foot.append(cancel, go);
    go.addEventListener("click", (event) => { event.preventDefault(); submit(); });
    form.addEventListener("submit", (event) => { event.preventDefault(); submit(); });

    const sync = () => { go.disabled = !pw.value || !form.querySelector("input[name=rv-meaning]:checked"); };
    pw.addEventListener("input", sync);
    fieldset.addEventListener("change", sync);
    const capsCheck = (event) => {
      if (event.getModifierState) caps.classList.toggle("hidden", !event.getModifierState("CapsLock"));
    };
    pw.addEventListener("keydown", capsCheck);
    pw.addEventListener("keyup", capsCheck);
    pw.addEventListener("blur", () => caps.classList.add("hidden"));
    setTimeout(() => pw.focus(), 40);

    let busy = false;
    async function submit() {
      if (busy || go.disabled) return;
      const meaning = (form.querySelector("input[name=rv-meaning]:checked") || {}).value;
      if (!meaning || !pw.value) return;
      busy = true;
      modal.dismissable = false;
      error.classList.add("hidden");
      const label = go.textContent;
      go.disabled = true;
      cancel.disabled = true;
      go.classList.add("is-loading");
      try {
        const crops = mode === "send" ? await C.cutCrops((text) => { go.textContent = text; }) : {};
        go.textContent = mode === "send" ? "Sending to the EDC…" : "Signing…";
        const res = await api(`/api/docs/${encodeURIComponent(state.docId)}/submit`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            payload: payloadForServer(),
            crops,
            password: pw.value,
            meaning,
            diff_sha: preflight.diff_sha,
            edits: cleanEdits(),
          }),
        });
        pw.value = "";
        succeed(modal, res, mode, meaning);
      } catch (err) {
        busy = false;
        modal.dismissable = true;
        go.classList.remove("is-loading");
        go.textContent = label;
        cancel.disabled = false;
        if (err.status === 403) {
          // Wrong signature password. The session stays.
          error.textContent = "That password isn't right.";
          error.classList.remove("hidden");
          shake(modal.panel);
          pw.value = "";
          sync();
          pw.focus();
        } else if (err.status === 409) {
          modal.close();
          runPreflight("The EDC changed since you checked — review again.");
        } else if (err.status === 401) {
          modal.close(); // the session ended; app.js shows sign-in
        } else {
          error.textContent = err.status === 502
            ? "The EDC couldn't be reached. Your signature and draft are kept — try again in a moment."
            : err.status === 423
              ? err.message || "Your account is locked."
              : err.message;
          error.classList.remove("hidden");
          sync();
        }
      }
    }
  }

  function succeed(modal, res, mode, meaning) {
    const sig = res.signature || {
      full_name: (state.user || {}).full_name || state.username,
      meaning,
      signed_at: new Date().toISOString(),
    };
    state.signature = sig;
    state.submitted = { submitted_at: sig.signed_at, submitted_by: (state.user || {}).full_name || state.username };

    const sent = res.status === "sent";
    const changed = sent
      ? ((res.delta && res.delta.changes) || []).length || preflight.changes.length
      : 0;
    const counts = res.counts || {};
    const matched = counts.unchanged != null ? counts.unchanged
      : counts.skipped != null ? counts.skipped : preflight.skipped.length;

    const previous = state.sentKeys || [];
    state.sentKeys = sent ? (preflight.keys || keysForChanges(preflight.changes)).filter(Boolean) : previous;
    // Every edit now has a reason that the record carries.
    (state.edits || []).forEach((e) => { e.reason_pending = false; });

    // The beat of "done": a check that draws itself, and the manifestation.
    modal.body.textContent = "";
    modal.foot.textContent = "";
    modal.title.textContent = sent ? "Signed and submitted" : "Signed";
    const done = el("div", "rv-done");
    done.innerHTML =
      '<svg viewBox="0 0 52 52" width="64" height="64" aria-hidden="true">' +
      '<circle class="rv-done-ring" cx="26" cy="26" r="23" fill="none" stroke="currentColor" stroke-width="3"/>' +
      '<path class="rv-done-tick" d="M15 27l7 7 15-16" fill="none" stroke="currentColor" stroke-width="3.6" stroke-linecap="round" stroke-linejoin="round"/></svg>';
    done.appendChild(el("p", "rv-done-line",
      `Signed by ${sig.full_name || "—"} · ${meaningShort(sig.meaning)} · ${utcStamp(sig.signed_at)}`));
    done.setAttribute("role", "status");
    modal.body.appendChild(done);
    modal.dismissable = true;

    C.markSavedToEdc();
    renderSignature();
    C.refreshMetaFor([...new Set([...previous, ...state.sentKeys])]);
    (res.warnings || []).forEach((w) => C.inlineNotice(els.formSections, String(w)));
    draft.dirty = false;
    draft.savedAt = Date.now();
    setPill("saved");
    C.saveSessionNow();
    updateRail();

    setTimeout(() => {
      closeAllLayers();
      toast(sent
        ? `Sent ${plural(changed, "changed value")}` + (matched ? ` · ${matched} already matched` : "")
        : "Marked as reviewed — nothing needed sending", "success", 4800);
    }, reduceMotion() ? 900 : 1500);
  }

  /* ------------------------------------------------------- draft history */

  async function openHistory() {
    if (!state.docId) return;
    const layer = openLayer({ kind: "sheet", title: "History", subtitle: subjectLine(), className: "rv-history" });
    const close = button("Close", "btn btn-quiet", () => layer.close());
    layer.foot.appendChild(close);
    const skel = el("div", "rv-hist-skel");
    skel.setAttribute("aria-hidden", "true");
    for (let i = 0; i < 5; i += 1) skel.appendChild(skeletonLines([54, 34], "rv-sk-card"));
    layer.body.appendChild(skel);
    let data;
    try {
      data = await api(`/api/docs/${encodeURIComponent(state.docId)}/history`);
    } catch (error) {
      if (layer.closed) return;
      layer.body.textContent = "";
      layer.body.appendChild(C.statusNode({
        kind: "error", title: "History didn't load", detail: error.message,
        action: { label: "Try again", onClick: () => { layer.close(); openHistory(); } },
      }));
      return;
    }
    if (layer.closed) return;
    layer.body.textContent = "";

    const drafts = [...(data.drafts || [])].sort((a, b) => b.revision - a.revision);
    const submissions = [...(data.submissions || [])]
      .sort((a, b) => String(b.submitted_at).localeCompare(String(a.submitted_at)));

    if (drafts.length) {
      const top = el("div", "rv-hist-top");
      const all = button("Everything changed from the AI read", "btn btn-quiet rv-small");
      const slot = el("div", "rv-hist-diff");
      all.addEventListener("click", () => toggleDiff(all, slot, 0, drafts[0].revision));
      top.append(all, slot);
      layer.body.appendChild(top);
    }

    if (submissions.length) {
      layer.body.appendChild(el("h3", "rv-pf-h", "Submissions"));
      const list = el("ol", "rv-hist-list");
      submissions.forEach((s) => {
        const li = el("li", "rv-hist-item is-signed");
        const sig = s.signature || {};
        li.appendChild(el("strong", "", `Signed by ${sig.full_name || s.submitted_by || "—"} · ${meaningShort(sig.meaning)}`));
        const changes = Array.isArray(s.changes) ? s.changes.length : Number(s.changes) || 0;
        const skipped = Array.isArray(s.skipped) ? s.skipped.length : Number(s.skipped) || 0;
        li.appendChild(el("span", "rv-muted",
          `${utcStamp(sig.signed_at || s.submitted_at)} · ${s.status || ""} · ${plural(changes, "change")} sent · ${skipped} already matched`));
        list.appendChild(li);
      });
      layer.body.appendChild(list);
    }

    layer.body.appendChild(el("h3", "rv-pf-h", "Drafts"));
    if (!drafts.length) {
      layer.body.appendChild(el("p", "rv-muted",
        "No drafts yet. Changes save on their own two seconds after you stop typing."));
      return;
    }
    const list = el("ol", "rv-hist-list");
    drafts.forEach((d, i) => {
      const li = el("li", "rv-hist-item");
      const row = el("div", "rv-hist-row");
      const text = el("div");
      const title = el("strong", "", `Revision ${d.revision}`);
      if (d.revision === state.draftRevision) title.appendChild(el("span", "rv-badge", "Current"));
      if (d.autosave) title.appendChild(el("span", "rv-badge is-quiet", "Autosave"));
      text.append(title, el("span", "rv-muted",
        `${d.saved_by || "—"} · ${relTime(Date.parse(d.saved_at))} · ${plural(Number(d.edits) || 0, "edit")}`));
      const prev = drafts[i + 1] ? drafts[i + 1].revision : 0;
      const slot = el("div", "rv-hist-diff");
      const show = button("Show changes", "btn btn-quiet rv-small");
      show.addEventListener("click", () => toggleDiff(show, slot, prev, d.revision));
      row.append(text, show);
      li.append(row, slot);
      list.appendChild(li);
    });
    layer.body.appendChild(list);
  }

  async function toggleDiff(trigger, slot, a, b) {
    if (slot.dataset.open === "1") {
      slot.dataset.open = "";
      slot.textContent = "";
      trigger.setAttribute("aria-expanded", "false");
      return;
    }
    slot.dataset.open = "1";
    trigger.setAttribute("aria-expanded", "true");
    slot.textContent = "";
    slot.appendChild(skeletonLines([70, 55, 62], "rv-sk-diff"));
    try {
      const data = await api(`/api/docs/${encodeURIComponent(state.docId)}/diff?a=${encodeURIComponent(a)}&b=${encodeURIComponent(b)}`);
      if (slot.dataset.open !== "1") return;
      slot.textContent = "";
      const changes = data.changes || [];
      if (!changes.length) {
        slot.appendChild(el("p", "rv-muted", "No values changed."));
        return;
      }
      const table = el("table", "rv-table rv-table-compact");
      const tbody = el("tbody");
      changes.forEach((c) => {
        const tr = el("tr");
        const name = el("td");
        const key = c.key && C.rowFor(c.key) ? c.key : null;
        name.appendChild(key
          ? button(c.label || c.key, "rv-link", () => goToField(key))
          : el("span", "", c.label || c.key || "—"));
        if (c.crf) name.appendChild(el("small", "rv-muted", c.crf));
        const before = el("td");
        before.appendChild(valueNode(c.a, "is-old"));
        const after = el("td");
        after.appendChild(valueNode(c.b, "is-new"));
        tr.append(name, before, after);
        tbody.appendChild(tr);
      });
      table.appendChild(tbody);
      slot.appendChild(table);
    } catch (error) {
      slot.textContent = "";
      slot.appendChild(el("p", "rv-sign-error", error.message));
    }
  }

  /* -------------------------------------------------------- keyboard */

  function isTyping(target) {
    if (!target) return false;
    if (target.isContentEditable) return true;
    const tag = (target.tagName || "").toLowerCase();
    if (tag === "textarea" || tag === "select") return true;
    if (tag !== "input") return false;
    return !["radio", "checkbox", "button", "submit"].includes((target.type || "").toLowerCase());
  }

  function onReviewScreen() {
    return document.body.classList.contains("viewing") && !els.viewer.classList.contains("hidden");
  }

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && layers.length) {
      const top = topLayer();
      if (top && top.dismissable) {
        event.preventDefault();
        top.close();
      }
      return;
    }
    if (!onReviewScreen()) return;

    if ((event.metaKey || event.ctrlKey) && !event.altKey && (event.key === "s" || event.key === "S")) {
      event.preventDefault();
      if (layers.length) return;
      if (popover) closePopover("default", { keepFocus: true });
      if (!draft.dirty && draft.status === "saved") {
        toast("Everything is already saved", "info", 1600);
        return;
      }
      draft.dirty = draft.dirty || canDraft();
      saveDraft({ autosave: false });
      return;
    }
    if (layers.length || popover || state.step !== "form" || !state.payload) return;

    const bracket = event.code === "BracketRight" ? 1 : event.code === "BracketLeft" ? -1 : 0;
    const plain = event.key === "]" ? 1 : event.key === "[" ? -1 : 0;
    // Plain [ and ] while not typing; Alt+[ / Alt+] work from inside a field.
    if ((plain && !event.altKey && !event.metaKey && !event.ctrlKey && !isTyping(event.target))
        || (bracket && event.altKey && !event.metaKey && !event.ctrlKey)) {
      event.preventDefault();
      go(plain || bracket);
      return;
    }
    if (event.key === "Enter" && event.altKey && !event.metaKey && !event.ctrlKey) {
      const key = state.selectedKey;
      const row = key && C.rowFor(key);
      const use = row && row.querySelector(".use-other");
      event.preventDefault();
      if (use) use.click();
      else toast("This field has no other read to use", "info", 1800);
    }
  });

  /* Selecting a field moves the rail's position marker. */
  document.addEventListener("focusin", () => { if (rail.root) setTimeout(updateRail, 0); });
  document.addEventListener("click", (event) => {
    if (rail.root && event.target.closest && event.target.closest(".field")) setTimeout(updateRail, 0);
  });

  /* ----------------------------------------------- lifecycle and guards */

  window.addEventListener("beforeunload", (event) => {
    // Only while a draft is pending or failing; the device copy covers the rest.
    if (!isDirty() && !["error", "offline", "conflict"].includes(draft.status)) return;
    if (draft.timer) saveDraft({ autosave: true, quietly: true });
    event.preventDefault();
    event.returnValue = "";
  });

  window.addEventListener("online", () => {
    if (draft.dirty && (draft.status === "offline" || draft.status === "error")) saveDraft({ autosave: true });
  });
  window.addEventListener("offline", () => {
    if (draft.dirty) setPill("offline");
  });

  window.addEventListener("wh:signed-in", () => {
    loadMeta(true);
    if (draft.dirty && canDraft()) schedule(600);
  });
  window.addEventListener("wh:signed-out", () => {
    if (popover) closePopover("default", { keepFocus: true });
    closeAllLayers();
  });

  /* Only writes extend the session, and a reviewer reading a long form
     writes nothing. Real activity on the review screen keeps it alive —
     at most one ping every two minutes. */
  let lastPing = 0;
  const activity = () => {
    if (!state.username || !onReviewScreen()) return;
    const now = Date.now();
    if (now - lastPing < 120000) return;
    lastPing = now;
    api("/api/session/ping", { method: "POST" }).catch(() => {});
  };
  ["keydown", "pointerdown", "wheel"].forEach((type) =>
    document.addEventListener(type, activity, { passive: true, capture: true }));

  /* ------------------------------------------------- hooks for app.js */

  function reset() {
    clearTimeout(draft.timer);
    clearInterval(pillTicker);
    if (popover) closePopover("discard", { keepFocus: true });
    closeAllLayers();
    Object.assign(draft, {
      status: "idle", dirty: false, timer: null, again: false, savedAt: 0, savedBy: null, error: "",
    });
    // An in-flight save is left to finish; its answer is ignored (docId check).
    preflight = null;
    rail.filter = "flags";
    if (pill) pill.classList.add("hidden");
    renderSignature();
    updateRail();
    syncButtons();
  }

  function reviewLoaded({ draft: serverDraft = null, pending = false } = {}) {
    ensureChrome();
    loadMeta();
    if (serverDraft && serverDraft.saved_at) {
      draft.savedAt = Date.parse(serverDraft.saved_at) || 0;
      draft.savedBy = serverDraft.saved_by || null;
    }
    if (pending && canDraft()) {
      draft.dirty = true;
      setPill(navigator.onLine === false ? "offline" : "dirty");
      schedule(800);
    } else {
      setPill("idle");
    }
    if (state.edcSaved) C.markSavedToEdc();
    renderSignature();
    updateRail();
  }

  function formChanged() {
    ensureChrome();
    if ((state.edits || []).length && !draft.savedAt && !draft.dirty && canDraft()) markDirty();
    updateRail();
    syncButtons();
  }

  function readOnlyChanged(on) {
    if (on && popover) closePopover("default", { keepFocus: true });
    syncButtons();
  }

  window.EDC = {
    // edits and reasons
    recordEdit, restoreEdit, editFor, reasonLabel, promptReason,
    // drafts
    saveDraft, flush, isDirty, hasUnsavedChanges,
    // submit
    openSubmit, openHistory,
    // navigation
    next: () => go(1), previous: () => go(-1), jumpTo,
    // lifecycle
    reset, reviewLoaded, formChanged, readOnlyChanged,
    get meta() { return meta; },
  };

  ensureChrome();
  if (state.username) loadMeta();
  // A review that was already on screen before this file ran.
  if (state.result) reviewLoaded({});
})();
