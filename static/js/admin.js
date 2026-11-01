/* Webo Healthtech eSource — Admin (#/admin).

   Users and roles: create a user with a temporary password (≥ 12
   characters, with a generator), change roles, deactivate / reactivate,
   unlock a locked account and reset a password. Admins can't demote or
   deactivate themselves from here, so the system can't lose its last admin
   by accident. */

(function () {
  "use strict";

  const WH = window.WH;
  const { h, ui } = WH;
  const MIN_PASSWORD = 12;

  const ROLES = [
    { value: "reviewer", label: "Reviewer", body: "Scans sources, reviews reads, signs and submits to the EDC." },
    { value: "monitor", label: "Monitor", body: "Read-only: sees documents, history and the audit trail." },
    { value: "admin", label: "Admin", body: "Everything a reviewer can do, plus managing users." },
  ];
  const roleLabel = (r) => (ROLES.find((x) => x.value === r) || { label: WH.fmt.humanize(r) }).label;

  const isLocked = (u) => u.locked_until && WH.parseDate(u.locked_until) > new Date();
  const userStatus = (u) => (!u.active ? "inactive" : isLocked(u) ? "locked" : "active");

  /* A readable random password: no look-alike characters. */
  function generatePassword(length = 16) {
    const alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789-_!?";
    const bytes = new Uint32Array(length);
    crypto.getRandomValues(bytes);
    return Array.from(bytes, (n) => alphabet[n % alphabet.length]).join("");
  }

  function strength(pw) {
    let score = 0;
    if (pw.length >= MIN_PASSWORD) score += 1;
    if (pw.length >= 16) score += 1;
    if (/[a-z]/.test(pw) && /[A-Z]/.test(pw)) score += 1;
    if (/\d/.test(pw)) score += 1;
    if (/[^A-Za-z0-9]/.test(pw)) score += 1;
    if (pw.length < MIN_PASSWORD) return { level: 0, label: `At least ${MIN_PASSWORD} characters` };
    return [null, { level: 1, label: "Weak" }, { level: 2, label: "Fair" }, { level: 3, label: "Good" }, { level: 4, label: "Strong" }, { level: 4, label: "Strong" }][score];
  }

  /* Password input with show/hide, generate, copy and a strength meter. */
  function passwordField(label) {
    const input = ui.input({ type: "password", autocomplete: "new-password", spellcheck: "false", class: "wh-input wh-input--mono" });
    const meter = h("span", { class: "wh-meter", "aria-hidden": "true" }, h("span"), h("span"), h("span"), h("span"));
    const meterText = h("span", { class: "wh-meter-text", "aria-live": "polite" });
    const reveal = ui.iconButton("eye", "Show password", () => {
      const showing = input.type === "text";
      input.type = showing ? "password" : "text";
      WH.clear(reveal).appendChild(ui.icon(showing ? "eye" : "eye-off", { size: 18 }));
      reveal.setAttribute("aria-label", showing ? "Show password" : "Hide password");
    });
    const gen = h("button", { type: "button", class: "btn btn-quiet wh-btn wh-btn-sm", onClick: () => {
      input.value = generatePassword();
      input.type = "text";
      WH.clear(reveal).appendChild(ui.icon("eye-off", { size: 18 }));
      update();
      input.focus();
      input.select();
    } }, ui.icon("sparkle", { size: 14 }), "Generate");
    const field = ui.field({ label, required: true, control: input, hint: "Temporary — the user is asked to change it after signing in. Share it privately." });
    const row = h("div", { class: "wh-pw-row" });
    field.replaceChild(row, input); // swap first: appending moves the input
    row.append(input, reveal, gen);
    field.insertBefore(h("div", { class: "wh-pw-meter" }, meter, meterText), field.querySelector(".wh-field-hint"));
    function update() {
      const s = strength(input.value);
      meter.dataset.level = String(s.level);
      meterText.textContent = input.value ? s.label : "";
      if (input.value.length >= MIN_PASSWORD) field.setError("");
    }
    input.addEventListener("input", update);
    return {
      el: field,
      input,
      get value() {
        return input.value;
      },
      validate() {
        if (input.value.length < MIN_PASSWORD) {
          field.setError(`Use at least ${MIN_PASSWORD} characters. “Generate” makes a strong one.`);
          input.focus();
          return false;
        }
        return true;
      },
    };
  }

  function roleCards(value, name) {
    const group = h("div", { class: "wh-role-cards", role: "radiogroup", "aria-label": "Role" });
    ROLES.forEach((r) => {
      const id = `${name}-${r.value}`;
      group.appendChild(h("label", { class: "wh-role-card", for: id },
        h("input", { type: "radio", name, id, value: r.value, checked: r.value === value }),
        h("span", { class: "wh-role-card-body" }, h("strong", null, r.label), h("span", null, r.body))));
    });
    group.getValue = () => (group.querySelector("input:checked") || {}).value;
    return group;
  }

  /* -------------------------------------------------------------- mount */

  function mount(root, ctx) {
    let users = [];
    let q = ctx.query.get("q") || "";
    let showInactive = ctx.query.get("inactive") === "1";

    const statsSlot = h("div");
    const search = ui.searchBox({ value: q, placeholder: "Find a user", label: "Find a user", onInput: (v) => {
      q = v;
      WH.router.setQuery({ q: v });
      renderTable();
    } });
    const inactiveToggle = h("button", { type: "button", class: "wh-toggle-chip", "aria-pressed": String(showInactive), onClick: () => {
      showInactive = !showInactive;
      inactiveToggle.setAttribute("aria-pressed", String(showInactive));
      WH.router.setQuery({ inactive: showInactive ? "1" : "" });
      renderTable();
    } }, ui.icon("ban", { size: 13 }), "Show deactivated");
    const tableEl = h("div");

    root.append(
      ui.pageHeader({
        eyebrow: "Administration",
        title: "Users & roles",
        sub: "Who can sign in, and what they're allowed to do. Every change here is recorded in the audit trail.",
        actions: [h("button", { type: "button", class: "btn btn-primary wh-btn", onClick: createUser }, ui.icon("user-plus", { size: 16 }), "Add user")],
      }),
      statsSlot,
      h("div", { class: "wh-card wh-admin" }, h("div", { class: "wh-inbox-head" }, h("div", { class: "wh-filterbar" }, search, inactiveToggle)), tableEl)
    );

    async function load() {
      await ui.load(tableEl, {
        skeleton: () => ui.sk.table(6, [30, 14, 14, 14, 20]),
        label: "users",
        task: () => WH.api("/api/admin/users"),
        render: (data) => {
          users = data.users || [];
          renderStats();
          renderTable();
        },
      });
    }

    function renderStats() {
      const active = users.filter((u) => u.active);
      const tiles = [
        ["Active users", active.length, "users", "brand"],
        ["Reviewers", active.filter((u) => u.role === "reviewer").length, "pen", "neutral"],
        ["Monitors", active.filter((u) => u.role === "monitor").length, "eye", "neutral"],
        ["Admins", active.filter((u) => u.role === "admin").length, "shield", "neutral"],
        ["Locked", users.filter(isLocked).length, "lock", users.some(isLocked) ? "danger" : "neutral"],
      ];
      WH.clear(statsSlot).appendChild(h("dl", { class: "wh-stats wh-stats--cards" },
        tiles.map(([label, n, icon, tone]) => h("div", { class: ["wh-stat", `wh-stat--${tone}`] }, h("dt", null, ui.icon(icon, { size: 14 }), label), h("dd", null, String(n))))));
    }

    function renderTable() {
      WH.clear(tableEl);
      const needle = q.toLowerCase();
      const list = users
        .filter((u) => showInactive || u.active)
        .filter((u) => !needle || `${u.username} ${u.full_name}`.toLowerCase().includes(needle))
        .sort((a, b) => Number(b.active) - Number(a.active) || String(a.full_name || a.username).localeCompare(String(b.full_name || b.username)));
      if (!list.length) {
        tableEl.appendChild(ui.emptyState({
          icon: "users",
          title: needle ? "No users match" : "No users yet",
          body: needle ? "Check the spelling, or show deactivated users." : "Add the first reviewer to get started.",
          action: needle ? null : { label: "Add user", onClick: createUser, icon: "user-plus" },
          compact: Boolean(needle),
        }));
        return;
      }
      const tbody = h("tbody");
      list.forEach((u) => tbody.appendChild(userRow(u)));
      tableEl.appendChild(h("table", { class: "wh-table wh-users-table", "aria-label": "Users" },
        h("thead", null, h("tr", null,
          h("th", { scope: "col" }, "User"),
          h("th", { scope: "col" }, "Role"),
          h("th", { scope: "col" }, "Status"),
          h("th", { scope: "col" }, "Last sign-in"),
          h("th", { scope: "col", class: "wh-col-actions" }, h("span", { class: "wh-sr" }, "Actions")))),
        tbody));
    }

    function userRow(u) {
      const self = WH.me && u.username === WH.me.username;
      const roleSelect = ui.select(ROLES.map((r) => ({ value: r.value, label: r.label, selected: r.value === u.role })), {
        class: "wh-input wh-input--sm wh-select",
        "aria-label": `Role for ${u.full_name || u.username}`,
        disabled: self || !u.active,
        title: self ? "You can't change your own role" : null,
      });
      roleSelect.addEventListener("change", () => changeRole(u, roleSelect));

      const actions = h("div", { class: "wh-row-actions" });
      if (isLocked(u)) actions.appendChild(h("button", { type: "button", class: "btn btn-quiet wh-btn wh-btn-xs", onClick: () => unlock(u) }, ui.icon("unlock", { size: 14 }), "Unlock"));
      if (u.active) actions.appendChild(h("button", { type: "button", class: "btn btn-quiet wh-btn wh-btn-xs", onClick: () => resetPassword(u) }, "Reset password"));
      if (!self) {
        actions.appendChild(u.active
          ? h("button", { type: "button", class: "btn btn-quiet wh-btn wh-btn-xs wh-btn-danger-quiet", onClick: () => setActive(u, false) }, "Deactivate")
          : h("button", { type: "button", class: "btn btn-quiet wh-btn wh-btn-xs", onClick: () => setActive(u, true) }, "Reactivate"));
      }
      const status = userStatus(u);
      return h("tr", { class: ["wh-row", !u.active && "is-inactive"], dataset: { user: u.username } },
        h("td", null, h("span", { class: "wh-user-cell" }, ui.avatar(u.full_name || u.username, { size: 32 }),
          h("span", null, h("strong", { class: "wh-block" }, u.full_name || u.username, self ? h("span", { class: "wh-you" }, " (you)") : null),
            h("span", { class: "wh-muted wh-mono-num" }, u.username)))),
        h("td", null, roleSelect),
        h("td", null, ui.chip(status, {
          size: "sm",
          label: status === "locked" ? `Locked until ${WH.fmt.time(u.locked_until)}` : null,
          title: status === "locked" ? WH.fmt.both(u.locked_until) : null,
        })),
        h("td", null, u.last_login_at ? ui.time(u.last_login_at) : h("span", { class: "wh-muted" }, "Never")),
        h("td", { class: "wh-col-actions" }, actions));
    }

    async function patch(u, body) {
      const data = await WH.api(`/api/admin/users/${encodeURIComponent(u.username)}`, { method: "PATCH", body });
      const next = data.user || { ...u, ...body };
      users = users.map((x) => (x.username === u.username ? next : x));
      renderStats();
      renderTable();
      return next;
    }

    async function changeRole(u, select) {
      const next = select.value;
      const ok = await ui.confirm({
        title: `Make ${u.full_name || u.username} ${/^[aeiou]/i.test(roleLabel(next)) ? "an" : "a"} ${roleLabel(next).toLowerCase()}?`,
        message: `${roleLabel(u.role)} → ${roleLabel(next)}. ${ROLES.find((r) => r.value === next).body} The change applies the next time they load a page.`,
        confirmLabel: "Change role",
      });
      if (!ok) {
        select.value = u.role;
        return;
      }
      try {
        await patch(u, { role: next });
        ui.toast({ message: `${u.full_name || u.username} is now ${roleLabel(next).toLowerCase()}` });
      } catch (error) {
        select.value = u.role;
        ui.toast({ message: "Couldn't change the role", detail: error.detail, kind: "error" });
      }
    }

    async function setActive(u, active) {
      if (!active) {
        const ok = await ui.confirm({
          title: `Deactivate ${u.full_name || u.username}?`,
          message: "They're signed out and can't sign in again. Their history stays in the audit trail. You can reactivate them later.",
          confirmLabel: "Deactivate",
          danger: true,
        });
        if (!ok) return;
      }
      try {
        await patch(u, { active });
        ui.toast({
          message: active ? `${u.full_name || u.username} reactivated` : `${u.full_name || u.username} deactivated`,
          action: { label: "Undo", onClick: () => patch(u, { active: !active }) },
        });
      } catch (error) {
        ui.toast({ message: "Couldn't update the user", detail: error.detail, kind: "error" });
      }
    }

    async function unlock(u) {
      try {
        await patch(u, { unlock: true });
        ui.toast({ message: `${u.full_name || u.username} can sign in again` });
      } catch (error) {
        ui.toast({ message: "Couldn't unlock", detail: error.detail, kind: "error" });
      }
    }

    function resetPassword(u) {
      const pw = passwordField("New temporary password");
      ui.modal({
        title: `Reset ${u.full_name || u.username}'s password`,
        description: "Their current password stops working immediately.",
        size: "sm",
        body: h("div", { class: "wh-form" }, pw.el),
        actions: [
          { label: "Cancel", kind: "quiet", value: false },
          { label: "Reset password", kind: "primary", busyLabel: "Saving…", onClick: async () => {
            if (!pw.validate()) return false;
            await patch(u, { password: pw.value });
            shareCredentials(u.username, pw.value, "Password reset");
            return true;
          } },
        ],
      });
    }

    function createUser() {
      const username = ui.input({ autocomplete: "off", spellcheck: "false", placeholder: "e.g. jsmith", class: "wh-input wh-input--mono" });
      const fullName = ui.input({ autocomplete: "off", placeholder: "e.g. Jane Smith" });
      const fUser = ui.field({ label: "Username", required: true, control: username, hint: "Lowercase letters, numbers, dot, dash or underscore. Used to sign in." });
      const fName = ui.field({ label: "Full name", required: true, control: fullName, hint: "Shown on electronic signatures exactly as typed." });
      const roles = roleCards("reviewer", "wh-new-role");
      const pw = passwordField("Temporary password");
      username.addEventListener("input", () => {
        username.value = username.value.toLowerCase().replace(/\s+/g, "");
        fUser.setError("");
      });
      fullName.addEventListener("input", () => fName.setError(""));
      // Suggest a username from the name while the username is untouched.
      let touched = false;
      username.addEventListener("keydown", () => (touched = true));
      fullName.addEventListener("input", () => {
        if (touched) return;
        const parts = fullName.value.trim().toLowerCase().split(/\s+/).filter(Boolean);
        username.value = parts.length > 1 ? `${parts[0][0]}${parts[parts.length - 1]}`.replace(/[^a-z0-9._-]/g, "") : (parts[0] || "").replace(/[^a-z0-9._-]/g, "");
      });

      ui.modal({
        title: "Add a user",
        size: "md",
        body: h("form", { class: "wh-form", onSubmit: (e) => e.preventDefault() },
          h("div", { class: "wh-form-row" }, fName, fUser),
          h("fieldset", { class: "wh-fieldset" }, h("legend", { class: "wh-field-label" }, "Role"), roles),
          pw.el),
        initialFocus: "input",
        actions: [
          { label: "Cancel", kind: "quiet", value: false },
          {
            label: "Add user",
            kind: "primary",
            busyLabel: "Adding…",
            onClick: async () => {
              let ok = true;
              if (!fullName.value.trim()) {
                fName.setError("Enter their full name.");
                ok = false;
              }
              if (!/^[a-z0-9._-]{3,}$/.test(username.value)) {
                fUser.setError("At least 3 characters: lowercase letters, numbers, dot, dash or underscore.");
                ok = false;
              } else if (users.some((x) => x.username === username.value)) {
                fUser.setError("That username is taken.");
                ok = false;
              }
              if (!pw.validate()) ok = false;
              if (!ok) {
                const bad = document.querySelector(".wh-layer-panel [aria-invalid='true']");
                if (bad) bad.focus();
                return false;
              }
              const data = await WH.api("/api/admin/users", {
                body: { username: username.value, full_name: fullName.value.trim(), role: roles.getValue(), password: pw.value },
              });
              users.push(data.user || { username: username.value, full_name: fullName.value.trim(), role: roles.getValue(), active: true });
              renderStats();
              renderTable();
              shareCredentials(username.value, pw.value, "User added");
              return true;
            },
          },
        ],
      });
    }

    /* Shown once, right after setting a password, so it can be passed on. */
    function shareCredentials(username, password, title) {
      setTimeout(() => {
        ui.modal({
          title,
          description: "Pass these on privately. The password isn't shown again.",
          size: "sm",
          body: h("dl", { class: "wh-kv wh-creds" },
            h("dt", null, "Username"), h("dd", null, h("span", { class: "wh-hash" }, h("code", null, username), ui.copyButton(username, { label: "Copy username" }))),
            h("dt", null, "Temporary password"), h("dd", null, h("span", { class: "wh-hash" }, h("code", null, password), ui.copyButton(password, { label: "Copy password" })))),
          actions: [{ label: "Done", kind: "primary", value: true }],
        });
      }, 240);
    }

    load();
    return {
      update(next) {
        q = next.query.get("q") || "";
        showInactive = next.query.get("inactive") === "1";
        search.querySelector("input").value = q;
        inactiveToggle.setAttribute("aria-pressed", String(showInactive));
        renderTable();
      },
    };
  }

  WH.router.add("/admin", {
    name: "admin",
    nav: "admin",
    title: "Users & roles",
    allow: (me) => Boolean(me && me.role === "admin"),
    crumbs: () => [{ label: "Admin" }, { label: "Users & roles" }],
    mount,
  });
})();
