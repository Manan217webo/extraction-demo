/* Webo Healthtech eSource — core.

   The small toolkit every screen shares: the API client, an element builder,
   formatters, an event bus, safe storage and a visibility-aware poller.
   Everything hangs off `window.WH`; no modules, no build step. */

(function () {
  "use strict";

  const WH = (window.WH = window.WH || {});

  /* ------------------------------------------------------------ event bus */

  const listeners = new Map();
  WH.bus = {
    on(name, fn) {
      if (!listeners.has(name)) listeners.set(name, new Set());
      listeners.get(name).add(fn);
      return () => WH.bus.off(name, fn);
    },
    off(name, fn) {
      const set = listeners.get(name);
      if (set) set.delete(fn);
    },
    emit(name, detail) {
      const set = listeners.get(name);
      if (!set) return;
      [...set].forEach((fn) => {
        try {
          fn(detail);
        } catch (error) {
          console.error(`bus handler for ${name} failed`, error);
        }
      });
    },
  };

  /* -------------------------------------------------------------- storage

     localStorage throws in private windows and when site data is blocked, so
     every read and write is wrapped and falls back to the default. */
  WH.store = {
    get(key, fallback = null) {
      try {
        const raw = window.localStorage.getItem(`wh.${key}`);
        return raw == null ? fallback : JSON.parse(raw);
      } catch {
        return fallback;
      }
    },
    set(key, value) {
      try {
        window.localStorage.setItem(`wh.${key}`, JSON.stringify(value));
      } catch {
        /* preference simply isn't remembered */
      }
    },
    remove(key) {
      try {
        window.localStorage.removeItem(`wh.${key}`);
      } catch {
        /* ignore */
      }
    },
  };

  /* ---------------------------------------------------------- API client */

  /* WH.api("/api/docs", {query:{status:"ready"}})  or  WH.api("docs")
     - `body` objects are sent as JSON.
     - `query` drops empty values, so filters can be passed straight through.
     - `background: true` marks polling requests (header X-WH-Background: 1) so
       the server may choose not to count them as user activity.
     - Errors carry .status (0 = network) and .detail; a 401 also dispatches
       `wh:signed-out` unless `quiet401` is set. */
  WH.api = async function api(path, opts = {}) {
    let url = path.startsWith("/") ? path : `/api/${path}`;
    if (opts.query) {
      const qs = WH.qs(opts.query);
      if (qs) url += (url.includes("?") ? "&" : "?") + qs;
    }
    const headers = { Accept: "application/json", ...(opts.headers || {}) };
    let body = opts.body;
    if (body != null && !(body instanceof FormData) && typeof body !== "string") {
      body = JSON.stringify(body);
      headers["Content-Type"] = "application/json";
    }
    if (opts.background) headers["X-WH-Background"] = "1";

    const method = opts.method || (body != null ? "POST" : "GET");
    let response;
    try {
      response = await fetch(url, {
        method,
        credentials: "same-origin",
        headers,
        body,
        signal: opts.signal,
      });
    } catch (cause) {
      if (cause && cause.name === "AbortError") throw cause;
      WH.bus.emit("net:fail", { url });
      const error = new Error("We couldn't reach the server. Check your connection and try again.");
      error.status = 0;
      error.offline = true;
      error.detail = error.message;
      throw error;
    }
    WH.bus.emit("net:ok");

    let data = null;
    const type = response.headers.get("content-type") || "";
    if (type.includes("json")) {
      try {
        data = await response.json();
      } catch {
        data = null;
      }
    }
    if (!response.ok) {
      let detail = data && data.detail;
      if (Array.isArray(detail)) detail = detail.map((d) => d.msg || "").filter(Boolean).join(" ");
      const error = new Error(detail || defaultMessage(response.status));
      error.status = response.status;
      error.detail = detail || error.message;
      error.data = data;
      if (response.status === 401 && !opts.quiet401 && url !== "/api/login") {
        WH.signOutDetected("expired");
      }
      throw error;
    }
    // Only writes (and the ping) extend the server session; GETs do not.
    if (method !== "GET") WH.bus.emit("session:extended", data);
    return data == null ? {} : data;
  };

  function defaultMessage(status) {
    if (status === 403) return "You don't have permission to do that.";
    if (status === 404) return "We couldn't find that. It may have been removed.";
    if (status === 409) return "Someone else changed this at the same time. Refresh and try again.";
    if (status === 423) return "This account is locked.";
    if (status >= 500) return "The server ran into a problem. Please try again in a moment.";
    return "Something went wrong. Please try again.";
  }

  /* One sign-out notice per session, however many requests fail with 401. */
  let signOutSent = false;
  WH.signOutDetected = function (reason) {
    if (signOutSent) return;
    signOutSent = true;
    window.dispatchEvent(new CustomEvent("wh:signed-out", { detail: { reason } }));
  };
  window.addEventListener("wh:signed-in", () => {
    signOutSent = false;
  });

  /* Query strings: skips null/undefined/"" and false, repeats arrays. */
  WH.qs = function (params) {
    const search = new URLSearchParams();
    Object.entries(params || {}).forEach(([key, value]) => {
      if (value == null || value === "" || value === false) return;
      if (Array.isArray(value)) value.forEach((v) => search.append(key, v));
      else search.append(key, String(value));
    });
    return search.toString();
  };

  /* ------------------------------------------------------ element builder

     WH.h("button", {class:"btn", onClick: fn, "aria-label": "Close"}, "Text")
     Children may be strings (always text nodes — never parsed as HTML), nodes,
     arrays, or null/false (skipped). */
  const SVG_NS = "http://www.w3.org/2000/svg";
  const SVG_TAGS = new Set(["svg", "path", "circle", "rect", "line", "polyline", "g", "polygon"]);

  WH.h = function h(tag, attrs, ...children) {
    const el = SVG_TAGS.has(tag) ? document.createElementNS(SVG_NS, tag) : document.createElement(tag);
    if (attrs && (attrs instanceof Node || typeof attrs !== "object" || Array.isArray(attrs))) {
      children.unshift(attrs);
      attrs = null;
    }
    if (attrs) {
      for (const [key, value] of Object.entries(attrs)) {
        if (value == null || value === false) continue;
        if (key === "class" || key === "className") {
          el.setAttribute("class", Array.isArray(value) ? value.filter(Boolean).join(" ") : value);
        } else if (key === "dataset") {
          Object.entries(value).forEach(([k, v]) => {
            if (v != null) el.dataset[k] = v;
          });
        } else if (key === "style" && typeof value === "object") {
          Object.entries(value).forEach(([k, v]) => {
            if (k.startsWith("--")) el.style.setProperty(k, v);
            else el.style[k] = v;
          });
        } else if (key === "text") {
          el.textContent = value;
        } else if (key.startsWith("on") && typeof value === "function") {
          el.addEventListener(key.slice(2).toLowerCase(), value);
        } else if (key === "value" && "value" in el) {
          el.value = value;
        } else if (key === "checked" || key === "selected" || key === "indeterminate") {
          el[key] = Boolean(value);
        } else if (value === true) {
          el.setAttribute(key, "");
        } else {
          el.setAttribute(key, String(value));
        }
      }
    }
    append(el, children);
    return el;
  };

  function append(el, children) {
    for (const child of children) {
      if (child == null || child === false || child === true) continue;
      if (Array.isArray(child)) append(el, child);
      else if (child instanceof Node) el.appendChild(child);
      else el.appendChild(document.createTextNode(String(child)));
    }
  }

  WH.clear = function (el) {
    while (el && el.firstChild) el.removeChild(el.firstChild);
    return el;
  };

  WH.$ = (selector, root = document) => root.querySelector(selector);
  WH.$$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

  /* ---------------------------------------------------------- formatters */

  WH.parseDate = function (value) {
    if (!value) return null;
    if (value instanceof Date) return value;
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? null : date;
  };

  const rtf = typeof Intl !== "undefined" && Intl.RelativeTimeFormat
    ? new Intl.RelativeTimeFormat(undefined, { numeric: "auto" })
    : null;

  /* "just now", "4 min ago", "yesterday", then a date. */
  WH.fmt = {
    relative(value, now = Date.now()) {
      const date = WH.parseDate(value);
      if (!date) return "—";
      const diff = (date.getTime() - now) / 1000;
      const abs = Math.abs(diff);
      if (abs < 45) return diff > 0 ? "in a moment" : "just now";
      const steps = [
        [3600, 60, "minute"],
        [86400, 3600, "hour"],
        [86400 * 7, 86400, "day"],
      ];
      for (const [limit, unit, name] of steps) {
        if (abs < limit) {
          const n = Math.round(diff / unit);
          return rtf ? rtf.format(n, name) : `${Math.abs(n)} ${name}s ${diff < 0 ? "ago" : ""}`;
        }
      }
      return WH.fmt.date(date);
    },
    date(value) {
      const date = WH.parseDate(value);
      if (!date) return "—";
      const sameYear = date.getFullYear() === new Date().getFullYear();
      return date.toLocaleDateString(undefined, {
        day: "numeric",
        month: "short",
        year: sameYear ? undefined : "numeric",
      });
    },
    time(value, seconds = false) {
      const date = WH.parseDate(value);
      if (!date) return "—";
      return date.toLocaleTimeString(undefined, {
        hour: "2-digit",
        minute: "2-digit",
        second: seconds ? "2-digit" : undefined,
      });
    },
    local(value) {
      const date = WH.parseDate(value);
      if (!date) return "—";
      return date.toLocaleString(undefined, {
        day: "numeric",
        month: "short",
        year: "numeric",
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
        timeZoneName: "short",
      });
    },
    /* "2026-11-01 14:03:22 UTC" — the form records are kept in. */
    utc(value) {
      const date = WH.parseDate(value);
      if (!date) return "—";
      return `${date.toISOString().replace("T", " ").slice(0, 19)} UTC`;
    },
    /* Both, for tooltips and signature manifestations. */
    both(value) {
      const date = WH.parseDate(value);
      if (!date) return "—";
      return `${WH.fmt.utc(date)} · ${WH.fmt.local(date)}`;
    },
    dayKey(value) {
      const date = WH.parseDate(value);
      if (!date) return "";
      return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;
    },
    dayLabel(value) {
      const date = WH.parseDate(value);
      if (!date) return "—";
      const today = new Date();
      const key = WH.fmt.dayKey(date);
      if (key === WH.fmt.dayKey(today)) return "Today";
      const yesterday = new Date(today);
      yesterday.setDate(today.getDate() - 1);
      if (key === WH.fmt.dayKey(yesterday)) return "Yesterday";
      return date.toLocaleDateString(undefined, {
        weekday: "long",
        day: "numeric",
        month: "long",
        year: date.getFullYear() === today.getFullYear() ? undefined : "numeric",
      });
    },
    bytes(n) {
      if (n == null || Number.isNaN(Number(n))) return "—";
      n = Number(n);
      if (n < 1024) return `${n} B`;
      const units = ["KB", "MB", "GB", "TB"];
      let i = -1;
      do {
        n /= 1024;
        i += 1;
      } while (n >= 1024 && i < units.length - 1);
      return `${n.toFixed(n < 10 ? 1 : 0)} ${units[i]}`;
    },
    number(n) {
      if (n == null) return "—";
      return Number(n).toLocaleString();
    },
    duration(seconds) {
      if (seconds == null || !Number.isFinite(seconds)) return "—";
      seconds = Math.max(0, Math.round(seconds));
      if (seconds < 60) return `${seconds}s`;
      const m = Math.floor(seconds / 60);
      if (m < 60) return `${m} min ${seconds % 60 ? `${seconds % 60}s` : ""}`.trim();
      const h = Math.floor(m / 60);
      return `${h} h ${m % 60} min`;
    },
    plural(n, one, many) {
      return `${WH.fmt.number(n)} ${n === 1 ? one : many || `${one}s`}`;
    },
    /* "visit_name" / "DOC_OPENED" → "Visit name" / "Doc opened" */
    humanize(text) {
      if (!text) return "";
      const s = String(text).replace(/[_.-]+/g, " ").trim().toLowerCase();
      return s.charAt(0).toUpperCase() + s.slice(1);
    },
    initials(name) {
      const parts = String(name || "?").trim().split(/[\s._-]+/).filter(Boolean);
      return ((parts[0] || "?")[0] + (parts.length > 1 ? parts[parts.length - 1][0] : "")).toUpperCase();
    },
    /* Confidence may arrive as 0–1 or 0–100; always answer 0–1 (or null). */
    confidence(value) {
      if (value == null || value === "") return null;
      const n = Number(value);
      if (!Number.isFinite(n)) return null;
      return Math.max(0, Math.min(1, n > 1 ? n / 100 : n));
    },
    /* Flags/conflicts may be a count or a list. */
    count(value) {
      if (value == null) return 0;
      if (Array.isArray(value)) return value.length;
      if (typeof value === "object") return Object.keys(value).length;
      const n = Number(value);
      return Number.isFinite(n) ? n : 0;
    },
  };

  /* --------------------------------------------------------------- timing */

  WH.debounce = function (fn, ms) {
    let timer = null;
    const debounced = (...args) => {
      clearTimeout(timer);
      timer = setTimeout(() => fn(...args), ms);
    };
    debounced.cancel = () => clearTimeout(timer);
    return debounced;
  };

  WH.throttle = function (fn, ms) {
    let last = 0;
    return (...args) => {
      const now = Date.now();
      if (now - last >= ms) {
        last = now;
        fn(...args);
      }
    };
  };

  WH.sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  /* Visibility-aware poller. Never overlaps calls, pauses while the tab is
     hidden (runs once immediately on return), backs off after errors.
     Returns {stop(), now(), setInterval(ms)}. */
  WH.poll = function (fn, ms, { immediate = false } = {}) {
    let timer = null;
    let running = false;
    let stopped = false;
    let interval = ms;
    let failures = 0;

    async function tick() {
      timer = null;
      if (stopped) return;
      if (document.visibilityState === "hidden") return; // resumed by visibilitychange
      if (running) return schedule();
      running = true;
      try {
        await fn();
        failures = 0;
      } catch (error) {
        failures += 1;
        if (error && error.status === 401) stopped = true;
      } finally {
        running = false;
      }
      schedule();
    }
    function schedule() {
      if (stopped) return;
      clearTimeout(timer);
      const backoff = Math.min(interval * 2 ** Math.min(failures, 4), 60000);
      timer = setTimeout(tick, failures ? backoff : interval);
    }
    function onVisible() {
      if (document.visibilityState === "visible" && !stopped) {
        clearTimeout(timer);
        tick();
      }
    }
    document.addEventListener("visibilitychange", onVisible);
    if (immediate) tick();
    else schedule();

    return {
      stop() {
        stopped = true;
        clearTimeout(timer);
        document.removeEventListener("visibilitychange", onVisible);
      },
      now() {
        clearTimeout(timer);
        tick();
      },
      setInterval(next) {
        if (next === interval) return;
        interval = next;
        schedule();
      },
    };
  };

  /* ----------------------------------------------------------- user/meta */

  WH.me = null; // {username, full_name, role, idle_seconds, expires_in}
  WH.can = {
    write: () => Boolean(WH.me && WH.me.role !== "monitor"),
    admin: () => Boolean(WH.me && WH.me.role === "admin"),
  };

  /* /api/meta — reason codes and signature meanings, fetched once. */
  let metaPromise = null;
  WH.meta = function () {
    if (!metaPromise) {
      metaPromise = WH.api("/api/meta").catch((error) => {
        metaPromise = null;
        return { reasons: [], meanings: [], error };
      });
    }
    return metaPromise;
  };
  WH.meaningLabel = function (code, meta) {
    const list = (meta && meta.meanings) || [];
    const found = list.find((m) => m.code === code);
    if (found) return found.label;
    if (code === "REVIEWED_ACCURATE") return "I confirm these values were verified against the source document";
    if (code === "AUTHOR") return "I am the author of these entries";
    return WH.fmt.humanize(code);
  };

  /* Polite screen-reader announcements. */
  WH.announce = function (text) {
    const region = document.getElementById("wh-announcer");
    if (!region) return;
    region.textContent = "";
    setTimeout(() => {
      region.textContent = text;
    }, 30);
  };

  WH.prefersReducedMotion = () =>
    window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  /* Is focus in something you type into? Shortcuts stay quiet there. */
  WH.isTyping = function (target = document.activeElement) {
    if (!target) return false;
    const tag = target.tagName;
    return (
      target.isContentEditable ||
      tag === "TEXTAREA" ||
      tag === "SELECT" ||
      (tag === "INPUT" && !["checkbox", "radio", "button", "submit", "range"].includes(target.type))
    );
  };

  /* Recently opened documents, newest first, for the command palette. */
  WH.recentDocs = {
    list() {
      return WH.store.get("recentDocs", []) || [];
    },
    add(doc) {
      if (!doc || doc.id == null) return;
      const entry = {
        id: String(doc.id),
        filename: doc.filename || `Document ${doc.id}`,
        screening_no: doc.screening_no || "",
        visit_name: doc.visit_name || "",
        protocol_no: doc.protocol_no || "",
        at: Date.now(),
      };
      const list = WH.recentDocs.list().filter((d) => d.id !== entry.id);
      list.unshift(entry);
      WH.store.set("recentDocs", list.slice(0, 12));
    },
  };
})();
