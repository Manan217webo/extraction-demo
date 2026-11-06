/* Webo Healthtech eSource — Sources (#/sources/:sourceId?path=).

   Browse the file server: roots and a folder tree on the left, the folder's
   files on the right with checkboxes (shift-click selects a range, a folder
   selects everything under it), status chips, a search filter and a sticky
   "Scan" bar. Scanning previews first; anything read before goes through a
   duplicate dialog where each file is skipped or reprocessed, with a reason
   whenever something is reprocessed. */

(function () {
  "use strict";

  const WH = window.WH;
  const { h, ui } = WH;

  const REPROCESS_PICKS = [
    "Source page was rescanned or replaced",
    "Previous read was incomplete or wrong",
    "Wrong header or visit was matched",
    "Re-read with the updated reader",
    "Other",
  ];
  const STATUS_FILTERS = ["new", "changed", "processed", "processing", "failed"];

  let sourcesCache = null;
  const browseCache = new Map(); // `${sourceId}|${path}` → browse response
  const expanded = new Map(); // sourceId → Set(paths)

  const key = (sourceId, path) => `${sourceId}|${path || ""}`;
  const browse = async (sourceId, path, { fresh = false, background = false } = {}) => {
    const k = key(sourceId, path);
    if (!fresh && browseCache.has(k)) return browseCache.get(k);
    const data = await WH.api(`/api/sources/${encodeURIComponent(sourceId)}/browse`, { query: { path: path || "" }, background });
    browseCache.set(k, data);
    return data;
  };
  const loadSources = async () => {
    if (!sourcesCache) sourcesCache = (await WH.api("/api/sources")).sources || [];
    return sourcesCache;
  };

  /* Is `path` inside folder `folder`? */
  const within = (path, folder) => folder === "" || path === folder || path.startsWith(`${folder.replace(/\/$/, "")}/`);

  /* ---------------------------------------------------------------- mount */

  function mount(root, ctx) {
    let sourceId = ctx.params.sourceId || null;
    let path = ctx.query.get("path") || "";
    let q = ctx.query.get("q") || "";
    let statusFilter = ctx.query.get("status") || "all";
    let selection = new Set(ctx.query.getAll("sel"));
    let listing = null;
    let anchorIndex = null;
    let focusIndex = 0;
    let token = 0;
    const knownEntries = new Map(); // path → entry, for the action bar breakdown

    const header = ui.pageHeader({
      eyebrow: "File server",
      title: "Sources",
      sub: "Open a connected server, then a folder. Extract the visit forms inside it. A form that was already scanned asks you before it is read again.",
    });

    const locationEl = h("div", { class: "wh-src-server-slot" });
    const treeEl = h("div", { class: "wh-tree-wrap" });
    const side = h("aside", { class: "wh-src-side", "aria-label": "Folders" }, h("h2", { class: "wh-side-title" }, "Folders"), treeEl);

    const pathCrumbs = h("nav", { class: "wh-path", "aria-label": "Folder path" });
    const searchBox = ui.searchBox({ value: q, placeholder: "Filter this folder", label: "Filter files in this folder", onInput: (v) => {
      q = v;
      WH.router.setQuery({ q: v });
      renderList();
    } });
    const statusBar = h("div", { class: "wh-status-filter", role: "group", "aria-label": "Show files by status" });
    const refreshBtn = ui.iconButton("refresh", "Refresh this folder", () => openFolder(path, { fresh: true, push: false }));
    const toolbar = h("div", { class: "wh-src-toolbar" }, pathCrumbs, h("div", { class: "wh-src-tools" }, searchBox, refreshBtn));
    const notice = h("div", { class: "wh-src-notice" });
    const listEl = h("div", { class: "wh-src-list" });
    const actionBar = h("div", { class: "wh-actionbar", hidden: true, role: "region", "aria-label": "Selection" });

    const mainPane = h("section", { class: "wh-src-main", "aria-label": "Files" }, toolbar, statusBar, notice, listEl);
    const layout = h("div", { class: "wh-src-layout wh-card" }, side, mainPane);
    root.append(header, locationEl, layout, actionBar);

    /* ------------------------------------------------------------ start */

    async function start() {
      await ui.load(treeEl, {
        skeleton: () => ui.sk.tree(6),
        label: "your sources",
        task: loadSources,
        render: (sources) => {
          if (!sources.length) {
            locationEl.replaceWith(
              ui.emptyState({
                icon: "database",
                title: "No file server is connected",
                body: WH.can.admin()
                  ? "Add a server location in the server configuration. Its folders and visit forms will show up here."
                  : "Ask an administrator to connect a file-server location. Its folders and visit forms will show up here.",
              })
            );
            layout.remove();
            return;
          }
          if (!sourceId || !sources.some((s) => String(s.id) === String(sourceId))) {
            WH.router.replace(`/sources/${encodeURIComponent(sources[0].id)}`);
            return;
          }
          renderTree();
          openFolder(path, { push: false });
        },
      });
    }

    /* ------------------------------------------------------------- tree */

    function expandedFor(id) {
      if (!expanded.has(id)) expanded.set(id, new Set([""]));
      return expanded.get(id);
    }

    function renderTree() {
      const sources = sourcesCache || [];
      const tree = h("ul", { class: "wh-tree", role: "tree", "aria-label": "Source folders" });
      sources.forEach((src) => {
        const isCurrent = String(src.id) === String(sourceId);
        tree.appendChild(treeNode(src, "", src.name, 0, isCurrent));
      });
      WH.clear(treeEl).appendChild(tree);
      const current = tree.querySelector('[aria-selected="true"]') || tree.querySelector('[role="treeitem"]');
      if (current) current.tabIndex = 0;
    }

    function treeNode(src, nodePath, label, depth, isCurrentSource) {
      const id = String(src.id);
      const open = isCurrentSource && expandedFor(id).has(nodePath);
      const isSelected = isCurrentSource && nodePath === path;
      const cached = browseCache.get(key(id, nodePath));
      const dirs = cached ? cached.entries.filter((e) => e.type === "dir") : null;
      const hasKids = !cached || dirs.length > 0;

      const li = h("li", {
        role: "treeitem",
        class: ["wh-tree-item", isSelected && "is-current"],
        "aria-expanded": hasKids ? String(Boolean(open)) : null,
        "aria-selected": String(isSelected),
        "aria-level": String(depth + 1),
        tabindex: "-1",
        dataset: { source: id, path: nodePath },
      });
      const row = h("div", { class: "wh-tree-row", style: { "--depth": String(depth) } },
        h("button", {
          type: "button",
          class: "wh-tree-twisty",
          tabindex: "-1",
          "aria-hidden": "true",
          hidden: !hasKids,
          onClick: (event) => {
            event.stopPropagation();
            toggleNode(id, nodePath, li);
          },
        }, ui.icon("chevron-right", { size: 14 })),
        h("span", { class: "wh-tree-icon" }, ui.icon(depth === 0 ? "database" : open ? "folder-open" : "folder", { size: 16 })),
        h("span", { class: "wh-tree-label", title: depth === 0 ? `${label} — ${src.location || ""}` : label }, label));
      row.addEventListener("click", () => {
        if (String(sourceId) !== id) {
          WH.router.go(`/sources/${encodeURIComponent(id)}`);
        } else {
          openFolder(nodePath);
        }
      });
      li.appendChild(row);
      if (open && dirs) {
        const group = h("ul", { role: "group" });
        dirs.forEach((d) => group.appendChild(treeNode(src, d.path, d.name, depth + 1, true)));
        li.appendChild(group);
      } else if (open && !dirs) {
        li.appendChild(h("div", { class: "wh-tree-loading", style: { "--depth": String(depth + 1) } }, ui.spinner(12), "Loading…"));
        browse(id, nodePath).then(renderTree).catch(() => {});
      }
      return li;
    }

    async function toggleNode(id, nodePath) {
      if (String(sourceId) !== id) return;
      const set = expandedFor(id);
      if (set.has(nodePath)) set.delete(nodePath);
      else {
        set.add(nodePath);
        try {
          await browse(id, nodePath);
        } catch {
          /* the node shows its loading row; a retry happens on next render */
        }
      }
      renderTree();
      const li = treeEl.querySelector(`[role="treeitem"][data-path="${CSS.escape(nodePath)}"][data-source="${CSS.escape(id)}"]`);
      if (li) {
        treeEl.querySelectorAll('[role="treeitem"]').forEach((n) => (n.tabIndex = -1));
        li.tabIndex = 0;
        li.focus();
      }
    }

    // Tree keyboard: Up/Down move, Right expands or enters, Left collapses or
    // goes to the parent, Enter opens the folder.
    treeEl.addEventListener("keydown", (event) => {
      const item = event.target.closest('[role="treeitem"]');
      if (!item) return;
      const items = Array.from(treeEl.querySelectorAll('[role="treeitem"]')).filter((n) => n.offsetParent !== null);
      const i = items.indexOf(item);
      const focusItem = (n) => {
        if (!n) return;
        items.forEach((x) => (x.tabIndex = -1));
        n.tabIndex = 0;
        n.focus();
      };
      const { source, path: p } = item.dataset;
      if (event.key === "ArrowDown") {
        event.preventDefault();
        focusItem(items[i + 1]);
      } else if (event.key === "ArrowUp") {
        event.preventDefault();
        focusItem(items[i - 1]);
      } else if (event.key === "ArrowRight") {
        event.preventDefault();
        if (item.getAttribute("aria-expanded") === "false") toggleNode(source, p);
        else if (item.getAttribute("aria-expanded") === "true") focusItem(item.querySelector('[role="treeitem"]'));
      } else if (event.key === "ArrowLeft") {
        event.preventDefault();
        if (item.getAttribute("aria-expanded") === "true") toggleNode(source, p);
        else focusItem(item.parentElement.closest('[role="treeitem"]'));
      } else if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        item.querySelector(".wh-tree-row").click();
      }
    });

    /* ------------------------------------------------------------ folder */

    async function openFolder(nextPath, { fresh = false, push = true } = {}) {
      if (push && nextPath !== path) {
        WH.router.go(`/sources/${encodeURIComponent(sourceId)}`, queryFor({ path: nextPath, q: "" }));
        return; // update() continues
      }
      path = nextPath || "";
      const my = ++token;
      // Expand the tree down to this folder.
      const set = expandedFor(String(sourceId));
      await ui.load(listEl, {
        skeleton: () => ui.sk.table(9, [4, 44, 14, 12, 16]),
        label: "this folder",
        isCurrent: () => my === token,
        task: () => browse(sourceId, path, { fresh: true }),
        render: (data) => {
          listing = data;
          (data.breadcrumbs || []).forEach((b) => set.add(b.path));
          set.add(path);
          data.entries.forEach((e) => knownEntries.set(e.path, e));
          anchorIndex = null;
          focusIndex = 0;
          renderPath();
          renderList();
          renderTree();
          updateCrumbs();
          scheduleLivePoll();
        },
      });
      if (fresh) ui.toast({ message: "Folder refreshed", kind: "info", duration: 1400 });
    }

    function queryFor(patch = {}) {
      return {
        path: patch.path != null ? patch.path : path,
        q: patch.q != null ? patch.q : q,
        status: statusFilter === "all" ? "" : statusFilter,
        sel: [...selection],
      };
    }

    function updateCrumbs() {
      const src = (sourcesCache || []).find((s) => String(s.id) === String(sourceId));
      const trail = [WH.shell.section("sources")];
      const crumbs = (listing && listing.breadcrumbs) || [];
      if (src) trail.push({ label: src.name, href: WH.router.href(`/sources/${encodeURIComponent(sourceId)}`) });
      crumbs.filter((b) => b.path).forEach((b) => trail.push({ label: b.name, href: WH.router.href(`/sources/${encodeURIComponent(sourceId)}`, { path: b.path }) }));
      if (trail.length > 1) trail[trail.length - 1] = { label: trail[trail.length - 1].label };
      WH.shell.setCrumbs(trail);
    }

    function renderPath() {
      WH.clear(pathCrumbs);
      const src = (sourcesCache || []).find((s) => String(s.id) === String(sourceId));
      const crumbs = (listing && listing.breadcrumbs && listing.breadcrumbs.length)
        ? listing.breadcrumbs
        : [{ name: src ? src.name : "Root", path: "" }];
      const ol = h("ol");
      if (listing && listing.parent != null && path) {
        pathCrumbs.appendChild(ui.iconButton("chevron-left", "Up one folder", () => openFolder(listing.parent || "")));
      }
      crumbs.forEach((c, i) => {
        const last = i === crumbs.length - 1;
        ol.appendChild(h("li", null,
          last
            ? h("span", { "aria-current": "location", class: "wh-path-current" }, c.name || (src && src.name) || "Root")
            : h("button", { type: "button", class: "wh-path-link", onClick: () => openFolder(c.path) }, c.name || (src && src.name) || "Root")));
      });
      pathCrumbs.appendChild(ol);
      renderLocation();
    }

    function renderLocation() {
      const src = (sourcesCache || []).find((s) => String(s.id) === String(sourceId));
      if (!src) return;
      const files = listing ? listing.entries.filter((e) => e.type === "pdf") : [];
      const folders = listing ? listing.entries.filter((e) => e.type === "dir") : [];
      const scanned = files.filter((e) => e.status === "processed" || e.status === "changed").length;
      const fresh = files.filter((e) => !e.status || e.status === "new").length;
      WH.clear(locationEl).appendChild(
        h("div", { class: "wh-src-server" },
          h("span", { class: "wh-src-server-mark", "aria-hidden": "true" }, ui.icon("database", { size: 20 })),
          h("div", { class: "wh-src-server-copy" },
            h("span", { class: "wh-eyebrow" }, "Connected server"),
            h("strong", null, src.name),
            src.location && h("span", { class: "wh-src-server-path", title: src.location }, src.location)),
          listing && h("dl", { class: "wh-src-server-counts" },
            folders.length ? [h("div", null, h("dt", null, "Folders"), h("dd", null, String(folders.length)))] : null,
            files.length ? [h("div", null, h("dt", null, "Visit forms"), h("dd", null, String(files.length)))] : null,
            fresh ? [h("div", null, h("dt", null, "Not scanned"), h("dd", null, String(fresh)))] : null,
            scanned ? [h("div", null, h("dt", null, "Already scanned"), h("dd", null, String(scanned)))] : null))
      );
    }

    /* -------------------------------------------------------------- list */

    function visibleEntries() {
      if (!listing) return [];
      const needle = q.toLowerCase();
      return listing.entries.filter((e) => {
        if (needle && !e.name.toLowerCase().includes(needle)) return false;
        if (statusFilter !== "all" && e.type === "pdf" && e.status !== statusFilter) return false;
        if (statusFilter !== "all" && e.type === "dir") return false;
        return true;
      });
    }

    function fileEntries() {
      return visibleEntries().filter((e) => e.type !== "dir");
    }
    function folderEntries() {
      return visibleEntries().filter((e) => e.type === "dir");
    }

    function coveringFolder(entryPath) {
      for (const sel of selection) {
        if (sel !== entryPath && within(entryPath, sel) && isFolderPath(sel)) return sel;
      }
      return null;
    }
    function isFolderPath(p) {
      const e = knownEntries.get(p);
      return e ? e.type === "dir" : false;
    }

    function renderStatusFilter() {
      WH.clear(statusBar);
      if (!listing) return;
      const files = listing.entries.filter((e) => e.type === "pdf");
      const counts = {};
      files.forEach((e) => (counts[e.status] = (counts[e.status] || 0) + 1));
      const options = [{ id: "all", label: "All", count: listing.entries.length }]
        .concat(STATUS_FILTERS.filter((s) => counts[s]).map((s) => ({ id: s, label: ui.statusInfo(s).label, count: counts[s] })));
      if (options.length <= 2 && statusFilter === "all") return; // nothing to filter by
      options.forEach((o) => {
        statusBar.appendChild(h("button", {
          type: "button",
          class: "wh-toggle-chip",
          "aria-pressed": String(statusFilter === o.id),
          onClick: () => {
            statusFilter = o.id;
            WH.router.setQuery({ status: o.id === "all" ? "" : o.id });
            renderList();
          },
        }, o.id !== "all" && ui.icon(ui.statusInfo(o.id).icon, { size: 13 }), o.label, h("span", { class: "wh-toggle-count" }, String(o.count))));
      });
    }

    function renderList() {
      renderStatusFilter();
      WH.clear(notice);
      if (listing && listing.truncated) {
        notice.appendChild(h("p", { class: "wh-inline-note wh-inline-note--warn" }, ui.icon("alert-triangle", { size: 15 }),
          "This folder is large, so only part of it is listed. Selecting the folder still includes every PDF inside it."));
      }
      const folders = folderEntries();
      const files = fileEntries();
      WH.clear(listEl);
      if (!listing) return;
      if (!listing.entries.length) {
        listEl.appendChild(ui.emptyState({ icon: "folder-open", title: "This folder is empty", body: "Add PDFs or folders on the computer at the path above, then refresh. They show up here to scan.", compact: true }));
        renderActionBar();
        return;
      }
      if (!folders.length && !files.length) {
        listEl.appendChild(ui.emptyState({
          icon: "search",
          title: "Nothing matches",
          body: q ? `No names in this folder contain “${q}”.` : "No files with that status here.",
          action: { label: "Show everything", onClick: () => {
            q = "";
            statusFilter = "all";
            searchBox.querySelector("input").value = "";
            WH.router.setQuery({ q: "", status: "" });
            renderList();
          } },
          compact: true,
        }));
        renderActionBar();
        return;
      }

      if (folders.length) {
        listEl.appendChild(h("section", { class: "wh-src-folders", "aria-label": "Folders" },
          h("h3", null, "Folders"),
          h("ul", null, folders.map((entry) => renderFolder(entry)))));
      }
      if (!files.length) {
        renderActionBar();
        return;
      }

      const selectable = files.filter((e) => !coveringFolder(e.path));
      const selectedHere = selectable.filter((e) => selection.has(e.path)).length;
      const headBox = h("input", {
        type: "checkbox",
        class: "wh-check",
        "aria-label": "Select every visit form shown",
        checked: selectable.length > 0 && selectedHere === selectable.length,
        indeterminate: selectedHere > 0 && selectedHere < selectable.length,
        disabled: !WH.can.write() || !selectable.length,
        onChange: (event) => {
          selectable.forEach((e) => setSelected(e, event.target.checked));
          commitSelection();
        },
      });

      const tbody = h("tbody");
      files.forEach((entry, index) => tbody.appendChild(renderEntry(entry, index)));
      listEl.appendChild(h("section", { class: "wh-src-files" },
        h("h3", null, "Visit forms"),
        h("table", { class: "wh-table wh-src-table", "aria-label": "Visit forms" },
          h("thead", null, h("tr", null,
            h("th", { class: "wh-col-check", scope: "col" }, headBox),
            h("th", { scope: "col" }, "Name"),
            h("th", { scope: "col" }, "Status"),
            h("th", { scope: "col", class: "wh-col-num" }, "Size"),
            h("th", { scope: "col" }, "Modified"),
            h("th", { scope: "col", class: "wh-col-actions" }, h("span", { class: "wh-sr" }, "Actions")))),
          tbody)));
      const rows = tbody.querySelectorAll("tr");
      if (rows[focusIndex]) rows[focusIndex].tabIndex = 0;
      else if (rows[0]) rows[0].tabIndex = 0;
      renderActionBar();
    }

    function renderFolder(entry) {
      const covered = coveringFolder(entry.path);
      const checked = Boolean(covered) || selection.has(entry.path);
      return h("li", null,
        h("div", { class: ["wh-src-folder", checked && "is-checked"] },
          WH.can.write() && h("input", {
            type: "checkbox",
            class: "wh-check",
            checked,
            disabled: Boolean(covered),
            "aria-label": `Select ${entry.name} and everything in it`,
            title: covered ? "Included because its folder is selected" : "Scan everything in this folder",
            onClick: (event) => event.stopPropagation(),
            onChange: (event) => {
              setSelected(entry, event.target.checked);
              commitSelection();
            },
          }),
          h("button", { type: "button", class: "wh-src-folder-open", onClick: () => openFolder(entry.path) },
            ui.icon("folder", { size: 22 }),
            h("span", null, entry.name),
            h("span", { class: "wh-src-folder-go" }, "Open", ui.icon("chevron-right", { size: 14 })))));
    }

    function renderEntry(entry, index) {
      const covered = coveringFolder(entry.path);
      const checked = Boolean(covered) || selection.has(entry.path);
      const isDir = entry.type === "dir";
      const box = h("input", {
        type: "checkbox",
        class: "wh-check",
        checked,
        disabled: Boolean(covered) || !WH.can.write(),
        tabindex: "-1",
        "aria-label": `Select ${entry.name}${isDir ? " and everything in it" : ""}`,
        title: covered ? "Included because its folder is selected" : null,
        onClick: (event) => {
          event.stopPropagation();
          toggleAt(index, event.shiftKey, event.target.checked);
        },
      });
      const nameCell = h("span", { class: "wh-entry-name" }, ui.icon("pdf", { size: 18 }), h("span", null, entry.name));
      const openHref = entry.document_id == null
        ? null
        : entry.status === "processed" || entry.status === "changed"
          ? `#/review/${encodeURIComponent(entry.document_id)}`
          : `#/doc/${encodeURIComponent(entry.document_id)}/history`;
      const scanned = entry.status === "processed" || entry.status === "changed" || entry.status === "failed";
      const action = h("div", { class: "wh-src-actions" },
        openHref && h("a", {
          class: "btn btn-quiet wh-btn wh-btn-sm",
          tabindex: "-1",
          href: openHref,
          onClick: (event) => event.stopPropagation(),
        }, "Open"),
        WH.can.write() && scanned && h("button", {
          type: "button",
          class: "btn btn-quiet wh-btn wh-btn-sm",
          tabindex: "-1",
          onClick: (event) => {
            event.stopPropagation();
            scanOne(entry);
          },
        }, "Scan again"),
        WH.can.write() && !scanned && entry.status !== "processing" && h("button", {
          type: "button",
          class: "btn btn-primary wh-btn wh-btn-sm",
          tabindex: "-1",
          onClick: (event) => {
            event.stopPropagation();
            scanOne(entry);
          },
        }, "Extract"));

      const row = h("tr", {
        class: ["wh-row", checked && "is-checked", covered && "is-covered"],
        tabindex: "-1",
        dataset: { index: String(index) },
        "aria-selected": String(checked),
        onClick: (event) => {
          if (event.target.closest("a, input, button")) return;
          if (!covered && WH.can.write()) toggleAt(index, event.shiftKey, !selection.has(entry.path));
        },
        onKeydown: (event) => onRowKey(event, entry, index),
      },
      h("td", { class: "wh-col-check" }, box),
      h("td", { class: "wh-col-name" }, nameCell),
      h("td", null, isDir ? h("span", { class: "wh-muted" }, "Folder") : entry.status ? ui.chip(entry.status, { size: "sm" }) : null),
      h("td", { class: "wh-col-num" }, isDir ? "" : WH.fmt.bytes(entry.size)),
      h("td", null, entry.modified ? ui.time(entry.modified) : ""),
      h("td", { class: "wh-col-actions" }, action));
      return row;
    }

    function onRowKey(event, entry, index) {
      const rows = Array.from(listEl.querySelectorAll("tbody tr"));
      const focusRow = (i) => {
        const r = rows[Math.max(0, Math.min(rows.length - 1, i))];
        if (!r) return;
        rows.forEach((x) => (x.tabIndex = -1));
        r.tabIndex = 0;
        r.focus();
        focusIndex = Number(r.dataset.index);
      };
      if (event.key === "ArrowDown" || event.key === "j") {
        event.preventDefault();
        focusRow(index + 1);
        if (event.shiftKey && WH.can.write()) toggleAt(index + 1, true, true);
      } else if (event.key === "ArrowUp" || event.key === "k") {
        event.preventDefault();
        focusRow(index - 1);
        if (event.shiftKey && WH.can.write()) toggleAt(index - 1, true, true);
      } else if (event.key === " ") {
        event.preventDefault();
        if (!coveringFolder(entry.path) && WH.can.write()) toggleAt(index, event.shiftKey, !selection.has(entry.path));
      } else if (event.key === "Enter") {
        event.preventDefault();
        if (entry.document_id != null) WH.router.go(`/review/${entry.document_id}`);
      } else if (event.key === "Backspace" && listing && path) {
        event.preventDefault();
        openFolder(listing.parent || "");
      }
    }

    /* --------------------------------------------------------- selection */

    function setSelected(entry, on) {
      knownEntries.set(entry.path, entry);
      if (on) {
        selection.add(entry.path);
        // A selected folder covers everything below it.
        if (entry.type === "dir") [...selection].forEach((p) => p !== entry.path && within(p, entry.path) && selection.delete(p));
      } else selection.delete(entry.path);
    }

    function toggleAt(index, shift, on) {
      const entries = fileEntries();
      const entry = entries[index];
      if (!entry) return;
      if (shift && anchorIndex != null) {
        const [a, b] = anchorIndex < index ? [anchorIndex, index] : [index, anchorIndex];
        entries.slice(a, b + 1).forEach((e) => {
          if (!coveringFolder(e.path)) setSelected(e, on);
        });
      } else {
        setSelected(entry, on);
      }
      anchorIndex = index;
      focusIndex = index;
      commitSelection();
    }

    function commitSelection() {
      WH.router.setQuery({ sel: selection.size <= 150 ? [...selection] : [] });
      const active = document.activeElement;
      const hadRowFocus = active && active.closest && active.closest(".wh-src-table");
      renderList();
      if (hadRowFocus) {
        const row = listEl.querySelector(`tbody tr[data-index="${focusIndex}"]`);
        if (row) {
          listEl.querySelectorAll("tbody tr").forEach((r) => (r.tabIndex = -1));
          row.tabIndex = 0;
          row.focus({ preventScroll: true });
        }
      }
    }

    function selectionSummary() {
      let files = 0;
      let folders = 0;
      const statuses = {};
      selection.forEach((p) => {
        const e = knownEntries.get(p);
        if (e && e.type === "dir") folders += 1;
        else {
          files += 1;
          const s = (e && e.status) || "new";
          statuses[s] = (statuses[s] || 0) + 1;
        }
      });
      return { files, folders, statuses };
    }

    function scanLabel({ files, folders }) {
      if (!folders) return `Scan ${WH.fmt.plural(files, "file")}`;
      if (!files) return `Scan ${WH.fmt.plural(folders, "folder")}`;
      return `Scan ${WH.fmt.plural(files, "file")} + ${WH.fmt.plural(folders, "folder")}`;
    }

    function renderActionBar() {
      if (!WH.can.write() || !selection.size) {
        actionBar.hidden = true;
        document.body.classList.remove("has-actionbar");
        return;
      }
      const summary = selectionSummary();
      const parts = Object.entries(summary.statuses).map(([s, n]) => `${n} ${ui.statusInfo(s).label.toLowerCase()}`);
      WH.clear(actionBar).append(
        h("div", { class: "wh-actionbar-inner" },
          h("span", { class: "wh-actionbar-count" }, h("strong", null, WH.fmt.number(selection.size)), " selected"),
          h("span", { class: "wh-actionbar-detail" },
            [summary.files && WH.fmt.plural(summary.files, "visit form"), summary.folders && `${WH.fmt.plural(summary.folders, "folder")} (everything inside)`].filter(Boolean).join(" · "),
            parts.length ? ` — ${parts.join(", ")}` : "",
            (summary.statuses.processed || summary.statuses.changed) ? " · already scanned ones ask first" : ""),
          h("span", { class: "wh-grow" }),
          h("button", { type: "button", class: "btn btn-quiet wh-btn", onClick: () => {
            selection.clear();
            commitSelection();
          } }, "Clear"),
          h("button", { type: "button", class: "btn btn-primary wh-btn", id: "wh-scan-btn", onClick: scan },
            ui.icon("play", { size: 15 }), h("span", { class: "wh-btn-label" }, scanLabel(summary)))));
      actionBar.hidden = false;
      document.body.classList.add("has-actionbar");
    }

    /* -------------------------------------------------------------- scan */

    async function scanOne(entry) {
      knownEntries.set(entry.path, entry);
      selection = new Set([entry.path]);
      WH.router.setQuery({ sel: [entry.path] });
      renderList();
      await scan();
    }

    async function scan() {
      const btn = document.getElementById("wh-scan-btn");
      const label = btn.querySelector(".wh-btn-label");
      const original = label.textContent;
      btn.disabled = true;
      btn.classList.add("is-busy");
      label.textContent = selectionSummary().folders ? "Checking folders…" : "Checking files…";
      try {
        const preview = await WH.api("/api/batches/preview", { body: { source_id: sourceId, paths: [...selection] } });
        const files = preview.files || [];
        if (!files.length) {
          ui.toast({ message: "No PDFs found in that selection", detail: "Folders are searched recursively; only PDF files are read.", kind: "warn" });
          return;
        }
        const needsDecision = files.some((f) => f.status !== "new");
        if (needsDecision) {
          duplicateDialog(files, preview.counts || {});
        } else {
          await createBatch(files.map((f) => ({ path: f.path, sha256: f.sha256, reprocess: false })), "");
        }
      } catch (error) {
        ui.toast({ message: "Couldn't check the selection", detail: error.detail || error.message, kind: "error" });
      } finally {
        if (btn.isConnected) {
          btn.disabled = false;
          btn.classList.remove("is-busy");
          label.textContent = original;
        }
      }
    }

    async function createBatch(files, reason) {
      const data = await WH.api("/api/batches", { body: { source_id: sourceId, files, reason: reason || null } });
      const batch = data.batch || {};
      const reprocessed = files.filter((f) => f.reprocess).length;
      ui.toast({
        message: `Batch #${batch.number || batch.id} started`,
        detail: [
          `${WH.fmt.plural(batch.total != null ? batch.total : files.length, "file")} queued`,
          reprocessed && `${reprocessed} scanned again`,
          batch.skipped && `${batch.skipped} skipped`,
        ].filter(Boolean).join(" · "),
      });
      selection.clear();
      // Statuses in this folder are about to change.
      browseCache.forEach((_, k) => k.startsWith(`${sourceId}|`) && browseCache.delete(k));
      WH.shell.refreshBadges();
      WH.router.go(`/batches/${encodeURIComponent(batch.id)}`);
      return batch;
    }

    /* The duplicate dialog: one decision per previously-read file. */
    function duplicateDialog(files) {
      const decisions = new Map(); // path → "skip" | "reprocess"
      const groups = { changed: [], processed: [], processing: [], unreadable: [], new: [] };
      files.forEach((f) => {
        (groups[f.status] || groups.new).push(f);
        if (f.status === "changed") decisions.set(f.path, "reprocess");
        else if (f.status === "processed") decisions.set(f.path, "skip");
      });
      const decidable = [...groups.changed, ...groups.processed];

      const reason = ui.reasonField({ label: "Why scan again?", picks: REPROCESS_PICKS });
      const reasonWrap = h("div", { class: "wh-dup-reason" }, reason.el);
      const summary = h("p", { class: "wh-dup-summary", "aria-live": "polite" });
      const segs = new Map();

      function fileRow(f, locked) {
        const prior = f.prior;
        const priorText = prior
          ? [`Read ${WH.fmt.relative(prior.processed_at)}`, prior.processed_by && `by ${prior.processed_by}`].filter(Boolean).join(" ")
          : null;
        let control;
        if (locked === "processing") {
          control = h("span", { class: "wh-dup-locked" }, ui.icon("loader", { size: 14, className: "wh-spin" }), "Being read now — skipped");
        } else if (locked === "unreadable") {
          control = h("span", { class: "wh-dup-locked wh-dup-locked--bad" }, ui.icon("ban", { size: 14 }), "Can't be read — skipped");
        } else {
          control = ui.segmented({
            label: `What to do with ${f.name}`,
            size: "sm",
            value: decisions.get(f.path),
            items: [
              { value: "skip", label: "Keep the last scan" },
              { value: "reprocess", label: "Scan again" },
            ],
            onChange: (v) => {
              decisions.set(f.path, v);
              refresh();
            },
          });
          segs.set(f.path, control);
        }
        return h("li", { class: "wh-dup-row" },
          h("div", { class: "wh-dup-file" },
            h("span", { class: "wh-dup-name" }, ui.icon("pdf", { size: 16 }), h("span", null, f.name)),
            h("span", { class: "wh-dup-meta" },
              ui.chip(f.status, { size: "sm" }),
              priorText && h("span", { title: prior && prior.processed_at ? WH.fmt.both(prior.processed_at) : null }, priorText),
              f.error && h("span", { class: "wh-dup-error" }, f.error),
              f.size != null && h("span", { class: "wh-muted" }, WH.fmt.bytes(f.size))),
            prior && prior.submitted && h("span", { class: "wh-dup-warn" }, ui.icon("check-seal", { size: 14 }),
              "Already submitted to the EDC. Scanning again makes a new version here. The EDC keeps the submitted values until someone reviews and submits again."),
            h("span", { class: "wh-dup-path", title: f.path }, f.path)),
          control);
      }

      function section(title, hint, list, locked) {
        if (!list.length) return null;
        return h("section", { class: "wh-dup-section" },
          h("h3", null, title, h("span", { class: "wh-dup-count" }, String(list.length))),
          hint && h("p", { class: "wh-dup-hint" }, hint),
          h("ul", { class: "wh-dup-list" }, list.map((f) => fileRow(f, locked))));
      }

      const bulk = decidable.length > 1
        ? h("div", { class: "wh-dup-bulk", role: "group", "aria-label": "Decide for all" },
            h("button", { type: "button", class: "btn btn-quiet wh-btn wh-btn-sm", onClick: () => setAll("skip") }, "Keep all"),
            h("button", { type: "button", class: "btn btn-quiet wh-btn wh-btn-sm", onClick: () => setAll("reprocess") }, "Scan all again"))
        : null;
      function setAll(v) {
        decidable.forEach((f) => {
          decisions.set(f.path, v);
          const seg = segs.get(f.path);
          if (seg) seg.set(v);
        });
        refresh();
      }

      const newNote = groups.new.length
        ? h("details", { class: "wh-dup-new" },
            h("summary", null, ui.icon("plus", { size: 14 }), `${WH.fmt.plural(groups.new.length, "new file")} will be read`),
            h("ul", null, groups.new.slice(0, 200).map((f) => h("li", { title: f.path }, f.name))))
        : null;

      const body = h("div", { class: "wh-dup" },
        h("div", { class: "wh-dup-top" }, summary, bulk),
        newNote,
        section("Changed on the server", "The file is different from the one scanned before. Scanning again is suggested.", groups.changed),
        section("Already scanned", "Same file as the last scan. Keeping it is suggested, and that choice is recorded in the audit trail.", groups.processed),
        section("Being read right now", null, groups.processing, "processing"),
        section("Can't be read", "These files are damaged or not real PDFs.", groups.unreadable, "unreadable"),
        reasonWrap);

      const layer = ui.modal({
        title: decidable.length === 1 && !groups.new.length ? "This visit was already scanned" : "Some of these were already scanned",
        description: "Keep the last scan, or scan again. Nothing is sent to the EDC from here.",
        size: "lg",
        body,
        actions: [
          { label: "Cancel", kind: "quiet", value: false },
          {
            id: "go",
            label: "Scan",
            kind: "primary",
            busyLabel: "Starting…",
            onClick: async () => {
              const anyReprocess = [...decisions.values()].includes("reprocess");
              if (anyReprocess && !reason.validate()) return false;
              const payload = [
                ...groups.new.map((f) => ({ path: f.path, sha256: f.sha256, reprocess: false })),
                ...decidable.map((f) => ({ path: f.path, sha256: f.sha256, reprocess: decisions.get(f.path) === "reprocess" })),
              ];
              await createBatch(payload, anyReprocess ? reason.value : "");
              return true;
            },
          },
        ],
      });

      function refresh() {
        const reprocess = [...decisions.values()].filter((v) => v === "reprocess").length;
        const skip = [...decisions.values()].filter((v) => v === "skip").length + groups.processing.length + groups.unreadable.length;
        const n = groups.new.length;
        reasonWrap.hidden = !reprocess;
        reasonWrap.classList.toggle("is-open", Boolean(reprocess));
        const btn = layer.button("go");
        const label = btn.querySelector(".wh-btn-label");
        let text;
        if (n && reprocess) text = `Scan ${n} new + ${reprocess} again`;
        else if (n) text = `Scan ${n} new`;
        else if (reprocess) text = `Scan ${reprocess} again`;
        else text = "Nothing to scan";
        label.textContent = text;
        btn.disabled = !n && !reprocess;
        summary.textContent = [
          n && `${WH.fmt.plural(n, "new file")} to read`,
          reprocess && `${reprocess} to scan again`,
          skip && `${skip} skipped`,
        ].filter(Boolean).join(" · ") || "Everything is skipped.";
      }
      refresh();
    }

    /* While anything in this folder is being read, refresh statuses. */
    let livePoll = null;
    function scheduleLivePoll() {
      const busy = listing && listing.entries.some((e) => e.status === "processing");
      if (busy && !livePoll) {
        livePoll = WH.poll(async () => {
          const data = await browse(sourceId, path, { fresh: true, background: true });
          if (JSON.stringify(data.entries) !== JSON.stringify(listing.entries)) {
            listing = data;
            data.entries.forEach((e) => knownEntries.set(e.path, e));
            renderList();
          }
          if (!data.entries.some((e) => e.status === "processing")) {
            livePoll.stop();
            livePoll = null;
          }
        }, 5000);
      } else if (!busy && livePoll) {
        livePoll.stop();
        livePoll = null;
      }
    }

    start();

    return {
      update(next) {
        const nextSource = next.params.sourceId || null;
        const nextPath = next.query.get("path") || "";
        q = next.query.get("q") || "";
        statusFilter = next.query.get("status") || "all";
        selection = new Set(next.query.getAll("sel"));
        searchBox.querySelector("input").value = q;
        if (String(nextSource) !== String(sourceId)) {
          sourceId = nextSource;
          path = nextPath;
          listing = null;
          if (livePoll) {
            livePoll.stop();
            livePoll = null;
          }
          start();
        } else if (nextPath !== path || !listing) {
          openFolder(nextPath, { push: false });
        } else {
          renderList();
        }
      },
      destroy() {
        token += 1;
        if (livePoll) livePoll.stop();
        document.body.classList.remove("has-actionbar");
      },
    };
  }

  WH.router.add("/sources/:sourceId?", {
    name: "sources",
    nav: "sources",
    title: "Sources",
    crumbs: () => [{ label: "Sources" }],
    mount,
  });
})();
