/* Webo Healthtech eSource — hash router.

   Routes look like  #/inbox?status=ready&q=smith  — every filter and
   selection lives in the hash, so any view can be bookmarked or shared.

   A route definition:
     WH.router.add("/batches/:id", {
       name: "batch", nav: "batches", title: (ctx) => "Batch",
       kind: "screen" | "review",            // default "screen"
       allow: (me) => bool,                  // optional role gate
       mount(el, ctx) -> {update(ctx)?, destroy()?}   // screens
       enter(ctx)                                     // review routes
     })
   ctx = {path, params, query (URLSearchParams), hash, route}

   Query-only changes to the same route call instance.update(ctx) instead of
   re-mounting, so typing in a filter never throws away the screen. */

(function () {
  "use strict";

  const WH = (window.WH = window.WH || {});
  const h = WH.h;
  const routes = [];

  let current = null; // {def, ctx, instance}
  let started = false;
  let suspended = true;
  let skipGuardFor = null;
  let silentHash = null;

  function compile(pattern) {
    const keys = [];
    const source = pattern
      .replace(/\/:(\w+)\?/g, (_, key) => {
        keys.push(key);
        return "(?:/([^/]+))?";
      })
      .replace(/:(\w+)/g, (_, key) => {
        keys.push(key);
        return "([^/]+)";
      });
    return { regex: new RegExp(`^${source}/?$`), keys };
  }

  function parse(hash = location.hash) {
    const raw = hash.replace(/^#/, "") || "/";
    const [pathPart, queryPart = ""] = raw.split("?");
    const path = pathPart.startsWith("/") ? pathPart : `/${pathPart}`;
    return { path, query: new URLSearchParams(queryPart), hash: `#${raw}` };
  }

  function match(path) {
    for (const def of routes) {
      const m = def.compiled.regex.exec(path);
      if (m) {
        const params = {};
        def.compiled.keys.forEach((key, i) => {
          if (m[i + 1] != null) params[key] = decodeURIComponent(m[i + 1]);
        });
        return { def, params };
      }
    }
    return null;
  }

  function href(path, query) {
    const qs = query instanceof URLSearchParams ? query.toString() : WH.qs(query || {});
    return `#${path}${qs ? `?${qs}` : ""}`;
  }

  const screenEl = () => document.getElementById("screen");
  const reviewEl = () => document.getElementById("review-root");

  function hasUnsaved() {
    try {
      return Boolean(window.Review && typeof window.Review.hasUnsavedChanges === "function" && window.Review.hasUnsavedChanges());
    } catch {
      return false;
    }
  }

  function showKind(kind) {
    document.body.dataset.routeKind = kind;
    const screen = screenEl();
    const review = reviewEl();
    if (screen) screen.hidden = kind !== "screen";
    if (review) review.hidden = kind !== "review";
  }

  function destroyCurrent() {
    if (current && current.instance && typeof current.instance.destroy === "function") {
      try {
        current.instance.destroy();
      } catch (error) {
        console.error("screen destroy failed", error);
      }
    }
    if (current) current.instance = null;
  }

  function titleFor(def, ctx) {
    const t = typeof def.title === "function" ? def.title(ctx) : def.title;
    return t || "eSource";
  }

  /* Resolve the current hash into a screen. */
  async function resolve() {
    if (suspended) return;
    const parsed = parse();
    if (parsed.path === "/" || parsed.path === "") {
      replace("/inbox");
      return;
    }
    if (silentHash && silentHash === parsed.hash) {
      silentHash = null;
      return;
    }
    const found = match(parsed.path);
    const ctx = { ...parsed, params: found ? found.params : {}, route: found ? found.def.name : "not-found" };
    const def = found ? found.def : NOT_FOUND;

    // Leaving a review with unsaved edits: put the old address back, ask, and
    // only then continue.
    const leavingReview =
      current && current.def.kind === "review" && (def !== current.def || ctx.path !== current.ctx.path);
    if (leavingReview && skipGuardFor !== parsed.hash && hasUnsaved()) {
      const target = parsed.hash;
      history.replaceState(null, "", current.ctx.hash);
      const ok = await WH.ui.confirm({
        title: "Leave this review?",
        message: "You have changes that haven't been saved as a draft yet. If you leave now they will be lost.",
        confirmLabel: "Leave without saving",
        cancelLabel: "Stay on review",
        danger: true,
      });
      if (ok) {
        skipGuardFor = target;
        location.hash = target;
      }
      return;
    }
    skipGuardFor = null;

    // Same route and params: just a query change.
    if (
      current &&
      current.def === def &&
      current.ctx.path === ctx.path &&
      current.instance &&
      typeof current.instance.update === "function"
    ) {
      current.ctx = ctx;
      current.instance.update(ctx);
      WH.bus.emit("route", { def, ctx, queryOnly: true });
      return;
    }

    destroyCurrent();
    current = { def, ctx, instance: null };
    document.title = `${titleFor(def, ctx)} · eSource — Webo Healthtech`;

    if (def.allow && !def.allow(WH.me)) {
      showKind("screen");
      mountInto((el) => {
        el.appendChild(
          WH.ui.errorState({ status: 403, detail: "Your role doesn't include this area. Ask an administrator if you need access." }, {})
        );
        return null;
      });
      WH.bus.emit("route", { def, ctx });
      return;
    }

    if (def.kind === "review") {
      showKind("review");
      WH.bus.emit("route", { def, ctx });
      try {
        await def.enter(ctx);
      } catch (error) {
        console.error(error);
      }
      return;
    }

    showKind("screen");
    mountInto((el) => def.mount(el, ctx));
    WH.bus.emit("route", { def, ctx });
  }

  function mountInto(mount) {
    const screen = screenEl();
    WH.clear(screen);
    const view = h("div", { class: "wh-view" });
    screen.appendChild(view);
    try {
      current.instance = mount(view, current.ctx) || null;
    } catch (error) {
      console.error("screen mount failed", error);
      view.appendChild(WH.ui.errorState(error, { title: "This screen failed to open", retry: () => resolve() }));
    }
    // Scroll to top and move focus to the page heading, so keyboard and
    // screen-reader users land at the start of the new screen.
    window.scrollTo({ top: 0 });
    requestAnimationFrame(() => {
      if (WH.ui.hasOpenLayer()) return; // a dialog keeps focus
      const target = screen.querySelector("[data-route-focus]") || screen;
      target.focus({ preventScroll: true });
      WH.announce(`${titleFor(current.def, current.ctx)} page`);
    });
  }

  function go(path, query, { replace: useReplace = false } = {}) {
    const next = href(path, query);
    if (next === location.hash) {
      resolve();
      return;
    }
    if (useReplace) location.replace(next);
    else location.hash = next;
  }
  function replace(path, query) {
    go(path, query, { replace: true });
  }

  /* Persist screen state into the hash without re-rendering or adding a
     history entry (filters, selection). */
  function setQuery(patch, { push = false } = {}) {
    if (!current) return;
    const query = new URLSearchParams(current.ctx.query);
    Object.entries(patch).forEach(([key, value]) => {
      query.delete(key);
      if (value == null || value === "" || value === false) return;
      if (Array.isArray(value)) value.forEach((v) => query.append(key, v));
      else query.set(key, String(value));
    });
    const next = href(current.ctx.path, query);
    if (next === location.hash) return;
    if (push) history.pushState(null, "", next);
    else history.replaceState(null, "", next);
    current.ctx = { ...current.ctx, query, hash: next };
    WH.bus.emit("route:query", { ctx: current.ctx });
  }

  /* Change the address without resolving it (the screen already shows it). */
  function silentReplace(path, query) {
    const next = href(path, query);
    history.replaceState(null, "", next);
    const parsed = parse(next);
    const found = match(parsed.path);
    if (found && current) {
      current = { def: found.def, ctx: { ...parsed, params: found.params, route: found.def.name }, instance: current.instance };
      WH.bus.emit("route", { def: found.def, ctx: current.ctx });
    }
  }

  const NOT_FOUND = {
    name: "not-found",
    title: "Not found",
    mount(el) {
      el.appendChild(
        WH.ui.emptyState({
          icon: "search",
          title: "There's nothing at this address",
          body: "The link may be out of date, or the item was removed.",
          action: { label: "Go to the inbox", href: "#/inbox", icon: "inbox" },
        })
      );
    },
  };

  WH.router = {
    add(pattern, def) {
      routes.push({ kind: "screen", ...def, pattern, compiled: compile(pattern) });
    },
    start() {
      suspended = false;
      if (!started) {
        started = true;
        window.addEventListener("hashchange", () => resolve());
      }
      resolve();
    },
    /* Signed out: stop every screen's polling and forget the instance. */
    suspend() {
      suspended = true;
      destroyCurrent();
      current = null;
    },
    refresh() {
      if (current) {
        destroyCurrent();
        current = null;
      }
      resolve();
    },
    go,
    replace,
    href,
    setQuery,
    silentReplace,
    get current() {
      return current ? { def: current.def, ctx: current.ctx } : null;
    },
    get started() {
      return started && !suspended;
    },
  };

  window.addEventListener("beforeunload", (event) => {
    if (current && current.def.kind === "review" && hasUnsaved()) {
      event.preventDefault();
      event.returnValue = "";
    }
  });

  /* ------------------------------------------------------- review routes

     The review screen itself lives in app.js (#workspace, #processing,
     #viewer inside #review-root). These routes only show it and ask it to
     open the right thing via window.Review. */
  let lastOpenedDoc = null;

  function reviewApi() {
    return window.Review && typeof window.Review.open === "function" ? window.Review : null;
  }

  WH.router.add("/upload", {
    name: "upload",
    nav: "sources",
    kind: "review",
    title: "Upload a PDF",
    enter() {
      lastOpenedDoc = null;
      const review = reviewApi();
      if (review && typeof review.newUpload === "function") return review.newUpload();
      return undefined;
    },
  });

  WH.router.add("/review/:docId", {
    name: "review",
    nav: "inbox",
    kind: "review",
    title: () => "Review",
    async enter(ctx) {
      const id = ctx.params.docId;
      const review = reviewApi();
      if (!review) {
        WH.ui.toast({ message: "The review screen is still loading. Try again in a moment.", kind: "warn" });
        return;
      }
      // Fetch the document for the breadcrumb and recents in parallel.
      WH.api(`/api/docs/${encodeURIComponent(id)}`)
        .then((data) => {
          const doc = data.document;
          if (!doc) return;
          WH.recentDocs.add(doc);
          if (current && current.ctx.params.docId === id) WH.bus.emit("crumbs:doc", doc);
        })
        .catch(() => {});
      lastOpenedDoc = id;
      try {
        await review.open(id);
      } catch (error) {
        WH.ui.toast({
          message: "This document couldn't be opened",
          detail: (error && (error.detail || error.message)) || "",
          kind: "error",
          action: { label: "Back to inbox", onClick: () => go("/inbox") },
        });
      }
    },
  });

  /* The review's own "New document" button closes the viewer and shows the
     upload landing; keep the address honest without re-running anything. */
  window.addEventListener("wh:review-closed", () => {
    if (!WH.me || !current) return;
    if (current.def.name === "review") {
      lastOpenedDoc = null;
      silentReplace("/upload");
    }
  });
  /* Optional hook: the review screen announces the server document it has
     open (e.g. after an upload finishes). */
  window.addEventListener("wh:review-opened", (event) => {
    const id = event.detail && (event.detail.document_id || event.detail.docId);
    if (!id || !current || current.def.kind !== "review") return;
    if (current.def.name === "upload" || current.ctx.params.docId !== String(id)) {
      lastOpenedDoc = String(id);
      silentReplace(`/review/${encodeURIComponent(id)}`);
    }
  });
})();
