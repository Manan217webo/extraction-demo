/* Webo Healthtech eSource — Audit trail (#/audit?user=&document_id=&…).

   A filterable, day-grouped timeline with sticky day headers and infinite
   scroll (via `before`), a hash-chain integrity badge with re-verify, the EDC
   delivery panel, a detail drawer per event (hashes, details JSON, old → new,
   reason) and CSV/JSON export of exactly what is filtered. */

(function () {
  "use strict";

  const WH = window.WH;
  const { h, ui } = WH;
  const PAGE = 100;
  const FILTERS = ["q", "user", "action", "subject", "document_id", "from", "to"];

  /* Action names → a tone and icon, by prefix. Unknown actions stay neutral. */
  function actionStyle(action) {
    const a = String(action || "").toLowerCase();
    if (/(login_failed|locked|denied|fail|error|broken)/.test(a)) return { tone: "danger", icon: "alert-circle" };
    if (/(submit|sign|edc)/.test(a)) return { tone: "success", icon: "send" };
    if (/(field|edit|draft|change|update)/.test(a)) return { tone: "warn", icon: "pen" };
    if (/(login|logout|session)/.test(a)) return { tone: "neutral", icon: "user" };
    if (/(batch|extract|reprocess|scan)/.test(a)) return { tone: "info", icon: "layers" };
    if (/(user|role|admin)/.test(a)) return { tone: "brand", icon: "users" };
    if (/(open|view|export)/.test(a)) return { tone: "neutral", icon: "eye" };
    return { tone: "neutral", icon: "history" };
  }

  function readFilters(query) {
    const f = {};
    FILTERS.forEach((k) => (f[k] = query.get(k) || ""));
    return f;
  }

  function mount(root, ctx) {
    let f = readFilters(ctx.query);
    let events = [];
    let nextBefore = null;
    let loadingMore = false;
    let token = 0;
    let openSeq = ctx.query.get("ev");
    let drawer = null;

    /* ------------------------------------------------------------ layout */

    const exportCsv = h("a", { class: "btn btn-quiet wh-btn", download: "" }, ui.icon("download", { size: 16 }), "CSV");
    const exportJson = h("a", { class: "btn btn-quiet wh-btn", download: "" }, ui.icon("download", { size: 16 }), "JSON");
    root.appendChild(ui.pageHeader({
      eyebrow: "Compliance",
      title: "Audit trail",
      sub: "Every sign-in, read, edit, signature and submission — in order, hash-chained so any tampering shows.",
      actions: [h("span", { class: "wh-export-label" }, "Export"), exportCsv, exportJson],
    }));

    const verifySlot = h("section", { class: "wh-card wh-integrity", "aria-label": "Chain integrity" });
    const deliverySlot = h("section", { class: "wh-card wh-delivery", "aria-label": "EDC delivery" });
    root.appendChild(h("div", { class: "wh-audit-top" }, verifySlot, deliverySlot));

    // Filters
    const search = ui.searchBox({ value: f.q, placeholder: "Search values, reasons, details…", label: "Search the audit trail", onInput: (v) => setFilter({ q: v }) });
    const userInput = ui.input({ value: f.user, placeholder: "User", "aria-label": "Filter by username", class: "wh-input wh-input--sm" });
    const subjectInput = ui.input({ value: f.subject, placeholder: "Subject", "aria-label": "Filter by subject", class: "wh-input wh-input--sm" });
    const docInput = ui.input({ value: f.document_id, placeholder: "Document ID", "aria-label": "Filter by document ID", class: "wh-input wh-input--sm wh-input--xs", inputmode: "numeric" });
    const actionSelect = ui.select([{ value: "", label: "All actions" }], { "aria-label": "Filter by action", class: "wh-input wh-input--sm wh-select" });
    const fromInput = ui.input({ type: "date", value: f.from, "aria-label": "From date", class: "wh-input wh-input--sm wh-input--date" });
    const toInput = ui.input({ type: "date", value: f.to, "aria-label": "To date", class: "wh-input wh-input--sm wh-input--date" });
    const clearBtn = h("button", { type: "button", class: "link-btn wh-clear-filters", onClick: clearFilters }, "Clear filters");
    const textChanged = WH.debounce(() => setFilter({ user: userInput.value.trim(), subject: subjectInput.value.trim(), document_id: docInput.value.trim() }), 350);
    [userInput, subjectInput, docInput].forEach((el) => el.addEventListener("input", textChanged));
    actionSelect.addEventListener("change", () => setFilter({ action: actionSelect.value }));
    [fromInput, toInput].forEach((el) => el.addEventListener("change", () => setFilter({ from: fromInput.value, to: toInput.value })));

    const resultLine = h("p", { class: "wh-result-line", "aria-live": "polite" });
    const newPill = h("button", { type: "button", class: "wh-new-pill", hidden: true, onClick: () => load() }, ui.icon("arrow-right", { size: 14 }), "New events — show");
    const filterBar = h("div", { class: "wh-filterbar", role: "search", "aria-label": "Filter the audit trail" },
      search, userInput, actionSelect, subjectInput, docInput,
      h("span", { class: "wh-daterange" }, fromInput, h("span", { "aria-hidden": "true" }, "–"), toInput),
      clearBtn);
    const listEl = h("div", { class: "wh-audit-list" });
    const sentinel = h("div", { class: "wh-sentinel", "aria-hidden": "true" });
    const moreSlot = h("div", { class: "wh-more-slot" });
    root.appendChild(h("div", { class: "wh-card wh-audit" }, h("div", { class: "wh-inbox-head" }, filterBar, h("div", { class: "wh-audit-meta" }, resultLine, newPill)), listEl, moreSlot, sentinel));

    /* ----------------------------------------------------------- filters */

    function anyFilter() {
      return FILTERS.some((k) => f[k]);
    }
    function setFilter(patch) {
      f = { ...f, ...patch };
      WH.router.setQuery({ ...f, ev: null });
      sync();
      load();
    }
    function clearFilters() {
      search.querySelector("input").value = "";
      [userInput, subjectInput, docInput, fromInput, toInput].forEach((el) => (el.value = ""));
      actionSelect.value = "";
      setFilter(Object.fromEntries(FILTERS.map((k) => [k, ""])));
    }
    function sync() {
      clearBtn.hidden = !anyFilter();
      const qs = WH.qs(f);
      exportCsv.href = `/api/audit/export?format=csv${qs ? `&${qs}` : ""}`;
      exportJson.href = `/api/audit/export?format=json${qs ? `&${qs}` : ""}`;
      const scope = anyFilter() ? "Exports only what matches these filters" : "Exports the full trail";
      exportCsv.title = `${scope} as CSV`;
      exportJson.title = `${scope} as JSON`;
    }

    async function loadActions() {
      try {
        const data = await WH.api("/api/audit/actions");
        const actions = (data.actions || []).slice().sort();
        // Prefix groups ("field.*") because the API accepts wildcards.
        const prefixes = [...new Set(actions.filter((a) => a.includes(".")).map((a) => a.split(".")[0]))];
        WH.clear(actionSelect);
        actionSelect.appendChild(h("option", { value: "" }, "All actions"));
        if (prefixes.length) {
          actionSelect.appendChild(h("optgroup", { label: "Groups" }, prefixes.map((p) => h("option", { value: `${p}.*` }, `All ${WH.fmt.humanize(p).toLowerCase()} actions`))));
        }
        actionSelect.appendChild(h("optgroup", { label: "Actions" }, actions.map((a) => h("option", { value: a }, WH.fmt.humanize(a)))));
        if (f.action && ![...actionSelect.options].some((o) => o.value === f.action)) {
          actionSelect.appendChild(h("option", { value: f.action }, f.action));
        }
        actionSelect.value = f.action;
      } catch {
        if (f.action) actionSelect.appendChild(h("option", { value: f.action, selected: true }, f.action));
      }
    }

    /* -------------------------------------------------------------- load */

    async function load() {
      const my = ++token;
      newPill.hidden = true;
      events = [];
      nextBefore = null;
      WH.clear(moreSlot);
      await ui.load(listEl, {
        skeleton: () => ui.sk.timeline(9),
        label: "the audit trail",
        isCurrent: () => my === token,
        task: () => WH.api("/api/audit", { query: { ...f, limit: PAGE } }),
        render: (data) => {
          events = data.events || [];
          nextBefore = data.next_before != null ? data.next_before : null;
          render();
          if (openSeq) openDetailBySeq(openSeq);
        },
      });
    }

    async function loadMore() {
      if (loadingMore || nextBefore == null) return;
      loadingMore = true;
      const my = token;
      WH.clear(moreSlot).appendChild(ui.sk.timeline(3));
      try {
        const data = await WH.api("/api/audit", { query: { ...f, limit: PAGE, before: nextBefore } });
        if (my !== token) return;
        const more = data.events || [];
        events = events.concat(more);
        nextBefore = data.next_before != null ? data.next_before : null;
        appendRows(more);
        WH.clear(moreSlot);
        renderEnd();
      } catch (error) {
        WH.clear(moreSlot).appendChild(ui.errorState(error, { title: "Couldn't load older events", compact: true, retry: () => {
          loadingMore = false;
          loadMore();
        } }));
      } finally {
        loadingMore = false;
      }
    }

    const observer = new IntersectionObserver((entries) => {
      if (entries.some((e) => e.isIntersecting)) loadMore();
    }, { rootMargin: "600px 0px" });
    observer.observe(sentinel);

    /* ------------------------------------------------------------ render */

    let currentDay = null;
    let currentGroup = null;

    function render() {
      WH.clear(listEl);
      currentDay = null;
      currentGroup = null;
      if (!events.length) {
        listEl.appendChild(ui.emptyState({
          icon: anyFilter() ? "filter" : "shield",
          title: anyFilter() ? "No events match these filters" : "No events recorded yet",
          body: anyFilter() ? "Widen the date range or clear a filter." : "Actions appear here the moment they happen.",
          action: anyFilter() ? { label: "Clear filters", onClick: clearFilters } : null,
        }));
        resultLine.textContent = "";
        return;
      }
      appendRows(events);
      renderEnd();
    }

    function appendRows(list) {
      list.forEach((ev) => {
        const key = WH.fmt.dayKey(ev.ts);
        if (key !== currentDay) {
          currentDay = key;
          currentGroup = h("ol", { class: "wh-audit-day-list" });
          listEl.appendChild(h("section", { class: "wh-audit-day", "aria-label": WH.fmt.dayLabel(ev.ts) },
            h("h2", { class: "wh-day-head" }, h("span", null, WH.fmt.dayLabel(ev.ts)), h("span", { class: "wh-day-date" }, key)),
            currentGroup));
        }
        currentGroup.appendChild(row(ev));
      });
      resultLine.textContent = `${WH.fmt.plural(events.length, "event")} loaded${nextBefore != null ? " · scroll for older" : ""}`;
    }

    function renderEnd() {
      const existing = listEl.querySelector(".wh-audit-end");
      if (existing) existing.remove();
      if (nextBefore == null && events.length) {
        listEl.appendChild(h("p", { class: "wh-audit-end" }, ui.icon("check", { size: 14 }),
          anyFilter() ? "That's every matching event." : "Beginning of the record."));
      } else if (nextBefore != null) {
        // A button too, for keyboard users and when the observer is not enough.
        WH.clear(moreSlot).appendChild(h("button", { type: "button", class: "btn btn-quiet wh-btn wh-more-btn", onClick: loadMore }, "Load older events"));
      }
    }

    function summary(ev) {
      const target = [ev.screening_no && `Subject ${ev.screening_no}`, ev.visit_name, ev.document_id != null && `Doc ${ev.document_id}`].filter(Boolean).join(" · ");
      const change = ev.field || ev.old_value != null || ev.new_value != null
        ? h("span", { class: "wh-audit-change" },
            ev.field && h("span", { class: "wh-audit-field" }, ev.field),
            (ev.old_value != null || ev.new_value != null) && [
              h("del", null, ev.old_value == null || ev.old_value === "" ? "empty" : String(ev.old_value)),
              h("span", { "aria-hidden": "true" }, " → "),
              h("span", { class: "wh-sr" }, " changed to "),
              h("ins", null, ev.new_value == null || ev.new_value === "" ? "empty" : String(ev.new_value)),
            ])
        : null;
      return { target, change };
    }

    function row(ev) {
      const style = actionStyle(ev.action);
      const { target, change } = summary(ev);
      const delivery = ev.delivery && ev.delivery.status;
      const btn = h("button", {
        type: "button",
        class: "wh-audit-row",
        dataset: { seq: String(ev.seq) },
        "aria-label": `${WH.fmt.humanize(ev.action)} by ${ev.full_name || ev.username || "system"} at ${WH.fmt.time(ev.ts, true)}. Open details.`,
        onClick: () => openDetail(ev),
      },
      h("span", { class: "wh-audit-time" }, h("time", { datetime: ev.ts, title: WH.fmt.both(ev.ts) }, WH.fmt.time(ev.ts, true))),
      h("span", { class: `wh-audit-dot wh-tone--${style.tone}`, "aria-hidden": "true" }, ui.icon(style.icon, { size: 13 })),
      h("span", { class: "wh-audit-main" },
        h("span", { class: "wh-audit-line" },
          h("strong", { class: "wh-audit-action" }, WH.fmt.humanize(ev.action)),
          h("span", { class: "wh-audit-actor" }, ev.full_name || ev.username || "System"),
          target && h("span", { class: "wh-audit-target" }, target)),
        change,
        ev.reason && h("span", { class: "wh-audit-reason" }, ui.icon("pen", { size: 11 }), ev.reason)),
      delivery && delivery !== "sent" ? ui.chip(delivery, { size: "sm", title: "EDC delivery" }) : null,
      h("span", { class: "wh-audit-seq" }, `#${ev.seq}`));
      return h("li", null, btn);
    }

    /* ------------------------------------------------------------ drawer */

    function openDetailBySeq(seq) {
      const ev = events.find((e) => String(e.seq) === String(seq));
      if (ev) openDetail(ev);
      else fetchSeq(seq);
    }

    async function fetchSeq(seq) {
      try {
        const data = await WH.api("/api/audit", { query: { before: Number(seq) + 1, limit: 1 } });
        const ev = (data.events || [])[0];
        if (ev && String(ev.seq) === String(seq)) openDetail(ev);
      } catch {
        /* not available */
      }
    }

    function openDetail(ev) {
      if (drawer) drawer.close(false);
      openSeq = String(ev.seq);
      WH.router.setQuery({ ev: openSeq });
      const index = events.findIndex((e) => e.seq === ev.seq);
      const older = index >= 0 ? events[index + 1] : null;
      const newer = index > 0 ? events[index - 1] : null;
      const style = actionStyle(ev.action);

      const linkOk = older ? older.hash === ev.prev_hash : null;
      const dl = (rows) => h("dl", { class: "wh-kv" }, rows.filter(Boolean).map(([k, v]) => [h("dt", null, k), h("dd", null, v)]));
      const mono = (value, label) => value
        ? h("span", { class: "wh-hash" }, h("code", null, value), ui.copyButton(value, { label: `Copy ${label}` }))
        : h("span", { class: "wh-muted" }, "—");
      let detailsText = "";
      try {
        detailsText = ev.details == null ? "" : typeof ev.details === "string" ? JSON.stringify(JSON.parse(ev.details), null, 2) : JSON.stringify(ev.details, null, 2);
      } catch {
        detailsText = String(ev.details);
      }

      const body = h("div", { class: "wh-audit-detail" },
        h("div", { class: `wh-audit-detail-head wh-tone--${style.tone}` }, ui.icon(style.icon, { size: 18 }), h("strong", null, WH.fmt.humanize(ev.action)), h("code", null, ev.action)),
        dl([
          ["When", h("span", null, h("span", { class: "wh-block" }, WH.fmt.utc(ev.ts)), h("span", { class: "wh-muted" }, WH.fmt.local(ev.ts)))],
          ["Who", h("span", null, ev.full_name || "System", ev.username ? h("span", { class: "wh-muted" }, ` (${ev.username}${ev.role ? ` · ${ev.role}` : ""})`) : null)],
          (ev.document_id != null || ev.screening_no) && ["Record", h("span", null,
            [ev.protocol_no && `Protocol ${ev.protocol_no}`, ev.screening_no && `Subject ${ev.screening_no}`, ev.visit_name].filter(Boolean).join(" · ") || "",
            ev.document_id != null && h("a", { class: "wh-block", href: `#/doc/${encodeURIComponent(ev.document_id)}/history` }, `Document ${ev.document_id} history →`))],
          ev.field && ["Field", h("code", null, ev.field)],
          (ev.old_value != null || ev.new_value != null) && ["Change", h("div", { class: "wh-change-box" },
            h("div", null, h("span", { class: "wh-change-label" }, "Old"), h("del", null, ev.old_value == null || ev.old_value === "" ? "empty" : String(ev.old_value))),
            h("div", null, h("span", { class: "wh-change-label" }, "New"), h("ins", null, ev.new_value == null || ev.new_value === "" ? "empty" : String(ev.new_value))))],
          ev.reason && ["Reason", ev.reason],
          ["Client", h("span", null, ev.client_ip || "—", ev.app_version ? h("span", { class: "wh-muted" }, ` · app ${ev.app_version}`) : null)],
          ev.delivery && ["EDC delivery", h("span", null, ui.chip(ev.delivery.status || "pending", { size: "sm" }),
            ev.delivery.attempts ? h("span", { class: "wh-muted" }, ` ${WH.fmt.plural(ev.delivery.attempts, "attempt")}`) : null,
            ev.delivery.sent_at ? h("span", { class: "wh-muted wh-block" }, `Sent ${WH.fmt.both(ev.delivery.sent_at)}`) : null,
            ev.delivery.last_error ? h("span", { class: "wh-bad wh-block" }, ev.delivery.last_error) : null)],
        ]),
        h("h3", { class: "wh-subhead" }, "Chain"),
        dl([
          ["Sequence", `#${ev.seq}`],
          ["Event ID", mono(ev.id, "event ID")],
          ["Hash", mono(ev.hash, "hash")],
          ["Previous hash", mono(ev.prev_hash, "previous hash")],
          older && ["Link", linkOk
            ? h("span", { class: "wh-good" }, ui.icon("check-circle", { size: 14 }), ` Matches event #${older.seq}`)
            : h("span", { class: "wh-bad" }, ui.icon("alert-triangle", { size: 14 }), older.seq === ev.seq - 1 ? ` Does not match event #${older.seq}` : " Previous event is filtered out — verify the chain above")],
        ]),
        detailsText && h("h3", { class: "wh-subhead" }, "Details"),
        detailsText && h("div", { class: "wh-json-wrap" }, h("pre", { class: "wh-json" }, detailsText), ui.copyButton(detailsText, { label: "Copy details JSON" })));

      drawer = ui.sheet({
        title: `Event #${ev.seq}`,
        description: `${WH.fmt.humanize(ev.action)} · ${WH.fmt.relative(ev.ts)}`,
        body,
        size: "md",
        actions: [
          { label: "Newer", kind: "quiet", icon: "chevron-left", disabled: !newer, onClick: () => { setTimeout(() => openDetail(newer)); return true; } },
          { label: "Older", kind: "quiet", icon: "chevron-right", disabled: !older, onClick: () => { setTimeout(() => openDetail(older)); return true; } },
        ],
        onClose: () => {
          drawer = null;
          if (openSeq === String(ev.seq)) {
            openSeq = null;
            WH.router.setQuery({ ev: null });
          }
          const rowBtn = listEl.querySelector(`[data-seq="${CSS.escape(String(ev.seq))}"]`);
          if (rowBtn) {
            rowBtn.classList.add("is-flash");
            setTimeout(() => rowBtn.classList.remove("is-flash"), 900);
          }
        },
      });
    }

    /* ------------------------------------------------------------ verify */

    async function verify(manual) {
      WH.clear(verifySlot).appendChild(h("div", { class: "wh-integrity-body is-checking" },
        h("span", { class: "wh-integrity-icon" }, ui.spinner(20)),
        h("div", null, h("strong", null, "Verifying the chain…"), h("span", { class: "wh-muted" }, "Recomputing every hash from the first event."))));
      try {
        const r = await WH.api("/api/audit/verify");
        const ok = Boolean(r.ok);
        verifySlot.classList.toggle("is-ok", ok);
        verifySlot.classList.toggle("is-broken", !ok);
        WH.clear(verifySlot).appendChild(h("div", { class: "wh-integrity-body" },
          h("span", { class: "wh-integrity-icon" }, ui.icon(ok ? "shield" : "shield-alert", { size: 24 })),
          h("div", { class: "wh-integrity-copy" },
            h("strong", null, ok ? "Chain intact" : `Chain broken at event #${r.broken_at}`),
            h("span", { class: "wh-muted" },
              `${WH.fmt.plural(r.checked || 0, "event")} verified · `, ui.time(r.verified_at || new Date().toISOString())),
            r.head && h("span", { class: "wh-hash wh-hash--sm" }, h("span", { class: "wh-muted" }, "Head "), h("code", { title: r.head }, `${String(r.head).slice(0, 16)}…`), ui.copyButton(r.head, { label: "Copy head hash" }))),
          h("div", { class: "wh-integrity-actions" },
            !ok && r.broken_at != null && h("button", { type: "button", class: "btn btn-quiet wh-btn wh-btn-sm", onClick: () => openDetailBySeq(r.broken_at) }, "Inspect event"),
            h("button", { type: "button", class: "btn btn-quiet wh-btn wh-btn-sm", onClick: () => verify(true) }, ui.icon("refresh", { size: 14 }), "Re-verify"))));
        if (manual) ui.toast(ok ? { message: "Audit chain verified", detail: `${WH.fmt.plural(r.checked || 0, "event")} intact` } : { message: "The audit chain is broken", detail: `First bad event: #${r.broken_at}`, kind: "error" });
      } catch (error) {
        WH.clear(verifySlot).appendChild(ui.errorState(error, { title: "Couldn't verify the chain", compact: true, retry: () => verify(true) }));
      }
    }

    /* ---------------------------------------------------------- delivery */

    async function delivery() {
      if (WH.features && WH.features.audit_delivery === false) {
        deliverySlot.hidden = true;
        return;
      }
      WH.clear(deliverySlot).appendChild(ui.sk.lines(3));
      try {
        const d = await WH.api("/api/audit/delivery", { background: true });
        const c = d.counts || {};
        const held = d.mode === "held";
        WH.clear(deliverySlot).append(
          h("header", { class: "wh-delivery-head" },
            h("h2", { class: "wh-section-title" }, "Audit delivery to EDC"),
            ui.chip(held ? "held" : "sent", { size: "sm", label: held ? "Held" : "Live" })),
          h("p", { class: "wh-muted wh-ellipsis", title: d.endpoint || "" }, d.endpoint || "No endpoint configured"),
          h("dl", { class: "wh-stats wh-stats--compact" },
            [["pending", "Pending", c.pending], ["sent", "Sent", c.sent], ["failed", "Failed", c.failed], ["held", "Held", c.held]].map(([s, label, n]) =>
              h("div", { class: ["wh-stat", `wh-stat--${ui.statusInfo(s).tone}`] }, h("dt", null, ui.icon(ui.statusInfo(s).icon, { size: 13 }), label), h("dd", null, WH.fmt.number(n || 0))))),
          h("div", { class: "wh-delivery-foot" },
            d.oldest_pending ? h("span", { class: "wh-muted" }, "Oldest pending ", ui.time(d.oldest_pending)) : h("span", { class: "wh-muted" }, "Nothing waiting"),
            WH.can.admin() && (c.failed || c.pending) ? h("button", { type: "button", class: "btn btn-quiet wh-btn wh-btn-sm", onClick: retryDelivery }, ui.icon("refresh", { size: 14 }), "Retry failed") : null));
      } catch (error) {
        if (error.status === 403 || error.status === 404) deliverySlot.hidden = true;
        else WH.clear(deliverySlot).appendChild(ui.errorState(error, { title: "Couldn't load delivery status", compact: true, retry: delivery }));
      }
    }

    async function retryDelivery(event) {
      const btn = event.currentTarget;
      btn.disabled = true;
      btn.classList.add("is-busy");
      try {
        const r = await WH.api("/api/audit/delivery/retry", { method: "POST" });
        ui.toast({ message: r.requeued ? `${WH.fmt.plural(r.requeued, "event")} queued again` : "Nothing needed retrying" });
        delivery();
      } catch (error) {
        ui.toast({ message: "Couldn't retry delivery", detail: error.detail, kind: "error" });
        btn.disabled = false;
        btn.classList.remove("is-busy");
      }
    }

    /* "New events" pill: peek at the newest event every 30s. */
    const peek = WH.poll(async () => {
      if (!events.length) return;
      const data = await WH.api("/api/audit", { query: { ...f, limit: 1 }, background: true });
      const top = (data.events || [])[0];
      newPill.hidden = !(top && top.seq > events[0].seq);
    }, 30000);

    sync();
    loadActions();
    load();
    verify(ctx.query.get("verify") === "1");
    delivery();
    const offFeatures = WH.bus.on("features", delivery);

    return {
      update(next) {
        const nf = readFilters(next.query);
        const changed = FILTERS.some((k) => nf[k] !== f[k]);
        if (changed) {
          f = nf;
          search.querySelector("input").value = f.q;
          userInput.value = f.user;
          subjectInput.value = f.subject;
          docInput.value = f.document_id;
          fromInput.value = f.from;
          toInput.value = f.to;
          actionSelect.value = f.action;
          sync();
          load();
        }
        const ev = next.query.get("ev");
        if (ev && ev !== openSeq) openDetailBySeq(ev);
        if (next.query.get("verify") === "1") verify(true);
      },
      destroy() {
        token += 1;
        observer.disconnect();
        peek.stop();
        offFeatures();
        textChanged.cancel();
        if (drawer) drawer.close(false);
      },
    };
  }

  WH.router.add("/audit", {
    name: "audit",
    nav: "audit",
    title: "Audit trail",
    crumbs: () => [{ label: "Audit trail" }],
    mount,
  });
})();
