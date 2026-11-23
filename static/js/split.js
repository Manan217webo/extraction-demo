/* Webo Healthtech eSource — Visits in a file (#/split/:id).

   A PDF can hold one visit or a whole case report form. The server reads the
   header of every page and proposes which pages make up which visit; this
   screen shows that proposal and lets a person correct it before anything is
   read (reading costs money, and a wrong split puts values on the wrong visit).

     left   one card per visit: tick to read, rename, see warnings, see how far
            each visit has got once it was sent
     right  every page as a thumbnail, banded in its visit's colour; select
            pages (click, shift-click) and move them to another visit
     foot   what will happen ("8 visits · 62 pages") and the one button

   Every change saves itself on the server as a draft of the split; nothing
   is read until "Read visits" is pressed. Also exports WH.visitStrip, the
   "Visit 3 of 8 · Next visit →" strip shown above a visit's review. */

(function () {
  "use strict";

  const WH = window.WH;
  const { h, ui } = WH;

  /* ------------------------------------------------------------ helpers */

  /* [5,6,7,9] -> "5–7, 9" */
  function ranges(pages) {
    const sorted = [...new Set(pages || [])].sort((a, b) => a - b);
    const out = [];
    let start = null;
    let prev = null;
    sorted.forEach((p) => {
      if (start === null) {
        start = prev = p;
      } else if (p === prev + 1) {
        prev = p;
      } else {
        out.push(start === prev ? `${start}` : `${start}–${prev}`);
        start = prev = p;
      }
    });
    if (start !== null) out.push(start === prev ? `${start}` : `${start}–${prev}`);
    return out.join(", ");
  }

  const pagesText = (pages) => `${pages.length === 1 ? "Page" : "Pages"} ${ranges(pages)}`;

  /* A stable, distinct colour per visit, readable in light and dark themes. */
  function hue(index) {
    return (index * 47 + 205) % 360;
  }

  const IGNORE_REASONS = {
    no_visit_header: "No visit in the header",
    not_a_visit: "Applies to all visits",
    blank_page: "Blank page",
    ignored_by_reviewer: "Set aside by you",
  };

  const FLAG_TEXT = {
    inferred_pages: (f) => `${pagesText(f.pages)} had no readable header and were added here because they follow this visit. Check them.`,
    misread_pages: (f) => `${pagesText(f.pages)} read as another visit, but the pages around ${f.pages.length === 1 ? "it" : "them"} are this visit. Check ${f.pages.length === 1 ? "it" : "them"}.`,
    not_contiguous: (f) => `This visit's pages aren't together: ${pagesText(f.pages)} sit between them and belong elsewhere.`,
    multiple_dates: (f) => `Its pages show more than one visit date (${(f.dates || []).join(", ")}). If these are two visits, move one date's pages into a new visit.`,
  };

  const ISSUE_TEXT = {
    no_protocol: "No protocol number could be read from the pages. Enter it here before reading.",
    no_screening: "No screening number could be read from the pages. Enter it here before reading.",
    protocol_disagree: "Not every page shows the same protocol number.",
    screening_disagree: "Not every page shows the same screening number.",
    no_visits: "No visit names could be read. Select pages and use “New visit” to group them.",
  };

  /* --------------------------------------------------------------- mount */

  function mount(root, ctx) {
    const id = ctx.params.id;
    let view = null;
    let edits = {};
    let selection = new Set();
    let anchor = null;
    let focusVisit = null;
    let saving = 0;
    let saveSeq = 0;
    let saveTimer = null;
    let saveError = null;
    let destroyed = false;
    let poller = null;
    let busy = false;
    let ignoredOpen = false;

    const headSlot = h("div");
    const bannerSlot = h("div", { "aria-live": "polite" });
    const identitySlot = h("div");
    const visitsEl = h("div", { class: "wh-split-visits", role: "list", "aria-label": "Visits in this file" });
    const pagesEl = h("div", { class: "wh-split-pages" });
    const toolbarSlot = h("div", { class: "wh-split-toolbar-slot" });
    const footSlot = h("div", { class: "wh-split-foot-slot" });
    const body = h("div", { class: "wh-split" },
      bannerSlot,
      identitySlot,
      h("div", { class: "wh-split-grid" },
        h("section", { class: "wh-split-col wh-split-col--visits", "aria-label": "Visits" }, visitsEl),
        h("section", { class: "wh-split-col wh-split-col--pages", "aria-label": "Pages" }, toolbarSlot, pagesEl)),
      footSlot);
    root.append(headSlot, body);
    headSlot.appendChild(ui.pageHeader({ eyebrow: "Visits in this file", title: "Loading…" }));

    const canEdit = () => Boolean(view && WH.can.write() && !view.readonly && !(view.lock && !view.lock.mine));

    /* ------------------------------------------------------------ data */

    async function load(background) {
      const task = () => WH.api(`/api/docs/${encodeURIComponent(id)}/split`, { background });
      if (!view) {
        await ui.load(body, {
          skeleton: () => h("div", null, ui.sk.stat(3), ui.sk.cards(4)),
          task,
          label: "this file",
          render: (data) => {
            WH.clear(body).append(bannerSlot, identitySlot,
              h("div", { class: "wh-split-grid" },
                h("section", { class: "wh-split-col wh-split-col--visits", "aria-label": "Visits" }, visitsEl),
                h("section", { class: "wh-split-col wh-split-col--pages", "aria-label": "Pages" }, toolbarSlot, pagesEl)),
              footSlot);
            apply(data, true);
          },
          onError: (error) => {
            if (error.status === 404) {
              WH.clear(body).appendChild(ui.emptyState({
                icon: "layers",
                title: "This file hasn't been checked for visits",
                body: "It may still be in the queue, or it was read as a single visit. Its history shows what happened.",
                action: { label: "Open its history", href: `#/doc/${encodeURIComponent(id)}/history` },
                secondary: { label: "All reviews", href: "#/inbox" },
              }));
              return true;
            }
            return false;
          },
        });
      } else {
        const data = await task();
        // A save in flight owns the screen; its answer is newer than this poll.
        if (!saving && !saveTimer) apply(data, false);
      }
    }

    function apply(data, first) {
      view = data;
      if (first || !saving) edits = JSON.parse(JSON.stringify(data.edits || {}));
      const known = new Set((view.pages || []).map((p) => p.page));
      selection = new Set([...selection].filter((p) => known.has(p)));
      render();
      const active = (view.visits || []).some((v) => v.child && ["processing"].includes(v.child.status));
      if (poller) poller.setInterval(active ? 3000 : 20000);
    }

    function queueSave(delay = 0) {
      clearTimeout(saveTimer);
      saveTimer = setTimeout(save, delay);
      renderFoot();
    }

    async function save() {
      saveTimer = null;
      const seq = ++saveSeq;
      saving += 1;
      renderFoot();
      try {
        const data = await WH.api(`/api/docs/${encodeURIComponent(id)}/split`, { method: "PUT", body: { edits } });
        saveError = null;
        if (seq === saveSeq && !destroyed) {
          view = data;
          render();
        }
      } catch (error) {
        saveError = error.detail || error.message;
        if (error.status === 423) {
          ui.toast({ message: "Someone else is working on this file", detail: saveError, kind: "warn" });
          load(true);
        }
      } finally {
        saving -= 1;
        if (!destroyed) renderFoot();
      }
    }

    function edit(change) {
      change(edits);
      queueSave(0);
    }

    /* ---------------------------------------------------------- render */

    function render() {
      const doc = view.document || {};
      document.title = `${doc.filename || "File"} · Visits · eSource — Webo Healthtech`;
      WH.shell.setCrumbs([WH.shell.section("inbox"), { label: doc.filename || "File" }]);
      const sub = h("span", null,
        WH.fmt.plural(view.page_count || 0, "page"),
        view.protocol && view.protocol.value ? ` · Protocol ${view.protocol.value}` : "",
        view.screening && view.screening.value ? ` · Screening ${view.screening.value}` : "",
        " · ", costNote());
      const actions = [];
      const firstReady = (view.visits || []).find((v) => v.child && ["ready", "needs_input", "in_review"].includes(v.child.status));
      if (firstReady) {
        actions.push(h("a", { class: "btn btn-primary wh-btn", href: `#/review/${encodeURIComponent(firstReady.child.document_id)}` },
          "Review visits", ui.icon("arrow-right", { size: 16 })));
      }
      actions.push(h("a", { class: "btn btn-quiet wh-btn", href: `#/doc/${encodeURIComponent(id)}/history` }, ui.icon("history", { size: 16 }), "History"));
      const focused = headSlot.contains(document.activeElement);
      WH.clear(headSlot).appendChild(ui.pageHeader({ eyebrow: "Visits in this file", title: doc.filename || "File", sub, actions }));
      if (focused) headSlot.querySelector("h1").focus({ preventScroll: true });
      renderBanner();
      // Never rebuild the inputs under someone's cursor while they type.
      if (!identitySlot.contains(document.activeElement)) renderIdentity();
      renderVisits();
      renderToolbar();
      renderPages();
      renderFoot();
    }

    function costNote() {
      const usage = (view.split && view.split.usage) || {};
      if (!usage.calls) return "visits found from the page headers, at no AI cost";
      return `visits found from the page headers (${WH.fmt.plural(usage.calls, "small AI call")})`;
    }

    function renderBanner() {
      WH.clear(bannerSlot);
      const visits = view.visits || [];
      const sent = visits.filter((v) => v.child);
      const lock = view.lock && !view.lock.mine ? view.lock : null;
      if (view.document && view.document.archived) {
        bannerSlot.appendChild(banner("warn", "archive", "This file is archived", `${view.document.archived.by || "Someone"} archived it: ${view.document.archived.reason || ""}`));
      } else if (lock) {
        bannerSlot.appendChild(banner("warn", "lock", `${lock.full_name || lock.username} is working on this file`, "It opens read-only until they close it."));
      } else if (view.readonly || !WH.can.write()) {
        bannerSlot.appendChild(banner("info", "eye", "Read-only", "Your role can see this file's visits but not change them."));
      }
      if (!visits.length) {
        bannerSlot.appendChild(banner("warn", "alert-triangle", "No visits found",
          (view.ignored || []).some((i) => i.reason === "not_a_visit")
            ? "Every page is a form that applies to all visits, so there is nothing to read per visit. If that's wrong, select pages and use “New visit”."
            : "No visit names could be read from the page headers. Select pages and use “New visit” to group them by hand."));
        return;
      }
      if (sent.length) {
        const c = {};
        sent.forEach((v) => (c[v.child.status] = (c[v.child.status] || 0) + 1));
        const parts = [
          c.processing && `${c.processing} being read`,
          c.ready && `${c.ready} ready for review`,
          c.needs_input && `${c.needs_input} need input`,
          c.in_review && `${c.in_review} in review`,
          c.submitted && `${c.submitted} submitted`,
          c.failed && `${c.failed} failed`,
        ].filter(Boolean);
        const when = view.split && view.split.confirmed_at;
        bannerSlot.appendChild(banner(c.failed ? "warn" : "success", c.processing ? "loader" : "check-circle",
          `${WH.fmt.plural(sent.length, "visit")} sent for reading`,
          `${parts.join(" · ") || "Waiting to start"}${when ? ` · sent ${WH.fmt.relative(when)} by ${view.split.confirmed_by || "someone"}` : ""}`,
          c.processing));
        return;
      }
      const pages = visits.reduce((n, v) => n + v.pages.length, 0);
      bannerSlot.appendChild(banner("info", "layers",
        `${WH.fmt.plural(visits.length, "visit")} found on ${WH.fmt.plural(pages, "page")}`,
        "Check each visit's pages, untick any you don't want read, then press “Read visits”. Nothing is read until you do."));
    }

    function banner(tone, icon, title, text, spin) {
      return h("div", { class: ["wh-banner", `wh-banner--${tone}`], role: "status" },
        ui.icon(icon, { size: 20, className: spin ? "wh-spin" : "" }),
        h("div", null, h("strong", null, title), text && h("span", null, text)));
    }

    /* Protocol and screening number: read once for the whole file. */
    function renderIdentity() {
      WH.clear(identitySlot);
      // A number entered by hand answers "couldn't be read" and "pages disagree".
      const entered = {
        no_protocol: edits.protocol_no || (view.protocol || {}).corrected,
        protocol_disagree: edits.protocol_no || (view.protocol || {}).corrected,
        no_screening: edits.screening_no || (view.screening || {}).corrected,
        screening_disagree: edits.screening_no || (view.screening || {}).corrected,
      };
      const issues = (view.issues || []).filter((i) => ISSUE_TEXT[i.code] && i.code !== "no_visits" && !entered[i.code]);
      const card = h("section", { class: "wh-card wh-split-identity", "aria-label": "Patient and study" });
      card.appendChild(identityField("Protocol No.", "protocol_no", view.protocol || {}));
      card.appendChild(identityField("Screening No.", "screening_no", view.screening || {}));
      if (issues.length) {
        card.appendChild(h("ul", { class: "wh-split-issues" }, issues.map((i) =>
          h("li", null, ui.icon("alert-triangle", { size: 14 }), ISSUE_TEXT[i.code],
            i.pages && i.pages.length
              ? h("span", { class: "wh-muted" }, " ", i.pages.slice(0, 6).map((p) => `page ${p.page} reads “${p.value}”`).join(", "), i.pages.length > 6 ? "…" : "")
              : null))));
      }
      identitySlot.appendChild(card);
    }

    function identityField(label, key, vote) {
      const value = edits[key] != null ? edits[key] : vote.value || "";
      const agreement = vote.pages_with_value
        ? vote.agreement >= 1
          ? `Same on all ${WH.fmt.plural(vote.pages_with_value, "page")} that show it`
          : `${Math.round(vote.agreement * 100)}% of ${WH.fmt.plural(vote.pages_with_value, "page")} agree`
        : vote.corrected ? "Entered by hand" : "Not read from the pages";
      const input = ui.input({
        value,
        "aria-label": label,
        disabled: !canEdit() || Boolean((view.visits || []).some((v) => v.child)),
        placeholder: key === "screening_no" ? "e.g. 03013" : "e.g. ICR/26/005",
        inputmode: key === "screening_no" ? "numeric" : null,
        onInput: (event) => {
          const text = event.target.value.trim();
          if (text && text !== (vote.value || "")) edits[key] = text;
          else delete edits[key];
          queueSave(700);
        },
        onChange: () => renderIdentity(),
      });
      const tone = !value ? "danger" : vote.agreement >= 1 || edits[key] ? "ok" : "warn";
      return h("div", { class: ["wh-split-id", `is-${tone}`] },
        h("label", { class: "wh-field-label" }, label),
        input,
        h("span", { class: "wh-split-id-note" },
          ui.icon(tone === "ok" ? "check" : "alert-triangle", { size: 12 }), agreement));
    }

    /* ------------------------------------------------------------ visits */

    function visitIndex() {
      const map = new Map();
      (view.visits || []).forEach((v, i) => map.set(v.id, i));
      return map;
    }

    function renderVisits() {
      const keepFocus = document.activeElement && visitsEl.contains(document.activeElement) && document.activeElement.dataset.focusKey;
      WH.clear(visitsEl);
      const visits = view.visits || [];
      visits.forEach((visit, index) => visitsEl.appendChild(visitCard(visit, index)));
      const ignored = view.ignored || [];
      if (ignored.length) {
        const byReason = {};
        ignored.forEach((i) => (byReason[i.reason] = byReason[i.reason] || []).push(i.page));
        visitsEl.appendChild(h("details", {
          class: "wh-split-ignored",
          open: ignoredOpen,
          onToggle: (event) => (ignoredOpen = event.target.open),
        },
        h("summary", null, ui.icon("minus", { size: 14 }),
          h("span", null, `Not part of any visit · ${WH.fmt.plural(ignored.length, "page")}`),
          h("span", { class: "wh-muted" }, ` (${ranges(ignored.map((i) => i.page))})`)),
        h("p", { class: "wh-muted" }, "These pages aren't read. Select one on the right and move it to a visit if it belongs to one."),
        h("ul", null, Object.entries(byReason).map(([reason, pages]) =>
          h("li", null, h("strong", null, IGNORE_REASONS[reason] || WH.fmt.humanize(reason)), ` — ${pagesText(pages)}`)))));
      }
      if (keepFocus) {
        const again = visitsEl.querySelector(`[data-focus-key="${CSS.escape(keepFocus)}"]`);
        if (again) again.focus({ preventScroll: true });
      }
    }

    function visitCard(visit, index) {
      const child = visit.child;
      const prior = visit.prior;
      const editable = canEdit() && !(child && child.extraction && ["queued", "running"].includes(child.extraction.status));
      const selected = Boolean(visit.selected);
      const color = `hsl(${hue(index)} 62% 48%)`;
      const notes = [];
      (visit.flags || []).forEach((f) => FLAG_TEXT[f.code] && notes.push({ tone: "warn", icon: "alert-triangle", text: FLAG_TEXT[f.code](f) }));
      if (prior && !child) {
        const when = prior.processed_at ? ` ${WH.fmt.relative(prior.processed_at)}` : "";
        notes.push({
          tone: "info",
          icon: "history",
          text: prior.hidden
            ? `This visit was already processed${prior.submitted ? " and submitted" : ""} at another site.`
            : `Already processed${when}${prior.submitted ? " and submitted" : ""}${prior.same_pages ? " from these same pages" : " from different pages"}. It is left unticked so it isn't read twice.`,
          href: prior.document_id && !prior.hidden ? `#/review/${encodeURIComponent(prior.document_id)}` : null,
        });
      }
      const head = h("div", { class: "wh-split-card-head" },
        h("span", { class: "wh-split-swatch", style: { background: color }, "aria-hidden": "true" }),
        child && !(editable && WH.can.write())
          ? null
          : h("input", {
              type: "checkbox",
              class: "wh-split-check",
              checked: selected,
              disabled: !editable,
              "aria-label": `Read ${visit.label}`,
              dataset: { focusKey: `check-${visit.id}` },
              onChange: (event) => edit((e) => {
                e.selected = e.selected || {};
                e.selected[visit.id] = event.target.checked;
              }),
            }),
        h("div", { class: "wh-split-card-titles" },
          h("strong", { class: "wh-split-card-title", title: visit.label }, visit.label),
          h("span", { class: "wh-split-card-sub" },
            pagesText(visit.pages), " · ", WH.fmt.plural(visit.pages.length, "page"),
            visit.renamed ? " · renamed" : "", visit.added ? " · added by you" : "")),
        child ? ui.chip(child.status, { size: "sm" }) : null);
      const actions = h("div", { class: "wh-split-card-actions" });
      if (child && ["ready", "needs_input", "in_review", "submitted"].includes(child.status)) {
        actions.appendChild(h("a", {
          class: ["btn", "wh-btn", "wh-btn-xs", child.status === "ready" ? "btn-primary" : "btn-quiet"],
          href: `#/review/${encodeURIComponent(child.document_id)}`,
        }, child.status === "submitted" ? "Open" : child.status === "needs_input" ? "Fix and review" : "Review", ui.icon("arrow-right", { size: 13 })));
      }
      if (child && child.status === "processing" && child.extraction) {
        actions.appendChild(h("span", { class: "wh-muted wh-split-stage" }, ui.icon("loader", { size: 12, className: "wh-spin" }),
          child.extraction.status === "queued" ? "Waiting to be read" : `Reading · ${child.extraction.stage || "starting"}`));
      }
      if (child && child.status === "failed" && child.extraction && WH.can.write()) {
        actions.appendChild(h("button", {
          type: "button",
          class: "btn btn-quiet wh-btn wh-btn-xs",
          onClick: (event) => retry(child, event.currentTarget),
        }, ui.icon("refresh", { size: 13 }), "Retry"));
      }
      if (editable) {
        actions.appendChild(h("button", {
          type: "button",
          class: "btn btn-quiet wh-btn wh-btn-xs",
          dataset: { focusKey: `rename-${visit.id}` },
          onClick: () => rename(visit),
        }, ui.icon("pen", { size: 13 }), "Rename"));
      }
      actions.appendChild(h("button", {
        type: "button",
        class: "btn btn-quiet wh-btn wh-btn-xs",
        "aria-pressed": String(focusVisit === visit.id),
        onClick: () => {
          focusVisit = focusVisit === visit.id ? null : visit.id;
          renderVisits();
          renderPages();
          if (focusVisit) {
            const first = pagesEl.querySelector(`[data-page="${visit.pages[0]}"]`);
            if (first) first.scrollIntoView({ block: "nearest", behavior: "smooth" });
          }
        },
      }, ui.icon("eye", { size: 13 }), focusVisit === visit.id ? "Show all pages" : "Show its pages"));

      return h("article", {
        class: ["wh-card", "wh-split-card", selected && !child && "is-selected", child && "is-sent", focusVisit === visit.id && "is-focused"],
        role: "listitem",
        style: { "--visit": color },
        dataset: { visit: visit.id },
      },
      head,
      child && child.extraction && child.extraction.error
        ? h("p", { class: "wh-split-note is-warn" }, ui.icon("alert-circle", { size: 13 }), child.extraction.error)
        : null,
      notes.map((n) => h("p", { class: ["wh-split-note", `is-${n.tone}`] }, ui.icon(n.icon, { size: 13 }),
        h("span", null, n.text, n.href ? [" ", h("a", { href: n.href }, "Open it")] : null))),
      actions);
    }

    async function rename(visit) {
      const input = ui.input({ value: visit.label, maxlength: "200", "aria-label": "Visit name" });
      const layer = ui.modal({
        title: "Rename visit",
        size: "sm",
        body: h("div", null,
          ui.field({ label: "Visit name, as printed on the form", control: input,
            hint: "The EDC is asked for this name first, then for its short form (e.g. “Visit 3”)." })),
        actions: [
          { label: "Cancel", kind: "quiet", value: false },
          { id: "ok", label: "Rename", kind: "primary", onClick: () => (input.value.trim() ? input.value.trim() : false) },
        ],
      });
      setTimeout(() => input.select(), 30);
      const label = await layer.result;
      if (!label || label === visit.label) return;
      edit((e) => {
        e.labels = e.labels || {};
        e.labels[visit.id] = label;
      });
    }

    async function retry(child, btn) {
      btn.disabled = true;
      try {
        await WH.api(`/api/extractions/${encodeURIComponent(child.extraction.id)}/retry`, { method: "POST" });
        ui.toast({ message: "Queued to read again", detail: child.filename });
        load(true);
      } catch (error) {
        btn.disabled = false;
        ui.toast({ message: "Couldn't retry this visit", detail: error.detail, kind: "error" });
      }
    }

    /* ------------------------------------------------------------- pages */

    function pageOwner() {
      const owner = new Map();
      (view.visits || []).forEach((v, i) => v.pages.forEach((p) => owner.set(p, { visit: v, index: i })));
      return owner;
    }

    function renderToolbar() {
      WH.clear(toolbarSlot);
      const count = selection.size;
      if (!count || !canEdit()) {
        toolbarSlot.appendChild(h("p", { class: "wh-split-hint wh-muted" },
          canEdit()
            ? "Click a page to select it (Shift-click for a range), then move it to another visit."
            : "Pages are banded in their visit's colour."));
        return;
      }
      const visits = view.visits || [];
      const target = ui.select([
        { value: "", label: "Move to…" },
        ...visits.map((v) => ({ value: v.id, label: v.label })),
        { value: "ignore", label: "Not part of any visit" },
        { value: "__new", label: "+ New visit…" },
      ], { "aria-label": "Move selected pages to", class: "wh-input wh-select wh-split-target" });
      toolbarSlot.appendChild(h("div", { class: "wh-split-toolbar", role: "toolbar", "aria-label": "Selected pages" },
        h("strong", null, `${WH.fmt.plural(count, "page")} selected`),
        h("span", { class: "wh-muted" }, `(${ranges([...selection])})`),
        h("span", { class: "wh-grow" }),
        target,
        h("button", { type: "button", class: "btn btn-primary wh-btn wh-btn-sm", onClick: () => move(target.value) }, "Move"),
        h("button", { type: "button", class: "btn btn-quiet wh-btn wh-btn-sm", onClick: () => {
          selection.clear();
          renderToolbar();
          renderPages();
        } }, "Clear")));
    }

    async function move(target) {
      if (!target) {
        ui.toast({ message: "Pick where the pages should go", kind: "warn" });
        return;
      }
      const pages = [...selection];
      if (target === "__new") {
        const input = ui.input({ placeholder: "e.g. Visit 4 or Unscheduled Visit", maxlength: "200", "aria-label": "Visit name" });
        const layer = ui.modal({
          title: `New visit from ${WH.fmt.plural(pages.length, "page")}`,
          size: "sm",
          body: ui.field({ label: "Visit name, as printed on the form", control: input }),
          actions: [
            { label: "Cancel", kind: "quiet", value: false },
            { id: "ok", label: "Create visit", kind: "primary", onClick: () => (input.value.trim() ? input.value.trim() : false) },
          ],
        });
        setTimeout(() => input.focus(), 30);
        const label = await layer.result;
        if (!label) return;
        const nid = `n${Date.now().toString(36)}`;
        edit((e) => {
          e.new = e.new || [];
          e.new.push({ id: nid, label });
          e.assign = e.assign || {};
          pages.forEach((p) => (e.assign[p] = nid));
        });
      } else {
        edit((e) => {
          e.assign = e.assign || {};
          pages.forEach((p) => (e.assign[p] = target));
        });
      }
      WH.announce(`${WH.fmt.plural(pages.length, "page")} moved`);
      selection.clear();
      renderToolbar();
    }

    function renderPages() {
      const owner = pageOwner();
      const ignored = new Map((view.ignored || []).map((i) => [i.page, i]));
      const reads = new Map((view.pages || []).map((p) => [p.page, p]));
      const flagged = new Map();
      (view.visits || []).forEach((v) => (v.flags || []).forEach((f) => {
        if (f.code === "inferred_pages" || f.code === "misread_pages") (f.pages || []).forEach((p) => flagged.set(p, f.code));
      }));
      const hadFocus = document.activeElement && pagesEl.contains(document.activeElement) && document.activeElement.dataset.page;
      WH.clear(pagesEl);
      const grid = h("ol", { class: "wh-split-thumbs", "aria-label": "Pages" });
      const count = view.page_count || (view.pages || []).length;
      for (let page = 1; page <= count; page += 1) {
        const own = owner.get(page);
        const off = ignored.get(page);
        const read = reads.get(page) || {};
        const dim = focusVisit && (!own || own.visit.id !== focusVisit);
        const color = own ? `hsl(${hue(own.index)} 62% 48%)` : null;
        const label = own ? own.visit.label : IGNORE_REASONS[(off || {}).reason] || "Not part of any visit";
        const picked = selection.has(page);
        const tile = h("li", { class: ["wh-split-thumb", !own && "is-ignored", picked && "is-picked", dim && "is-dim"], style: color ? { "--visit": color } : null },
          h("button", {
            type: "button",
            class: "wh-split-thumb-btn",
            dataset: { page: String(page) },
            "aria-pressed": canEdit() ? String(picked) : null,
            "aria-label": `Page ${page}: ${label}${flagged.get(page) ? " (check this page)" : ""}`,
            title: read.visit_label ? `Header reads: ${read.visit_label}` : "No visit in the header",
            onClick: (event) => pick(page, event),
            onKeydown: (event) => thumbKeys(event, page, count),
          },
          h("img", {
            src: `/api/docs/${encodeURIComponent(id)}/pages/${page}/thumb`,
            alt: "",
            loading: "lazy",
            decoding: "async",
            width: "120",
            height: "155",
          }),
          h("span", { class: "wh-split-thumb-num" }, String(page)),
          flagged.get(page) ? h("span", { class: "wh-split-thumb-flag", title: "Check this page" }, ui.icon("alert-triangle", { size: 12 })) : null,
          picked ? h("span", { class: "wh-split-thumb-tick" }, ui.icon("check", { size: 12 })) : null),
          h("span", { class: "wh-split-thumb-label", title: label }, label));
        grid.appendChild(tile);
      }
      pagesEl.appendChild(grid);
      if (hadFocus) {
        const again = pagesEl.querySelector(`[data-page="${hadFocus}"]`);
        if (again) again.focus({ preventScroll: true });
      }
    }

    function pick(page, event) {
      if (!canEdit()) {
        window.open(`/api/docs/${encodeURIComponent(id)}/file#page=${page}`, "_blank", "noopener");
        return;
      }
      if (event.shiftKey && anchor != null) {
        const [a, b] = [Math.min(anchor, page), Math.max(anchor, page)];
        for (let p = a; p <= b; p += 1) selection.add(p);
      } else if (selection.has(page)) {
        selection.delete(page);
      } else {
        selection.add(page);
      }
      anchor = page;
      renderToolbar();
      renderPages();
    }

    function thumbKeys(event, page, count) {
      const step = { ArrowRight: 1, ArrowLeft: -1, ArrowDown: 4, ArrowUp: -4 }[event.key];
      if (!step) return;
      event.preventDefault();
      const next = Math.min(count, Math.max(1, page + step));
      const el = pagesEl.querySelector(`[data-page="${next}"]`);
      if (el) el.focus();
    }

    /* -------------------------------------------------------------- foot */

    function blockers() {
      const out = [];
      const protocol = edits.protocol_no || (view.protocol || {}).value;
      const screening = edits.screening_no || (view.screening || {}).value;
      if (!protocol) out.push("Enter the protocol number");
      if (!screening) out.push("Enter the screening number");
      else if (!/\d/.test(screening)) out.push("The screening number should contain digits");
      const chosen = toRead();
      if (!chosen.length) out.push((view.visits || []).length ? "Tick at least one visit to read" : "Group pages into a visit first");
      const keys = new Map();
      chosen.forEach((v) => {
        if (keys.has(v.key)) out.push(`“${keys.get(v.key)}” and “${v.label}” are the same visit — merge or rename one`);
        keys.set(v.key, v.label);
      });
      return out;
    }

    const toRead = () => (view.visits || []).filter((v) => v.selected && !(v.child && v.child.extraction && ["queued", "running"].includes(v.child.extraction.status)));

    function renderFoot() {
      if (!view) return;
      WH.clear(footSlot);
      if (!canEdit()) return;
      const chosen = toRead();
      const left = (view.visits || []).filter((v) => !v.child);
      if (!chosen.length && !left.length && (view.visits || []).length) {
        // Everything was sent; nothing to choose. Say so instead of a warning.
        footSlot.appendChild(h("div", { class: "wh-split-foot", role: "region", "aria-label": "Visits" },
          h("div", { class: "wh-split-foot-text" },
            h("strong", null, "Every visit in this file has been sent for reading"),
            h("span", { class: "wh-muted" }, "Open each one from its card when it's ready. To read one again, tick it and give a reason."))));
        return;
      }
      const pages = chosen.reduce((n, v) => n + v.pages.length, 0);
      const again = chosen.filter((v) => v.child || v.prior);
      const problems = blockers();
      const status = saveTimer || saving
        ? h("span", { class: "wh-muted" }, ui.spinner(12), " Saving…")
        : saveError
          ? h("span", { class: "wh-bad" }, ui.icon("alert-circle", { size: 13 }), ` Not saved: ${saveError}`)
          : view.split && view.split.edits_saved_at
            ? h("span", { class: "wh-muted" }, ui.icon("check", { size: 13 }), " Changes saved")
            : null;
      const button = h("button", {
        type: "button",
        class: ["btn", "btn-primary", "wh-btn", busy && "is-busy"],
        disabled: Boolean(problems.length || busy || saving || saveTimer),
        "aria-describedby": "wh-split-why",
        onClick: confirmRead,
      }, ui.icon("play", { size: 16 }), chosen.length ? `Read ${WH.fmt.plural(chosen.length, "visit")}` : "Read visits");
      footSlot.appendChild(h("div", { class: "wh-split-foot", role: "region", "aria-label": "Read the visits" },
        h("div", { class: "wh-split-foot-text" },
          h("strong", null, chosen.length
            ? `${WH.fmt.plural(chosen.length, "visit")} · ${WH.fmt.plural(pages, "page")} will be read`
            : "Nothing selected to read"),
          h("span", { id: "wh-split-why", class: problems.length ? "wh-warn" : "wh-muted" },
            problems.length ? problems[0] : again.length
              ? `${WH.fmt.plural(again.length, "visit")} ${again.length === 1 ? "was" : "were"} read before — you'll be asked why`
              : "Each visit is read on its own and then waits for review. Nothing goes to the EDC without your signature."),
          status),
        button));
    }

    async function confirmRead() {
      const chosen = toRead();
      if (!chosen.length || blockers().length) return;
      const again = chosen.filter((v) => v.child || v.prior);
      const pages = chosen.reduce((n, v) => n + v.pages.length, 0);
      const ok = await ui.confirm({
        title: `Read ${WH.fmt.plural(chosen.length, "visit")}?`,
        message: `${WH.fmt.plural(pages, "page")} will be read. Each visit becomes its own record that you review and sign.`,
        details: h("ul", { class: "wh-split-confirm-list" }, chosen.map((v) =>
          h("li", null, h("strong", null, v.label), h("span", { class: "wh-muted" }, ` · ${pagesText(v.pages)}`),
            v.child || v.prior ? h("span", { class: "wh-warn" }, " · read before") : null))),
        confirmLabel: `Read ${WH.fmt.plural(chosen.length, "visit")}`,
        reason: again.length ? { label: "Why read these visits again?", picks: ["The earlier read was wrong", "The pages were rescanned", "Corrected the visit's pages", "Other"] } : null,
      });
      if (!ok) return;
      busy = true;
      renderFoot();
      try {
        clearTimeout(saveTimer);
        saveTimer = null;
        const data = await WH.api(`/api/docs/${encodeURIComponent(id)}/split/confirm`, {
          method: "POST",
          body: { edits, reason: ok.reason || null },
        });
        const result = data.result || {};
        ui.toast({
          message: `${WH.fmt.plural((result.created || []).length, "visit")} queued for reading`,
          detail: result.busy && result.busy.length ? `Already being read: ${result.busy.join(", ")}` : "You can leave this page; each visit is read in the background.",
          kind: "success",
        });
        view = data;
        edits = JSON.parse(JSON.stringify(data.edits || {}));
        render();
        if (poller) {
          poller.setInterval(3000);
          poller.now();
        }
      } catch (error) {
        ui.toast({ message: "The visits couldn't be sent for reading", detail: error.detail || error.message, kind: "error" });
        load(true);
      } finally {
        busy = false;
        renderFoot();
      }
    }

    load(false).then(() => {
      if (destroyed) return;
      const active = Boolean(view && (view.visits || []).some((v) => v.child && v.child.status === "processing"));
      poller = WH.poll(() => (view ? load(true) : undefined), active ? 3000 : 20000);
    });

    return {
      destroy() {
        destroyed = true;
        clearTimeout(saveTimer);
        if (saveTimer && view) save(); // keep the last change
        if (poller) poller.stop();
      },
    };
  }

  WH.router.add("/split/:id", {
    name: "split",
    nav: "inbox",
    title: () => "Visits in this file",
    crumbs: () => [WH.shell.section("inbox"), { label: "Visits in this file" }],
    mount,
  });

  /* ------------------------------------------------- strip above review

     "Visit 3 of 8 in <file> · pages 28–34 · ← All visits · Next visit →"
     Shown above the review of a visit that came from a larger file. */

  let stripEl = null;

  function hideStrip() {
    if (stripEl) stripEl.remove();
    stripEl = null;
  }

  async function showStrip(doc) {
    hideStrip();
    if (!doc || doc.kind !== "child" || !doc.parent_id) return;
    let data;
    try {
      data = await WH.api(`/api/docs/${encodeURIComponent(doc.parent_id)}/split`, { background: true });
    } catch {
      return;
    }
    const visits = (data.visits || []).filter((v) => v.child);
    const at = visits.findIndex((v) => v.child.document_id === doc.id);
    if (at < 0) return;
    const next = visits.slice(at + 1).concat(visits.slice(0, at))
      .find((v) => ["ready", "needs_input", "in_review"].includes(v.child.status));
    const viewer = document.getElementById("viewer");
    if (!viewer) return;
    hideStrip();
    stripEl = h("nav", { class: "wh-visit-strip", "aria-label": "Visits in this file" },
      ui.icon("layers", { size: 15 }),
      h("span", null,
        h("strong", null, doc.visit_label || doc.visit_name || "Visit"),
        h("span", { class: "wh-muted" }, ` · visit ${at + 1} of ${visits.length} in ${(data.document || {}).filename || "this file"}`),
        doc.source_pages && doc.source_pages.length ? h("span", { class: "wh-muted" }, ` · original ${pagesText(doc.source_pages).toLowerCase()}`) : null),
      h("span", { class: "wh-grow" }),
      h("a", { class: "btn btn-quiet wh-btn wh-btn-xs", href: `#/split/${encodeURIComponent(doc.parent_id)}` }, ui.icon("chevron-left", { size: 13 }), "All visits"),
      next
        ? h("a", { class: "btn btn-quiet wh-btn wh-btn-xs", href: `#/review/${encodeURIComponent(next.child.document_id)}`, title: next.label }, "Next visit", ui.icon("chevron-right", { size: 13 }))
        : null);
    const anchorEl = document.getElementById("tab-banner");
    if (anchorEl && anchorEl.parentNode) anchorEl.parentNode.insertBefore(stripEl, anchorEl);
    else viewer.prepend(stripEl);
  }

  WH.visitStrip = { show: showStrip, hide: hideStrip, ranges };
  WH.bus.on("route", ({ def }) => {
    if (!def || def.name !== "review") hideStrip();
  });
})();
