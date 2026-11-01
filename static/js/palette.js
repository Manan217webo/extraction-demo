/* Webo Healthtech eSource — command palette and keyboard shortcuts.

   Cmd/Ctrl-K opens a palette that jumps anywhere, finds documents (recent
   ones instantly, then /api/docs?q= as you type) and runs actions. "?" shows
   every shortcut; "g" then a letter goes to a section. */

(function () {
  "use strict";

  const WH = window.WH;
  const { h, ui } = WH;
  const isMac = /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent);
  const MOD = isMac ? "⌘" : "Ctrl";

  /* ---------------------------------------------------------- commands */

  const GOTO = [
    { key: "i", label: "Inbox", path: "/inbox", icon: "inbox", keywords: "worklist documents queue" },
    { key: "s", label: "Sources", path: "/sources", icon: "folder", keywords: "file server folders scan", feature: "sources" },
    { key: "b", label: "Batches", path: "/batches", icon: "layers", keywords: "jobs runs progress" },
    { key: "a", label: "Audit trail", path: "/audit", icon: "shield", keywords: "log history events compliance" },
    { key: "u", label: "Upload a PDF", path: "/upload", icon: "upload", keywords: "new document file", write: true },
    { key: "m", label: "Admin · Users", path: "/admin", icon: "users", keywords: "users roles accounts", admin: true },
  ];

  function allowed(item) {
    if (item.admin && !WH.can.admin()) return false;
    if (item.write && !WH.can.write()) return false;
    if (item.feature && WH.features && WH.features[item.feature] === false) return false;
    return true;
  }

  function staticCommands() {
    const go = GOTO.filter(allowed).map((g) => ({
      id: `go-${g.path}`,
      group: "Go to",
      label: g.label,
      icon: g.icon,
      keywords: g.keywords,
      shortcut: ["g", g.key],
      run: () => WH.router.go(g.path),
    }));
    const inboxViews = [
      ["ready", "Ready for review", "sparkle"],
      ["in_review", "In review", "pen"],
      ["needs_input", "Needs input", "alert-circle"],
      ["failed", "Failed", "x-circle"],
    ].map(([status, label, icon]) => ({
      id: `inbox-${status}`,
      group: "Go to",
      label: `Inbox · ${label}`,
      icon,
      keywords: `inbox filter ${status}`,
      run: () => WH.router.go("/inbox", { status }),
    }));
    inboxViews.push({
      id: "inbox-mine",
      group: "Go to",
      label: "Inbox · Assigned to me",
      icon: "user",
      keywords: "inbox mine my assigned",
      run: () => WH.router.go("/inbox", { assignee: "me" }),
    });
    const dark = WH.shell.theme() === "dark";
    const compact = WH.shell.density() === "compact";
    const actions = [
      { id: "theme", label: dark ? "Switch to light theme" : "Switch to dark theme", icon: dark ? "sun" : "moon", keywords: "dark light mode appearance theme", run: WH.shell.toggleTheme },
      { id: "density", label: compact ? "Comfortable rows" : "Compact rows", icon: compact ? "rows" : "rows-compact", keywords: "density compact comfortable spacing", run: WH.shell.toggleDensity },
      { id: "nav", label: document.documentElement.hasAttribute("data-nav-rail") ? "Expand sidebar" : "Collapse sidebar", icon: "panel-left", keywords: "sidebar navigation rail", run: WH.shell.toggleNav },
      { id: "shortcuts", label: "Keyboard shortcuts", icon: "keyboard", keywords: "help keys hotkeys", shortcut: ["?"], run: () => setTimeout(openShortcuts, 30) },
      { id: "verify", label: "Verify the audit chain", icon: "shield", keywords: "integrity hash audit verify", run: () => WH.router.go("/audit", { verify: "1" }) },
      { id: "logout", label: "Sign out", icon: "log-out", keywords: "logout log out exit", run: () => { const b = document.getElementById("logout-btn"); if (b) b.click(); } },
    ].map((a) => ({ group: "Actions", ...a }));
    return [...go, ...inboxViews, ...actions];
  }

  function docCommand(doc, group) {
    const meta = [doc.screening_no, doc.visit_name, doc.protocol_no].filter(Boolean).join(" · ");
    return {
      id: `doc-${doc.id}`,
      group,
      label: doc.filename || `Document ${doc.id}`,
      hint: meta,
      icon: "pdf",
      status: doc.status,
      keywords: `${meta} ${doc.path || ""}`,
      run: (event) => WH.router.go(event && event.shiftKey ? `/doc/${doc.id}/history` : `/review/${doc.id}`),
    };
  }

  /* Subsequence fuzzy match: rewards consecutive letters and word starts.
     Returns {score, hits:[indexes in label]} or null. */
  function fuzzy(query, item) {
    if (!query) return { score: 0, hits: [] };
    const q = query.toLowerCase();
    const label = item.label.toLowerCase();
    const direct = label.indexOf(q);
    if (direct >= 0) {
      return { score: 100 - direct + (direct === 0 ? 50 : 0), hits: Array.from({ length: q.length }, (_, i) => direct + i) };
    }
    let score = 0;
    let last = -2;
    const hits = [];
    let li = 0;
    for (const ch of q) {
      if (ch === " ") continue;
      const found = label.indexOf(ch, li);
      if (found < 0) {
        const kw = (item.keywords || "").toLowerCase();
        return kw.includes(q) ? { score: 10, hits: [] } : null;
      }
      score += found === last + 1 ? 6 : 1;
      if (found === 0 || label[found - 1] === " ") score += 4;
      hits.push(found);
      last = found;
      li = found + 1;
    }
    return { score, hits };
  }

  function highlight(label, hits) {
    if (!hits || !hits.length) return label;
    const set = new Set(hits);
    const out = [];
    let run = "";
    let inHit = false;
    for (let i = 0; i < label.length; i += 1) {
      const hit = set.has(i);
      if (hit !== inHit && run) {
        out.push(inHit ? h("mark", { class: "wh-pal-hit" }, run) : run);
        run = "";
      }
      inHit = hit;
      run += label[i];
    }
    if (run) out.push(inHit ? h("mark", { class: "wh-pal-hit" }, run) : run);
    return out;
  }

  /* ------------------------------------------------------------ palette */

  let open = null;

  function openPalette(initial = "") {
    if (open) {
      open.input.focus();
      return;
    }
    if (!WH.me) return;
    const listId = "wh-pal-list";
    const input = h("input", {
      class: "wh-pal-input",
      type: "text",
      role: "combobox",
      "aria-expanded": "true",
      "aria-controls": listId,
      "aria-autocomplete": "list",
      "aria-label": "Search commands and documents",
      placeholder: "Search documents, jump to a page, or run a command…",
      autocomplete: "off",
      spellcheck: "false",
      value: initial,
    });
    const list = h("div", { id: listId, class: "wh-pal-list", role: "listbox", "aria-label": "Results" });
    const status = h("div", { class: "wh-pal-status", "aria-live": "polite" });
    const foot = h("div", { class: "wh-pal-foot", "aria-hidden": "true" },
      h("span", null, ui.kbd("↑", "↓"), " move"),
      h("span", null, ui.kbd("↵"), " open"),
      h("span", null, ui.kbd("⇧", "↵"), " document history"),
      h("span", null, ui.kbd("esc"), " close"));

    const layer = ui.modal({
      title: "Command palette",
      size: "palette",
      className: "wh-palette",
      body: h("div", { class: "wh-pal" },
        h("div", { class: "wh-pal-search" }, ui.icon("search", { size: 18 }), input, ui.spinner(14)),
        list, status, foot),
      initialFocus: ".wh-pal-input",
      onClose: () => {
        if (controller) controller.abort();
        open = null;
      },
    });

    let items = [];
    let active = 0;
    let serverDocs = [];
    let controller = null;
    let searching = false;
    const commands = staticCommands();

    function compute() {
      const q = input.value.trim();
      const recents = WH.recentDocs.list().map((d) => docCommand(d, "Recent documents"));
      const recentIds = new Set(recents.map((r) => r.id));
      const server = serverDocs.map((d) => docCommand(d, "Documents")).filter((d) => !recentIds.has(d.id) || !q);
      let pool;
      if (!q) {
        pool = [...recents.slice(0, 5), ...commands].map((item) => ({ item, m: { score: 0, hits: [] } }));
      } else {
        pool = [...recents, ...commands]
          .map((item) => ({ item, m: fuzzy(q, item) }))
          .filter((x) => x.m)
          .sort((a, b) => b.m.score - a.m.score);
        // Server matches already matched on the server; keep their order.
        pool = pool.concat(server.map((item) => ({ item, m: fuzzy(q, item) || { score: 0, hits: [] } })));
      }
      // Group in a stable order.
      const order = ["Recent documents", "Documents", "Go to", "Actions"];
      pool.sort((a, b) => order.indexOf(a.item.group) - order.indexOf(b.item.group));
      return pool.slice(0, 40);
    }

    function render() {
      items = compute();
      active = Math.min(active, Math.max(items.length - 1, 0));
      WH.clear(list);
      let group = null;
      let groupEl = null;
      items.forEach(({ item, m }, i) => {
        if (item.group !== group) {
          group = item.group;
          const gid = `wh-pal-g-${i}`;
          groupEl = h("div", { role: "group", "aria-labelledby": gid, class: "wh-pal-group" },
            h("div", { id: gid, class: "wh-pal-group-label", role: "presentation" }, group));
          list.appendChild(groupEl);
        }
        const option = h("div", {
          id: `wh-pal-o-${i}`,
          role: "option",
          class: "wh-pal-option",
          "aria-selected": String(i === active),
          onMousemove: () => {
            if (active !== i) setActive(i, false);
          },
          onClick: (event) => runItem(i, event),
        },
        h("span", { class: "wh-pal-icon" }, ui.icon(item.icon || "arrow-right", { size: 17 })),
        h("span", { class: "wh-pal-text" },
          h("span", { class: "wh-pal-label" }, highlight(item.label, m.hits)),
          item.hint && h("span", { class: "wh-pal-hint" }, item.hint)),
        item.status && ui.chip(item.status, { size: "sm" }),
        item.shortcut && ui.kbd(...item.shortcut));
        groupEl.appendChild(option);
      });
      if (!items.length) {
        list.appendChild(h("div", { class: "wh-pal-empty" },
          searching ? "Searching documents…" : `Nothing matches “${input.value.trim()}”. Try a subject number, file name or protocol.`));
      }
      input.setAttribute("aria-activedescendant", items.length ? `wh-pal-o-${active}` : "");
      status.textContent = items.length ? `${items.length} results` : "No results";
    }

    function setActive(i, scroll = true) {
      const prev = list.querySelector(`#wh-pal-o-${active}`);
      if (prev) prev.setAttribute("aria-selected", "false");
      active = i;
      const next = list.querySelector(`#wh-pal-o-${active}`);
      if (next) {
        next.setAttribute("aria-selected", "true");
        if (scroll) next.scrollIntoView({ block: "nearest" });
      }
      input.setAttribute("aria-activedescendant", next ? next.id : "");
    }

    function runItem(i, event) {
      const entry = items[i];
      if (!entry) return;
      layer.close(true);
      entry.item.run(event);
    }

    const search = WH.debounce(async () => {
      const q = input.value.trim();
      if (controller) controller.abort();
      if (q.length < 2) {
        serverDocs = [];
        searching = false;
        layer.panel.classList.remove("is-searching");
        render();
        return;
      }
      controller = new AbortController();
      searching = true;
      layer.panel.classList.add("is-searching");
      try {
        const data = await WH.api("/api/docs", { query: { q, limit: 8 }, signal: controller.signal });
        serverDocs = data.documents || [];
      } catch (error) {
        if (error.name === "AbortError") return;
        serverDocs = [];
      }
      searching = false;
      layer.panel.classList.remove("is-searching");
      if (open) render();
    }, 160);

    input.addEventListener("input", () => {
      active = 0;
      render();
      search();
    });
    input.addEventListener("keydown", (event) => {
      if (event.key === "ArrowDown" || (event.ctrlKey && event.key === "n")) {
        event.preventDefault();
        if (items.length) setActive((active + 1) % items.length);
      } else if (event.key === "ArrowUp" || (event.ctrlKey && event.key === "p")) {
        event.preventDefault();
        if (items.length) setActive((active - 1 + items.length) % items.length);
      } else if (event.key === "Enter") {
        event.preventDefault();
        runItem(active, event);
      } else if (event.key === "Home" && !input.value) {
        event.preventDefault();
        setActive(0);
      }
    });

    open = { layer, input };
    render();
    if (initial) search();
  }
  WH.palette = { open: openPalette };

  document.getElementById("cmdk-btn").addEventListener("click", () => openPalette());

  /* ---------------------------------------------------- shortcuts sheet */

  const SHORTCUTS = [
    ["Anywhere", [
      [[MOD, "K"], "Open the command palette"],
      [["?"], "Show this list"],
      [["g", "i"], "Go to the inbox"],
      [["g", "s"], "Go to sources"],
      [["g", "b"], "Go to batches"],
      [["g", "a"], "Go to the audit trail"],
      [["g", "u"], "Upload a PDF"],
      [["/"], "Search on this screen"],
      [["Esc"], "Close a dialog or clear a search"],
    ]],
    ["Inbox", [
      [["J"], "Next document"],
      [["K"], "Previous document"],
      [["↵"], "Open the selected document"],
      [["A"], "Assign the selected document to me"],
      [["H"], "Open its history"],
    ]],
    ["Sources", [
      [["Space"], "Select or clear the focused row"],
      [["⇧", "click"], "Select a range"],
      [["↵"], "Open the focused folder"],
      [["Backspace"], "Up one folder"],
    ]],
  ];

  let shortcutsOpen = false;
  function openShortcuts() {
    if (shortcutsOpen) return;
    shortcutsOpen = true;
    const body = h("div", { class: "wh-shortcuts" },
      SHORTCUTS.map(([group, rows]) =>
        h("section", { class: "wh-shortcuts-group" },
          h("h3", null, group),
          h("dl", null, rows.map(([keys, text]) => [
            h("dt", null, ui.kbd(...keys)),
            h("dd", null, text),
          ])))));
    ui.modal({
      title: "Keyboard shortcuts",
      description: "Shortcuts pause while you're typing in a field.",
      body,
      size: "md",
      onClose: () => {
        shortcutsOpen = false;
      },
    });
  }
  WH.palette.shortcuts = openShortcuts;

  /* ----------------------------------------------------- global listener */

  let pendingG = null;
  const gHint = h("div", { class: "wh-g-hint", role: "status", hidden: true },
    h("strong", null, "Go to…"),
    GOTO.map((g) => h("span", { dataset: { key: g.key } }, ui.kbd(g.key), ` ${g.label.split(" ·")[0]}`)));
  document.body.appendChild(gHint);

  function clearG() {
    clearTimeout(pendingG);
    pendingG = null;
    gHint.hidden = true;
  }

  document.addEventListener("keydown", (event) => {
    const key = event.key;
    if ((event.metaKey || event.ctrlKey) && !event.altKey && key.toLowerCase() === "k") {
      if (!WH.me) return;
      event.preventDefault();
      if (open) open.layer.close(false);
      else if (!ui.hasOpenLayer()) openPalette();
      return;
    }
    if (event.metaKey || event.ctrlKey || event.altKey) return;
    if (!WH.me || WH.isTyping(event.target) || ui.hasOpenLayer()) {
      clearG();
      return;
    }
    if (pendingG) {
      const target = GOTO.find((g) => g.key === key.toLowerCase());
      clearG();
      if (target && allowed(target)) {
        event.preventDefault();
        WH.router.go(target.path);
      }
      return;
    }
    if (key === "g") {
      pendingG = setTimeout(clearG, 1500);
      // Show the hint only if the second key doesn't come quickly.
      setTimeout(() => {
        if (pendingG) {
          gHint.querySelectorAll("[data-key]").forEach((el) => {
            const item = GOTO.find((g) => g.key === el.dataset.key);
            el.hidden = !allowed(item);
          });
          gHint.hidden = false;
        }
      }, 350);
      return;
    }
    if (key === "?") {
      event.preventDefault();
      openShortcuts();
    }
  });
})();
