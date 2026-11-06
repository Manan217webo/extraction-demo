/* Webo Healthtech eSource — Inbox (#/inbox).

   The reviewer's worklist: status tabs with counts, filters that live in the
   hash, J/K + Enter keyboard flow, "Assign to me" with undo, a confidence
   heat bar per document and a "Continue where you left off" card. Polls
   every 10s while the tab is visible, updating rows in place. */

(function () {
  "use strict";

  const WH = window.WH;
  const { h, ui } = WH;
  const PAGE = 50;

  const TABS = [
    { id: "all", label: "All" },
    { id: "ready", label: "Ready", icon: "sparkle" },
    { id: "in_review", label: "In review", icon: "pen" },
    { id: "needs_input", label: "Needs input", icon: "alert-circle" },
    { id: "processing", label: "Processing", icon: "loader" },
    { id: "submitted", label: "Submitted", icon: "check-seal" },
    { id: "failed", label: "Failed", icon: "x-circle" },
  ];
  const FILTER_KEYS = ["q", "protocol", "subject", "assignee", "flagged"];

  function readFilters(query) {
    return {
      status: query.get("status") || "all",
      q: query.get("q") || "",
      protocol: query.get("protocol") || "",
      subject: query.get("subject") || "",
      assignee: query.get("assignee") || "",
      flagged: query.get("flagged") === "1",
      sel: query.get("sel") || "",
    };
  }

  /* Hash filters → API query. "me" becomes the username; "none" is passed
     through for unassigned documents. */
  function apiQuery(f, limit, offset) {
    return {
      status: f.status === "all" ? "" : f.status,
      q: f.q,
      protocol: f.protocol,
      subject: f.subject,
      assignee: f.assignee === "me" ? WH.me && WH.me.username : f.assignee,
      flagged: f.flagged ? "true" : "",
      limit,
      offset,
    };
  }

  const docHref = (doc) =>
    doc.status === "processing" || doc.status === "failed" ? `#/doc/${encodeURIComponent(doc.id)}/history` : `#/review/${encodeURIComponent(doc.id)}`;

  function mount(root, ctx) {
    let f = readFilters(ctx.query);
    let docs = [];
    let total = 0;
    let counts = {};
    let loading = false;
    let token = 0;
    let lastSig = "";
    let continueDismissed = false;
    const seenOptions = { protocol: new Set(), subject: new Set() };

    /* ------------------------------------------------------------ layout */

    const densitySeg = ui.segmented({
      label: "Row density",
      size: "sm",
      value: WH.shell.density(),
      items: [
        { value: "comfortable", icon: "rows", title: "Comfortable rows" },
        { value: "compact", icon: "rows-compact", title: "Compact rows" },
      ],
      onChange: (v) => WH.shell.setDensity(v),
    });
    const offDensity = WH.bus.on("density", (v) => densitySeg.set(v));
    const header = ui.pageHeader({
      eyebrow: "Already in eSource",
      title: "Reviews",
      sub: "Visit forms that have been scanned. Open one to check it, or scan a folder from Sources.",
      actions: [
        densitySeg,
        WH.can.write() && h("a", { class: "btn btn-quiet wh-btn", href: "#/sources" }, ui.icon("folder", { size: 16 }), "Scan sources"),
      ],
    });

    const continueSlot = h("div", { class: "wh-continue-slot" });
    const tabs = ui.tabs({ label: "Document status", items: tabItems(), value: f.status, onChange: (id) => setFilter({ status: id }) });

    const search = ui.searchBox({ value: f.q, placeholder: "Search file, subject, visit…", label: "Search documents", onInput: (q) => setFilter({ q }) });
    const protocolInput = ui.input({ value: f.protocol, placeholder: "Protocol", list: "wh-dl-protocol", "aria-label": "Filter by protocol", class: "wh-input wh-input--sm" });
    const subjectInput = ui.input({ value: f.subject, placeholder: "Subject", list: "wh-dl-subject", "aria-label": "Filter by subject (screening number)", class: "wh-input wh-input--sm" });
    const dlProtocol = h("datalist", { id: "wh-dl-protocol" });
    const dlSubject = h("datalist", { id: "wh-dl-subject" });
    const debouncedText = WH.debounce(() => setFilter({ protocol: protocolInput.value.trim(), subject: subjectInput.value.trim() }), 300);
    protocolInput.addEventListener("input", debouncedText);
    subjectInput.addEventListener("input", debouncedText);

    const assigneeSelect = ui.select(
      [
        { value: "", label: "Anyone" },
        { value: "me", label: "Assigned to me" },
        { value: "none", label: "Unassigned" },
      ],
      { "aria-label": "Filter by assignee", class: "wh-input wh-input--sm wh-select" }
    );
    assigneeSelect.value = f.assignee === "me" || f.assignee === "none" ? f.assignee : "";
    assigneeSelect.addEventListener("change", () => setFilter({ assignee: assigneeSelect.value }));

    const flaggedBtn = h("button", {
      type: "button",
      class: "wh-toggle-chip",
      "aria-pressed": String(f.flagged),
      onClick: () => setFilter({ flagged: !f.flagged }),
    }, ui.icon("flag", { size: 14 }), "Flagged");

    const clearBtn = h("button", { type: "button", class: "link-btn wh-clear-filters", onClick: clearFilters }, "Clear filters");
    const resultLine = h("p", { class: "wh-result-line", "aria-live": "polite" });

    const filterBar = h("div", { class: "wh-filterbar", role: "search", "aria-label": "Filter documents" },
      search, protocolInput, subjectInput, assigneeSelect, flaggedBtn, clearBtn, dlProtocol, dlSubject);

    const listEl = h("div", { class: "wh-inbox-list" });
    const moreSlot = h("div", { class: "wh-more-slot" });

    root.append(header, continueSlot, h("div", { class: "wh-card wh-inbox" }, h("div", { class: "wh-inbox-head" }, tabs, filterBar, resultLine), listEl, moreSlot));

    /* ------------------------------------------------------------ state */

    function anyFilter() {
      return FILTER_KEYS.some((k) => (k === "flagged" ? f.flagged : f[k]));
    }

    function tabItems() {
      const all = TABS.slice(1).reduce((sum, t) => sum + (Number(counts[t.id]) || 0), 0);
      return TABS.map((t) => ({ ...t, count: t.id === "all" ? (Object.keys(counts).length ? all : null) : counts[t.id] != null ? counts[t.id] : null }));
    }

    function setFilter(patch) {
      f = { ...f, ...patch };
      WH.router.setQuery({
        status: f.status === "all" ? "" : f.status,
        q: f.q,
        protocol: f.protocol,
        subject: f.subject,
        assignee: f.assignee,
        flagged: f.flagged ? "1" : "",
        sel: f.sel,
      });
      syncControls();
      load({ reset: true });
    }

    function clearFilters() {
      search.querySelector("input").value = "";
      protocolInput.value = "";
      subjectInput.value = "";
      setFilter({ q: "", protocol: "", subject: "", assignee: "", flagged: false });
    }

    function syncControls() {
      flaggedBtn.setAttribute("aria-pressed", String(f.flagged));
      clearBtn.hidden = !anyFilter();
      tabs.update(tabItems(), f.status);
    }

    /* ------------------------------------------------------------ data */

    async function load({ reset = false, background = false, append = false } = {}) {
      const my = ++token;
      const limit = append ? PAGE : Math.max(PAGE, background ? Math.min(docs.length, 200) : PAGE);
      const offset = append ? docs.length : 0;
      if (reset) lastSig = "";
      const task = () => WH.api("/api/docs", { query: apiQuery(f, limit, offset), background });

      if (background || append) {
        try {
          if (append) moreButtonBusy(true);
          const data = await task();
          if (my !== token) return;
          apply(data, append);
        } catch (error) {
          if (append) ui.toast({ message: "Couldn't load more documents", detail: error.detail, kind: "error" });
        } finally {
          if (append) moreButtonBusy(false);
        }
        return;
      }

      loading = true;
      await ui.load(listEl, {
        skeleton: () => ui.sk.table(8, [12, 30, 11, 11, 11, 12, 6, 8, 9]),
        task,
        label: "the inbox",
        isCurrent: () => my === token,
        render: (data) => apply(data, false),
      });
      loading = false;
    }

    function apply(data, append) {
      const incoming = data.documents || [];
      total = data.total != null ? data.total : incoming.length;
      counts = data.counts || counts;
      if (!anyFilter()) WH.bus.emit("docs:counts", counts);
      docs = append ? docs.concat(incoming.filter((d) => !docs.some((x) => x.id === d.id))) : incoming;
      docs.forEach((d) => {
        if (d.protocol_no) seenOptions.protocol.add(d.protocol_no);
        if (d.screening_no) seenOptions.subject.add(d.screening_no);
      });
      fillDatalist(dlProtocol, seenOptions.protocol);
      fillDatalist(dlSubject, seenOptions.subject);
      const sig = JSON.stringify([docs, counts, total]);
      if (sig === lastSig && listEl.querySelector("table")) return;
      lastSig = sig;
      syncControls();
      renderList();
    }

    function fillDatalist(dl, set) {
      if (dl.children.length === set.size) return;
      WH.clear(dl);
      [...set].sort().forEach((v) => dl.appendChild(h("option", { value: v })));
    }

    /* ------------------------------------------------------------ list */

    let rowsById = new Map();

    function renderList() {
      const focusedId = document.activeElement && document.activeElement.closest && document.activeElement.closest("tr[data-id]")
        ? document.activeElement.closest("tr[data-id]").dataset.id
        : null;
      resultLine.textContent = docs.length
        ? `Showing ${WH.fmt.number(docs.length)} of ${WH.fmt.plural(total, "document")}`
        : "";
      WH.clear(listEl);
      WH.clear(moreSlot);
      rowsById = new Map();

      if (!docs.length) {
        listEl.appendChild(emptyFor());
        return;
      }

      const tbody = h("tbody");
      docs.forEach((doc) => {
        const row = renderRow(doc);
        rowsById.set(String(doc.id), row);
        tbody.appendChild(row);
      });
      const table = h("table", { class: "wh-table wh-inbox-table", "aria-label": "Documents", "aria-rowcount": String(total) },
        h("thead", null, h("tr", null,
          h("th", { scope: "col", class: "wh-col-status" }, "Status"),
          h("th", { scope: "col" }, "Document"),
          h("th", { scope: "col" }, "Subject"),
          h("th", { scope: "col" }, "Visit"),
          h("th", { scope: "col" }, "Protocol"),
          h("th", { scope: "col", class: "wh-col-conf" }, "Confidence"),
          h("th", { scope: "col", class: "wh-col-flags" }, h("span", { class: "wh-sr" }, "Flags and conflicts"), ui.icon("flag", { size: 14 })),
          h("th", { scope: "col" }, "Assignee"),
          h("th", { scope: "col", class: "wh-col-updated" }, "Updated"),
          h("th", { scope: "col", class: "wh-col-actions" }, h("span", { class: "wh-sr" }, "Actions")))),
        tbody);
      listEl.appendChild(table);

      // Keep a selection; default to the first row so J/K work at once.
      if (!f.sel || !rowsById.has(f.sel)) f.sel = "";
      markSelected(f.sel || String(docs[0].id), { focus: false, persist: false });
      if (focusedId && rowsById.has(focusedId)) rowsById.get(focusedId).focus({ preventScroll: true });

      if (docs.length < total) {
        moreSlot.appendChild(h("button", { type: "button", class: "btn btn-quiet wh-btn wh-more-btn", onClick: () => load({ append: true }) },
          `Load ${Math.min(PAGE, total - docs.length)} more`));
      }
    }

    function moreButtonBusy(on) {
      const btn = moreSlot.querySelector("button");
      if (!btn) return;
      btn.disabled = on;
      btn.classList.toggle("is-busy", on);
    }

    function emptyFor() {
      if (anyFilter()) {
        return ui.emptyState({
          icon: "filter",
          title: "No documents match these filters",
          body: "Try a broader search, or clear the filters to see everything.",
          action: { label: "Clear filters", onClick: clearFilters },
        });
      }
      const copy = {
        ready: ["You're all caught up", "Nothing is waiting for review. New reads land here as soon as a batch finishes them."],
        in_review: ["No reviews in progress", "Documents you or a colleague have opened but not submitted appear here."],
        needs_input: ["Nothing needs your input", "When the header or visit can't be determined, the document waits here for you."],
        processing: ["Nothing is being read right now", "Start a scan from Sources to read new documents."],
        submitted: ["Nothing submitted yet", "Signed submissions to the EDC appear here."],
        failed: ["No failures", "Every read finished. Nice."],
        all: ["Nothing has been scanned yet", "Open Sources, pick a folder on the file server, and extract the visit forms inside it."],
      }[f.status] || ["Nothing here", ""];
      return ui.emptyState({
        icon: f.status === "failed" ? "check-circle" : "inbox",
        title: copy[0],
        body: copy[1],
        action: WH.can.write() && (f.status === "all" || f.status === "ready" || f.status === "processing")
          ? { label: "Scan sources", href: "#/sources", icon: "folder" }
          : null,
      });
    }

    function renderRow(doc) {
      const id = String(doc.id);
      const flags = WH.fmt.count(doc.flags);
      const conflicts = WH.fmt.count(doc.conflicts);
      const mine = WH.me && doc.assignee === WH.me.username;
      const stage = doc.status === "processing" && doc.extraction && doc.extraction.stage ? `Reading · ${doc.extraction.stage}` : null;

      const actions = h("div", { class: "wh-row-actions" });
      if (WH.can.write() && !["submitted", "processing"].includes(doc.status)) {
        actions.appendChild(
          h("button", {
            type: "button",
            class: "btn btn-quiet wh-btn wh-btn-xs",
            onClick: (event) => {
              event.stopPropagation();
              mine ? assign(doc, null) : assign(doc, WH.me.username);
            },
          }, mine ? "Release" : "Assign to me")
        );
      }
      actions.appendChild(
        h("a", {
          class: "wh-icon-btn wh-icon-btn--sm",
          href: `#/doc/${encodeURIComponent(id)}/history`,
          "aria-label": `History of ${doc.filename}`,
          title: "History",
          tabindex: "-1",
          onClick: (event) => event.stopPropagation(),
        }, ui.icon("history", { size: 15 }))
      );

      const row = h("tr", {
        class: ["wh-row", flags || conflicts ? "has-flags" : null],
        dataset: { id },
        tabindex: "-1",
        "aria-selected": "false",
        onClick: (event) => {
          if (event.target.closest("a, button")) return;
          markSelected(id, { focus: true });
        },
        onDblclick: (event) => {
          if (event.target.closest("a, button")) return;
          location.hash = docHref(doc);
        },
      },
      h("td", { class: "wh-col-status" }, ui.chip(doc.status, { size: "sm", title: doc.extraction && doc.extraction.error ? doc.extraction.error : null })),
      h("td", { class: "wh-col-doc" },
        h("a", { class: "wh-doc-link", href: docHref(doc), tabindex: "-1" }, doc.filename || `Document ${id}`),
        h("span", { class: "wh-doc-sub" }, stage || doc.path || "")),
      h("td", { class: "wh-mono-num" }, doc.screening_no || h("span", { class: "wh-muted" }, "—")),
      h("td", null, doc.visit_name || h("span", { class: "wh-muted" }, "—")),
      h("td", { class: "wh-mono-num" }, doc.protocol_no || h("span", { class: "wh-muted" }, "—")),
      h("td", { class: "wh-col-conf" }, ui.confidence(doc.confidence)),
      h("td", { class: "wh-col-flags" },
        flags || conflicts
          ? h("span", { class: "wh-flagcount", title: `${flags} flagged, ${conflicts} conflicts` },
              flags ? h("span", { class: "wh-flag" }, ui.icon("flag", { size: 13 }), String(flags)) : null,
              conflicts ? h("span", { class: "wh-conflict" }, ui.icon("compare", { size: 13 }), String(conflicts)) : null,
              h("span", { class: "wh-sr" }, `${flags} flagged fields, ${conflicts} read conflicts`))
          : h("span", { class: "wh-muted", "aria-label": "None" }, "—")),
      h("td", { class: "wh-col-assignee" },
        doc.assignee
          ? h("span", { class: "wh-assignee" }, ui.avatar(doc.assignee, { size: 22 }), h("span", null, mine ? "You" : doc.assignee))
          : h("span", { class: "wh-muted" }, "Unassigned")),
      h("td", { class: "wh-col-updated" }, ui.time(doc.updated_at)),
      h("td", { class: "wh-col-actions" }, actions));
      row.doc = doc;
      return row;
    }

    function markSelected(id, { focus = true, persist = true } = {}) {
      rowsById.forEach((row, rid) => {
        const on = rid === id;
        row.setAttribute("aria-selected", String(on));
        row.tabIndex = on ? 0 : -1;
      });
      const row = rowsById.get(id);
      if (!row) return;
      if (focus) {
        row.focus({ preventScroll: true });
        row.scrollIntoView({ block: "nearest", behavior: WH.prefersReducedMotion() ? "auto" : "smooth" });
      }
      if (persist && f.sel !== id) {
        f.sel = id;
        WH.router.setQuery({ sel: id });
      }
    }

    function move(delta) {
      const ids = [...rowsById.keys()];
      if (!ids.length) return;
      const current = ids.indexOf(f.sel || ids[0]);
      const next = Math.max(0, Math.min(ids.length - 1, (current < 0 ? 0 : current) + delta));
      markSelected(ids[next]);
      // Near the end with more to fetch: fetch ahead.
      if (next >= ids.length - 3 && docs.length < total) load({ append: true });
    }

    /* ----------------------------------------------------------- assign */

    async function assign(doc, username, { quiet = false } = {}) {
      const previous = doc.assignee || null;
      if (username && previous && previous !== username && !quiet) {
        const ok = await ui.confirm({
          title: "Take over this document?",
          message: `${doc.filename} is assigned to ${previous}. Assigning it to yourself lets them know someone else has it.`,
          confirmLabel: "Assign to me",
        });
        if (!ok) return;
      }
      // Optimistic: show it now, put it back if the server says no.
      doc.assignee = username;
      replaceRow(doc);
      try {
        const data = await WH.api(`/api/docs/${encodeURIComponent(doc.id)}/assign`, { method: "POST", body: { username } });
        if (data.document) Object.assign(doc, data.document);
        replaceRow(doc);
        if (!quiet) {
          ui.toast({
            message: username ? "Assigned to you" : "Released",
            detail: doc.filename,
            action: { label: "Undo", onClick: () => assign(doc, previous, { quiet: true }) },
          });
        }
      } catch (error) {
        doc.assignee = previous;
        replaceRow(doc);
        ui.toast({ message: "Couldn't change the assignee", detail: error.detail, kind: "error" });
      }
    }

    function replaceRow(doc) {
      const id = String(doc.id);
      const old = rowsById.get(id);
      if (!old) return;
      const hadFocus = old.contains(document.activeElement);
      const row = renderRow(doc);
      old.replaceWith(row);
      rowsById.set(id, row);
      markSelected(f.sel || id, { focus: hadFocus, persist: false });
    }

    /* --------------------------------------------------- continue card */

    async function loadContinue() {
      if (continueDismissed) return;
      try {
        const data = await WH.api("/api/docs", { query: { status: "in_review", limit: 10 }, background: true });
        const list = data.documents || [];
        const me = WH.me && WH.me.username;
        const pick = list.find((d) => d.assignee === me) || list.find((d) => !d.assignee) || null;
        renderContinue(pick);
      } catch {
        /* the card is a nicety; stay quiet */
      }
    }

    function renderContinue(doc) {
      WH.clear(continueSlot);
      if (!doc || continueDismissed) return;
      const meta = [doc.screening_no && `Subject ${doc.screening_no}`, doc.visit_name, doc.protocol_no].filter(Boolean).join(" · ");
      continueSlot.appendChild(
        h("section", { class: "wh-continue", "aria-label": "Continue where you left off" },
          h("span", { class: "wh-continue-icon" }, ui.icon("pen", { size: 20 })),
          h("div", { class: "wh-continue-copy" },
            h("p", { class: "wh-eyebrow" }, "Continue where you left off"),
            h("strong", { class: "wh-continue-title" }, doc.filename || `Document ${doc.id}`),
            h("span", { class: "wh-continue-meta" },
              meta,
              meta && " · ",
              doc.draft_revision ? `Draft ${doc.draft_revision} saved ` : "Opened ",
              ui.time(doc.updated_at))),
          ui.confidence(doc.confidence, { compact: true }),
          h("a", { class: "btn btn-primary wh-btn", href: `#/review/${encodeURIComponent(doc.id)}` }, "Continue", ui.icon("arrow-right", { size: 16 })),
          ui.iconButton("x", "Hide this card", () => {
            continueDismissed = true;
            WH.clear(continueSlot);
          })));
    }

    /* ---------------------------------------------------------- keyboard */

    function onKey(event) {
      if (event.metaKey || event.ctrlKey || event.altKey) return;
      if (WH.isTyping(event.target) || ui.hasOpenLayer()) return;
      const key = event.key.toLowerCase();
      const row = rowsById.get(f.sel);
      if (key === "j" || event.key === "ArrowDown") {
        if (event.key === "ArrowDown" && !(event.target.closest && event.target.closest("tr[data-id]"))) return;
        event.preventDefault();
        move(1);
      } else if (key === "k" || event.key === "ArrowUp") {
        if (event.key === "ArrowUp" && !(event.target.closest && event.target.closest("tr[data-id]"))) return;
        event.preventDefault();
        move(-1);
      } else if (event.key === "Enter" && row && !event.target.closest("a, button")) {
        event.preventDefault();
        location.hash = docHref(row.doc);
      } else if (key === "a" && row && WH.can.write() && !["submitted", "processing"].includes(row.doc.status)) {
        event.preventDefault();
        const mine = row.doc.assignee === WH.me.username;
        assign(row.doc, mine ? null : WH.me.username);
      } else if (key === "h" && row) {
        event.preventDefault();
        WH.router.go(`/doc/${row.doc.id}/history`);
      }
    }
    document.addEventListener("keydown", onKey);

    /* ------------------------------------------------------------- go */

    syncControls();
    load({ reset: true });
    loadContinue();
    const poller = WH.poll(async () => {
      if (loading) return;
      await load({ background: true });
      await loadContinue();
    }, 10000);
    const offOnline = WH.bus.on("net:online", () => load({ background: true }));

    return {
      update(next) {
        const nf = readFilters(next.query);
        const changed = ["status", ...FILTER_KEYS].some((k) => nf[k] !== f[k]);
        f = nf;
        search.querySelector("input").value = f.q;
        protocolInput.value = f.protocol;
        subjectInput.value = f.subject;
        assigneeSelect.value = f.assignee === "me" || f.assignee === "none" ? f.assignee : "";
        syncControls();
        if (changed) load({ reset: true });
        else if (f.sel) markSelected(f.sel, { focus: false, persist: false });
      },
      destroy() {
        poller.stop();
        offOnline();
        offDensity();
        debouncedText.cancel();
        document.removeEventListener("keydown", onKey);
        token += 1;
      },
    };
  }

  WH.router.add("/inbox", {
    name: "inbox",
    nav: "inbox",
    title: "Reviews",
    crumbs: () => [{ label: "Reviews" }],
    mount,
  });
})();
