/* Webo Healthtech eSource — Dashboard (#/).

   The screen after sign-in: counts for the review worklist, the latest
   documents, and scans that are still running. */

(function () {
  "use strict";

  const WH = window.WH;
  const { h, ui } = WH;

  const TILES = [
    ["ready", "Ready for review"],
    ["in_review", "In review"],
    ["needs_input", "Needs input"],
    ["processing", "Processing"],
    ["submitted", "Submitted"],
    ["failed", "Failed"],
  ];

  function hello() {
    const hour = new Date().getHours();
    const part = hour < 12 ? "Good morning" : hour < 17 ? "Good afternoon" : "Good evening";
    const name = WH.me && (WH.me.full_name || WH.me.username);
    const first = name ? String(name).trim().split(/\s+/)[0] : "";
    return first ? `${part}, ${first}` : part;
  }

  function docHref(doc) {
    return doc.status === "processing" || doc.status === "failed"
      ? `#/doc/${encodeURIComponent(doc.id)}/history`
      : `#/review/${encodeURIComponent(doc.id)}`;
  }

  function mount(root) {
    let docs = [];
    let counts = {};
    let batches = [];
    let first = true;
    const statsSlot = h("div");
    const grid = h("div", { class: "wh-dash-grid" });

    root.append(
      ui.pageHeader({
        eyebrow: "eSource",
        title: hello(),
        sub: "What is waiting for review, what is being read, and the latest visit forms.",
        actions: [
          h("a", { class: "btn btn-quiet wh-btn", href: "#/inbox" }, ui.icon("file", { size: 16 }), "Reviews"),
          WH.can.write() && h("a", { class: "btn btn-primary wh-btn", href: "#/sources" }, ui.icon("folder", { size: 16 }), "Scan sources"),
        ],
      }),
      statsSlot,
      grid
    );

    async function load(background) {
      const task = () => Promise.all([
        WH.api("/api/docs", { query: { limit: 8 }, background }),
        WH.api("/api/batches", { query: { limit: 5 }, background }),
      ]);
      if (first) {
        first = false;
        await ui.load(grid, {
          skeleton: () => h("div", null, ui.sk.stat(6), ui.sk.table(4, [46, 24, 18])),
          task,
          label: "the dashboard",
          render: apply,
        });
        return;
      }
      try {
        apply(await task());
      } catch (error) {
        if (!background) ui.toast({ message: "Couldn't refresh the dashboard", detail: error.detail || error.message, kind: "error" });
      }
    }

    function apply(pair) {
      const docData = pair[0] || {};
      const batchData = pair[1] || {};
      docs = docData.documents || [];
      counts = docData.counts || {};
      batches = batchData.batches || [];
      if (docData.counts) WH.bus.emit("docs:counts", docData.counts);
      WH.bus.emit("batches:list", batches);
      render();
    }

    function render() {
      WH.clear(statsSlot).appendChild(h("div", { class: "wh-stats wh-stats--cards" },
        TILES.map(([status, label]) => {
          const info = ui.statusInfo(status);
          return h("a", {
            class: ["wh-stat", "wh-stat-link", `wh-stat--${info.tone}`],
            href: `#/inbox?status=${encodeURIComponent(status)}`,
          },
            h("dt", null, ui.icon(info.icon, { size: 14 }), label),
            h("dd", null, WH.fmt.number(counts[status] || 0)));
        })));

      const running = batches.filter((b) => !b.done && !b.cancelled);
      const shown = (running.length ? running : batches).slice(0, 5);
      WH.clear(grid).append(
        h("section", { class: "wh-card wh-dash-card" },
          h("div", { class: "wh-dash-card-head" },
            h("h2", { class: "wh-section-title" }, "Latest documents"),
            h("a", { class: "wh-dash-more", href: "#/inbox" }, "All reviews")),
          docs.length
            ? h("ul", { class: "wh-dash-list" }, docs.map(docRow))
            : ui.emptyState({
              icon: "file",
              title: "No documents yet",
              body: "Scan a folder in Sources and the visit forms show up here.",
              compact: true,
            })),
        h("section", { class: "wh-card wh-dash-card" },
          h("div", { class: "wh-dash-card-head" },
            h("h2", { class: "wh-section-title" }, running.length ? "Scans in progress" : "Recent scans"),
            h("a", { class: "wh-dash-more", href: "#/batches" }, "All batches")),
          shown.length
            ? h("ul", { class: "wh-dash-list" }, shown.map(batchRow))
            : ui.emptyState({
              icon: "layers",
              title: "No scans yet",
              body: "A scan from Sources appears here while it runs.",
              compact: true,
            }))
      );
    }

    function docRow(doc) {
      const meta = [doc.screening_no, doc.visit_name, doc.protocol_no].filter(Boolean).join(" · ");
      return h("li", null, h("a", { class: "wh-dash-row", href: docHref(doc) },
        h("span", { class: "wh-dash-row-main" },
          h("strong", null, doc.filename || "Document"),
          meta ? h("span", null, meta) : null),
        ui.chip(doc.status, { size: "sm" }),
        ui.time(doc.updated_at)));
    }

    function batchRow(batch) {
      const c = batch.counts || {};
      const done = (c.ready || 0) + (c.failed || 0) + (c.cancelled || 0) + (c.needs_input || 0);
      const status = batch.cancelled ? "cancelled" : !batch.done ? "running" : (c.failed ? "done_failures" : "done");
      return h("li", null, h("a", { class: "wh-dash-row", href: `#/batches/${encodeURIComponent(batch.id)}` },
        h("span", { class: "wh-dash-row-main" },
          h("strong", null, batch.label || `Batch #${batch.number || batch.id}`),
          h("span", null, `${done} of ${batch.total || 0} finished`)),
        ui.chip(status, { size: "sm" }),
        ui.time(batch.created_at)));
    }

    const poller = WH.poll(() => load(true), 15000);
    const offOnline = WH.bus.on("net:online", () => load(true));
    load(false);
    return {
      destroy() {
        poller.stop();
        offOnline();
      },
    };
  }

  WH.router.add("/", {
    name: "dashboard",
    nav: "dashboard",
    title: "Dashboard",
    mount,
  });
})();
