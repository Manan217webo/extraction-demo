/* Webo Healthtech eSource — app shell.

   Owns the chrome around every screen: who is signed in (and their role),
   the side navigation and its live badges, the breadcrumb, theme and
   density, the offline banner, the idle-session warning, and desktop
   notifications when a batch finishes.

   Sign-in itself stays in app.js (login overlay). We listen for
   `wh:signed-in` / `wh:signed-out` on window and also ask /api/me on load. */

(function () {
  "use strict";

  const WH = window.WH;
  const { h, ui } = WH;
  const $ = (id) => document.getElementById(id);
  const shell = (WH.shell = {});

  /* ----------------------------------------------------------- static icons */

  document.querySelectorAll("#sidenav [data-icon]").forEach((slot) => {
    slot.appendChild(ui.icon(slot.dataset.icon, { size: 20 }));
  });
  $("nav-toggle").appendChild(ui.icon("menu", { size: 20 }));
  document.querySelector(".wh-cmdk-icon").appendChild(ui.icon("search", { size: 16 }));
  const isMac = /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent);
  $("cmdk-kbd").textContent = isMac ? "⌘K" : "Ctrl K";

  /* Skip link: move focus without touching the hash (the router owns it). */
  $("skip-link").addEventListener("click", (event) => {
    event.preventDefault();
    const target = document.body.dataset.routeKind === "review" ? $("review-root") : $("screen");
    const focusable = target.querySelector("[data-route-focus], h1, h2") || target;
    if (!focusable.hasAttribute("tabindex")) focusable.setAttribute("tabindex", "-1");
    focusable.focus();
  });

  /* ------------------------------------------------------------- theme */

  const media = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;

  function applyTheme(theme) {
    document.documentElement.setAttribute("data-theme", theme);
    const btn = $("theme-btn");
    WH.clear(btn).appendChild(ui.icon(theme === "dark" ? "sun" : "moon", { size: 18 }));
    const label = theme === "dark" ? "Switch to light theme" : "Switch to dark theme";
    btn.setAttribute("aria-label", label);
    btn.title = label;
    const meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.content = theme === "dark" ? "#14111c" : "#ffffff";
  }
  shell.theme = () => document.documentElement.getAttribute("data-theme") || "light";
  shell.toggleTheme = function () {
    const next = shell.theme() === "dark" ? "light" : "dark";
    WH.store.set("theme", next);
    applyTheme(next);
    ui.toast({ message: next === "dark" ? "Dark theme on" : "Light theme on", kind: "info", duration: 1600 });
  };
  applyTheme(shell.theme());
  $("theme-btn").addEventListener("click", shell.toggleTheme);
  if (media && media.addEventListener) {
    media.addEventListener("change", (event) => {
      // Follow the system only until the reader picks a theme themselves.
      if (!WH.store.get("theme")) applyTheme(event.matches ? "dark" : "light");
    });
  }

  /* ------------------------------------------------------------ density */

  shell.density = () => document.documentElement.getAttribute("data-density") || "comfortable";
  shell.setDensity = function (value) {
    document.documentElement.setAttribute("data-density", value);
    WH.store.set("density", value);
    WH.bus.emit("density", value);
  };
  shell.toggleDensity = function () {
    const next = shell.density() === "compact" ? "comfortable" : "compact";
    shell.setDensity(next);
    ui.toast({ message: next === "compact" ? "Compact rows" : "Comfortable rows", kind: "info", duration: 1600 });
  };

  /* ---------------------------------------------------------- navigation

     ≥1180px: full sidebar (reader may collapse it to a rail, remembered).
     900–1179px: icon rail.  <900px: off-canvas drawer behind the menu button.
     Inside a review the sidebar always sits as a rail to give the panes room. */
  const sidenav = $("sidenav");
  const navToggle = $("nav-toggle");
  const scrim = $("nav-scrim");
  const collapseBtn = $("nav-collapse");
  const drawerQuery = window.matchMedia("(max-width: 899.98px)");
  let railQuery = null; // set below; applyRail tolerates the early call

  function setCollapsed(on, remember = true) {
    document.documentElement.toggleAttribute("data-nav-rail", on);
    collapseBtn.setAttribute("aria-pressed", String(on));
    collapseBtn.querySelector(".wh-nav-label").textContent = on ? "Expand sidebar" : "Collapse sidebar";
    collapseBtn.title = on ? "Expand sidebar" : "Collapse sidebar";
    if (remember) WH.store.set("navRail", on);
    applyRail();
  }

  /* One class says whether the sidebar shows as an icon rail right now. */
  railQuery = window.matchMedia("(min-width: 900px) and (max-width: 1179.98px)");
  function applyRail() {
    if (!railQuery) return;
    const wide = !drawerQuery.matches;
    const rail = wide && (railQuery.matches || document.documentElement.hasAttribute("data-nav-rail") || document.body.dataset.routeKind === "review");
    document.body.classList.toggle("is-rail", rail);
    collapseBtn.hidden = !wide || railQuery.matches || document.body.dataset.routeKind === "review";
  }
  railQuery.addEventListener("change", applyRail);
  shell.toggleNav = () => setCollapsed(!document.documentElement.hasAttribute("data-nav-rail"));
  setCollapsed(Boolean(WH.store.get("navRail", false)), false);
  collapseBtn.addEventListener("click", shell.toggleNav);

  // Rail mode: labels become tooltips.
  sidenav.querySelectorAll(".wh-nav-link").forEach((link) => {
    const label = link.querySelector(".wh-nav-label");
    if (label) link.setAttribute("data-tip", label.textContent);
  });

  function openDrawer() {
    document.body.classList.add("nav-open");
    scrim.hidden = false;
    navToggle.setAttribute("aria-expanded", "true");
    navToggle.setAttribute("aria-label", "Close navigation");
    sidenav.setAttribute("role", "dialog");
    sidenav.setAttribute("aria-modal", "true");
    const first = sidenav.querySelector(".wh-nav-link[aria-current]") || sidenav.querySelector(".wh-nav-link");
    if (first) first.focus();
  }
  function closeDrawer(returnFocus = true) {
    if (!document.body.classList.contains("nav-open")) return;
    document.body.classList.remove("nav-open");
    scrim.hidden = true;
    navToggle.setAttribute("aria-expanded", "false");
    navToggle.setAttribute("aria-label", "Open navigation");
    sidenav.removeAttribute("role");
    sidenav.removeAttribute("aria-modal");
    if (returnFocus) navToggle.focus();
  }
  navToggle.addEventListener("click", () =>
    document.body.classList.contains("nav-open") ? closeDrawer() : openDrawer()
  );
  scrim.addEventListener("click", () => closeDrawer());
  sidenav.addEventListener("keydown", (event) => {
    if (!document.body.classList.contains("nav-open")) return;
    if (event.key === "Escape") {
      event.preventDefault();
      closeDrawer();
    } else if (event.key === "Tab") {
      const items = Array.from(sidenav.querySelectorAll(".wh-nav-link")).filter((el) => el.offsetParent);
      const first = items[0];
      const last = items[items.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    }
  });
  drawerQuery.addEventListener("change", () => {
    closeDrawer(false);
    applyRail();
  });

  function setActiveNav(navKey) {
    sidenav.querySelectorAll(".wh-nav-link[data-nav]").forEach((link) => {
      if (link.dataset.nav === navKey) link.setAttribute("aria-current", "page");
      else link.removeAttribute("aria-current");
    });
  }

  /* ---------------------------------------------------------- breadcrumb

     Routes give a default trail ({label, href}); screens refine it once they
     know names (folder path, batch number, file name). */
  const crumbsList = $("crumbs-list");
  shell.setCrumbs = function (items) {
    WH.clear(crumbsList);
    items.filter(Boolean).forEach((item, i) => {
      const last = i === items.length - 1;
      const node = last || !item.href
        ? h("span", { class: "wh-crumb-current", "aria-current": last ? "page" : null, title: item.label }, item.label)
        : h("a", { href: item.href, class: "wh-crumb-link", title: item.label }, item.label);
      crumbsList.appendChild(h("li", { class: "wh-crumb" }, node));
    });
  };

  const SECTION = {
    dashboard: { label: "Dashboard", href: "#/" },
    inbox: { label: "Reviews", href: "#/inbox" },
    sources: { label: "Sources", href: "#/sources" },
    batches: { label: "Batches", href: "#/batches" },
    audit: { label: "Audit trail", href: "#/audit" },
    admin: { label: "Admin", href: "#/admin" },
  };
  shell.section = (key) => SECTION[key];

  WH.bus.on("route", ({ def, ctx }) => {
    setActiveNav(def.nav);
    closeDrawer(false);
    applyRail();
    if (def.crumbs) shell.setCrumbs(def.crumbs(ctx));
    else if (def.name === "review") shell.setCrumbs([SECTION.inbox, { label: "Review" }]);
    else if (def.name === "upload") shell.setCrumbs([SECTION.sources, { label: "Upload a PDF" }]);
    else if (SECTION[def.nav]) shell.setCrumbs([{ label: SECTION[def.nav].label }]);
    else shell.setCrumbs([{ label: typeof def.title === "string" ? def.title : "eSource" }]);
  });
  WH.bus.on("crumbs:doc", (doc) => {
    shell.setCrumbs([SECTION.inbox, { label: doc.filename || `Document ${doc.id}` }]);
  });

  /* ---------------------------------------------------------------- badges */

  const badgeInbox = $("badge-inbox");
  const badgeBatches = $("badge-batches");

  function setBadge(el, count, label) {
    const link = el.closest(".wh-nav-link");
    const base = link.querySelector(".wh-nav-label").textContent;
    if (!count) {
      el.hidden = true;
      link.setAttribute("aria-label", base);
      link.setAttribute("data-tip", base);
      return;
    }
    const text = count > 99 ? "99+" : String(count);
    if (el.textContent !== text && !el.hidden) {
      el.classList.remove("is-bumped");
      void el.offsetWidth; // restart the bump animation
      el.classList.add("is-bumped");
    }
    el.textContent = text;
    el.hidden = false;
    link.setAttribute("aria-label", `${base}, ${count} ${label}`);
    link.setAttribute("data-tip", `${base} · ${count} ${label}`);
  }

  WH.bus.on("docs:counts", (counts) => {
    if (counts && counts.ready != null) setBadge(badgeInbox, counts.ready, "ready to review");
  });

  /* Batch status memory, for the "finished" notification. Seeded on first
     sight so batches that finished before this page loaded stay quiet. */
  const batchDone = new Map();
  WH.bus.on("batches:list", (batches) => {
    if (!Array.isArray(batches)) return;
    const running = batches.filter((b) => !b.done && !b.cancelled).length;
    setBadge(badgeBatches, running, running === 1 ? "running batch" : "running batches");
    batches.forEach((b) => {
      const wasDone = batchDone.get(b.id);
      const isDone = Boolean(b.done || b.cancelled);
      if (wasDone === false && isDone) WH.bus.emit("batch:finished", b);
      batchDone.set(b.id, isDone);
    });
  });
  WH.bus.on("batch:detail", (batch) => {
    if (batch) WH.bus.emit("batches:list", mergeBatch(batch));
  });
  let lastBatches = [];
  function mergeBatch(batch) {
    const list = lastBatches.filter((b) => b.id !== batch.id);
    list.unshift(batch);
    return list;
  }
  WH.bus.on("batches:list", (list) => {
    lastBatches = list;
  });

  let badgePoll = null;
  async function refreshBadges() {
    if (!WH.me) return;
    const [docs, batches] = await Promise.allSettled([
      WH.api("/api/docs", { query: { limit: 1 }, background: true }),
      WH.api("/api/batches", { background: true }),
    ]);
    if (docs.status === "fulfilled") WH.bus.emit("docs:counts", docs.value.counts || {});
    if (batches.status === "fulfilled") WH.bus.emit("batches:list", batches.value.batches || []);
    // Poll faster while something is running so the badge feels live.
    const running = lastBatches.some((b) => !b.done && !b.cancelled);
    if (badgePoll) badgePoll.setInterval(running ? 5000 : 20000);
  }
  shell.refreshBadges = refreshBadges;

  /* ------------------------------------------------- batch finished */

  WH.bus.on("batch:finished", (batch) => {
    const counts = batch.counts || {};
    const parts = [];
    if (counts.ready) parts.push(`${counts.ready} ready`);
    if (counts.failed) parts.push(`${counts.failed} failed`);
    if (counts.cancelled) parts.push(`${counts.cancelled} cancelled`);
    const title = `Batch #${batch.number || batch.id} ${batch.cancelled ? "was cancelled" : "finished"}`;
    const body = parts.join(" · ") || "Nothing left to read.";
    const onBatch = WH.router.current && WH.router.current.ctx.path === `/batches/${batch.id}`;
    if (!onBatch || document.hidden) {
      ui.toast({
        message: title,
        detail: body,
        kind: counts.failed ? "warn" : "success",
        duration: 9000,
        action: counts.ready ? { label: "Review", onClick: () => WH.router.go(`/batches/${batch.id}`) } : null,
      });
    }
  });

  /* ------------------------------------------------------- offline banner */

  const offline = $("wh-offline");
  let serverDown = false;
  let probeTimer = null;

  function showOffline(kind) {
    WH.clear(offline);
    offline.className = `wh-offline wh-offline--${kind}`;
    if (kind === "online") {
      offline.append(ui.icon("check-circle", { size: 16 }), h("span", null, "Back online. Everything is up to date."));
      setTimeout(() => {
        if (offline.classList.contains("wh-offline--online")) offline.hidden = true;
      }, 2400);
    } else if (kind === "offline") {
      offline.append(ui.icon("wifi-off", { size: 16 }),
        h("span", null, h("strong", null, "You're offline. "), "Changes can't be saved until the connection is back — we'll reconnect automatically."));
    } else {
      offline.append(ui.icon("alert-triangle", { size: 16 }),
        h("span", null, h("strong", null, "Can't reach the server. "), "Retrying…"),
        h("button", { type: "button", class: "wh-offline-retry", onClick: probe }, "Retry now"));
    }
    offline.hidden = false;
    document.body.classList.toggle("is-offline", kind !== "online");
  }

  async function probe() {
    clearTimeout(probeTimer);
    try {
      const response = await fetch("/api/me", { credentials: "same-origin", headers: { "X-WH-Background": "1" } });
      if (response.status < 500) {
        recovered();
        return;
      }
    } catch {
      /* still down */
    }
    probeTimer = setTimeout(probe, 5000);
  }
  function recovered() {
    if (!serverDown && navigator.onLine) return;
    serverDown = false;
    clearTimeout(probeTimer);
    showOffline("online");
    document.body.classList.remove("is-offline");
    WH.bus.emit("net:online");
  }

  window.addEventListener("offline", () => {
    serverDown = true;
    showOffline("offline");
  });
  window.addEventListener("online", () => probe());
  WH.bus.on("net:fail", () => {
    if (serverDown) return;
    serverDown = true;
    showOffline(navigator.onLine ? "server" : "offline");
    probeTimer = setTimeout(probe, 3000);
  });
  WH.bus.on("net:ok", () => {
    if (serverDown) recovered();
  });
  if (!navigator.onLine) {
    serverDown = true;
    showOffline("offline");
  }

  /* --------------------------------------------------------- idle session

     Only writes and POST /api/session/ping extend the server session; GET
     polling does not. So: real user activity pings at most once a minute,
     every successful write moves the deadline too, and two minutes before
     the deadline we re-check /api/me (another tab may have kept the session
     alive) before warning. */
  const WARN_MS = 120 * 1000;
  let expiresAt = 0;
  let idleTick = null;
  let warning = null;
  let checking = false;

  function setExpiry(seconds) {
    if (seconds == null || !Number.isFinite(Number(seconds))) return;
    expiresAt = Date.now() + Number(seconds) * 1000;
  }

  WH.bus.on("session:extended", (data) => {
    if (!WH.me) return;
    if (data && data.expires_in != null) setExpiry(data.expires_in);
    else if (WH.me.idle_seconds) setExpiry(WH.me.idle_seconds);
  });

  const ping = WH.throttle(() => {
    if (!WH.me || warning || serverDown) return;
    WH.api("/api/session/ping", { method: "POST" }).catch(() => {});
  }, 60 * 1000);
  ["pointerdown", "keydown", "wheel", "touchstart"].forEach((type) =>
    window.addEventListener(type, () => {
      if (WH.me && !warning) ping();
    }, { passive: true, capture: true })
  );

  async function idleCheck() {
    if (!WH.me || !expiresAt || checking) return;
    const remaining = expiresAt - Date.now();
    if (warning) {
      updateWarning(remaining);
      if (remaining <= 0) {
        checking = true;
        try {
          const me = await WH.api("/api/me", { background: true });
          setExpiry(me.expires_in);
          if (me.expires_in > WARN_MS / 1000) closeWarning();
        } catch {
          /* 401 → wh:signed-out closes everything */
        } finally {
          checking = false;
        }
      }
      return;
    }
    if (remaining > WARN_MS) return;
    checking = true;
    try {
      const me = await WH.api("/api/me", { background: true });
      setExpiry(me.expires_in);
      if (me.expires_in * 1000 <= WARN_MS) openWarning();
    } catch {
      /* signed out or offline; the banner / login handle it */
    } finally {
      checking = false;
    }
  }

  function openWarning() {
    const count = h("strong", { class: "wh-idle-count" }, "2:00");
    const ring = h("span", { class: "wh-idle-ring", "aria-hidden": "true" });
    warning = ui.modal({
      title: "Are you still there?",
      size: "sm",
      dismissible: false,
      body: h("div", { class: "wh-idle" },
        ring,
        h("p", null, "For the security of patient data you'll be signed out after a period of inactivity. You'll be signed out in ", count, "."),
        h("p", { class: "wh-muted" }, "Drafts are saved automatically; anything unsaved in an open review may be lost.")),
      actions: [
        {
          label: "Sign out",
          kind: "quiet",
          onClick: () => {
            const logout = $("logout-btn");
            if (logout) logout.click();
            return true;
          },
        },
        {
          label: "Stay signed in",
          kind: "primary",
          busyLabel: "Keeping you signed in…",
          onClick: async () => {
            const data = await WH.api("/api/session/ping", { method: "POST" });
            setExpiry(data.expires_in);
            ui.toast({ message: "You're still signed in", kind: "success", duration: 1800 });
            return true;
          },
        },
      ],
      initialFocus: ".btn-primary",
      onClose: () => {
        warning = null;
      },
    });
    warning.count = count;
    warning.ring = ring;
    updateWarning(expiresAt - Date.now());
  }
  function updateWarning(remaining) {
    if (!warning) return;
    const s = Math.max(0, Math.ceil(remaining / 1000));
    warning.count.textContent = `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
    warning.ring.style.setProperty("--p", String(Math.max(0, Math.min(1, remaining / WARN_MS))));
  }
  function closeWarning() {
    if (warning) warning.close(false);
    warning = null;
  }

  /* ------------------------------------------------------------- sign-in */

  let starting = null;

  async function signedIn(detail) {
    // Detail from app.js may be partial; /api/me is the source of truth.
    if (starting) return starting;
    starting = (async () => {
      let me = detail && detail.role ? { ...detail } : null;
      try {
        me = { ...(me || {}), ...(await WH.api("/api/me", { quiet401: true })) };
      } catch (error) {
        if (!me) {
          starting = null;
          return;
        }
      }
      const changedUser = WH.me && WH.me.username !== me.username;
      WH.me = me;
      setExpiry(me.expires_in != null ? me.expires_in : me.idle_seconds);
      applyRole(me);
      document.body.classList.add("shell-ready");
      loadFeatures();
      WH.bus.emit("me", me);

      if (changedUser) WH.router.suspend();
      WH.router.start();

      if (badgePoll) badgePoll.stop();
      badgePoll = WH.poll(refreshBadges, 20000, { immediate: true });
      clearInterval(idleTick);
      idleTick = setInterval(idleCheck, 1000);
      starting = null;
    })();
    return starting;
  }

  function applyRole(me) {
    document.documentElement.dataset.role = me.role || "";
    document.querySelectorAll('#sidenav [data-role="admin"]').forEach((el) => {
      el.hidden = me.role !== "admin";
    });
    document.querySelectorAll("#sidenav [data-write]").forEach((el) => {
      el.closest("li, a").hidden = me.role === "monitor";
    });
    const chip = $("user-chip");
    if (chip) chip.title = `${me.full_name || me.username} · ${WH.fmt.humanize(me.role)}`;
  }

  async function loadFeatures() {
    try {
      const session = await WH.api("/api/session", { background: true });
      const features = session.features || {};
      WH.features = features;
      document.querySelectorAll('#sidenav [data-feature="sources"]').forEach((el) => {
        el.hidden = features.sources === false;
      });
      WH.bus.emit("features", features);
    } catch {
      WH.features = WH.features || {};
    }
  }

  function signedOut() {
    WH.me = null;
    expiresAt = 0;
    clearInterval(idleTick);
    if (badgePoll) badgePoll.stop();
    badgePoll = null;
    closeWarning();
    ui.closeAllLayers();
    WH.router.suspend();
    document.body.classList.remove("shell-ready");
    // app.js shows its login overlay; if it didn't, show it ourselves so the
    // reader is never left on a dead screen.
    setTimeout(() => {
      const overlay = $("login-overlay");
      if (overlay && overlay.classList.contains("hidden")) {
        overlay.classList.remove("hidden");
        document.body.classList.add("signed-out");
        const user = $("login-username");
        if (user) user.focus();
      }
    }, 80);
  }

  window.addEventListener("wh:signed-in", (event) => signedIn(event.detail));
  window.addEventListener("wh:signed-out", signedOut);

  /* Boot: app.js runs its own /api/me; we ask too, quietly. */
  (async function boot() {
    try {
      const me = await WH.api("/api/me", { quiet401: true });
      await signedIn(me);
    } catch {
      /* not signed in: wait for wh:signed-in from the login form */
    }
  })();

  /* "/" focuses the screen's search box. */
  document.addEventListener("keydown", (event) => {
    if (event.key !== "/" || event.metaKey || event.ctrlKey || event.altKey) return;
    if (WH.isTyping(event.target) || ui.hasOpenLayer() || document.body.dataset.routeKind === "review") return;
    const box = document.querySelector('#screen [data-shortcut="/"]');
    if (box) {
      event.preventDefault();
      box.focus();
      box.select();
    }
  });
})();
