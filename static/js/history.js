/* Webo Healthtech eSource — Document history (#/doc/:id/history?a=&b=).

   One timeline, four lanes — file versions, extraction runs, draft
   revisions, EDC submissions — each event placed in its lane in time order.
   Submissions show the signature manifestation (who, what it means, when in
   UTC and local time). Any two draft revisions can be compared (revision 0
   is the AI's result), and a document can be sent back for reprocessing
   with a reason. */

(function () {
  "use strict";

  const WH = window.WH;
  const { h, ui } = WH;

  const LANES = [
    { id: "versions", label: "File versions", icon: "file" },
    { id: "extractions", label: "Extraction runs", icon: "sparkle" },
    { id: "drafts", label: "Draft revisions", icon: "pen" },
    { id: "submissions", label: "EDC submissions", icon: "send" },
  ];
  const REPROCESS_PICKS = [
    "Source page was rescanned or replaced",
    "Previous read was incomplete or wrong",
    "Wrong header or visit was matched",
    "Re-read with the updated reader",
    "Other",
  ];

  const empty = (v) => v == null || v === "";
  const show = (v) => (empty(v) ? h("span", { class: "wh-diff-empty" }, "empty") : typeof v === "object" ? JSON.stringify(v) : String(v));

  function mount(root, ctx) {
    const id = ctx.params.id;
    let doc = null;
    let hist = null;
    let meta = { meanings: [] };
    let hiddenLanes = new Set((ctx.query.get("hide") || "").split(",").filter(Boolean));
    let a = ctx.query.get("a");
    let b = ctx.query.get("b");
    let token = 0;

    const headSlot = h("div", null, ui.pageHeader({ eyebrow: "Document history", title: "Loading…" }));
    const compareSlot = h("div", { class: "wh-compare-slot" });
    const diffSlot = h("div", { class: "wh-diff-slot", "aria-live": "polite" });
    const laneFilter = h("div", { class: "wh-status-filter", role: "group", "aria-label": "Show lanes" });
    const timelineSlot = h("div", { class: "wh-card wh-lanes-card" });
    const body = h("div", { class: "wh-history" }, compareSlot, diffSlot, laneFilter, timelineSlot);
    root.append(headSlot, body);

    async function load() {
      const my = ++token;
      await ui.load(timelineSlot, {
        skeleton: () => ui.sk.lanes(),
        label: "this document's history",
        isCurrent: () => my === token,
        task: () => Promise.all([
          WH.api(`/api/docs/${encodeURIComponent(id)}`),
          WH.api(`/api/docs/${encodeURIComponent(id)}/history`),
          WH.meta(),
        ]),
        render: ([docData, historyData, metaData]) => {
          doc = docData.document || { id };
          hist = historyData;
          meta = metaData || meta;
          WH.recentDocs.add(doc);
          renderHeader();
          renderCompare();
          renderLaneFilter();
          renderTimeline();
          if (!empty(a) && !empty(b)) loadDiff();
        },
      });
    }

    /* ------------------------------------------------------------ header */

    function renderHeader() {
      const name = doc.filename || `Document ${id}`;
      document.title = `${name} · History · eSource`;
      WH.shell.setCrumbs([WH.shell.section("inbox"), { label: name, href: `#/review/${encodeURIComponent(id)}` }, { label: "History" }]);
      const metaLine = [doc.screening_no && `Subject ${doc.screening_no}`, doc.visit_name, doc.protocol_no && `Protocol ${doc.protocol_no}`].filter(Boolean).join(" · ");
      const actions = [
        h("a", { class: "btn btn-quiet wh-btn", href: WH.router.href("/audit", { document_id: id }) }, ui.icon("shield", { size: 16 }), "Audit trail"),
      ];
      if (WH.can.write() && doc.status !== "processing") {
        actions.push(h("button", { type: "button", class: "btn btn-quiet wh-btn", onClick: reprocess }, ui.icon("refresh", { size: 16 }), "Reprocess"));
      }
      if (doc.status !== "processing" && doc.status !== "failed") {
        actions.push(h("a", { class: "btn btn-primary wh-btn", href: `#/review/${encodeURIComponent(id)}` }, "Open review", ui.icon("arrow-right", { size: 16 })));
      }
      WH.clear(headSlot).appendChild(ui.pageHeader({
        eyebrow: "Document history",
        title: name,
        sub: h("span", { class: "wh-head-meta" }, ui.chip(doc.status, { size: "sm" }), metaLine && h("span", null, metaLine), doc.path && h("span", { class: "wh-muted wh-ellipsis", title: doc.path }, doc.path)),
        actions,
      }));
    }

    async function reprocess() {
      const result = await ui.confirm({
        title: "Reprocess this document?",
        message: "The current file is read again from scratch as a new extraction run. Existing drafts and submissions are kept in the history.",
        confirmLabel: "Reprocess",
        reason: { label: "Why reprocess?", picks: REPROCESS_PICKS },
      });
      if (!result) return;
      try {
        await WH.api(`/api/docs/${encodeURIComponent(id)}/reprocess`, { body: { reason: result.reason } });
        ui.toast({ message: "Sent for reprocessing", detail: "The new run appears in this history and in Batches.", action: { label: "Batches", onClick: () => WH.router.go("/batches") } });
        WH.shell.refreshBadges();
        load();
      } catch (error) {
        ui.toast({ message: "Couldn't reprocess", detail: error.detail, kind: "error" });
      }
    }

    /* ----------------------------------------------------------- compare */

    function revisions() {
      const drafts = (hist.drafts || []).map((d) => d.revision).filter((r) => r != null);
      return [0, ...drafts.filter((r) => r !== 0).sort((x, y) => x - y)];
    }
    const revLabel = (r) => (Number(r) === 0 ? "AI result (revision 0)" : `Revision ${r}`);

    function renderCompare() {
      WH.clear(compareSlot);
      const revs = revisions();
      if (revs.length < 2) return;
      const latest = revs[revs.length - 1];
      const opts = (selected) => revs.map((r) => ({ value: String(r), label: revLabel(r), selected: String(r) === String(selected) }));
      const selA = ui.select(opts(empty(a) ? 0 : a), { "aria-label": "Compare from", class: "wh-input wh-input--sm wh-select" });
      const selB = ui.select(opts(empty(b) ? latest : b), { "aria-label": "Compare to", class: "wh-input wh-input--sm wh-select" });
      const go = () => setCompare(selA.value, selB.value);
      compareSlot.appendChild(h("div", { class: "wh-compare" },
        h("span", { class: "wh-compare-icon" }, ui.icon("compare", { size: 18 })),
        h("span", { class: "wh-compare-label" }, "Compare"),
        selA,
        ui.iconButton("arrow-right", "Swap", () => {
          const t = selA.value;
          selA.value = selB.value;
          selB.value = t;
          if (!empty(a)) go();
        }, { class: "wh-icon-btn wh-swap" }),
        selB,
        h("button", { type: "button", class: "btn btn-quiet wh-btn wh-btn-sm", onClick: go }, "Show changes"),
        !empty(a) && h("button", { type: "button", class: "link-btn", onClick: () => setCompare(null, null) }, "Close comparison")));
    }

    function setCompare(nextA, nextB) {
      a = nextA;
      b = nextB;
      WH.router.setQuery({ a, b });
      renderCompare();
      renderTimeline();
      if (empty(a)) {
        WH.clear(diffSlot);
        return;
      }
      loadDiff();
    }

    async function loadDiff() {
      if (String(a) === String(b)) {
        WH.clear(diffSlot).appendChild(h("p", { class: "wh-inline-note" }, ui.icon("alert-circle", { size: 15 }), "Pick two different revisions to compare."));
        return;
      }
      const panel = h("div");
      WH.clear(diffSlot).appendChild(h("section", { class: "wh-card wh-diff", "aria-label": "Comparison" }, panel));
      await ui.load(panel, {
        skeleton: () => ui.sk.table(5, [20, 14, 30, 30]),
        label: "the comparison",
        task: () => WH.api(`/api/docs/${encodeURIComponent(id)}/diff`, { query: { a, b } }),
        render: (data) => renderDiff(panel, data),
      });
      diffSlot.scrollIntoView({ block: "nearest", behavior: WH.prefersReducedMotion() ? "auto" : "smooth" });
    }

    function renderDiff(panel, data) {
      const changes = data.changes || [];
      let filterText = "";
      const head = h("header", { class: "wh-diff-head" },
        h("h2", { class: "wh-section-title" }, `${revLabel(data.a != null ? data.a : a)} → ${revLabel(data.b != null ? data.b : b)}`),
        h("span", { class: "wh-muted" }, WH.fmt.plural(changes.length, "changed field")),
        h("span", { class: "wh-grow" }),
        changes.length > 8 && ui.searchBox({ placeholder: "Filter fields", label: "Filter changed fields", shortcut: "", onInput: (v) => {
          filterText = v.toLowerCase();
          renderRows();
        } }));
      const tableWrap = h("div");
      panel.append(head, tableWrap);

      function renderRows() {
        WH.clear(tableWrap);
        const rows = changes.filter((c) => !filterText || `${c.label} ${c.crf} ${c.key}`.toLowerCase().includes(filterText));
        if (!changes.length) {
          tableWrap.appendChild(ui.emptyState({ icon: "check-circle", title: "No differences", body: "Every value is the same in both revisions.", compact: true }));
          return;
        }
        if (!rows.length) {
          tableWrap.appendChild(ui.emptyState({ icon: "search", title: "No fields match", compact: true }));
          return;
        }
        let lastCrf = null;
        const tbody = h("tbody");
        rows.sort((x, y) => String(x.crf || "").localeCompare(String(y.crf || ""))).forEach((c) => {
          if (c.crf !== lastCrf) {
            lastCrf = c.crf;
            tbody.appendChild(h("tr", { class: "wh-diff-group" }, h("th", { colspan: "3", scope: "rowgroup" }, c.crf || "Other fields")));
          }
          const kind = empty(c.a) ? "added" : empty(c.b) ? "removed" : "changed";
          tbody.appendChild(h("tr", { class: `wh-diff-row is-${kind}` },
            h("th", { scope: "row", class: "wh-diff-field" }, c.label || c.key, h("span", { class: "wh-diff-kind" }, kind)),
            h("td", { class: "wh-diff-a" }, empty(c.a) ? show(c.a) : h("del", null, h("span", { class: "wh-sr" }, "was "), show(c.a))),
            h("td", { class: "wh-diff-b" }, empty(c.b) ? show(c.b) : h("ins", null, h("span", { class: "wh-sr" }, "now "), show(c.b)))));
        });
        tableWrap.appendChild(h("table", { class: "wh-table wh-diff-table" },
          h("thead", null, h("tr", null,
            h("th", { scope: "col" }, "Field"),
            h("th", { scope: "col" }, revLabel(data.a != null ? data.a : a)),
            h("th", { scope: "col" }, revLabel(data.b != null ? data.b : b)))),
          tbody));
      }
      renderRows();
    }

    /* ---------------------------------------------------------- timeline */

    function events() {
      const list = [];
      (hist.versions || []).forEach((v) => list.push({ lane: "versions", at: v.created_at, data: v }));
      (hist.extractions || []).forEach((x) => list.push({ lane: "extractions", at: x.created_at, data: x }));
      (hist.drafts || []).forEach((d) => list.push({ lane: "drafts", at: d.saved_at, data: d }));
      (hist.submissions || []).forEach((s) => list.push({ lane: "submissions", at: s.submitted_at, data: s }));
      // Revision 0: the AI result of the current extraction.
      const exts = (hist.extractions || []).slice().sort((x, y) => WH.parseDate(y.created_at) - WH.parseDate(x.created_at));
      if (exts.length && (hist.drafts || []).length) {
        list.push({ lane: "drafts", at: exts[0].created_at, data: { revision: 0, ai: true, run: exts[0].run } });
      }
      return list.sort((x, y) => (WH.parseDate(y.at) || 0) - (WH.parseDate(x.at) || 0));
    }

    function renderLaneFilter() {
      WH.clear(laneFilter);
      const counts = {
        versions: (hist.versions || []).length,
        extractions: (hist.extractions || []).length,
        drafts: (hist.drafts || []).length,
        submissions: (hist.submissions || []).length,
      };
      LANES.forEach((lane) => {
        laneFilter.appendChild(h("button", {
          type: "button",
          class: "wh-toggle-chip",
          "aria-pressed": String(!hiddenLanes.has(lane.id)),
          onClick: () => {
            if (hiddenLanes.has(lane.id)) hiddenLanes.delete(lane.id);
            else hiddenLanes.add(lane.id);
            WH.router.setQuery({ hide: [...hiddenLanes].join(",") });
            renderLaneFilter();
            renderTimeline();
          },
        }, ui.icon(lane.icon, { size: 13 }), lane.label, h("span", { class: "wh-toggle-count" }, String(counts[lane.id]))));
      });
    }

    function renderTimeline() {
      WH.clear(timelineSlot);
      const lanes = LANES.filter((l) => !hiddenLanes.has(l.id));
      const list = events().filter((e) => !hiddenLanes.has(e.lane));
      if (!list.length) {
        timelineSlot.appendChild(ui.emptyState({ icon: "history", title: hiddenLanes.size ? "All lanes are hidden" : "No history yet", body: hiddenLanes.size ? "Turn a lane back on above." : "Events appear here as the document is read, reviewed and submitted.", compact: true }));
        return;
      }
      const grid = h("div", { class: "wh-lanes", style: { "--lanes": String(lanes.length) }, role: "list", "aria-label": "History, newest first" });
      grid.appendChild(h("div", { class: "wh-lanes-head", "aria-hidden": "true" },
        h("span", { class: "wh-lanes-time" }, "When"),
        lanes.map((l) => h("span", { class: `wh-lane-title wh-lane--${l.id}` }, ui.icon(l.icon, { size: 14 }), l.label))));
      let day = null;
      list.forEach((event) => {
        const key = WH.fmt.dayKey(event.at);
        if (key !== day) {
          day = key;
          grid.appendChild(h("div", { class: "wh-lanes-day", role: "presentation" }, WH.fmt.dayLabel(event.at)));
        }
        const col = lanes.findIndex((l) => l.id === event.lane);
        grid.appendChild(h("div", { class: "wh-lanes-row", role: "listitem" },
          h("span", { class: "wh-lanes-time" }, ui.time(event.at, { mode: "time" })),
          h("div", { class: `wh-lane-cell wh-lane--${event.lane}`, style: { "--col": String(col + 2) } },
            h("span", { class: "wh-lane-mobile", "aria-hidden": "true" }, LANES.find((l) => l.id === event.lane).label),
            card(event))));
      });
      timelineSlot.appendChild(grid);
    }

    function card(event) {
      const d = event.data;
      switch (event.lane) {
        case "versions":
          return h("article", { class: "wh-ev" },
            h("header", { class: "wh-ev-head" }, h("strong", null, `Version ${d.version}`),
              String(d.version) === String(doc.current_version) && ui.tag("Current", "brand")),
            h("p", { class: "wh-ev-meta" }, d.created_by ? `Added by ${d.created_by}` : "Added"),
            d.reason && h("p", { class: "wh-ev-reason" }, ui.icon("pen", { size: 12 }), d.reason),
            h("p", { class: "wh-ev-hash" }, h("code", { title: d.sha256 }, `sha256 ${String(d.sha256 || "").slice(0, 12)}…`), d.sha256 && ui.copyButton(d.sha256, { label: "Copy file hash" })),
            h("a", { class: "wh-ev-link", href: `/api/docs/${encodeURIComponent(id)}/file?version=${encodeURIComponent(d.version)}`, target: "_blank", rel: "noopener" }, ui.icon("external", { size: 13 }), "Open this PDF"));
        case "extractions":
          return h("article", { class: "wh-ev" },
            h("header", { class: "wh-ev-head" }, h("strong", null, `Run ${d.run != null ? d.run : d.id}`), ui.chip(d.status, { size: "sm" })),
            h("p", { class: "wh-ev-meta" }, `File version ${d.version}`, d.created_by ? ` · started by ${d.created_by}` : ""),
            d.models && h("p", { class: "wh-ev-meta wh-muted" }, Array.isArray(d.models) ? d.models.join(", ") : typeof d.models === "object" ? Object.values(d.models).join(", ") : String(d.models)),
            d.reason && h("p", { class: "wh-ev-reason" }, ui.icon("pen", { size: 12 }), d.reason));
        case "drafts": {
          const rev = String(d.revision);
          const inCompare = rev === String(a) || rev === String(b);
          return h("article", { class: ["wh-ev", inCompare && "is-compared"] },
            h("header", { class: "wh-ev-head" },
              h("strong", null, d.ai ? "AI result" : `Revision ${d.revision}`),
              d.ai ? ui.tag("Revision 0", "brand") : d.autosave ? ui.tag("Autosave") : ui.tag("Saved", "success")),
            h("p", { class: "wh-ev-meta" }, d.ai ? `What the reader produced (run ${d.run != null ? d.run : "latest"})` : `${d.saved_by || "Someone"} · ${WH.fmt.plural(d.edits || 0, "edit")}`),
            revisions().length > 1 && h("div", { class: "wh-ev-actions" },
              h("button", { type: "button", class: "link-btn", "aria-pressed": String(rev === String(a)), onClick: () => setCompare(rev, empty(b) || b === rev ? String(revisions().slice(-1)[0]) : b) }, rev === String(a) ? "Comparing from" : "Compare from"),
              !d.ai && h("button", { type: "button", class: "link-btn", "aria-pressed": String(rev === String(b)), onClick: () => setCompare(empty(a) || a === rev ? "0" : a, rev) }, rev === String(b) ? "Comparing to" : "Compare to")));
        }
        case "submissions":
          return submissionCard(d);
        default:
          return h("span");
      }
    }

    function submissionCard(s) {
      const sig = s.signature;
      const changes = Array.isArray(s.changes) ? s.changes : [];
      return h("article", { class: "wh-ev wh-ev--submission" },
        h("header", { class: "wh-ev-head" }, h("strong", null, "Submitted to EDC"), ui.chip(s.status, { size: "sm" })),
        h("p", { class: "wh-ev-meta" }, `${s.submitted_by || "Someone"} · ${WH.fmt.plural(changes.length, "change")}`, s.skipped ? ` · ${WH.fmt.count(s.skipped)} skipped` : ""),
        sig && h("div", { class: "wh-signature", role: "group", "aria-label": "Electronic signature" },
          h("span", { class: "wh-signature-icon" }, ui.icon("signature", { size: 18 })),
          h("div", null,
            h("p", { class: "wh-signature-by" }, "Electronically signed by ", h("strong", null, sig.full_name || s.submitted_by)),
            h("p", { class: "wh-signature-meaning" }, "Meaning: ", h("q", null, WH.meaningLabel(sig.meaning, meta))),
            h("p", { class: "wh-signature-time" },
              h("time", { datetime: sig.signed_at }, WH.fmt.utc(sig.signed_at)),
              h("span", { class: "wh-muted" }, ` · ${WH.fmt.local(sig.signed_at)}`)))),
        changes.length > 0 && h("details", { class: "wh-ev-changes" },
          h("summary", null, `Show ${WH.fmt.plural(changes.length, "change")}`),
          h("ul", null, changes.slice(0, 200).map((c) => {
            if (c && typeof c === "object") {
              const label = c.label || c.fieldName || c.key || "Field";
              return h("li", null, h("span", { class: "wh-ev-field" }, label), " ",
                h("del", null, show(c.old != null ? c.old : c.a)), " → ", h("ins", null, show(c.new != null ? c.new : c.b)));
            }
            return h("li", null, String(c));
          }))));
    }

    load();
    return {
      update(next) {
        const na = next.query.get("a");
        const nb = next.query.get("b");
        if (na !== a || nb !== b) {
          a = na;
          b = nb;
          if (hist) {
            renderCompare();
            renderTimeline();
            if (empty(a)) WH.clear(diffSlot);
            else loadDiff();
          }
        }
      },
      destroy() {
        token += 1;
      },
    };
  }

  WH.router.add("/doc/:id/history", {
    name: "history",
    nav: "inbox",
    title: "Document history",
    crumbs: () => [WH.shell.section("inbox"), { label: "History" }],
    mount,
  });
  WH.router.add("/doc/:id", {
    name: "doc",
    nav: "inbox",
    title: "Document",
    mount(el, ctx) {
      WH.router.replace(`/doc/${encodeURIComponent(ctx.params.id)}/history`);
    },
  });
})();
