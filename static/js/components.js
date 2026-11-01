/* Webo Healthtech eSource — components.

   Shared UI pieces: icons, status chips, skeleton loading, toasts (with
   undo), modal / side sheet / confirm with focus trap and focus return,
   empty and error states, and small form controls. All text is set with
   textContent; the only markup strings here are the constant icon paths. */

(function () {
  "use strict";

  const WH = (window.WH = window.WH || {});
  const h = WH.h;
  const ui = (WH.ui = {});

  /* ---------------------------------------------------------------- icons

     24×24 stroke icons (constant markup, never data). */
  const ICONS = {
    inbox: '<path d="M4 13.5V18a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-4.5"/><path d="M4 13.5 6.4 6.3A2 2 0 0 1 8.3 5h7.4a2 2 0 0 1 1.9 1.3l2.4 7.2"/><path d="M4 13.5h4.2l1.3 2.5h5l1.3-2.5H20"/>',
    folder: '<path d="M3.5 7.5A2 2 0 0 1 5.5 5.5h3.6l2 2.2h7.4a2 2 0 0 1 2 2v7.8a2 2 0 0 1-2 2h-13a2 2 0 0 1-2-2z"/>',
    "folder-open": '<path d="M3.5 17.5v-10a2 2 0 0 1 2-2h3.6l2 2.2h6.4a2 2 0 0 1 2 2v1"/><path d="M3.6 18.4 6 11.8a1.5 1.5 0 0 1 1.4-1h12.3a1 1 0 0 1 .9 1.4l-2.3 5.9a1.5 1.5 0 0 1-1.4 1H4.5a1 1 0 0 1-.9-.7z"/>',
    file: '<path d="M14 3.5H7.5a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2h9a2 2 0 0 0 2-2V8z"/><path d="M14 3.5V8h4.5"/>',
    pdf: '<path d="M14 3.5H7.5a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2h9a2 2 0 0 0 2-2V8z"/><path d="M14 3.5V8h4.5"/><path d="M8.5 13h7M8.5 16.5h4.5"/>',
    layers: '<path d="m12 4 8.5 4.5L12 13 3.5 8.5z"/><path d="m3.5 12.5 8.5 4.5 8.5-4.5"/><path d="m3.5 16.3 8.5 4.2 8.5-4.2"/>',
    shield: '<path d="M12 3.5 5 6v5.5c0 4.4 3 7.8 7 9 4-1.2 7-4.6 7-9V6z"/><path d="m9 12 2.2 2.2L15.5 10"/>',
    "shield-alert": '<path d="M12 3.5 5 6v5.5c0 4.4 3 7.8 7 9 4-1.2 7-4.6 7-9V6z"/><path d="M12 8.5v4M12 15.5v.01"/>',
    users: '<circle cx="9" cy="8.5" r="3.2"/><path d="M3.5 19c.6-3.2 2.8-5 5.5-5s4.9 1.8 5.5 5"/><path d="M15.5 5.6a3.2 3.2 0 0 1 0 5.8M17.5 14.3c1.6.7 2.7 2.3 3 4.7"/>',
    search: '<circle cx="11" cy="11" r="6.5"/><path d="m16 16 4 4"/>',
    "chevron-right": '<path d="m9.5 6 6 6-6 6"/>',
    "chevron-left": '<path d="m14.5 6-6 6 6 6"/>',
    "chevron-down": '<path d="m6 9.5 6 6 6-6"/>',
    x: '<path d="M6.5 6.5l11 11M17.5 6.5l-11 11"/>',
    check: '<path d="m5 12.5 4.5 4.5L19 7.5"/>',
    "check-circle": '<circle cx="12" cy="12" r="8.5"/><path d="m8.3 12.3 2.6 2.6 5-5.2"/>',
    "check-seal": '<path d="M12 3.2 14.3 5l2.9-.3.9 2.8 2.5 1.5-.9 2.8 1 2.8-2.6 1.4-.8 2.8-2.9-.2L12 20.8 9.7 19l-2.9.3-.9-2.8L3.4 15l.9-2.8-1-2.8 2.6-1.4.8-2.8 2.9.2z"/><path d="m8.8 12.2 2.2 2.2 4.3-4.4"/>',
    "alert-triangle": '<path d="M10.3 4.6 3.2 17a2 2 0 0 0 1.7 3h14.2a2 2 0 0 0 1.7-3L13.7 4.6a2 2 0 0 0-3.4 0z"/><path d="M12 9.5v4M12 16.8v.01"/>',
    "alert-circle": '<circle cx="12" cy="12" r="8.5"/><path d="M12 8v4.5M12 15.8v.01"/>',
    "x-circle": '<circle cx="12" cy="12" r="8.5"/><path d="m9.2 9.2 5.6 5.6M14.8 9.2l-5.6 5.6"/>',
    clock: '<circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/>',
    loader: '<path d="M12 3.5a8.5 8.5 0 1 0 8.5 8.5"/>',
    pen: '<path d="M4.5 19.5 5.3 16 15.8 5.5a1.8 1.8 0 0 1 2.6 0l.1.1a1.8 1.8 0 0 1 0 2.6L8 18.7z"/><path d="m14 7.3 2.7 2.7"/>',
    refresh: '<path d="M19.5 12a7.5 7.5 0 1 1-2.2-5.3"/><path d="M19.5 4.5v4h-4"/>',
    ban: '<circle cx="12" cy="12" r="8.5"/><path d="m6 6 12 12"/>',
    upload: '<path d="M12 15.5V4.5m0 0-4 4m4-4 4 4"/><path d="M4.5 15v2.5a2 2 0 0 0 2 2h11a2 2 0 0 0 2-2V15"/>',
    download: '<path d="M12 4.5v11m0 0-4-4m4 4 4-4"/><path d="M4.5 15v2.5a2 2 0 0 0 2 2h11a2 2 0 0 0 2-2V15"/>',
    external: '<path d="M13.5 4.5h6v6M19.5 4.5l-8 8"/><path d="M17.5 13.5v4a2 2 0 0 1-2 2h-9a2 2 0 0 1-2-2v-9a2 2 0 0 1 2-2h4"/>',
    sun: '<circle cx="12" cy="12" r="3.8"/><path d="M12 2.8v2M12 19.2v2M2.8 12h2M19.2 12h2M5.5 5.5l1.4 1.4M17.1 17.1l1.4 1.4M5.5 18.5l1.4-1.4M17.1 6.9l1.4-1.4"/>',
    moon: '<path d="M19.5 14.2A7.8 7.8 0 0 1 9.8 4.5a7.8 7.8 0 1 0 9.7 9.7z"/>',
    command: '<path d="M9 9V6.5A2.5 2.5 0 1 0 6.5 9H9zm0 0h6m-6 0v6m6-6V6.5A2.5 2.5 0 1 1 17.5 9H15zm0 0v6m0 0h-6m6 0v2.5a2.5 2.5 0 1 0 2.5-2.5H15zm-6 0v2.5A2.5 2.5 0 1 1 6.5 15H9z"/>',
    keyboard: '<rect x="3" y="6.5" width="18" height="11" rx="2"/><path d="M7 10h.01M10 10h.01M13 10h.01M16 10h.01M7.5 14h9"/>',
    menu: '<path d="M4.5 7h15M4.5 12h15M4.5 17h15"/>',
    "panel-left": '<rect x="3.5" y="4.5" width="17" height="15" rx="2.5"/><path d="M9.5 4.5v15"/>',
    filter: '<path d="M4.5 6h15M7.5 12h9M10.5 18h3"/>',
    history: '<path d="M4.5 12a7.5 7.5 0 1 0 2.2-5.3L4.5 9"/><path d="M4.5 4.5V9H9"/><path d="M12 8v4.2l2.8 1.8"/>',
    user: '<circle cx="12" cy="8.5" r="3.5"/><path d="M5 19.5c.8-3.4 3.6-5.5 7-5.5s6.2 2.1 7 5.5"/>',
    "user-plus": '<circle cx="10" cy="8.5" r="3.5"/><path d="M3.5 19.5c.8-3.4 3.4-5.5 6.5-5.5 1.4 0 2.7.4 3.8 1.2M18 13v6M15 16h6"/>',
    lock: '<rect x="5" y="10.5" width="14" height="9.5" rx="2"/><path d="M8 10.5V8a4 4 0 0 1 8 0v2.5"/>',
    unlock: '<rect x="5" y="10.5" width="14" height="9.5" rx="2"/><path d="M8 10.5V8a4 4 0 0 1 7.6-1.7"/>',
    flag: '<path d="M5.5 20.5V4.5"/><path d="M5.5 5h11l-2 4 2 4h-11"/>',
    bell: '<path d="M6.5 16.5V11a5.5 5.5 0 0 1 11 0v5.5l1.5 2h-14z"/><path d="M10 20.5h4"/>',
    "wifi-off": '<path d="m4 4 16 16"/><path d="M8.5 16.2a5 5 0 0 1 7 0M5 12.6a10 10 0 0 1 4.4-2.4M19 12.6a10 10 0 0 0-3-1.9M2.5 9a14.5 14.5 0 0 1 4-2.4M21.5 9A14.5 14.5 0 0 0 11 5.6"/><path d="M12 19.5h.01"/>',
    link: '<path d="M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1"/><path d="M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1"/>',
    copy: '<rect x="8.5" y="8.5" width="11" height="11" rx="2"/><path d="M15.5 8.5V6.5a2 2 0 0 0-2-2h-7a2 2 0 0 0-2 2v7a2 2 0 0 0 2 2h2"/>',
    "arrow-right": '<path d="M4.5 12h15m0 0-5.5-5.5M19.5 12l-5.5 5.5"/>',
    sparkle: '<path d="M12 3.5c.6 4.2 2.3 5.9 6.5 6.5-4.2.6-5.9 2.3-6.5 6.5-.6-4.2-2.3-5.9-6.5-6.5 4.2-.6 5.9-2.3 6.5-6.5z"/><path d="M18.5 15.5c.3 1.6 1 2.3 2.5 2.5-1.6.3-2.2 1-2.5 2.5-.3-1.6-1-2.2-2.5-2.5 1.6-.3 2.2-1 2.5-2.5z"/>',
    eye: '<path d="M2.8 12S6 5.5 12 5.5 21.2 12 21.2 12 18 18.5 12 18.5 2.8 12 2.8 12z"/><circle cx="12" cy="12" r="2.8"/>',
    "eye-off": '<path d="m4 4 16 16"/><path d="M10.6 6A9.6 9.6 0 0 1 12 5.5c6 0 9.2 6.5 9.2 6.5a16 16 0 0 1-2.6 3.4M6.6 7.4A15.6 15.6 0 0 0 2.8 12S6 18.5 12 18.5c1.6 0 3-.4 4.2-1.1"/><path d="M9.9 10a2.8 2.8 0 0 0 4 4"/>',
    signature: '<path d="M3.5 17.5c2.5 0 3.5-9.5 6-9.5 2 0-.5 8 1.5 8 1.3 0 2-2.5 3-2.5s1 2 2.5 2"/><path d="M3.5 20.5h17"/>',
    pause: '<circle cx="12" cy="12" r="8.5"/><path d="M10 9v6M14 9v6"/>',
    send: '<path d="M20.5 3.5 10 14"/><path d="m20.5 3.5-6.5 17-4-6.5-6.5-4z"/>',
    more: '<path d="M6 12h.01M12 12h.01M18 12h.01"/>',
    plus: '<path d="M12 5v14M5 12h14"/>',
    minus: '<path d="M5 12h14"/>',
    rows: '<path d="M4.5 6.5h15M4.5 12h15M4.5 17.5h15"/>',
    "rows-compact": '<path d="M4.5 5h15M4.5 9.5h15M4.5 14h15M4.5 18.5h15"/>',
    compare: '<path d="M8 4.5v15M16 4.5v15"/><path d="M4.5 8 8 4.5 11.5 8M12.5 16l3.5 3.5 3.5-3.5"/>',
    play: '<path d="M8 5.5v13l10-6.5z"/>',
    home: '<path d="m4 11 8-6.5 8 6.5"/><path d="M6 9.5v9.5h12V9.5"/>',
    database: '<ellipse cx="12" cy="6" rx="7" ry="2.5"/><path d="M5 6v12c0 1.4 3.1 2.5 7 2.5s7-1.1 7-2.5V6M5 12c0 1.4 3.1 2.5 7 2.5s7-1.1 7-2.5"/>',
    "log-out": '<path d="M14.5 4.5h3a2 2 0 0 1 2 2v11a2 2 0 0 1-2 2h-3"/><path d="M10 16.5 5.5 12 10 7.5M5.5 12H15"/>',
  };

  ui.icon = function (name, { size = 18, label, className } = {}) {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("width", size);
    svg.setAttribute("height", size);
    svg.setAttribute("fill", "none");
    svg.setAttribute("stroke", "currentColor");
    svg.setAttribute("stroke-width", "1.8");
    svg.setAttribute("stroke-linecap", "round");
    svg.setAttribute("stroke-linejoin", "round");
    svg.setAttribute("class", `wh-icon wh-icon--${name}${className ? ` ${className}` : ""}`);
    if (label) {
      svg.setAttribute("role", "img");
      svg.setAttribute("aria-label", label);
    } else {
      svg.setAttribute("aria-hidden", "true");
      svg.setAttribute("focusable", "false");
    }
    svg.innerHTML = ICONS[name] || ICONS["alert-circle"]; // constant markup only
    return svg;
  };

  /* --------------------------------------------------------- status chips

     Every status has an icon and words, so color is never the only cue. */
  const STATUS = {
    // documents
    processing: { label: "Processing", icon: "loader", tone: "info", spin: true },
    ready: { label: "Ready for review", icon: "sparkle", tone: "brand" },
    in_review: { label: "In review", icon: "pen", tone: "warn" },
    submitted: { label: "Submitted", icon: "check-seal", tone: "success" },
    failed: { label: "Failed", icon: "x-circle", tone: "danger" },
    needs_input: { label: "Needs input", icon: "alert-circle", tone: "warn" },
    // source files
    new: { label: "New", icon: "plus", tone: "info" },
    processed: { label: "Processed", icon: "check", tone: "success" },
    changed: { label: "Changed", icon: "alert-triangle", tone: "warn" },
    unreadable: { label: "Can't be read", icon: "ban", tone: "danger" },
    // batch items and batches
    queued: { label: "Queued", icon: "clock", tone: "neutral" },
    running: { label: "Running", icon: "loader", tone: "info", spin: true },
    cancelled: { label: "Cancelled", icon: "ban", tone: "neutral" },
    done: { label: "Done", icon: "check-circle", tone: "success" },
    done_failures: { label: "Done with failures", icon: "alert-triangle", tone: "warn" },
    skipped: { label: "Skipped", icon: "minus", tone: "neutral" },
    // EDC delivery
    pending: { label: "Pending", icon: "clock", tone: "neutral" },
    sent: { label: "Sent", icon: "check", tone: "success" },
    held: { label: "Held", icon: "pause", tone: "warn" },
    unchanged: { label: "Unchanged", icon: "minus", tone: "neutral" },
    // users
    active: { label: "Active", icon: "check-circle", tone: "success" },
    inactive: { label: "Deactivated", icon: "ban", tone: "neutral" },
    locked: { label: "Locked", icon: "lock", tone: "danger" },
    // generic
    ok: { label: "OK", icon: "check-circle", tone: "success" },
    broken: { label: "Broken", icon: "shield-alert", tone: "danger" },
  };
  ui.STATUS = STATUS;

  ui.statusInfo = (status) =>
    STATUS[status] || { label: WH.fmt.humanize(status || "unknown"), icon: "alert-circle", tone: "neutral" };

  ui.chip = function (status, { label, title, size } = {}) {
    const info = ui.statusInfo(status);
    return h(
      "span",
      {
        class: ["wh-chip", `wh-chip--${info.tone}`, size === "sm" && "wh-chip--sm"],
        title: title || null,
        dataset: { status: status || "" },
      },
      ui.icon(info.icon, { size: size === "sm" ? 12 : 14, className: info.spin ? "wh-spin" : "" }),
      h("span", { class: "wh-chip-text" }, label || info.label)
    );
  };

  /* A neutral tag: "Autosave", "AI result", role names. */
  ui.tag = (text, tone = "neutral") => h("span", { class: ["wh-tag", `wh-tag--${tone}`] }, text);

  ui.kbd = (...keys) => h("span", { class: "wh-kbd-group" }, keys.map((k) => h("kbd", { class: "wh-kbd" }, k)));

  ui.spinner = (size = 16) => h("span", { class: "wh-spinner", style: { "--s": `${size}px` }, "aria-hidden": "true" });

  ui.avatar = function (name, { size = 26, title } = {}) {
    const text = String(name || "?");
    // A stable hue per person so the same reviewer always looks the same.
    let hash = 0;
    for (const ch of text) hash = (hash * 31 + ch.charCodeAt(0)) >>> 0;
    return h(
      "span",
      {
        class: "wh-avatar",
        style: { "--hue": String(hash % 360), "--s": `${size}px` },
        title: title || text,
        "aria-hidden": "true",
      },
      WH.fmt.initials(text)
    );
  };

  /* --------------------------------------------------------------- time

     <time> with relative text, exact UTC + local in the tooltip. A single
     ticker keeps every relative time on screen current. */
  ui.time = function (value, { mode = "relative" } = {}) {
    const date = WH.parseDate(value);
    if (!date) return h("span", { class: "wh-muted" }, "—");
    const el = h("time", {
      datetime: date.toISOString(),
      title: WH.fmt.both(date),
      class: "wh-time",
    });
    if (mode === "relative") {
      el.dataset.rel = "1";
      el.textContent = WH.fmt.relative(date);
    } else if (mode === "time") {
      el.textContent = WH.fmt.time(date, true);
    } else {
      el.textContent = WH.fmt.local(date);
    }
    return el;
  };
  setInterval(() => {
    document.querySelectorAll("time[data-rel]").forEach((el) => {
      el.textContent = WH.fmt.relative(el.getAttribute("datetime"));
    });
  }, 30000);

  /* ------------------------------------------------------------ skeletons

     A skeleton appears only if loading takes longer than 150ms (so fast
     responses never flash), and once shown stays at least 300ms (so it never
     blinks). Builders mirror the real layouts so nothing jumps on arrival. */
  const SK_DELAY = 150;
  const SK_MIN = 300;

  const bar = (w, cls = "") => h("span", { class: `wh-sk ${cls}`, style: { width: typeof w === "number" ? `${w}%` : w } });

  ui.sk = {
    lines(n = 3) {
      return h("div", { class: "wh-sk-lines", "aria-hidden": "true" },
        Array.from({ length: n }, (_, i) => bar(i === n - 1 ? 55 : 92 - (i % 3) * 9)));
    },
    table(rows = 8, cols = [6, 30, 14, 12, 14, 12, 10]) {
      return h("div", { class: "wh-sk-table", "aria-hidden": "true" },
        Array.from({ length: rows }, (_, r) =>
          h("div", { class: "wh-sk-row", style: { "--d": `${r * 40}ms` } },
            cols.map((w, c) => bar(`${w}%`, c === 1 ? "wh-sk--strong" : "")))));
    },
    cards(n = 3) {
      return h("div", { class: "wh-sk-cards", "aria-hidden": "true" },
        Array.from({ length: n }, () =>
          h("div", { class: "wh-sk-card" }, bar(40, "wh-sk--strong"), bar(85), bar(70), h("span", { class: "wh-sk wh-sk-progress" }))));
    },
    tree(n = 7) {
      return h("div", { class: "wh-sk-tree", "aria-hidden": "true" },
        Array.from({ length: n }, (_, i) =>
          h("div", { class: "wh-sk-treerow", style: { paddingLeft: `${(i % 3) * 14 + 8}px` } },
            h("span", { class: "wh-sk wh-sk-dot" }), bar(`${50 + ((i * 17) % 40)}%`))));
    },
    timeline(n = 6) {
      return h("div", { class: "wh-sk-timeline", "aria-hidden": "true" },
        h("span", { class: "wh-sk wh-sk-dayhead" }),
        Array.from({ length: n }, () =>
          h("div", { class: "wh-sk-event" }, h("span", { class: "wh-sk wh-sk-dot" }), bar(12), bar(26, "wh-sk--strong"), bar(40))));
    },
    lanes() {
      return h("div", { class: "wh-sk-lanes", "aria-hidden": "true" },
        Array.from({ length: 4 }, () =>
          h("div", { class: "wh-sk-lane" }, bar(50, "wh-sk--strong"),
            Array.from({ length: 3 }, () => h("div", { class: "wh-sk-card wh-sk-card--sm" }, bar(60), bar(85))))));
    },
    stat(n = 4) {
      return h("div", { class: "wh-sk-stats", "aria-hidden": "true" },
        Array.from({ length: n }, () => h("div", { class: "wh-sk-stat" }, bar(50), bar(30, "wh-sk--strong wh-sk--tall"))));
    },
  };

  /* Load into a container with skeleton timing, an error state with Retry,
     and protection against out-of-order responses (pass `isCurrent`).
       ui.load(el, {skeleton: () => ui.sk.table(), task: () => WH.api(...),
                    render: (data) => ..., label: "documents"}) */
  ui.load = async function (container, { skeleton, task, render, label = "this", isCurrent = () => true, onError }) {
    container.setAttribute("aria-busy", "true");
    let shownAt = 0;
    const timer = setTimeout(() => {
      if (!isCurrent()) return;
      WH.clear(container).appendChild(skeleton ? skeleton() : ui.sk.lines());
      shownAt = performance.now();
    }, SK_DELAY);
    const settle = async () => {
      clearTimeout(timer);
      if (shownAt) {
        const wait = SK_MIN - (performance.now() - shownAt);
        if (wait > 0) await WH.sleep(wait);
      }
    };
    try {
      const data = await task();
      await settle();
      if (!isCurrent()) return undefined;
      container.removeAttribute("aria-busy");
      WH.clear(container);
      render(data);
      return data;
    } catch (error) {
      await settle();
      container.removeAttribute("aria-busy");
      if (!isCurrent() || (error && error.name === "AbortError")) return undefined;
      if (onError && onError(error) === true) return undefined;
      WH.clear(container).appendChild(
        ui.errorState(error, {
          title: `We couldn't load ${label}`,
          retry: () => ui.load(container, { skeleton, task, render, label, isCurrent, onError }),
        })
      );
      return undefined;
    }
  };

  /* --------------------------------------------------- empty / error state */

  ui.emptyState = function ({ icon = "inbox", title, body, action, secondary, compact } = {}) {
    return h(
      "div",
      { class: ["wh-empty", compact && "wh-empty--compact"] },
      h("span", { class: "wh-empty-icon" }, ui.icon(icon, { size: compact ? 20 : 26 })),
      h("h3", { class: "wh-empty-title" }, title),
      body && h("p", { class: "wh-empty-body" }, body),
      (action || secondary) &&
        h("div", { class: "wh-empty-actions" }, action && ui.actionButton(action, "primary"), secondary && ui.actionButton(secondary, "quiet"))
    );
  };

  ui.actionButton = function (action, kind = "quiet") {
    const cls = `btn ${kind === "primary" ? "btn-primary" : "btn-quiet"} wh-btn`;
    const content = [action.icon && ui.icon(action.icon, { size: 16 }), action.label];
    if (action.href) return h("a", { class: cls, href: action.href }, content);
    return h("button", { type: "button", class: cls, onClick: action.onClick }, content);
  };

  ui.errorState = function (error, { title = "Something went wrong", retry, compact } = {}) {
    const offline = error && (error.offline || error.status === 0);
    const forbidden = error && error.status === 403;
    const detail = offline
      ? "You appear to be offline or the server can't be reached. We'll keep trying in the background."
      : (error && (error.detail || error.message)) || "Please try again.";
    const retryBtn =
      retry &&
      h("button", { type: "button", class: "btn btn-quiet wh-btn", onClick: retry }, ui.icon("refresh", { size: 16 }), "Try again");
    return h(
      "div",
      { class: ["wh-empty", "wh-empty--error", compact && "wh-empty--compact"], role: "alert" },
      h("span", { class: "wh-empty-icon" }, ui.icon(offline ? "wifi-off" : forbidden ? "lock" : "alert-triangle", { size: 24 })),
      h("h3", { class: "wh-empty-title" }, forbidden ? "You don't have access to this" : title),
      h("p", { class: "wh-empty-body" }, detail),
      error && error.status ? h("p", { class: "wh-empty-code" }, `Error ${error.status}`) : null,
      !forbidden && retryBtn && h("div", { class: "wh-empty-actions" }, retryBtn)
    );
  };

  /* ---------------------------------------------------------------- toasts

     ui.toast("Saved") or ui.toast({message, kind, action:{label,onClick},
     duration}). An action (Undo) adds a draining timer bar; hovering or
     focusing a toast pauses it. Errors stay longer. */
  function toastHost() {
    let host = document.getElementById("wh-toasts");
    if (!host) {
      host = h("div", { id: "wh-toasts", class: "wh-toasts", role: "region", "aria-label": "Notifications" });
      document.body.appendChild(host);
    }
    return host;
  }

  ui.toast = function (opts) {
    if (typeof opts === "string") opts = { message: opts };
    const { message, detail, kind = "success", action } = opts;
    const duration = opts.duration || (kind === "error" ? 8000 : action ? 6500 : 3200);
    const host = toastHost();
    const icon = { success: "check-circle", error: "alert-circle", warn: "alert-triangle", info: "bell" }[kind] || "bell";

    let remaining = duration;
    let started = performance.now();
    let timer = null;
    let closed = false;

    const close = () => {
      if (closed) return;
      closed = true;
      clearTimeout(timer);
      node.classList.add("is-leaving");
      setTimeout(() => node.remove(), WH.prefersReducedMotion() ? 0 : 220);
    };
    const pause = () => {
      clearTimeout(timer);
      remaining -= performance.now() - started;
      node.classList.add("is-paused");
    };
    const resume = () => {
      started = performance.now();
      node.classList.remove("is-paused");
      timer = setTimeout(close, Math.max(remaining, 1200));
    };

    const actionBtn =
      action &&
      h("button", {
        type: "button",
        class: "wh-toast-action",
        onClick: async () => {
          close();
          try {
            await action.onClick();
          } catch (error) {
            ui.toast({ message: error.detail || error.message, kind: "error" });
          }
        },
      }, action.label);

    const node = h(
      "div",
      {
        class: ["wh-toast", `wh-toast--${kind}`],
        role: kind === "error" ? "alert" : "status",
        style: { "--life": `${duration}ms` },
        onMouseenter: pause,
        onMouseleave: resume,
        onFocusin: pause,
        onFocusout: resume,
      },
      h("span", { class: "wh-toast-icon" }, ui.icon(icon, { size: 18 })),
      h("div", { class: "wh-toast-copy" }, h("strong", null, message), detail && h("span", null, detail)),
      actionBtn,
      h("button", { type: "button", class: "wh-toast-close", "aria-label": "Dismiss", onClick: close }, ui.icon("x", { size: 14 })),
      action && h("span", { class: "wh-toast-life", "aria-hidden": "true" })
    );
    host.appendChild(node);
    // Keep the stack short: the oldest quietly goes.
    const live = host.querySelectorAll(".wh-toast:not(.is-leaving)");
    if (live.length > 4) live[0].remove();
    timer = setTimeout(close, duration);
    return { close };
  };

  /* --------------------------------------------------------------- layers

     Modals and side sheets share one stack: Escape closes the top one, Tab is
     trapped inside it, the rest of the page is made inert, and focus returns
     to whatever opened it. */
  const stack = [];
  let inerted = [];

  function layersHost() {
    let host = document.getElementById("wh-layers");
    if (!host) {
      host = h("div", { id: "wh-layers", class: "wh-layers" });
      document.body.appendChild(host);
    }
    return host;
  }

  function setInert(on) {
    if (on && !inerted.length) {
      inerted = Array.from(document.body.children).filter(
        (el) => !["wh-layers", "wh-toasts", "wh-announcer"].includes(el.id) && el.tagName !== "SCRIPT" && !el.inert
      );
      inerted.forEach((el) => {
        el.inert = true;
      });
    } else if (!on) {
      inerted.forEach((el) => {
        el.inert = false;
      });
      inerted = [];
    }
  }

  const FOCUSABLE =
    'a[href], button:not([disabled]), input:not([disabled]):not([type="hidden"]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

  function focusables(root) {
    return Array.from(root.querySelectorAll(FOCUSABLE)).filter((el) => el.offsetParent !== null || el === document.activeElement);
  }

  document.addEventListener(
    "keydown",
    (event) => {
      const top = stack[stack.length - 1];
      if (!top) return;
      if (event.key === "Escape" && top.dismissible) {
        event.preventDefault();
        event.stopPropagation();
        top.close(false);
      } else if (event.key === "Tab") {
        const items = focusables(top.panel);
        if (!items.length) {
          event.preventDefault();
          top.panel.focus();
          return;
        }
        const first = items[0];
        const last = items[items.length - 1];
        if (event.shiftKey && (document.activeElement === first || !top.panel.contains(document.activeElement))) {
          event.preventDefault();
          last.focus();
        } else if (!event.shiftKey && (document.activeElement === last || !top.panel.contains(document.activeElement))) {
          event.preventDefault();
          first.focus();
        }
      }
    },
    true
  );

  ui.hasOpenLayer = () => stack.length > 0;
  ui.closeAllLayers = () => [...stack].reverse().forEach((layer) => layer.close(false));

  /* ui.modal({title, description, body, actions, size, dismissible, kind, onClose})
     actions: [{id, label, kind: "primary"|"quiet"|"danger", icon, onClick(ctx)}]
       An onClick may return a Promise; the button shows busy until it
       settles. Returning `false` keeps the layer open; anything else closes
       it and becomes the result. Throwing shows the error inside the layer.
     Returns {panel, body, close(result), result: Promise, button(id), setError(msg)} */
  ui.modal = function (opts) {
    return openLayer({ ...opts, variant: "modal" });
  };
  ui.sheet = function (opts) {
    return openLayer({ ...opts, variant: "sheet" });
  };

  function openLayer({
    title,
    description,
    body,
    actions = [],
    size = "md",
    dismissible = true,
    variant,
    onClose,
    initialFocus,
    className,
    headerExtra,
  }) {
    const opener = document.activeElement;
    const titleId = `wh-l-${Math.random().toString(36).slice(2, 8)}`;
    let resolve;
    const result = new Promise((r) => {
      resolve = r;
    });

    const errorBox = h("p", { class: "wh-layer-error", role: "alert", hidden: true });
    const buttons = new Map();
    const footer = actions.length
      ? h(
          "footer",
          { class: "wh-layer-foot" },
          errorBox,
          h("div", { class: "wh-layer-actions" },
            actions.map((action) => {
              const btn = h(
                "button",
                {
                  type: "button",
                  class: [
                    "btn",
                    "wh-btn",
                    action.kind === "primary" ? "btn-primary" : action.kind === "danger" ? "wh-btn-danger" : "btn-quiet",
                  ],
                  disabled: action.disabled,
                  onClick: () => run(action, btn),
                },
                action.icon && ui.icon(action.icon, { size: 16 }),
                h("span", { class: "wh-btn-label" }, action.label)
              );
              if (action.id) buttons.set(action.id, btn);
              return btn;
            })
          )
        )
      : null;

    const closeBtn = dismissible
      ? h("button", { type: "button", class: "wh-icon-btn wh-layer-close", "aria-label": "Close", onClick: () => layer.close(false) }, ui.icon("x", { size: 18 }))
      : null;

    const bodyEl = h("div", { class: "wh-layer-body" }, body);
    const panel = h(
      "div",
      {
        class: ["wh-layer-panel", `wh-${variant}`, `wh-${variant}--${size}`, className],
        role: "dialog",
        "aria-modal": "true",
        "aria-labelledby": titleId,
        tabindex: "-1",
      },
      h("header", { class: "wh-layer-head" },
        h("div", { class: "wh-layer-titles" },
          h("h2", { id: titleId, class: "wh-layer-title" }, title),
          description && h("p", { class: "wh-layer-desc" }, description)),
        headerExtra || null,
        closeBtn),
      bodyEl,
      footer
    );
    const scrim = h("div", {
      class: ["wh-scrim", `wh-scrim--${variant}`],
      onMousedown: (event) => {
        if (event.target === scrim && dismissible) layer.close(false);
      },
    }, panel);

    const layer = {
      panel,
      body: bodyEl,
      dismissible,
      result,
      button: (id) => buttons.get(id),
      setError(message) {
        errorBox.textContent = message || "";
        errorBox.hidden = !message;
      },
      close(value) {
        if (layer.closed) return;
        layer.closed = true;
        const index = stack.indexOf(layer);
        if (index >= 0) stack.splice(index, 1);
        scrim.classList.add("is-leaving");
        setTimeout(() => scrim.remove(), WH.prefersReducedMotion() ? 0 : 220);
        if (!stack.length) setInert(false);
        if (onClose) onClose(value);
        resolve(value);
        if (opener && opener.isConnected && typeof opener.focus === "function") {
          opener.focus({ preventScroll: true });
        }
      },
    };

    async function run(action, btn) {
      if (!action.onClick) return layer.close(action.value !== undefined ? action.value : action.id || true);
      layer.setError("");
      const label = btn.querySelector(".wh-btn-label");
      const original = label.textContent;
      const wasDisabled = btn.disabled;
      btn.disabled = true;
      btn.classList.add("is-busy");
      if (action.busyLabel) label.textContent = action.busyLabel;
      try {
        const value = await action.onClick(layer);
        if (value === false) return;
        layer.close(value === undefined ? true : value);
      } catch (error) {
        layer.setError((error && (error.detail || error.message)) || "That didn't work. Please try again.");
      } finally {
        if (!layer.closed) {
          btn.disabled = wasDisabled;
          btn.classList.remove("is-busy");
          label.textContent = original;
        }
      }
    }

    layersHost().appendChild(scrim);
    stack.push(layer);
    setInert(true);
    requestAnimationFrame(() => {
      const target =
        (initialFocus && panel.querySelector(initialFocus)) ||
        panel.querySelector("[autofocus]") ||
        focusables(bodyEl)[0] ||
        panel;
      target.focus({ preventScroll: true });
    });
    return layer;
  }

  /* Confirm without window.confirm. Resolves false on cancel, or
     {reason} (truthy) on confirm. Pass `reason: {picks, label}` to require a
     reason before the confirm button works. */
  ui.confirm = function ({
    title,
    message,
    confirmLabel = "Confirm",
    cancelLabel = "Cancel",
    danger = false,
    reason = null,
    details = null,
  }) {
    let field = null;
    const body = h("div", { class: "wh-confirm" },
      message && h("p", { class: "wh-confirm-msg" }, message),
      details);
    if (reason) {
      field = ui.reasonField(reason);
      body.appendChild(field.el);
    }
    const layer = ui.modal({
      title,
      body,
      size: "sm",
      actions: [
        { label: cancelLabel, kind: "quiet", value: false },
        {
          id: "ok",
          label: confirmLabel,
          kind: danger ? "danger" : "primary",
          onClick: () => {
            if (field && !field.validate()) return false;
            return { reason: field ? field.value : "" };
          },
        },
      ],
    });
    return layer.result;
  };

  /* ---------------------------------------------------------- form pieces */

  ui.field = function ({ label, hint, control, error, id, required, optional }) {
    const fid = id || (control && control.id) || `wh-f-${Math.random().toString(36).slice(2, 8)}`;
    if (control && !control.id) control.id = fid;
    const hintId = hint ? `${fid}-hint` : null;
    const errorEl = h("p", { class: "wh-field-error", id: `${fid}-err`, hidden: !error }, error || "");
    if (control) {
      const describedBy = [hintId, `${fid}-err`].filter(Boolean).join(" ");
      control.setAttribute("aria-describedby", describedBy);
      if (required) control.setAttribute("aria-required", "true");
    }
    const wrap = h("div", { class: "wh-field" },
      h("label", { class: "wh-field-label", for: fid }, label,
        required && h("span", { class: "wh-req", "aria-hidden": "true" }, " *"),
        optional && h("span", { class: "wh-opt" }, " (optional)")),
      control,
      hint && h("p", { class: "wh-field-hint", id: hintId }, hint),
      errorEl);
    wrap.setError = (message) => {
      errorEl.textContent = message || "";
      errorEl.hidden = !message;
      if (control) control.setAttribute("aria-invalid", message ? "true" : "false");
      wrap.classList.toggle("has-error", Boolean(message));
    };
    return wrap;
  };

  ui.input = (attrs = {}) => h("input", { class: "wh-input", type: "text", autocomplete: "off", ...attrs });
  ui.select = (options, attrs = {}) =>
    h("select", { class: "wh-input wh-select", ...attrs },
      options.map((o) => h("option", { value: o.value, selected: o.selected }, o.label)));

  /* Reason entry: quick-pick chips that fill a text box, which stays the
     record of what was said. {label, picks:[str], required, placeholder} */
  ui.reasonField = function ({
    label = "Reason",
    picks = [],
    required = true,
    placeholder = "Say why, in a sentence",
    hint = "Recorded in the audit trail with your name and the time.",
  } = {}) {
    const textarea = h("textarea", { class: "wh-input wh-textarea", rows: "2", placeholder, maxlength: "500" });
    const pickButtons = picks.map((text) =>
      h("button", {
        type: "button",
        class: "wh-pick",
        "aria-pressed": "false",
        onClick: () => {
          pickButtons.forEach((b) => b.setAttribute("aria-pressed", String(b === btnFor(text))));
          if (/^other/i.test(text)) {
            textarea.value = "";
            textarea.placeholder = "Describe the reason";
          } else {
            textarea.value = text;
          }
          field.setError("");
          textarea.focus();
          textarea.dispatchEvent(new Event("input", { bubbles: true }));
        },
      }, text)
    );
    const btnFor = (text) => pickButtons[picks.indexOf(text)];
    const picksRow = picks.length ? h("div", { class: "wh-picks", role: "group", "aria-label": "Common reasons" }, pickButtons) : null;
    const field = ui.field({ label, hint, control: textarea, required });
    if (picksRow) field.insertBefore(picksRow, textarea);
    textarea.addEventListener("input", () => {
      if (textarea.value.trim()) field.setError("");
      pickButtons.forEach((b) => b.setAttribute("aria-pressed", String(b.textContent === textarea.value)));
    });
    return {
      el: field,
      textarea,
      get value() {
        return textarea.value.trim();
      },
      validate() {
        if (required && textarea.value.trim().length < 3) {
          field.setError("A reason is required. Pick one above or write your own.");
          textarea.focus();
          return false;
        }
        return true;
      },
    };
  };

  /* Segmented control (role=radiogroup, arrow keys move). */
  ui.segmented = function ({ items, value, onChange, label, size }) {
    const group = h("div", { class: ["wh-seg", size === "sm" && "wh-seg--sm"], role: "radiogroup", "aria-label": label });
    const buttons = items.map((item) =>
      h("button", {
        type: "button",
        role: "radio",
        class: "wh-seg-btn",
        "aria-checked": String(item.value === value),
        tabindex: item.value === value ? "0" : "-1",
        title: item.title || null,
        onClick: () => select(item.value, true),
      }, item.icon && ui.icon(item.icon, { size: 15 }), item.label && h("span", null, item.label))
    );
    function select(next, fire) {
      buttons.forEach((b, i) => {
        const on = items[i].value === next;
        b.setAttribute("aria-checked", String(on));
        b.tabIndex = on ? 0 : -1;
      });
      group.value = next;
      if (fire && onChange) onChange(next);
    }
    group.addEventListener("keydown", (event) => {
      if (!["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"].includes(event.key)) return;
      event.preventDefault();
      const index = items.findIndex((i) => i.value === group.value);
      const delta = event.key === "ArrowLeft" || event.key === "ArrowUp" ? -1 : 1;
      const next = (index + delta + items.length) % items.length;
      select(items[next].value, true);
      buttons[next].focus();
    });
    buttons.forEach((b) => group.appendChild(b));
    group.value = value;
    group.set = (v) => select(v, false);
    return group;
  };

  /* Tabs with counts (role=tablist; Left/Right/Home/End). */
  ui.tabs = function ({ items, value, onChange, label }) {
    const list = h("div", { class: "wh-tabs", role: "tablist", "aria-label": label });
    const render = (current) => {
      WH.clear(list);
      items.forEach((item, i) => {
        const selected = item.id === current;
        const tab = h("button", {
          type: "button",
          role: "tab",
          class: "wh-tab",
          "aria-selected": String(selected),
          tabindex: selected ? "0" : "-1",
          dataset: { id: item.id },
          onClick: () => {
            render(item.id);
            onChange(item.id);
          },
          onKeydown: (event) => {
            const keys = { ArrowRight: i + 1, ArrowLeft: i - 1, Home: 0, End: items.length - 1 };
            if (!(event.key in keys)) return;
            event.preventDefault();
            const next = items[(keys[event.key] + items.length) % items.length];
            render(next.id);
            onChange(next.id);
            list.querySelector(`[data-id="${next.id}"]`).focus();
          },
        },
        item.icon && ui.icon(item.icon, { size: 15 }),
        h("span", null, item.label),
        item.count != null && h("span", { class: "wh-tab-count", "aria-label": `${item.count} items` }, WH.fmt.number(item.count)));
        list.appendChild(tab);
      });
    };
    render(value);
    list.update = (nextItems, current) => {
      items = nextItems;
      render(current);
    };
    return list;
  };

  /* Progress bar. Segments = [{value, tone}] for a stacked bar. */
  ui.progress = function ({ value = 0, max = 100, segments, label, indeterminate } = {}) {
    const pct = max ? Math.max(0, Math.min(100, (value / max) * 100)) : 0;
    const track = h("div", {
      class: ["wh-progress", indeterminate && "is-indeterminate"],
      role: "progressbar",
      "aria-valuemin": "0",
      "aria-valuemax": String(max),
      "aria-valuenow": indeterminate ? null : String(value),
      "aria-label": label || "Progress",
    });
    if (segments) {
      segments.forEach((s) => {
        if (!s.value) return;
        track.appendChild(h("span", { class: ["wh-progress-seg", `is-${s.tone}`], style: { width: `${(s.value / max) * 100}%` } }));
      });
    } else {
      track.appendChild(h("span", { class: "wh-progress-seg is-brand", style: { width: `${pct}%` } }));
    }
    return track;
  };

  /* Confidence heat bar: a fill plus the number and a word, so it reads
     without color. */
  ui.confidence = function (value, { compact } = {}) {
    const c = WH.fmt.confidence(value);
    if (c == null) return h("span", { class: "wh-muted wh-conf-none" }, "—");
    const band = c >= 0.9 ? "high" : c >= 0.75 ? "medium" : "low";
    const pct = Math.round(c * 100);
    return h("span", { class: ["wh-conf", `wh-conf--${band}`, compact && "wh-conf--compact"], title: `Read confidence ${pct}% (${band})` },
      h("span", { class: "wh-conf-track", "aria-hidden": "true" }, h("span", { class: "wh-conf-fill", style: { width: `${pct}%` } })),
      h("span", { class: "wh-conf-num" }, `${pct}%`),
      h("span", { class: "wh-sr" }, ` ${band} confidence`));
  };

  ui.copyButton = function (text, { label = "Copy" } = {}) {
    return h("button", {
      type: "button",
      class: "wh-icon-btn wh-icon-btn--sm",
      "aria-label": label,
      title: label,
      onClick: async (event) => {
        event.stopPropagation();
        try {
          await navigator.clipboard.writeText(String(text));
          ui.toast({ message: "Copied to clipboard", kind: "success", duration: 1800 });
        } catch {
          ui.toast({ message: "Couldn't copy — select the text instead", kind: "warn" });
        }
      },
    }, ui.icon("copy", { size: 14 }));
  };

  /* Icon-only button with a tooltip and an accessible name. */
  ui.iconButton = function (icon, label, onClick, attrs = {}) {
    return h("button", { type: "button", class: "wh-icon-btn", "aria-label": label, title: label, onClick, ...attrs }, ui.icon(icon, { size: 18 }));
  };

  /* A labelled, debounced search box. */
  ui.searchBox = function ({ value = "", placeholder = "Search", onInput, label = "Search", shortcut = "/" } = {}) {
    const input = h("input", {
      type: "search",
      class: "wh-search-input",
      placeholder,
      value,
      "aria-label": label,
      autocomplete: "off",
      spellcheck: "false",
      dataset: { shortcut: shortcut || "" },
    });
    const debounced = WH.debounce(() => onInput && onInput(input.value.trim()), 220);
    input.addEventListener("input", debounced);
    input.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && input.value) {
        event.stopPropagation();
        input.value = "";
        debounced.cancel();
        if (onInput) onInput("");
      } else if (event.key === "Enter") {
        debounced.cancel();
        if (onInput) onInput(input.value.trim());
      }
    });
    return h("label", { class: "wh-search" },
      ui.icon("search", { size: 16 }),
      input,
      shortcut && h("kbd", { class: "wh-kbd wh-search-kbd", "aria-hidden": "true" }, shortcut));
  };

  /* Screen header: eyebrow, h1 (route focus target), sub, actions. */
  ui.pageHeader = function ({ eyebrow, title, sub, actions, after }) {
    return h("header", { class: "wh-page-head" },
      h("div", { class: "wh-page-titles" },
        eyebrow && h("p", { class: "wh-eyebrow" }, eyebrow),
        h("h1", { class: "wh-h1", tabindex: "-1", dataset: { routeFocus: "1" } }, title),
        sub && h("p", { class: "wh-page-sub" }, sub)),
      actions && h("div", { class: "wh-page-actions" }, actions),
      after || null);
  };
})();
