/* Webo Healthtech eSource — Batches (#/batches, #/batches/:id).

   A list of scan batches and a detail view with each file's pipeline stage
   as dots (render → header → visit → read → done), an overall progress bar
   with an ETA, cancel, retry and "Review →". Polls every 2s while a batch
   runs; the shell raises a desktop notification when one finishes. */

(function () {
  "use strict";

  const WH = window.WH;
  const { h, ui } = WH;

  const STAGES = [
    { id: "render", label: "Render pages" },
    { id: "header", label: "Read header" },
    { id: "visit", label: "Match visit" },
    { id: "read", label: "Read fields" },
    { id: "done", label: "Done" },
  ];

  const isRunning = (b) => b && !b.done && !b.cancelled;
  const finishedCount = (c = {}) => (c.ready || 0) + (c.failed || 0) + (c.cancelled || 0) + (c.needs_input || 0);

  function batchStatus(b) {
    if (b.cancelled) return "cancelled";
    if (!b.done) return "running";
    return (b.counts && b.counts.failed) ? "done_failures" : "done";
  }

  function stackedBar(b, label) {
    const c = b.counts || {};
    return ui.progress({
      max: Math.max(b.total || 0, 1),
      label: label || `Batch progress: ${finishedCount(c)} of ${b.total} finished`,
      value: finishedCount(c),
      segments: [
        { value: c.ready || 0, tone: "success" },
        { value: c.needs_input || 0, tone: "warn" },
        { value: c.failed || 0, tone: "danger" },
        { value: c.cancelled || 0, tone: "neutral" },
        { value: c.running || 0, tone: "running" },
      ],
    });
  }

  /* --------------------------------------------------------------- list */

  function mountList(root) {
    let batches = [];
    let first = true;
    const listEl = h("div", { class: "wh-batch-list" });
    root.append(
      ui.pageHeader({
        eyebrow: "Processing",
        title: "Batches",
        sub: "Every scan you start becomes a batch. Files are read in the background — you can leave this page.",
        actions: WH.can.write() ? [h("a", { class: "btn btn-primary wh-btn", href: "#/sources" }, ui.icon("folder", { size: 16 }), "New scan")] : null,
      }),
      listEl
    );

    async function load(background) {
      const task = () => WH.api("/api/batches", { background });
      if (first) {
        first = false;
        await ui.load(listEl, { skeleton: () => ui.sk.cards(4), task, label: "batches", render: apply });
      } else {
        apply(await task());
      }
      poller.setInterval(batches.some(isRunning) ? 2000 : 15000);
    }

    function apply(data) {
      batches = data.batches || [];
      WH.bus.emit("batches:list", batches);
      render();
    }

    function render() {
      WH.clear(listEl);
      if (!batches.length) {
        listEl.appendChild(ui.emptyState({
          icon: "layers",
          title: "No batches yet",
          body: "Pick files or folders in Sources and press Scan. Progress for every file shows up here.",
          action: WH.can.write() ? { label: "Go to Sources", href: "#/sources", icon: "folder" } : null,
        }));
        return;
      }
      const running = batches.filter(isRunning);
      const rest = batches.filter((b) => !isRunning(b));
      if (running.length) listEl.appendChild(group("Running now", running));
      if (rest.length) listEl.appendChild(group(running.length ? "Earlier" : "Recent", rest));
    }

    function group(title, list) {
      return h("section", { class: "wh-batch-group" },
        h("h2", { class: "wh-section-title" }, title),
        h("ul", { class: "wh-batch-cards" }, list.map(card)));
    }

    function card(b) {
      const c = b.counts || {};
      const done = finishedCount(c);
      return h("li", null,
        h("a", { class: ["wh-batch-card", isRunning(b) && "is-running"], href: `#/batches/${encodeURIComponent(b.id)}` },
          h("div", { class: "wh-batch-card-head" },
            h("span", { class: "wh-batch-num" }, `#${b.number || b.id}`),
            h("strong", { class: "wh-batch-label" }, b.label || "Scan"),
            ui.chip(batchStatus(b), { size: "sm" })),
          stackedBar(b),
          h("div", { class: "wh-batch-card-foot" },
            h("span", null, h("strong", null, `${done}/${b.total || 0}`), " finished"),
            c.ready ? h("span", { class: "wh-good" }, `${c.ready} ready`) : null,
            c.needs_input ? h("span", { class: "wh-warn" }, `${c.needs_input} need input`) : null,
            c.failed ? h("span", { class: "wh-bad" }, `${c.failed} failed`) : null,
            b.skipped ? h("span", { class: "wh-muted" }, `${b.skipped} skipped`) : null,
            h("span", { class: "wh-grow" }),
            h("span", { class: "wh-muted" }, b.created_by ? `${b.created_by} · ` : "", ui.time(b.created_at)))));
    }

    const poller = WH.poll(() => load(true), 2000);
    load(false);
    return { destroy: () => poller.stop() };
  }

  /* ------------------------------------------------------------- detail */

  function mountDetail(root, ctx) {
    const id = ctx.params.id;
    let batch = null;
    let items = [];
    let filter = ctx.query.get("status") || "all";
    let wasRunning = null;
    let first = true;
    let poller = null;

    const headSlot = h("div");
    const summarySlot = h("div");
    const askSlot = h("div");
    const doneSlot = h("div", { "aria-live": "polite" });
    const filterSlot = h("div", { class: "wh-status-filter", role: "group", "aria-label": "Show files by status" });
    const itemsEl = h("div", { class: "wh-card wh-batch-items" });
    const body = h("div", { class: "wh-batch-detail" }, summarySlot, askSlot, doneSlot, filterSlot, itemsEl);
    root.append(headSlot, body);

    headSlot.appendChild(ui.pageHeader({ eyebrow: "Batch", title: "Loading batch…" }));

    async function load(background) {
      const task = () => WH.api(`/api/batches/${encodeURIComponent(id)}`, { background });
      if (first) {
        first = false;
        await ui.load(body, {
          skeleton: () => h("div", null, ui.sk.stat(5), ui.sk.table(6, [30, 20, 12, 12, 10, 10])),
          task,
          label: "this batch",
          render: (data) => {
            body.append(summarySlot, askSlot, doneSlot, filterSlot, itemsEl);
            apply(data);
          },
          onError: (error) => {
            if (error.status === 404) {
              WH.clear(body).appendChild(ui.emptyState({ icon: "layers", title: "This batch doesn't exist", body: "It may have been removed.", action: { label: "All batches", href: "#/batches" } }));
              return true;
            }
            return false;
          },
        });
      } else {
        apply(await task());
      }
    }

    function apply(data) {
      batch = data.batch;
      items = data.items || [];
      WH.bus.emit("batch:detail", batch);
      const running = isRunning(batch);
      if (wasRunning === true && !running) celebrate();
      wasRunning = running;
      if (poller) poller.setInterval(running ? 2000 : 30000);
      render();
    }

    function render() {
      const title = `Batch #${batch.number || batch.id}`;
      document.title = `${title} · eSource — Webo Healthtech`;
      WH.shell.setCrumbs([WH.shell.section("batches"), { label: title }]);

      // Header (keep focus target stable across polls).
      const actions = [];
      const c = batch.counts || {};
      if (c.ready) {
        const firstReady = items.find((i) => i.status === "ready" && i.document_id != null);
        if (firstReady) actions.push(h("a", { class: "btn btn-primary wh-btn", href: `#/review/${encodeURIComponent(firstReady.document_id)}` }, "Start reviewing", ui.icon("arrow-right", { size: 16 })));
      }
      if (isRunning(batch) && WH.can.write()) {
        actions.unshift(h("button", { type: "button", class: "btn btn-quiet wh-btn wh-btn-danger-quiet", onClick: cancel }, ui.icon("ban", { size: 16 }), "Cancel batch"));
      }
      const sub = h("span", null,
        batch.label ? `${batch.label} · ` : "",
        "Started by ", batch.created_by || "someone", " ", ui.time(batch.created_at));
      const focused = headSlot.contains(document.activeElement);
      WH.clear(headSlot).appendChild(ui.pageHeader({ eyebrow: "Batch", title, sub, actions }));
      if (focused) headSlot.querySelector("h1").focus({ preventScroll: true });

      renderSummary();
      renderAsk();
      renderFilter();
      renderItems();
    }

    function renderSummary() {
      const c = batch.counts || {};
      const done = finishedCount(c);
      const total = batch.total || 0;
      const pct = total ? Math.round((done / total) * 100) : 100;
      const eta = isRunning(batch) ? estimate() : null;
      const tiles = [
        ["queued", "Queued", c.queued],
        ["running", "Reading", c.running],
        ["ready", "Ready", c.ready],
        ["needs_input", "Needs input", c.needs_input],
        ["failed", "Failed", c.failed],
        ["cancelled", "Cancelled", c.cancelled],
        ["skipped", "Skipped", batch.skipped],
      ].filter(([s, , n]) => n || ["queued", "running", "ready"].includes(s));
      WH.clear(summarySlot).appendChild(
        h("section", { class: "wh-card wh-batch-summary", "aria-label": "Progress" },
          h("div", { class: "wh-batch-summary-top" },
            h("div", { class: "wh-batch-pct" }, h("strong", null, `${pct}%`), h("span", null, `${done} of ${WH.fmt.plural(total, "file")} finished`)),
            h("div", { class: "wh-batch-eta" },
              ui.chip(batchStatus(batch)),
              eta && h("span", { class: "wh-muted" }, ui.icon("clock", { size: 14 }), eta))),
          stackedBar(batch),
          h("dl", { class: "wh-stats" }, tiles.map(([s, label, n]) =>
            h("div", { class: ["wh-stat", `wh-stat--${ui.statusInfo(s).tone}`] },
              h("dt", null, ui.icon(ui.statusInfo(s).icon, { size: 14, className: s === "running" && n ? "wh-spin" : "" }), label),
              h("dd", null, WH.fmt.number(n || 0)))))));
    }

    /* ETA from how long finished files took and how many run at once. */
    function estimate() {
      const finished = items.filter((i) => i.started_at && i.finished_at && i.status !== "cancelled");
      const c = batch.counts || {};
      const remaining = (c.queued || 0) + (c.running || 0);
      if (!remaining) return "Finishing up…";
      if (finished.length < 1) return "Estimating time left…";
      const durations = finished.map((i) => (WH.parseDate(i.finished_at) - WH.parseDate(i.started_at)) / 1000).filter((d) => d > 0);
      if (!durations.length) return "Estimating time left…";
      const avg = durations.reduce((a, b) => a + b, 0) / durations.length;
      const parallel = Math.max(1, c.running || 1);
      // Running files are on average half done.
      const seconds = (avg * ((c.queued || 0) + (c.running || 0) * 0.5)) / parallel;
      if (seconds < 45) return "Less than a minute left";
      return `About ${WH.fmt.duration(Math.round(seconds / 60) * 60)} left`;
    }

    function renderAsk() {
      WH.clear(askSlot);
      if (!isRunning(batch)) return;
      const prompt = WH.shell.notifyPrompt();
      if (prompt) askSlot.appendChild(prompt);
    }

    function celebrate() {
      const c = batch.counts || {};
      WH.clear(doneSlot).appendChild(
        h("div", { class: ["wh-banner", c.failed ? "wh-banner--warn" : "wh-banner--success"], role: "status" },
          ui.icon(c.failed ? "alert-triangle" : "check-circle", { size: 20 }),
          h("div", null,
            h("strong", null, batch.cancelled ? "Batch cancelled" : "Batch finished"),
            h("span", null, [c.ready && `${c.ready} ready for review`, c.needs_input && `${c.needs_input} need input`, c.failed && `${c.failed} failed`].filter(Boolean).join(" · ") || "Nothing left to do.")),
          c.ready ? h("a", { class: "btn btn-primary wh-btn wh-btn-sm", href: "#/inbox?status=ready" }, "Open the inbox") : null));
      WH.announce("Batch finished");
    }

    function renderFilter() {
      WH.clear(filterSlot);
      const counts = {};
      items.forEach((i) => (counts[i.status] = (counts[i.status] || 0) + 1));
      const present = ["running", "queued", "ready", "needs_input", "failed", "cancelled"].filter((s) => counts[s]);
      if (present.length < 2 && filter === "all") return;
      [["all", "All", items.length], ...present.map((s) => [s, ui.statusInfo(s).label, counts[s]])].forEach(([s, label, n]) => {
        filterSlot.appendChild(h("button", {
          type: "button",
          class: "wh-toggle-chip",
          "aria-pressed": String(filter === s),
          onClick: () => {
            filter = s;
            WH.router.setQuery({ status: s === "all" ? "" : s });
            renderFilter();
            renderItems();
          },
        }, s !== "all" && ui.icon(ui.statusInfo(s).icon, { size: 13 }), label, h("span", { class: "wh-toggle-count" }, String(n))));
      });
    }

    function stageDots(item) {
      const idx = STAGES.findIndex((s) => s.id === item.stage);
      const ol = h("ol", { class: "wh-stages" });
      let current = "";
      STAGES.forEach((s, i) => {
        let state = "todo";
        if (item.status === "ready") state = "done";
        else if (item.status === "queued") state = "todo";
        else if (idx >= 0 && i < idx) state = "done";
        else if (i === idx) {
          state = { running: "active", failed: "failed", needs_input: "warn", cancelled: "stopped" }[item.status] || "active";
          if (s.id === "done") state = "done";
        }
        if (state !== "todo" && state !== "done") current = s.label;
        ol.appendChild(h("li", { class: `wh-stage is-${state}`, title: `${s.label}: ${{ done: "done", todo: "waiting", active: "in progress", failed: "failed here", warn: "needs input", stopped: "stopped" }[state]}` },
          h("span", { class: "wh-stage-dot" }, state === "done" ? ui.icon("check", { size: 10 }) : state === "failed" ? ui.icon("x", { size: 10 }) : null),
          h("span", { class: "wh-stage-label" }, s.label)));
      });
      const summary = item.status === "ready"
        ? "All stages done"
        : item.status === "queued"
          ? "Waiting to start"
          : current ? `${current} (${idx + 1} of ${STAGES.length}) — ${ui.statusInfo(item.status).label.toLowerCase()}` : ui.statusInfo(item.status).label;
      ol.setAttribute("aria-label", summary);
      return ol;
    }

    function renderItems() {
      const list = filter === "all" ? items : items.filter((i) => i.status === filter);
      const hadFocusKey = document.activeElement && document.activeElement.dataset && document.activeElement.dataset.focusKey;
      WH.clear(itemsEl);
      if (!items.length) {
        itemsEl.appendChild(ui.emptyState({ icon: "layers", title: "No files in this batch", body: batch.skipped ? `${batch.skipped} files were skipped as already read.` : "", compact: true }));
        return;
      }
      if (!list.length) {
        itemsEl.appendChild(ui.emptyState({ icon: "filter", title: "No files with this status", compact: true, action: { label: "Show all", onClick: () => {
          filter = "all";
          WH.router.setQuery({ status: "" });
          renderFilter();
          renderItems();
        } } }));
        return;
      }
      const order = { running: 0, needs_input: 1, failed: 2, queued: 3, ready: 4, cancelled: 5 };
      const sorted = [...list].sort((a, b) => (order[a.status] ?? 9) - (order[b.status] ?? 9));
      const tbody = h("tbody");
      sorted.forEach((item) => {
        const actions = h("div", { class: "wh-row-actions" });
        if ((item.status === "ready" || item.status === "needs_input") && item.document_id != null) {
          actions.appendChild(h("a", {
            class: ["btn", "wh-btn", "wh-btn-xs", item.status === "ready" ? "btn-primary" : "btn-quiet"],
            href: `#/review/${encodeURIComponent(item.document_id)}`,
            dataset: { focusKey: `review-${item.extraction_id}` },
          }, item.status === "ready" ? "Review" : "Fix header", ui.icon("arrow-right", { size: 14 })));
        }
        if (item.status === "failed" && WH.can.write()) {
          actions.appendChild(h("button", {
            type: "button",
            class: "btn btn-quiet wh-btn wh-btn-xs",
            dataset: { focusKey: `retry-${item.extraction_id}` },
            onClick: (event) => retry(item, event.currentTarget),
          }, ui.icon("refresh", { size: 14 }), "Retry"));
        }
        if (item.document_id != null) {
          actions.appendChild(h("a", { class: "wh-icon-btn wh-icon-btn--sm", href: `#/doc/${encodeURIComponent(item.document_id)}/history`, "aria-label": `History of ${item.filename}`, title: "History" }, ui.icon("history", { size: 15 })));
        }
        const duration = item.started_at
          ? WH.fmt.duration(((WH.parseDate(item.finished_at) || new Date()) - WH.parseDate(item.started_at)) / 1000)
          : "—";
        tbody.appendChild(h("tr", { class: ["wh-row", `is-${item.status}`] },
          h("td", { class: "wh-col-doc" },
            h("span", { class: "wh-doc-title" }, item.filename || item.path),
            h("span", { class: "wh-doc-sub", title: item.path }, item.path || ""),
            item.error && h("span", { class: "wh-row-error", role: "note" }, ui.icon("alert-circle", { size: 13 }), item.error)),
          h("td", { class: "wh-col-stages" }, stageDots(item)),
          h("td", null, ui.chip(item.status, { size: "sm" })),
          h("td", { class: "wh-col-conf" }, ui.confidence(item.confidence)),
          h("td", { class: "wh-col-num wh-muted" }, duration),
          h("td", { class: "wh-col-actions" }, actions)));
      });
      itemsEl.appendChild(h("table", { class: "wh-table wh-batch-table", "aria-label": "Files in this batch" },
        h("thead", null, h("tr", null,
          h("th", { scope: "col" }, "File"),
          h("th", { scope: "col" }, "Stage"),
          h("th", { scope: "col" }, "Status"),
          h("th", { scope: "col", class: "wh-col-conf" }, "Confidence"),
          h("th", { scope: "col", class: "wh-col-num" }, "Time"),
          h("th", { scope: "col", class: "wh-col-actions" }, h("span", { class: "wh-sr" }, "Actions")))),
        tbody));
      if (hadFocusKey) {
        const again = itemsEl.querySelector(`[data-focus-key="${CSS.escape(hadFocusKey)}"]`);
        if (again) again.focus({ preventScroll: true });
      }
    }

    async function retry(item, btn) {
      btn.disabled = true;
      btn.classList.add("is-busy");
      try {
        const data = await WH.api(`/api/extractions/${encodeURIComponent(item.extraction_id)}/retry`, { method: "POST" });
        Object.assign(item, data.item || { status: "queued", error: null });
        ui.toast({ message: "Queued to read again", detail: item.filename });
        if (poller) {
          poller.setInterval(2000);
          poller.now();
        }
        renderItems();
      } catch (error) {
        ui.toast({ message: "Couldn't retry this file", detail: error.detail, kind: "error" });
        btn.disabled = false;
        btn.classList.remove("is-busy");
      }
    }

    async function cancel() {
      const c = batch.counts || {};
      const ok = await ui.confirm({
        title: `Cancel batch #${batch.number || batch.id}?`,
        message: `${WH.fmt.plural((c.queued || 0) + (c.running || 0), "file")} that haven't finished will stop. Files already read stay available for review.`,
        confirmLabel: "Cancel batch",
        cancelLabel: "Keep running",
        danger: true,
      });
      if (!ok) return;
      try {
        const data = await WH.api(`/api/batches/${encodeURIComponent(batch.id)}/cancel`, { method: "POST" });
        if (data.batch) batch = data.batch;
        ui.toast({ message: "Batch cancelled", kind: "info" });
        poller.now();
      } catch (error) {
        ui.toast({ message: "Couldn't cancel the batch", detail: error.detail, kind: "error" });
      }
    }

    let destroyed = false;
    load(false).then(() => {
      if (destroyed) return;
      poller = WH.poll(() => load(true), batch && isRunning(batch) ? 2000 : 30000);
    });

    return {
      update(next) {
        filter = next.query.get("status") || "all";
        if (batch) {
          renderFilter();
          renderItems();
        }
      },
      destroy() {
        destroyed = true;
        if (poller) poller.stop();
      },
    };
  }

  WH.router.add("/batches", { name: "batches", nav: "batches", title: "Batches", crumbs: () => [{ label: "Batches" }], mount: mountList });
  WH.router.add("/batches/:id", {
    name: "batch",
    nav: "batches",
    title: (ctx) => `Batch ${ctx.params.id}`,
    crumbs: (ctx) => [WH.shell.section("batches"), { label: `Batch ${ctx.params.id}` }],
    mount: mountDetail,
  });
})();
