const view = document.getElementById("view");
const crumb = document.getElementById("crumb");
const errbar = document.getElementById("errbar");
const loginWrap = document.getElementById("login");
const shell = document.getElementById("shell");
const sidebar = document.getElementById("sidebar");
const menubtn = document.getElementById("menubtn");
const demoBanner = document.getElementById("demo-banner");
const logoutBtn = document.getElementById("logout-btn");

let me = null;
let authStatus = null;
let currentWs = null;
let routeVersion = 0;
let routeCtl = null;

const WS_KEY = "partial_workspace_id";
const ROLE_RANK = { viewer: 0, member: 1, admin: 2, owner: 3 };

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}

function showErr(msg) {
  errbar.textContent = String(msg);
  errbar.classList.add("show");
  clearTimeout(showErr._t);
  showErr._t = setTimeout(() => errbar.classList.remove("show"), 6000);
}

class HttpError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

function isAbort(err) {
  return !!err && err.name === "AbortError";
}

function demoMode() { return !!(me && me.demo); }

function wsHeaders(extra, wsId) {
  const h = { ...(extra || {}) };
  const id = wsId !== undefined ? wsId
    : (currentWs && currentWs.id);
  if (id && !demoMode()) {
    h["X-Partial-Workspace"] = id;
  }
  return h;
}

async function api(path, opts = {}) {
  const init = { credentials: "same-origin", ...opts };
  init.headers = wsHeaders(init.headers, opts.ws);
  const res = await fetch(path, init);
  let data = null;
  const ct = res.headers.get("Content-Type") || "";
  if (ct.includes("json")) data = await res.json().catch(() => null);
  if (!res.ok) {
    if (res.status === 401 && !me?.demo) showLogin("signin");
    throw new HttpError(res.status,
      (data && data.error) || `HTTP ${res.status}`);
  }
  return data;
}

async function fetchBlob(path, filename, wsId, signal) {
  try {
    const res = await fetch(path, {
      credentials: "same-origin", signal,
      headers: wsHeaders(null, wsId) });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 10000);
  } catch (err) {
    if (isAbort(err)) return;
    showErr(err.message || "Download failed");
  }
}

function setSidebarOpen(open) {
  sidebar.classList.toggle("open", open);
  menubtn.setAttribute("aria-expanded", open ? "true" : "false");
}

const LOGIN_SUBS = {
  signin: "Sign in to this workspace",
  setup: "Create the first workspace account",
  invite: "Accept a workspace invitation",
};

function showLogin(mode) {
  me = null;
  currentWs = null;
  routeVersion++;
  if (routeCtl) routeCtl.abort();
  closeDocOverlay();
  view.textContent = "";
  setSidebarOpen(false);
  document.getElementById("sys-status").textContent = "";
  setCrumb("");
  demoBanner.hidden = true;
  shell.hidden = true;
  loginWrap.hidden = false;
  mode = mode || "signin";
  document.getElementById("login-form").hidden = mode !== "signin";
  document.getElementById("setup-form").hidden = mode !== "setup";
  document.getElementById("invite-form").hidden = mode !== "invite";
  const toggle = document.getElementById("invite-toggle");
  toggle.hidden = mode === "setup";
  toggle.textContent = mode === "invite"
    ? "Back to sign-in" : "Have an invitation token?";
  document.getElementById("login-sub").textContent =
    LOGIN_SUBS[mode] || LOGIN_SUBS.signin;
  document.getElementById("login-hint").hidden = mode !== "setup";
  const errEl = document.getElementById("login-error");
  errEl.textContent = "";
  const focusId = { signin: "login-email", setup: "setup-email",
                    invite: "invite-token" }[mode];
  const f = focusId && document.getElementById(focusId);
  if (f) f.focus();
}

function showApp() {
  loginWrap.hidden = true;
  shell.hidden = false;
}

async function postAuth(path, obj) {
  const res = await fetch(path, {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(obj),
  });
  const data = await res.json().catch(() => null);
  if (!res.ok) {
    throw new Error((data && data.error) || `HTTP ${res.status}`);
  }
  return data;
}

document.getElementById("invite-toggle")
  .addEventListener("click", () => {
    const invite = document.getElementById("invite-form");
    showLogin(invite.hidden ? "invite" : "signin");
  });

document.getElementById("login-form")
  .addEventListener("submit", async (e) => {
    e.preventDefault();
    const email = document.getElementById("login-email").value.trim();
    const pwIn = document.getElementById("login-password");
    const errEl = document.getElementById("login-error");
    errEl.textContent = "";
    try {
      await postAuth("/api/login", { email, password: pwIn.value });
      pwIn.value = "";
      await boot();
    } catch (err) {
      errEl.textContent = err.message || "Sign-in failed";
    } finally {
      pwIn.value = "";
    }
  });

document.getElementById("setup-form")
  .addEventListener("submit", async (e) => {
    e.preventDefault();
    const errEl = document.getElementById("login-error");
    errEl.textContent = "";
    const pw = document.getElementById("setup-password");
    const bt = document.getElementById("setup-bootstrap");
    try {
      await postAuth("/api/setup", {
        email: document.getElementById("setup-email").value.trim(),
        name: document.getElementById("setup-name").value.trim(),
        password: pw.value,
        bootstrap_token: bt.value,
      });
      pw.value = ""; bt.value = "";
      await boot();
    } catch (err) {
      errEl.textContent = err.message || "Setup failed";
    } finally {
      pw.value = ""; bt.value = "";
    }
  });

document.getElementById("invite-form")
  .addEventListener("submit", async (e) => {
    e.preventDefault();
    const errEl = document.getElementById("login-error");
    errEl.textContent = "";
    const tok = document.getElementById("invite-token");
    const pw = document.getElementById("invite-password");
    try {
      await postAuth("/api/invites/accept", {
        token: tok.value.trim(),
        email: document.getElementById("invite-email").value.trim(),
        name: document.getElementById("invite-name").value.trim(),
        password: pw.value,
      });
      tok.value = ""; pw.value = "";
      showLogin("signin");
      document.getElementById("login-error").textContent =
        "Invitation accepted — sign in with your new password.";
    } catch (err) {
      errEl.textContent = err.message || "Invitation failed";
      pw.value = "";
    }
  });

logoutBtn.addEventListener("click", async () => {
  try {
    const res = await fetch("/api/logout", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
  } catch (err) {
    showErr(err.message || "Sign-out failed");
    return;
  }
  localStorage.removeItem(WS_KEY);
  showLogin("signin");
});

menubtn.addEventListener("click", () => {
  setSidebarOpen(!sidebar.classList.contains("open"));
});

sidebar.addEventListener("click", (e) => {
  if (e.target.closest("a")) setSidebarOpen(false);
});

document.getElementById("gsearch").addEventListener("submit", (e) => {
  e.preventDefault();
  const q = document.getElementById("gsearch-q").value.trim();
  if (q) location.hash = "#/search?q=" + encodeURIComponent(q);
});

document.addEventListener("keydown", (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") {
    e.preventDefault();
    document.getElementById("gsearch-q").focus();
  }
  if (e.key === "Escape" && docOverlay) {
    closeDocOverlay();
    return;
  }
  if (e.key === "Escape" && sidebar.classList.contains("open")) {
    setSidebarOpen(false);
    menubtn.focus();
  }
});

window.addEventListener("hashchange", route);

function setCrumb(text) { crumb.textContent = text; }

function setNav(name) {
  for (const a of document.querySelectorAll("#nav a")) {
    a.classList.toggle("active", a.dataset.nav === name);
  }
}

function parseHash() {
  const h = location.hash.replace(/^#/, "") || "/overview";
  const [path, qs] = h.split("?");
  const parts = path.split("/").filter(Boolean);
  const params = new URLSearchParams(qs || "");
  return { parts, params };
}

function fmtDate(ts) {
  if (!ts) return "—";
  const d = new Date(ts);
  if (isNaN(d)) return ts;
  return d.toLocaleString();
}

function shortId(id) { return id ? id.slice(0, 12) : ""; }

function agentPill(agent) {
  return el("span", `pill agent-${agent}`, agent || "?");
}

function statusPill(status) {
  return el("span", `pill status-${status}`, status || "unknown");
}

function plural(n, singular, pluralWord) {
  const word = n === 1 ? singular : (pluralWord || `${singular}s`);
  return `${n == null ? 0 : n} ${word}`;
}

function agentList(agents, fallback) {
  const names = Array.isArray(agents) ? agents.filter(Boolean)
    : (fallback ? [fallback] : []);
  const box = el("span", "agent-list");
  if (!names.length) {
    box.appendChild(el("span", "pill", "—"));
    return box;
  }
  for (const a of names) box.appendChild(agentPill(a));
  return box;
}

function authorCell(author) {
  const name = author || "—";
  const initials = name === "—" ? "?" : name.split(/\s+/)
    .map((w) => w[0]).join("").slice(0, 2).toUpperCase();
  const badge = el("span", "avatar", initials);
  badge.title = name;
  const cell = el("span", "author-cell");
  cell.append(badge, document.createTextNode(name));
  return cell;
}

function sessionRole(s) {
  return (s.is_subagent || s.parent_session_id) ? "sub-agent" : "session";
}

function sessionTitleCell(s) {
  const box = el("div", "cell-main");
  const a = sessionLink(s);
  a.classList.add("cell-title");
  box.appendChild(a);
  const sub = [];
  if (s.is_subagent || s.parent_session_id)
    sub.push("sub-agent session");
  if (s.model) sub.push(s.model);
  if (s.native_id) sub.push(`native ${s.native_id}`);
  if (sub.length) box.appendChild(el("div", "cell-sub", sub.join(" · ")));
  return box;
}

function tokenLine(usage) {
  if (!usage) return "Not reported";
  const parts = [];
  if (usage.input_tokens != null) parts.push(`in ${usage.input_tokens}`);
  if (usage.output_tokens != null) parts.push(`out ${usage.output_tokens}`);
  if (usage.cached_input_tokens != null)
    parts.push(`cached ${usage.cached_input_tokens}`);
  if (usage.cache_creation_input_tokens != null)
    parts.push(`cache write ${usage.cache_creation_input_tokens}`);
  if (!parts.length) return "Not reported";
  return `${parts.join(" · ")}${usage.complete ? "" : " · partial"}`;
}

function diffDelta(c) {
  const box = el("span", "diff-delta mono");
  box.appendChild(el("span", "d-add", `+${c.additions || 0}`));
  box.appendChild(document.createTextNode(" / "));
  box.appendChild(el("span", "d-del", `−${c.deletions || 0}`));
  return box;
}

function aiCell(c) {
  if (c.ai_percentage == null) return "—";
  const box = el("div", "cell-main");
  box.appendChild(el("span", "cell-title", `${c.ai_percentage}% AI`));
  if (c.coverage_percentage != null)
    box.appendChild(el("div", "cell-sub",
      `${c.coverage_percentage}% covered`));
  return box;
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch (_) {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.className = "clipboard-ta";
    document.body.appendChild(ta);
    ta.select();
    let ok = false;
    try { ok = document.execCommand("copy"); } catch (_) { ok = false; }
    ta.remove();
    return ok;
  }
}

function copyBtn(text) {
  const b = el("button", "ghost", "Copy");
  b.addEventListener("click", async () => {
    b.textContent = (await copyText(text)) ? "Copied" : "Copy failed";
    setTimeout(() => { b.textContent = "Copy"; }, 1500);
  });
  return b;
}

function sessionLink(s) {
  const a = el("a", null, s.title || s.native_id || shortId(s.id));
  a.href = `#/sessions/${s.id}`;
  return a;
}

function checkpointLink(c) {
  const a = el("a", "mono",
    (c.commit_sha || "").slice(0, 10) || shortId(c.id));
  a.href = `#/checkpoints/${c.id}`;
  a.title = c.message || "";
  return a;
}

function table(headers, rows) {
  const scroll = el("div", "table-scroll");
  const t = el("table", "list");
  const tr = el("tr");
  for (const h of headers) tr.appendChild(el("th", null, h));
  const thead = document.createElement("thead");
  thead.appendChild(tr);
  t.appendChild(thead);
  const tb = document.createElement("tbody");
  for (const r of rows) {
    const tr2 = el("tr");
    for (const cell of r) {
      const td = document.createElement("td");
      if (cell instanceof Node) td.appendChild(cell);
      else td.textContent = cell == null ? "—" : String(cell);
      tr2.appendChild(td);
    }
    tb.appendChild(tr2);
  }
  t.appendChild(tb);
  scroll.appendChild(t);
  return scroll;
}

function pager(hasMore, offset, limit, go) {
  const p = el("div", "pager");
  const prev = el("button", null, "← Prev");
  prev.disabled = offset <= 0;
  prev.addEventListener("click", () => go(Math.max(0, offset - limit)));
  const next = el("button", null, "Next →");
  next.disabled = !hasMore;
  next.addEventListener("click", () => go(offset + limit));
  p.append(prev, next);
  return p;
}

async function repoOptions(signal, wsId) {
  const d = await api("/api/repos", { signal, ws: wsId });
  return d.items || [];
}

function pickWorkspace() {
  const list = me.workspaces || [];
  const saved = localStorage.getItem(WS_KEY);
  currentWs = list.find((w) => w.id === saved)
    || me.workspace || list[0] || null;
  if (currentWs) localStorage.setItem(WS_KEY, currentWs.id);
}

function renderWsSelect() {
  const sel = document.getElementById("ws-select");
  sel.textContent = "";
  for (const w of me.workspaces || []) {
    const o = new Option(w.name, w.id);
    sel.appendChild(o);
  }
  if (currentWs) sel.value = currentWs.id;
  sel.disabled = demoMode() || (me.workspaces || []).length < 2;
}

document.getElementById("ws-select")
  .addEventListener("change", (e) => {
    const w = (me.workspaces || []).find((x) => x.id === e.target.value);
    if (!w || w === currentWs) return;
    currentWs = w;
    localStorage.setItem(WS_KEY, w.id);
    routeVersion++;
    if (routeCtl) routeCtl.abort();
    view.textContent = "";
    document.getElementById("user-label").textContent =
      `${me.user.name} · ${w.role}`;
    location.hash = "#/overview";
    route();
  });

async function vOverview(root, signal) {
  const wsId = currentWs && currentWs.id;
  setCrumb("Overview");
  setNav("overview");
  const [ov, sessions, integ, repos, cps] = await Promise.all([
    api("/api/overview", { signal, ws: wsId }),
    api("/api/sessions?limit=8", { signal, ws: wsId }),
    api("/api/integrations", { signal, ws: wsId }),
    api("/api/repos", { signal, ws: wsId }),
    api("/api/checkpoints?limit=8", { signal, ws: wsId }),
  ]);

  const hero = el("div", "panel hero");
  hero.appendChild(el("h1", null, "Every change has a story."));
  hero.appendChild(el("p", null,
    demoMode() ?
      "Browsing the demo workspace — sample data only." :
      "Sessions captured from your agents, linked to your commits."));
  const connect = el("a", "button primary", "Connect an agent");
  connect.href = "#/integrations";
  hero.appendChild(connect);
  root.appendChild(hero);

  const cards = el("div", "cards");
  for (const [n, l] of [
    [ov.repositories, "Repositories"],
    [ov.sessions, "Captured sessions"],
    [ov.checkpoints, "Checkpoints"],
  ]) {
    const m = el("div", "metric");
    m.appendChild(el("div", "n", String(n)));
    m.appendChild(el("div", "l", l));
    cards.appendChild(m);
  }
  root.appendChild(cards);

  const sp = el("div", "panel");
  sp.appendChild(el("h2", null, "Recent sessions"));
  if (sessions.items.length) {
    sp.appendChild(table(
      ["Session", "Agent", "Role", "Status", "Updated"],
      sessions.items.map((s) => [
        sessionTitleCell(s), agentPill(s.agent), sessionRole(s),
        statusPill(s.status), fmtDate(s.updated_at),
      ])));
  } else {
    sp.appendChild(emptySetup());
  }
  root.appendChild(sp);

  const cp = el("div", "panel");
  cp.appendChild(el("h2", null, "Recent checkpoints"));
  if (cps.items.length) {
    cp.appendChild(table(
      ["Commit", "Message", "Agents", "AI", "Branch", "Captured"],
      cps.items.map((c) => [
        checkpointLink(c), c.message || "—", agentList(c.agents),
        aiCell(c), c.branch || "—", fmtDate(c.created_at),
      ])));
  } else {
    cp.appendChild(el("div", "empty",
      repos.items.length ?
        "No checkpoints yet — they are created when linked sessions overlap committed files." :
        "No repositories registered yet."));
  }
  root.appendChild(cp);

  const ip = el("div", "panel");
  ip.appendChild(el("h2", null, "Agents"));
  ip.appendChild(table(
    ["Agent", "Capture"],
    integ.items.map((i) => [i.name, i.capture])));
  root.appendChild(ip);
}

function emptySetup() {
  const e = el("div", "empty");
  e.appendChild(el("div", null,
    "No sessions captured yet. Enable hooks and run your agent:"));
  const c = el("div", "cmdline");
  c.classList.add("mt12");
  c.appendChild(el("code", null, "partial enable --agent all"));
  c.appendChild(copyBtn("partial enable --agent all"));
  e.appendChild(c);
  return e;
}

async function vRepos(root, signal) {
  const wsId = currentWs && currentWs.id;
  setCrumb("Repositories");
  setNav("repos");
  const d = await api("/api/repos", { signal, ws: wsId });
  const p = el("div", "panel");
  p.appendChild(el("h2", null, "Repositories"));
  if (!d.items.length) {
    p.appendChild(emptySetup());
  } else {
    p.appendChild(table(
      ["Repository", "Agents", "Sessions", "Checkpoints", "Branches",
       "Last activity"],
      d.items.map((r) => {
        const main = el("div", "cell-main");
        const a = el("a", "cell-title", r.name);
        a.href = `#/repos/${r.id}`;
        main.appendChild(a);
        const latest = r.latest_checkpoint && r.latest_checkpoint.message
          ? `latest ${r.latest_checkpoint.message}`
          : (r.latest_session && r.latest_session.title
            ? `latest ${r.latest_session.title}`
            : (r.remote || "local only"));
        main.appendChild(el("div", "cell-sub", latest));
        return [main, agentList(r.agents),
                String(r.session_count ?? 0),
                String(r.checkpoint_count ?? 0),
                String(r.branch_count ?? 0),
                fmtDate(r.last_activity || r.created_at)];
      })));
  }
  root.appendChild(p);

  const pj = await api("/api/projects", { signal, ws: wsId });
  const pp = el("div", "panel");
  pp.appendChild(el("h2", null, "Project groups"));
  pp.appendChild(el("p", "hint small",
    "Project groups organize repositories inside this workspace;" +
    " they do not change access control — the workspace is the" +
    " trust boundary."));
  if (!pj.items.length)
    pp.appendChild(el("div", "empty", "No project groups yet."));
  else
    for (const proj of pj.items) {
      const card = el("div", "cap");
      card.appendChild(el("strong", null, `${proj.name} — `));
      const rids = proj.repos || [];
      if (!rids.length)
        card.appendChild(document.createTextNode("no repositories"));
      rids.forEach((rid, i) => {
        if (i) card.appendChild(document.createTextNode(", "));
        const r = d.items.find((x) => x.id === rid);
        if (r) {
          const a = el("a", null, r.name);
          a.href = `#/repos/${r.id}`;
          card.appendChild(a);
        } else {
          card.appendChild(el("span", "mono", rid.slice(0, 10)));
        }
      });
      pp.appendChild(card);
    }
  if (canWrite()) {
    const pjStatus = el("div");
    pjStatus.setAttribute("aria-live", "polite");
    const cForm = el("div", "filters");
    const nameIn = el("input");
    nameIn.placeholder = "New project name";
    nameIn.setAttribute("aria-label", "New project name");
    const cBtn = el("button", null, "Create project");
    cBtn.addEventListener("click", async () => {
      pjStatus.textContent = "";
      cBtn.disabled = true;
      try {
        await api("/api/projects", {
          method: "POST", ws: wsId, signal,
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name: nameIn.value }) });
        route();
      } catch (err) {
        if (isAbort(err)) return;
        pjStatus.className = "notice-err";
        pjStatus.textContent = err.message;
        cBtn.disabled = false;
      }
    });
    cForm.append(nameIn, cBtn);
    pp.appendChild(cForm);
    if (pj.items.length && d.items.length) {
      const aForm = el("div", "filters");
      const pSel = el("select");
      pSel.setAttribute("aria-label", "Project");
      for (const proj of pj.items)
        pSel.appendChild(new Option(proj.name, proj.id));
      const rSel = el("select");
      rSel.setAttribute("aria-label", "Repository to attach");
      for (const r of d.items)
        rSel.appendChild(new Option(r.name, r.id));
      const aBtn = el("button", null, "Attach repository");
      aBtn.addEventListener("click", async () => {
        pjStatus.textContent = "";
        aBtn.disabled = true;
        try {
          await api(`/api/projects/${pSel.value}/attach`, {
            method: "POST", ws: wsId, signal,
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ repo_id: rSel.value }) });
          route();
        } catch (err) {
          if (isAbort(err)) return;
          pjStatus.className = "notice-err";
          pjStatus.textContent = err.message;
          aBtn.disabled = false;
        }
      });
      aForm.append(pSel, rSel, aBtn);
      pp.appendChild(aForm);
    }
    pp.appendChild(pjStatus);
  } else {
    pp.appendChild(el("p", "hint small",
      demoMode()
        ? "Demo workspace is read-only."
        : "Managing project groups requires the member role."));
  }
  root.appendChild(pp);
}

async function vRepoDetail(root, id, params, signal) {
  const wsId = currentWs && currentWs.id;
  setCrumb("Repository");
  setNav("repos");
  const d = await api(`/api/repos/${id}`, { signal, ws: wsId });

  const head = el("div", "panel repo-head");
  const title = el("div");
  title.appendChild(el("h2", null, d.repository.name));
  const meta = el("div", null);
  meta.appendChild(el("span", "mono",
    d.repository.remote || "local only"));
  title.appendChild(meta);
  head.appendChild(title);
  const stats = d.stats || {};
  const cards = el("div", "cards repo-stats");
  for (const [n, l] of [
    [stats.session_count ?? 0, "Sessions"],
    [stats.checkpoint_count ?? 0, "Checkpoints"],
    [stats.branch_count ?? d.branches.length, "Branches"],
    [(stats.agents || []).length, "Agents"],
  ]) {
    const m = el("div", "metric");
    m.appendChild(el("div", "n", String(n)));
    m.appendChild(el("div", "l", l));
    cards.appendChild(m);
  }
  head.appendChild(cards);
  const latest = el("div", "repo-latest");
  if (stats.latest_session) {
    const a = sessionLink(stats.latest_session);
    a.className = "mono blocklink";
    latest.appendChild(el("span", "hint", "Latest session "));
    latest.appendChild(a);
  }
  if (stats.latest_checkpoint) {
    const a = checkpointLink(stats.latest_checkpoint);
    a.classList.add("blocklink");
    latest.appendChild(el("span", "hint", "Latest checkpoint "));
    latest.appendChild(a);
    const detail = [];
    if (stats.latest_checkpoint.message)
      detail.push(stats.latest_checkpoint.message);
    if (stats.latest_checkpoint.branch)
      detail.push(`on ${stats.latest_checkpoint.branch}`);
    if (stats.latest_checkpoint.author)
      detail.push(`by ${stats.latest_checkpoint.author}`);
    if (detail.length)
      latest.appendChild(el("span", "hint", detail.join(" · ")));
  }
  if (stats.indexed) {
    latest.appendChild(el("span", "hint",
      `Memory indexed ${fmtDate(stats.indexed.indexed_at)} ·` +
      ` commit ${String(stats.indexed.commit_sha || "").slice(0, 10)}`));
  } else {
    latest.appendChild(el("span", "hint",
      "Repository memory has not been indexed yet."));
  }
  head.appendChild(latest);
  root.appendChild(head);

  const branch = params.get("branch") || "";
  const q = params.get("q") || "";
  const tab = params.get("tab") === "checkpoints"
    ? "checkpoints" : "sessions";
  const offset = parseInt(params.get("offset") || "0", 10) || 0;
  const limit = 50;

  const nav = (over) => {
    const p = new URLSearchParams();
    const nextBranch = over.branch !== undefined ? over.branch : branch;
    const nextQ = over.q !== undefined ? over.q : q;
    if (nextBranch) p.set("branch", nextBranch);
    if (nextQ) p.set("q", nextQ);
    p.set("tab", over.tab || tab);
    if (over.offset) p.set("offset", String(over.offset));
    location.hash = `#/repos/${id}?${p}`;
  };

  const filter = el("div", "filters");
  const sel = el("select");
  sel.setAttribute("aria-label", "Branch filter");
  sel.appendChild(new Option("All branches", ""));
  for (const b of d.branches) sel.appendChild(new Option(b, b));
  sel.value = branch;
  sel.addEventListener("change", () => nav({ branch: sel.value }));
  const qIn = el("input");
  qIn.value = q;
  qIn.placeholder = tab === "checkpoints"
    ? "Search checkpoints…" : "Search sessions…";
  qIn.setAttribute("aria-label",
    tab === "checkpoints" ? "Search checkpoints" : "Search sessions");
  qIn.addEventListener("keydown", (e) => {
    if (e.key === "Enter") nav({ q: qIn.value.trim(), offset: 0 });
  });
  const qBtn = el("button", null, "Filter");
  qBtn.addEventListener("click", () =>
    nav({ q: qIn.value.trim(), offset: 0 }));
  filter.append(sel, qIn, qBtn);
  root.appendChild(filter);

  const tabs = el("div", "tabs");
  const bS = el("button", tab === "sessions" ? "active" : null,
    "Sessions");
  const bC = el("button", tab === "checkpoints" ? "active" : null,
    "Checkpoints");
  bS.addEventListener("click", () => nav({ tab: "sessions" }));
  bC.addEventListener("click", () => nav({ tab: "checkpoints" }));
  tabs.append(bS, bC);
  root.appendChild(tabs);

  const panel = el("div", "panel");
  const qp = new URLSearchParams({ repo: id, limit: String(limit),
                                   offset: String(offset) });
  if (branch) qp.set("branch", branch);
  if (q) qp.set("q", q);
  if (tab === "checkpoints") {
    const cps = await api(`/api/checkpoints?${qp}`, { signal, ws: wsId });
    if (cps.items.length) {
      panel.appendChild(table(
        ["Commit", "Message", "Agents", "AI", "Changes", "Files",
         "Author", "Captured"],
        cps.items.map((c) => [
          checkpointLink(c), c.message || "—",
          agentList(c.agents), aiCell(c), diffDelta(c),
          String(c.file_count ?? (c.files || []).length),
          authorCell(c.author), fmtDate(c.created_at)])));
    } else {
      panel.appendChild(el("div", "empty",
        "No checkpoints for this repository."));
    }
    if (cps.has_more || offset > 0) {
      panel.appendChild(pager(cps.has_more, offset, limit,
        (o) => nav({ offset: o })));
    }
  } else {
    const ss = await api(`/api/sessions?${qp}`, { signal, ws: wsId });
    if (ss.items.length) {
      panel.appendChild(table(
        ["Session", "Agent", "Role", "Status", "Checkpoints", "Steps",
         "Updated"],
        ss.items.map((s) => [
          sessionTitleCell(s), agentPill(s.agent), sessionRole(s),
          statusPill(s.status), String(s.checkpoint_count ?? 0),
          String(s.event_count ?? 0), fmtDate(s.updated_at)])));
    } else {
      panel.appendChild(el("div", "empty",
        "No sessions for this repository."));
    }
    if (ss.has_more || offset > 0) {
      panel.appendChild(pager(ss.has_more, offset, limit,
        (o) => nav({ offset: o })));
    }
  }
  root.appendChild(panel);
}

async function vSessions(root, params, signal) {
  const wsId = currentWs && currentWs.id;
  setCrumb("Sessions");
  setNav("sessions");
  const repos = await repoOptions(signal, wsId);

  const agent = params.get("agent") || "";
  const repo = params.get("repo") || "";
  const q = params.get("q") || "";
  const branch = params.get("branch") || "";
  const offset = parseInt(params.get("offset") || "0", 10) || 0;
  const limit = 50;

  const filters = el("div", "filters");
  const aSel = el("select");
  aSel.setAttribute("aria-label", "Agent filter");
  aSel.appendChild(new Option("All agents", ""));
  for (const a of ["devin", "codex", "claude", "chatgpt"])
    aSel.appendChild(new Option(a, a));
  aSel.value = agent;
  const rSel = el("select");
  rSel.setAttribute("aria-label", "Repository filter");
  rSel.appendChild(new Option("All repos", ""));
  for (const r of repos) rSel.appendChild(new Option(r.name, r.id));
  rSel.value = repo;
  const bIn = el("input");
  bIn.placeholder = "Branch";
  bIn.value = branch;
  bIn.setAttribute("aria-label", "Branch filter");
  const qIn = el("input");
  qIn.placeholder = "Search sessions…";
  qIn.value = q;
  qIn.setAttribute("aria-label", "Search sessions");
  const apply = () => {
    const p = new URLSearchParams();
    if (aSel.value) p.set("agent", aSel.value);
    if (rSel.value) p.set("repo", rSel.value);
    if (bIn.value.trim()) p.set("branch", bIn.value.trim());
    if (qIn.value.trim()) p.set("q", qIn.value.trim());
    location.hash = `#/sessions?${p}`;
  };
  aSel.addEventListener("change", apply);
  rSel.addEventListener("change", apply);
  bIn.addEventListener("keydown", (e) => {
    if (e.key === "Enter") apply(); });
  qIn.addEventListener("keydown", (e) => {
    if (e.key === "Enter") apply(); });
  const go = el("button", null, "Filter");
  go.addEventListener("click", apply);
  filters.append(aSel, rSel, bIn, qIn, go);
  root.appendChild(filters);

  const qp = new URLSearchParams();
  if (agent) qp.set("agent", agent);
  if (repo) qp.set("repo", repo);
  if (branch) qp.set("branch", branch);
  if (q) qp.set("q", q);
  qp.set("limit", String(limit));
  qp.set("offset", String(offset));
  const d = await api(`/api/sessions?${qp}`, { signal, ws: wsId });

  const p = el("div", "panel");
  if (!d.items.length) {
    p.appendChild(el("div", "empty", "No matching sessions."));
  } else {
    p.appendChild(table(
      ["Session", "Agent", "Model", "Role", "Status", "Branch",
       "Checkpoints", "Steps", "Updated"],
      d.items.map((s) => [
        sessionTitleCell(s), agentPill(s.agent),
        s.model || "Not reported", sessionRole(s),
        statusPill(s.status), s.branch || "—",
        String(s.checkpoint_count ?? 0), String(s.event_count ?? 0),
        fmtDate(s.updated_at),
      ])));
  }
  if (d.has_more || offset > 0) {
    const goOff = (o) => {
      const np = new URLSearchParams(qp);
      np.set("offset", String(o));
      location.hash = `#/sessions?${np}`;
    };
    p.appendChild(pager(d.has_more, offset, limit, goOff));
  }
  root.appendChild(p);
}

const KIND_LABELS = {
  prompt: "Prompt", response: "Response", tool: "Tool",
  session_start: "Session start", session_end: "Session end",
  compaction: "Compaction", usage: "Usage", error: "Error",
};

async function vSessionDetail(root, id, params, signal) {
  const wsId = currentWs && currentWs.id;
  setCrumb("Session");
  setNav("sessions");
  const kind = params.get("kind") || "";
  const offset = parseInt(params.get("offset") || "0", 10) || 0;
  const limit = 200;
  const qp = new URLSearchParams({ limit: String(limit),
                                   offset: String(offset) });
  if (kind) qp.set("kind", kind);
  const d = await api(`/api/sessions/${id}?${qp}`, { signal, ws: wsId });
  const s = d.session;

  const head = el("div", "panel");
  head.appendChild(el("h2", null, s.title || s.native_id || "Session"));
  const meta = el("div", "filters");
  meta.appendChild(agentPill(s.agent));
  meta.appendChild(statusPill(s.status));
  meta.appendChild(el("span", "pill", sessionRole(s)));
  if (s.branch) meta.appendChild(el("span", "pill", `⎇ ${s.branch}`));
  if (d.children.length)
    meta.appendChild(el("span", "pill",
      plural(d.children.length, "direct sub-agent")));
  if (s.parent_session_id) {
    const pa = el("a", "pill", "parent session");
    pa.href = `#/sessions/${s.parent_session_id}`;
    meta.appendChild(pa);
  }
  head.appendChild(meta);
  const ids = el("div", null);
  ids.appendChild(el("span", "mono",
    `id ${s.id}  ·  native ${s.native_id}  `));
  ids.appendChild(copyBtn(s.id));
  head.appendChild(ids);
  const times = el("div", null);
  times.className = "hint mt8";
  times.textContent =
    `started ${fmtDate(s.started_at)} · updated ${fmtDate(s.updated_at)}` +
    ` · model ${s.model || "Not reported"}`;
  head.appendChild(times);
  const acts = el("div", "filters");
  acts.classList.add("mt8");
  const copyH = el("button", null, "Copy handoff");
  copyH.addEventListener("click", async () => {
    try {
      const res = await fetch(`/api/sessions/${id}/handoff`, {
        credentials: "same-origin", signal,
        headers: wsHeaders(null, wsId) });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const text = await res.text();
      copyH.textContent =
        (await copyText(text)) ? "Copied" : "Copy failed";
    } catch (err) {
      if (!isAbort(err)) copyH.textContent = "Copy failed";
    }
    setTimeout(() => { copyH.textContent = "Copy handoff"; }, 1500);
  });
  const dlH = el("button", "button", "Download handoff");
  dlH.addEventListener("click", () =>
    fetchBlob(`/api/sessions/${id}/handoff`, "partial-handoff.md",
      wsId, signal));
  acts.append(copyH, dlH);
  head.appendChild(acts);
  root.appendChild(head);

  const cols = el("div", "grid2");
  const left = el("div");
  const right = el("div");

  const filters = el("div", "filters");
  const kinds = ["", "prompt", "response", "tool", "usage"];
  const labels = ["All", "Prompts", "Responses", "Tools", "Usage"];
  kinds.forEach((k, i) => {
    const b = el("button", k === kind ? "primary" : null, labels[i]);
    b.addEventListener("click", () => {
      const p = new URLSearchParams();
      if (k) p.set("kind", k);
      const tail = p.toString();
      location.hash = `#/sessions/${id}${tail ? "?" + tail : ""}`;
    });
    filters.appendChild(b);
  });
  left.appendChild(filters);

  const tl = el("div", "timeline");
  for (const ev of d.events) {
    const item = el("div", `tl-item kind-${ev.kind}`);
    const k = el("div", "k");
    k.appendChild(el("span", null, KIND_LABELS[ev.kind] || ev.kind));
    if (ev.tool_name) k.appendChild(el("span", "mono", ev.tool_name));
    k.appendChild(el("span", "ts", fmtDate(ev.timestamp)));
    item.appendChild(k);
    if (ev.text) item.appendChild(el("div", "body", ev.text));
    const data = ev.data || {};
    if (Object.keys(data).length) {
      const det = el("details", "toolbox");
      det.appendChild(el("summary", null, "Details"));
      det.appendChild(el("pre", null, JSON.stringify(data, null, 2)));
      item.appendChild(det);
    }
    if (ev.kind === "usage" && data.usage) {
      const u = el("div", "body mono");
      u.textContent = JSON.stringify(data.usage);
      item.appendChild(u);
    }
    tl.appendChild(item);
  }
  if (!d.events.length) {
    tl.appendChild(el("div", "empty", "No events of this kind."));
  }
  left.appendChild(tl);

  if (d.has_more || offset > 0) {
    const go = (o) => {
      const p = new URLSearchParams();
      if (kind) p.set("kind", kind);
      p.set("offset", String(o));
      location.hash = `#/sessions/${id}?${p}`;
    };
    left.appendChild(pager(d.has_more, offset, limit, go));
  }

  const side = el("div", "panel");
  side.appendChild(el("h3", null, "Contribution"));
  side.appendChild(el("div", "cap",
    `${sessionRole(s)} · ${s.event_count ?? d.events.length} recorded` +
    ` events · ${s.checkpoint_count ?? d.checkpoints.length}` +
    ` linked checkpoints` +
    (d.children.length ? ` · ${d.children.length} direct sub-agents` : "")));
  side.appendChild(el("h3", null, "Checkpoints"));
  if (d.checkpoints.length) {
    for (const cid of d.checkpoints) {
      const a = el("a", "mono blocklink", cid.slice(0, 12));
      a.href = `#/checkpoints/${cid}`;
      side.appendChild(a);
    }
  } else {
    side.appendChild(el("div", null, "No linked checkpoints."));
  }
  if (d.children.length) {
    side.appendChild(el("h3", null, "Child sessions"));
    for (const c of d.children) {
      const a = sessionLink(c);
      a.className = "mono blocklink";
      side.appendChild(a);
    }
  }
  let nat = null;
  try {
    nat = await api(`/api/sessions/${id}/native`,
      { signal, ws: wsId });
  } catch (e) {
    if (!(e instanceof HttpError && e.status === 404)) throw e;
  }
  if (nat && nat.native) {
    side.appendChild(el("h3", null, "Native resume"));
    const nr = el("div", "hint",
      `${nat.native.agent} · ${nat.native.format}` +
      ` · ${nat.native.native_id}`);
    side.appendChild(nr);
    const cmd = `partial resume ${s.id}`;
    const row = el("div", "filters");
    row.appendChild(el("span", "mono", cmd));
    row.appendChild(copyBtn(cmd));
    side.appendChild(row);
    side.appendChild(el("div", "hint",
      (nat.native.has_local_path ? "Native state registered"
        : "requires local native state") +
      (nat.native.has_archive ? " · archive stored" : "")));
  }
  let usage = null;
  try {
    usage = await api(`/api/sessions/${id}/usage`,
      { signal, ws: wsId });
  } catch (e) {
    if (!(e instanceof HttpError && e.status === 404)) throw e;
  }
  if (usage && usage.usage) {
    const u = usage.usage;
    side.appendChild(el("h3", null, "Token usage"));
    const rows = [
      ["input", u.input_tokens],
      ["output", u.output_tokens],
      ["cached input", u.cached_input_tokens],
      ["cache creation", u.cache_creation_input_tokens],
    ];
    const t = el("div", "cap");
    t.textContent = rows
      .filter(([, v]) => v != null)
      .map(([k, v]) => `${k} ${v}`).join(" · ") || "no reported usage";
    side.appendChild(t);
    side.appendChild(el("div", "cap",
      `basis ${u.basis} · events ${u.events_used}` +
      (u.unclassified_events
        ? ` · ${u.unclassified_events} unclassified` : "") +
      (u.complete ? " · complete" : " · incomplete")));
  }
  const note = el("p", null,
    "Handoff exports the recorded context (prompts, responses, tool" +
    " activity) as Markdown. It does not restore native agent state.");
  note.className = "hint";
  side.appendChild(note);
  right.appendChild(side);
  cols.append(left, right);
  root.appendChild(cols);
}

async function vCheckpoints(root, params, signal) {
  const wsId = currentWs && currentWs.id;
  setCrumb("Checkpoints");
  setNav("checkpoints");
  const repos = await repoOptions(signal, wsId);
  const repo = params.get("repo") || "";
  const branch = params.get("branch") || "";
  const q = params.get("q") || "";
  const offset = parseInt(params.get("offset") || "0", 10) || 0;
  const limit = 50;

  const filters = el("div", "filters");
  const rSel = el("select");
  rSel.setAttribute("aria-label", "Repository filter");
  rSel.appendChild(new Option("All repos", ""));
  for (const r of repos) rSel.appendChild(new Option(r.name, r.id));
  rSel.value = repo;
  const bIn = el("input");
  bIn.placeholder = "Branch";
  bIn.value = branch;
  bIn.setAttribute("aria-label", "Branch filter");
  const qIn = el("input");
  qIn.placeholder = "Search commit, message, or branch…";
  qIn.value = q;
  qIn.setAttribute("aria-label", "Checkpoint search");
  const apply = () => {
    const p = new URLSearchParams();
    if (rSel.value) p.set("repo", rSel.value);
    if (bIn.value.trim()) p.set("branch", bIn.value.trim());
    if (qIn.value.trim()) p.set("q", qIn.value.trim());
    location.hash = `#/checkpoints?${p}`;
  };
  rSel.addEventListener("change", apply);
  bIn.addEventListener("keydown", (e) => {
    if (e.key === "Enter") apply(); });
  qIn.addEventListener("keydown", (e) => {
    if (e.key === "Enter") apply(); });
  const go = el("button", null, "Filter");
  go.addEventListener("click", apply);
  filters.append(rSel, bIn, qIn, go);
  root.appendChild(filters);

  const qp = new URLSearchParams({ limit: String(limit),
                                   offset: String(offset) });
  if (repo) qp.set("repo", repo);
  if (branch) qp.set("branch", branch);
  if (q) qp.set("q", q);
  const d = await api(`/api/checkpoints?${qp}`, { signal, ws: wsId });
  const p = el("div", "panel");
  p.appendChild(el("h2", null, "Checkpoints"));
  if (!d.items.length) {
    p.appendChild(el("div", "empty", "No checkpoints captured yet."));
  } else {
    p.appendChild(table(
      ["Commit", "Message", "Branch", "Agents", "AI", "Changes",
       "Files", "Sessions", "Author", "Captured"],
      d.items.map((c) => [
        checkpointLink(c), c.message || "—", c.branch || "—",
        agentList(c.agents), aiCell(c), diffDelta(c),
        String(c.file_count ?? (c.files || []).length),
        String(c.session_count ?? (c.session_ids || []).length),
        authorCell(c.author), fmtDate(c.created_at),
      ])));
  }
  if (d.has_more || offset > 0) {
    const goOff = (o) => {
      const np = new URLSearchParams(qp);
      np.set("offset", String(o));
      location.hash = `#/checkpoints?${np}`;
    };
    p.appendChild(pager(d.has_more, offset, limit, goOff));
  }
  root.appendChild(p);
}

function _attrLines(att) {
  const m = {};
  for (const f of (att && att.files) || []) {
    const e = { "new": {}, "old": {} };
    for (const l of f.lines || []) {
      if (l && (l.side === "new" || l.side === "old")
          && typeof l.line === "number") {
        e[l.side][l.line] = l;
      }
    }
    m[f.path] = e;
  }
  return m;
}

function _attrBadge(ent, sessionsById) {
  const kind = ent && ent.kind ? ent.kind : "unknown";
  const cls = kind === "agent" ? "attr-ai"
    : kind === "human" ? "attr-hu" : "attr-unk";
  const label = kind === "agent" ? "AI" : kind === "human" ? "HU" : "?";
  const b = el("span", `attr-badge ${cls}`, label);
  const sess = ent && ent.session_id
    ? (sessionsById || {})[ent.session_id] : null;
  const parts = [kind === "human" ? "human-side (inferred)" : kind];
  if (sess && sess.agent) parts.push(sess.agent);
  if (sess && sess.model) parts.push(sess.model);
  if (ent && ent.evidence) parts.push(ent.evidence);
  if (ent && ent.session_id) parts.push(`session ${ent.session_id}`);
  b.title = `AI attribution estimate: ${parts.join(" · ")}`;
  return b;
}

function renderAttribution(att, sessions) {
  const sessionsById = {};
  for (const s of sessions || []) sessionsById[s.id] = s;
  const card = el("div", "panel attr-card");
  card.appendChild(el("h3", null, "AI attribution estimate"));
  if (!att || !att.summary) {
    card.appendChild(el("div", "hint",
      "No attribution captured for this checkpoint."));
    return card;
  }
  const s = att.summary;
  const counts = el("div", "attr-counts");
  counts.textContent =
    `agent +${s.agent_added}/−${s.agent_removed}` +
    ` · human-side (inferred) +${s.human_added}/−${s.human_removed}` +
    ` · unknown +${s.unknown_added}/−${s.unknown_removed}` +
    ` · changed ${s.total_changed}`;
  card.appendChild(counts);
  const cov = el("div", "hint");
  if (s.total_changed > 0 && s.coverage_percentage != null) {
    cov.textContent =
      `agent share ${s.agent_percentage}% ·` +
      ` coverage ${s.coverage_percentage}% of changed lines`;
  } else {
    cov.textContent = "Not available — no changed lines measured.";
  }
  card.appendChild(cov);
  const src = att.capture_source === "imported-claim"
    ? "imported claim (unverified)" : "local observation";
  card.appendChild(el("div", "hint", `source: ${src}`));
  if ((att.excluded || []).length) {
    card.appendChild(el("div", "hint",
      `${att.excluded.length} file(s) excluded from measurement` +
      ` (${att.excluded.map((x) => x.reason).join(", ")});` +
      " percentages cover measured files only."));
  }
  for (const lim of att.limitations || []) {
    card.appendChild(el("div", "hint", String(lim)));
  }
  const rows = [];
  for (const f of att.files || []) {
    for (const l of f.lines || []) {
      const sess = l.session_id ? sessionsById[l.session_id] : null;
      rows.push([
        `${f.path}:${l.side === "new" ? "+" : "-"}${l.line}`,
        _attrBadge(l, sessionsById),
        sess ? sess.agent : "—",
        sess ? (sess.title || sess.native_id || l.session_id.slice(0, 12))
          : (l.session_id ? l.session_id.slice(0, 12) : "—"),
        l.evidence || "—",
      ]);
    }
  }
  if (rows.length) {
    const det = el("details");
    det.appendChild(el("summary", "hint",
      `All attributed lines (${rows.length})`));
    det.appendChild(table(
      ["Line", "Estimate", "Agent", "Session", "Evidence"],
      rows.slice(0, 2000)));
    if (rows.length > 2000) {
      det.appendChild(el("div", "hint",
        `Showing 2000 of ${rows.length} attributed lines` +
        " (truncated)."));
    }
    card.appendChild(det);
  }
  return card;
}

function renderDiff(diff, files, att, sessions) {
  const sessionsById = {};
  for (const s of sessions || []) sessionsById[s.id] = s;
  const amap = _attrLines(att);
  const wrap = el("div");
  const sections = [];
  let cur = null;
  for (const line of (diff || "").split("\n")) {
    if (line.startsWith("diff --git")) {
      if (cur) sections.push(cur);
      cur = { head: line, lines: [] };
    } else if (cur) {
      cur.lines.push(line);
    } else {
      cur = { head: "", lines: [line] };
    }
  }
  if (cur) sections.push(cur);
  const fileFor = (sec) => {
    const m = / b\/(.+)$/.exec(sec.head);
    return m ? m[1] : null;
  };
  for (const sec of sections) {
    if (!sec.head && !sec.lines.join("").trim()) continue;
    const fname = fileFor(sec);
    const det = el("details", "diff-file");
    det.open = true;
    det.appendChild(el("summary", "fname", fname || "patch"));
    const pre = el("pre", "diff");
    const fmap = fname ? amap[fname] : null;
    let oldNo = 0;
    let newNo = 0;
    for (const line of sec.lines) {
      const hm = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/.exec(line);
      if (hm) {
        oldNo = parseInt(hm[1], 10);
        newNo = parseInt(hm[2], 10);
      }
      const span = el("span");
      let badge = null;
      if (line.startsWith("+") && !line.startsWith("+++")) {
        span.className = "d-add";
        if (fmap) badge = _attrBadge(fmap.new[newNo], sessionsById);
        newNo += 1;
      } else if (line.startsWith("-") && !line.startsWith("---")) {
        span.className = "d-del";
        if (fmap) badge = _attrBadge(fmap.old[oldNo], sessionsById);
        oldNo += 1;
      } else if (line.startsWith("@@")) {
        span.className = "d-hunk";
      } else if (line.startsWith("\\")) {
        span.className = "d-head";
      } else if (line.startsWith("diff ") || line.startsWith("index ") ||
                 line.startsWith("---") || line.startsWith("+++")) {
        span.className = "d-head";
      } else if (line.startsWith(" ")) {
        oldNo += 1;
        newNo += 1;
      }
      span.textContent = line + "\n";
      if (badge) pre.appendChild(badge);
      pre.appendChild(span);
    }
    det.appendChild(pre);
    wrap.appendChild(det);
  }
  const covered = new Set(
    sections.map(fileFor).filter(Boolean));
  for (const f of files || []) {
    if (!covered.has(f)) {
      const box = el("div", "diff-file omitted");
      const head = el("div", "fname", f);
      head.appendChild(el("span", "omit-tag",
        "diff omitted — sensitive path"));
      box.appendChild(head);
      wrap.appendChild(box);
    }
  }
  if (!wrap.children.length) {
    wrap.appendChild(el("div", "empty", "No diff captured."));
  }
  return wrap;
}

async function vCheckpointDetail(root, id, params, signal) {
  const wsId = currentWs && currentWs.id;
  const wsCanWrite = !demoMode() && currentWs
    && ROLE_RANK[currentWs.role] >= ROLE_RANK.member;
  setCrumb("Checkpoint");
  setNav("checkpoints");
  const d = await api(`/api/checkpoints/${id}`, { signal, ws: wsId });
  const c = d.checkpoint;

  const head = el("div", "panel");
  head.appendChild(el("h2", null, c.message || "Checkpoint"));
  const meta = el("div", "filters");
  meta.appendChild(el("span", "mono", `commit ${c.commit_sha}`));
  meta.appendChild(copyBtn(c.commit_sha));
  head.appendChild(meta);
  const sub = el("div", null);
  sub.className = "hint";
  sub.textContent =
    `branch ${c.branch || "—"} · author ${c.author || "—"} ·` +
    ` captured ${fmtDate(c.created_at)}`;
  head.appendChild(sub);
  const facts = el("div", "filters checkpoint-facts");
  facts.appendChild(el("span", "pill",
    plural(c.file_count ?? (c.files || []).length, "file")));
  facts.appendChild(el("span", "pill",
    plural(c.session_count ?? (c.session_ids || []).length,
           "linked session")));
  facts.appendChild(diffDelta(c));
  const aiPct = c.ai_percentage != null ? c.ai_percentage
    : (d.attribution && d.attribution.summary
      ? d.attribution.summary.agent_percentage : null);
  facts.appendChild(el("span", "pill",
    aiPct == null ? "AI share not measured" : `${aiPct}% AI`));
  if ((c.agents || []).length || d.sessions.length) {
    facts.appendChild(agentList(
      c.agents && c.agents.length
        ? c.agents : d.sessions.map((s) => s.agent)));
  }
  if (c.token_usage)
    facts.appendChild(el("span", "pill", tokenLine(c.token_usage)));
  head.appendChild(facts);
  root.appendChild(head);
  root.appendChild(renderAttribution(d.attribution, d.sessions));

  const tab = params.get("tab") || "changes";
  const tabs = el("div", "tabs");
  const bCh = el("button", tab === "changes" ? "active" : null,
                 `Changes (${(c.files || []).length})`);
  const bSe = el("button", tab === "sessions" ? "active" : null,
                 `Sessions (${d.sessions.length})`);
  bCh.addEventListener("click", () => {
    location.hash = `#/checkpoints/${id}?tab=changes`; });
  bSe.addEventListener("click", () => {
    location.hash = `#/checkpoints/${id}?tab=sessions`; });
  tabs.append(bCh, bSe);
  root.appendChild(tabs);

  if (tab === "sessions") {
    const p = el("div", "panel");
    if (d.sessions.length) {
      p.appendChild(table(
        ["Session", "Agent", "Role", "Model", "Status", "Link"],
        d.sessions.map((s) => {
          const link = (c.links || []).find(
            (l) => l.session_id === s.id);
          return [sessionTitleCell(s), agentPill(s.agent),
                  sessionRole(s), s.model || "Not reported",
                  statusPill(s.status), link ? link.method : "—"];
        })));
    } else {
      p.appendChild(el("div", "empty",
        "No sessions linked. Sessions are linked when their observed" +
        " file edits overlap this commit's paths."));
    }
    root.appendChild(p);
  } else {
    const p = el("div", "panel");
    p.appendChild(renderDiff(c.diff, c.files, d.attribution,
      d.sessions));
    root.appendChild(p);
  }

  const rev = el("div", "panel");
  rev.appendChild(el("h3", null, "Review notes"));
  const list = el("div");
  const reviews = await api(`/api/checkpoints/${id}/reviews`,
    { signal, ws: wsId });
  if (!reviews.items.length) {
    list.appendChild(el("div", null, "No notes yet."));
  }
  for (const r of reviews.items) {
    const rv = el("div", "review");
    rv.appendChild(el("div", "who",
      `${r.author || "anonymous"} · ${fmtDate(r.created_at)}`));
    rv.appendChild(el("div", "what", r.body || ""));
    list.appendChild(rv);
  }
  rev.appendChild(list);

  if (wsCanWrite) {
    const form = el("div");
    form.className = "mt12";
    const who = el("div", "hint",
      `Posting as ${me.user ? me.user.name : "you"}`);
    const bodyIn = document.createElement("textarea");
    bodyIn.placeholder = "Add a review note…";
    bodyIn.setAttribute("aria-label", "Review note");
    bodyIn.maxLength = 10000;
    bodyIn.className = "review-input";
    const status = el("div");
    status.setAttribute("aria-live", "polite");
    const save = el("button", "primary", "Add note");
    save.addEventListener("click", async () => {
      status.textContent = "";
      save.disabled = true;
      try {
        const res = await fetch(`/api/checkpoints/${id}/reviews`, {
          method: "POST",
          credentials: "same-origin", signal,
          headers: wsHeaders(
            { "Content-Type": "application/json" }, wsId),
          body: JSON.stringify({ body: bodyIn.value }),
        });
        const data = await res.json().catch(() => null);
        if (!res.ok) {
          throw new Error((data && data.error) || `HTTP ${res.status}`);
        }
        status.className = "notice-ok";
        status.textContent = "Note saved.";
        bodyIn.value = "";
        const r = data.item;
        if (list.firstChild
            && list.firstChild.textContent === "No notes yet.") {
          list.textContent = "";
        }
        const rv = el("div", "review");
        rv.appendChild(el("div", "who",
          `${r.author} · ${fmtDate(r.created_at)}`));
        rv.appendChild(el("div", "what", r.body));
        list.appendChild(rv);
      } catch (err) {
        if (isAbort(err)) return;
        status.className = "notice-err";
        status.textContent = err.message || "Save failed";
      } finally {
        save.disabled = false;
      }
    });
    form.append(who, bodyIn, save, status);
    rev.appendChild(form);
  } else if (!demoMode()) {
    rev.appendChild(el("p", "hint",
      "Your role is read-only in this workspace."));
  }
  const local = el("p", null,
    "Review notes are stored locally and are not synced with Git" +
    " checkpoint metadata yet.");
  local.className = "hint small";
  rev.appendChild(local);
  root.appendChild(rev);
}

async function vSearch(root, params, signal) {
  const wsId = currentWs && currentWs.id;
  setCrumb("Search");
  setNav("search");
  const q = params.get("q") || "";
  const offset = parseInt(params.get("offset") || "0", 10) || 0;
  const limit = 50;
  const repos = await repoOptions(signal, wsId);

  const filters = el("div", "filters");
  const qIn = el("input");
  qIn.value = q;
  qIn.placeholder = "Search prompts, responses, tool output…";
  qIn.setAttribute("aria-label", "Search transcripts");
  qIn.className = "wide";
  const rSel = el("select");
  rSel.setAttribute("aria-label", "Repository filter");
  rSel.appendChild(new Option("All repos", ""));
  for (const r of repos) rSel.appendChild(new Option(r.name, r.id));
  rSel.value = params.get("repo") || "";
  const aSel = el("select");
  aSel.setAttribute("aria-label", "Agent filter");
  aSel.appendChild(new Option("All agents", ""));
  for (const a of ["devin", "codex", "claude", "chatgpt"])
    aSel.appendChild(new Option(a, a));
  aSel.value = params.get("agent") || "";
  const go = el("button", "primary", "Search");
  const apply = () => {
    const p = new URLSearchParams();
    if (qIn.value.trim()) p.set("q", qIn.value.trim());
    if (rSel.value) p.set("repo", rSel.value);
    if (aSel.value) p.set("agent", aSel.value);
    location.hash = `#/search?${p}`;
  };
  go.addEventListener("click", apply);
  qIn.addEventListener("keydown", (e) => {
    if (e.key === "Enter") apply(); });
  filters.append(qIn, rSel, aSel, go);
  root.appendChild(filters);

  if (!q) {
    root.appendChild(el("div", "empty",
      "Type a query to search recorded sessions and tool output."));
    return;
  }
  const qp = new URLSearchParams({ q, limit: String(limit),
                                   offset: String(offset) });
  if (rSel.value) qp.set("repo", rSel.value);
  if (aSel.value) qp.set("agent", aSel.value);
  const d = await api(`/api/search?${qp}`, { signal, ws: wsId });
  const p = el("div", "panel");
  if (!d.items.length) {
    p.appendChild(el("div", "empty", `No results for "${q}".`));
  } else {
    p.appendChild(table(
      ["Match", "Agent", "Session", "Time"],
      d.items.map((r) => {
        const snip = el("div");
        snip.appendChild(el("span", "pill",
          KIND_LABELS[r.kind] || r.kind));
        snip.appendChild(document.createTextNode(" "));
        snip.appendChild(el("span", null,
          (r.text || "").slice(0, 140)));
        const sa = el("a", null, r.title || shortId(r.session_id));
        sa.href = `#/sessions/${r.session_id}`;
        return [snip, agentPill(r.agent), sa, fmtDate(r.timestamp)];
      })));
  }
  if (d.has_more || offset > 0) {
    const goOff = (o) => {
      const np = new URLSearchParams(qp);
      np.set("offset", String(o));
      location.hash = `#/search?${np}`;
    };
    p.appendChild(pager(d.has_more, offset, limit, goOff));
  }
  root.appendChild(p);
}

const INTEGRATION_MATRIX = [
  ["Devin", "Native hooks", "Lifecycle + ATIF import", "When reported",
   "Nested trajectories / parent links", "devin --resume", "Yes"],
  ["Claude Code", "Native hooks", "Hooks + JSONL import", "Yes",
   "Nested hook sessions", "claude -r", "Yes"],
  ["Codex", "Exec JSON stream", "Stream + rollout import", "Yes",
   "Recorded file-change work", "codex resume", "Yes"],
  ["ChatGPT", "None", "Conversation export", "No", "No", "No", "Yes"],
];

const INTEGRATION_DETAILS = {
  devin: [
    "SessionStart, UserPromptSubmit, PostToolUse, Stop, SessionEnd and compaction hooks are captured through .devin/hooks.v1.json.",
    "A session row keeps the Devin session id, model when supplied, branch, parent_session_id, event count, linked checkpoints and recorded token usage.",
    "Devin sub-agents are represented as child sessions when hooks carry parent_session_id or when an ATIF export contains nested subagent_trajectories.",
    "Line attribution links measured changed lines back to the exact session id; review the checkpoint's Sessions tab to see which agent or sub-agent produced the evidence.",
    "`partial resume SESSION --run` plans the documented `devin --resume <native-id>` path; ATIF archives preserve context but are not presented as private Devin state restoration.",
  ],
  claude: [
    "Generated hook settings capture prompts, tool calls, responses, usage and nested sessions.",
    "Claude JSONL transcripts can be archived and restored only with an explicit trust flag and no overwrite.",
  ],
  codex: [
    "`partial run codex` consumes `codex exec --json`; rollout files can be imported or registered for native resume.",
    "Turn usage is aggregated from reported token_count/usage events.",
  ],
  chatgpt: [
    "ChatGPT conversations are imported only from an explicit export; Partial does not claim live hooks, native resume, or sub-agent capture for ChatGPT.",
  ],
};

async function vIntegrations(root, signal) {
  const wsId = currentWs && currentWs.id;
  const wsCanWrite = !demoMode() && currentWs
    && ROLE_RANK[currentWs.role] >= ROLE_RANK.member;
  setCrumb("Integrations");
  setNav("integrations");
  const d = await api("/api/integrations", { signal, ws: wsId });

  const intro = el("div", "panel");
  intro.appendChild(el("h2", null, "Connect an agent"));
  intro.appendChild(el("p", null,
    "Partial supports the agents below. Enablement installs repository" +
    " hooks or records explicit imports — nothing is connected until" +
    " you run the setup commands in your project."));
  for (const cmd of [
    "uv pip install --python .venv/bin/python -e .",
    "partial enable --agent all",
    "partial serve",
    "partial account create --email you@example.com --name You",
  ]) {
    const row = el("div", "cmdline");
    row.appendChild(el("code", null, cmd));
    row.appendChild(copyBtn(cmd));
    intro.appendChild(row);
  }
  root.appendChild(intro);

  const matrix = el("div", "panel");
  matrix.appendChild(el("h2", null, "Capture capability matrix"));
  matrix.appendChild(el("p", "hint small",
    "Capabilities describe the recorded evidence available to" +
    " Partial, not vendor-hosted agent state. Sub-agent rows stay" +
    " linked to their parent session and are included in checkpoint" +
    " context."));
  matrix.appendChild(table(
    ["Agent", "Hooks", "Transcript", "Tokens", "Sub-agents",
     "Native resume", "Import"],
    INTEGRATION_MATRIX));
  root.appendChild(matrix);

  for (const i of d.items) {
    const card = el("div", "integ");
    const h = el("h3");
    h.appendChild(agentPill(i.agent));
    h.appendChild(el("span", null, i.name));
    card.appendChild(h);
    card.appendChild(el("div", "cap", `Capture: ${i.capture}`));
    for (const cmd of i.setup) {
      const row = el("div", "cmdline");
      row.appendChild(el("code", null, cmd));
      row.appendChild(copyBtn(cmd));
      card.appendChild(row);
    }
    card.appendChild(el("p", null, i.note));
    const details = INTEGRATION_DETAILS[i.agent] || [];
    if (details.length) {
      const det = el("details", "integration-details");
      det.appendChild(el("summary", null,
        i.agent === "devin"
          ? "Devin capture, sub-agents, resume, and attribution"
          : "Captured fields and attribution"));
      const ul = document.createElement("ul");
      for (const item of details) {
        const li = document.createElement("li");
        li.textContent = item;
        ul.appendChild(li);
      }
      det.appendChild(ul);
      card.appendChild(det);
    }
    root.appendChild(card);
  }

  const tools = el("div", "panel");
  tools.appendChild(el("h3", null, "Bundle tools"));
  const up = el("div", "filters");
  const file = document.createElement("input");
  file.type = "file";
  file.accept = "application/json,.json";
  file.setAttribute("aria-label", "Bundle file");
  file.disabled = !wsCanWrite;
  const upBtn = el("button", null, "Upload bundle");
  upBtn.disabled = !wsCanWrite;
  const upStatus = el("div");
  upStatus.setAttribute("aria-live", "polite");
  upBtn.addEventListener("click", async () => {
    upStatus.textContent = "";
    upBtn.disabled = true;
    try {
    const f = file.files && file.files[0];
    if (!f) { upStatus.className = "notice-err";
              upStatus.textContent = "Choose a bundle file first.";
              return; }
    if (f.size > 16 * 1024 * 1024) {
      upStatus.className = "notice-err";
      upStatus.textContent = "Bundle exceeds 16 MiB.";
      return;
    }
    let text;
    try {
      text = await f.text();
      JSON.parse(text);
    } catch (_) {
      upStatus.className = "notice-err";
      upStatus.textContent = "Not a valid JSON bundle.";
      return;
    }
    if (wsId !== (currentWs && currentWs.id)) {
      upStatus.className = "notice-err";
      upStatus.textContent = "Workspace changed — upload aborted.";
      return;
    }
    try {
      const res = await fetch("/api/bundles", {
        method: "POST",
        credentials: "same-origin", signal,
        headers: wsHeaders(
          { "Content-Type": "application/json" }, wsId),
        body: text,
      });
      const data = await res.json().catch(() => null);
      if (!res.ok) {
        throw new Error((data && data.error) || `HTTP ${res.status}`);
      }
      upStatus.className = "notice-ok";
      upStatus.textContent = "Bundle imported.";
      file.value = "";
      setTimeout(() => route(), 400);
    } catch (err) {
      if (isAbort(err)) return;
      upStatus.className = "notice-err";
      upStatus.textContent = err.message || "Upload failed";
    }
    } finally {
      upBtn.disabled = !wsCanWrite;
    }
  });
  up.append(file, upBtn);
  tools.appendChild(up);
  tools.appendChild(upStatus);
  const warn = el("p", null,
    "Bundles contain recorded session context. Share them only with" +
    " workspaces you trust; imported data is stored in this workspace.");
  warn.className = "hint small";
  tools.appendChild(warn);
  const exp = el("div", "filters");
  const expB = el("button", "button", "Download export bundle");
  expB.addEventListener("click", () =>
    fetchBlob("/api/export", "partial-export.json", wsId, signal));
  exp.appendChild(expB);
  tools.appendChild(exp);
  root.appendChild(tools);
}

async function vSettings(root, signal) {
  const wsId = currentWs && currentWs.id;
  const wsRole = currentWs ? currentWs.role : "viewer";
  setCrumb("Settings");
  setNav("settings");

  const head = el("div", "panel");
  head.appendChild(el("h2", null, "Settings"));
  head.appendChild(el("p", null,
    `Signed in as ${me.user.name} · workspace` +
    ` ${currentWs ? currentWs.name : ""}` +
    ` · role ${currentWs ? currentWs.role : "—"}`));
  root.appendChild(head);

  if (!demoMode()) {
    const nw = el("div", "panel");
    nw.appendChild(el("h3", null, "New workspace"));
    const nwName = el("input");
    nwName.placeholder = "Workspace name";
    nwName.setAttribute("aria-label", "Workspace name");
    const nwBtn = el("button", null, "Create workspace");
    const nwStatus = el("div");
    nwStatus.setAttribute("aria-live", "polite");
    nwBtn.addEventListener("click", async () => {
      nwStatus.textContent = "";
      nwBtn.disabled = true;
      try {
        const res = await fetch("/api/workspaces", {
          method: "POST", credentials: "same-origin", signal,
          headers: wsHeaders(
            { "Content-Type": "application/json" }, wsId),
          body: JSON.stringify({ name: nwName.value.trim() }),
        });
        const data = await res.json().catch(() => null);
        if (!res.ok) {
          throw new Error((data && data.error) || `HTTP ${res.status}`);
        }
        me = await api("/api/me", {
          headers: wsHeaders(null, wsId), signal });
        pickWorkspace();
        renderWsSelect();
        nwStatus.className = "notice-ok";
        nwStatus.textContent = "Workspace created.";
        nwName.value = "";
      } catch (err) {
        if (isAbort(err)) return;
        nwStatus.className = "notice-err";
        nwStatus.textContent = err.message || "Create failed";
      } finally {
        nwBtn.disabled = false;
      }
    });
    nw.append(nwName, nwBtn, nwStatus);
    root.appendChild(nw);

    const aj = el("div", "panel");
    aj.appendChild(el("h3", null, "Accept invitation"));
    aj.appendChild(el("p", "hint small",
      "Paste an invitation token to join another workspace" +
      ` as ${me.user.email}.`));
    const ajRow = el("div", "filters");
    const ajTok = el("input");
    ajTok.placeholder = "Invitation token";
    ajTok.setAttribute("aria-label", "Invitation token");
    const ajBtn = el("button", null, "Accept invitation");
    ajRow.append(ajTok, ajBtn);
    aj.appendChild(ajRow);
    const ajStatus = el("div");
    ajStatus.setAttribute("aria-live", "polite");
    aj.appendChild(ajStatus);
    ajBtn.addEventListener("click", async () => {
      ajStatus.textContent = "";
      ajBtn.disabled = true;
      const tok = ajTok.value;
      ajTok.value = "";
      try {
        const res = await fetch("/api/invites/accept", {
          method: "POST", credentials: "same-origin", signal,
          headers: wsHeaders(
            { "Content-Type": "application/json" }, wsId),
          body: JSON.stringify({ token: tok, email: me.user.email }),
        });
        const data = await res.json().catch(() => null);
        if (!res.ok) {
          throw new Error((data && data.error) || `HTTP ${res.status}`);
        }
        me = await api("/api/me", {
          headers: wsHeaders(null, wsId), signal });
        pickWorkspace();
        renderWsSelect();
        ajStatus.className = "notice-ok";
        ajStatus.textContent =
          "Invitation accepted — select the workspace above.";
      } catch (err) {
        if (isAbort(err)) return;
        ajStatus.className = "notice-err";
        ajStatus.textContent = err.message || "Accept failed";
      } finally {
        ajBtn.disabled = false;
      }
    });
    root.appendChild(aj);

    const mp = el("div", "panel");
    mp.appendChild(el("h3", null, "Members"));
    const members = await api(
      `/api/workspaces/${wsId}/members`, { signal, ws: wsId });
    const myRole = wsRole;
    const mrows = members.items.map((m) => {
      const cells = [m.name, el("span", "mono", m.email), m.role];
      const ops = el("span");
      const manageable = ROLE_RANK[myRole] >= ROLE_RANK.admin
        && m.user_id !== me.user.id
        && (myRole === "owner"
            || ROLE_RANK[m.role] < ROLE_RANK[myRole]);
      if (manageable) {
        const sel = el("select");
        sel.setAttribute("aria-label", `Role for ${m.email}`);
        const allowed = myRole === "owner"
          ? ["owner", "admin", "member", "viewer"]
          : ["member", "viewer"];
        for (const r of allowed) sel.appendChild(new Option(r, r));
        sel.value = allowed.includes(m.role) ? m.role : "viewer";
        sel.addEventListener("change", async () => {
          sel.disabled = true;
          try {
            const res = await fetch(
              `/api/workspaces/${wsId}/members/${m.user_id}`, {
                method: "PATCH", credentials: "same-origin", signal,
                headers: wsHeaders(
                  { "Content-Type": "application/json" }, wsId),
                body: JSON.stringify({ role: sel.value }),
              });
            if (!res.ok) {
              const d = await res.json().catch(() => null);
              throw new Error((d && d.error) || `HTTP ${res.status}`);
            }
            route();
          } catch (err) {
            if (isAbort(err)) return;
            sel.disabled = false;
            showErr(err.message);
          }
        });
        const rm = el("button", "ghost", "Remove");
        rm.addEventListener("click", async () => {
          if (!window.confirm(
            `Remove ${m.email} from this workspace?`)) return;
          rm.disabled = true;
          try {
            const res = await fetch(
              `/api/workspaces/${wsId}/members/${m.user_id}`, {
                method: "DELETE", credentials: "same-origin", signal,
                headers: wsHeaders(
                  { "Content-Type": "application/json" }, wsId),
                body: "{}",
              });
            if (!res.ok) {
              const d = await res.json().catch(() => null);
              throw new Error((d && d.error) || `HTTP ${res.status}`);
            }
            route();
          } catch (err) {
            if (isAbort(err)) return;
            rm.disabled = false;
            showErr(err.message);
          }
        });
        ops.append(sel, rm);
      }
      cells.push(ops);
      return cells;
    });
    mp.appendChild(table(["Name", "Email", "Role", ""], mrows));
    root.appendChild(mp);

    if (ROLE_RANK[myRole] >= ROLE_RANK.admin) {
      const ip = el("div", "panel");
      ip.appendChild(el("h3", null, "Invite a member"));
      const iRow = el("div", "filters");
      const iEmail = el("input");
      iEmail.type = "email";
      iEmail.placeholder = "email@example.com";
      iEmail.setAttribute("aria-label", "Invitee email");
      const iRole = el("select");
      iRole.setAttribute("aria-label", "Invite role");
      const roles = myRole === "owner"
        ? ["member", "viewer", "admin", "owner"]
        : ["member", "viewer"];
      for (const r of roles) iRole.appendChild(new Option(r, r));
      const iBtn = el("button", "primary", "Create invitation");
      iRow.append(iEmail, iRole, iBtn);
      ip.appendChild(iRow);
      const iStatus = el("div");
      iStatus.setAttribute("aria-live", "polite");
      const iTokenBox = el("div");
      iBtn.addEventListener("click", async () => {
        iStatus.textContent = ""; iTokenBox.textContent = "";
        iBtn.disabled = true;
        try {
          const res = await fetch(
            `/api/workspaces/${wsId}/invites`, {
              method: "POST", credentials: "same-origin", signal,
              headers: wsHeaders(
                { "Content-Type": "application/json" }, wsId),
              body: JSON.stringify({
                email: iEmail.value.trim(), role: iRole.value }),
            });
          const data = await res.json().catch(() => null);
          if (!res.ok) {
            throw new Error((data && data.error) || `HTTP ${res.status}`);
          }
          iStatus.className = "notice-ok";
          iStatus.textContent =
            "Invitation created — share this token once:";
          const tb = el("div", "cmdline");
          tb.appendChild(el("code", null, data.item.token));
          tb.appendChild(copyBtn(data.item.token));
          iTokenBox.appendChild(tb);
          iEmail.value = "";
        } catch (err) {
          if (isAbort(err)) return;
          iStatus.className = "notice-err";
          iStatus.textContent = err.message || "Invite failed";
        } finally {
          iBtn.disabled = false;
        }
      });
      ip.append(iStatus, iTokenBox);
      root.appendChild(ip);
    }

    const tp = el("div", "panel");
    tp.appendChild(el("h3", null, "API tokens"));
    const tRow = el("div", "filters");
    const tName = el("input");
    tName.placeholder = "Token name";
    tName.setAttribute("aria-label", "Token name");
    const tRole = el("select");
    tRole.setAttribute("aria-label", "Token role");
    for (const r of ["member", "viewer"]) {
      if (ROLE_RANK[r] <= ROLE_RANK[myRole]) {
        tRole.appendChild(new Option(r, r));
      }
    }
    tRole.value = ROLE_RANK[myRole] >= ROLE_RANK.member
      ? "member" : "viewer";
    const tBtn = el("button", "primary", "Create token");
    tRow.append(tName, tRole, tBtn);
    tp.appendChild(tRow);
    const tStatus = el("div");
    tStatus.setAttribute("aria-live", "polite");
    const tBox = el("div");
    tBtn.addEventListener("click", async () => {
      tStatus.textContent = ""; tBox.textContent = "";
      tBtn.disabled = true;
      try {
        const res = await fetch(
          `/api/workspaces/${wsId}/tokens`, {
            method: "POST", credentials: "same-origin", signal,
            headers: wsHeaders(
              { "Content-Type": "application/json" }, wsId),
            body: JSON.stringify({
              name: tName.value.trim() || "token",
              role: tRole.value }),
          });
        const data = await res.json().catch(() => null);
        if (!res.ok) {
          throw new Error((data && data.error) || `HTTP ${res.status}`);
        }
        tStatus.className = "notice-ok";
        tStatus.textContent =
          "Token created — copy it now; it is shown only once:";
        const tb = el("div", "cmdline");
        tb.appendChild(el("code", null, data.item.token));
        tb.appendChild(copyBtn(data.item.token));
        tBox.appendChild(tb);
        tName.value = "";
      } catch (err) {
        if (isAbort(err)) return;
        tStatus.className = "notice-err";
        tStatus.textContent = err.message || "Token create failed";
      } finally {
        tBtn.disabled = false;
      }
    });
    tp.append(tStatus, tBox);
    const tokens = await api(
      `/api/workspaces/${wsId}/tokens`, { signal, ws: wsId });
    if (tokens.items.length) {
      tp.appendChild(table(
        ["Name", "Role", "Owner", "Expires", ""],
        tokens.items.map((t) => {
          const cells = [
            t.name, t.role, t.email || "—",
            fmtDate(t.expires_at ? t.expires_at * 1000 : null)];
          const ops = el("span");
          if (ROLE_RANK[myRole] >= ROLE_RANK.admin
              || t.user_id === me.user.id) {
            const rb = el("button", "ghost", "Revoke");
            rb.addEventListener("click", async () => {
              if (!window.confirm(`Revoke token "${t.name}"?`))
                return;
              rb.disabled = true;
              try {
                const res = await fetch(`/api/tokens/${t.id}`, {
                  method: "DELETE", credentials: "same-origin", signal,
                  headers: wsHeaders(
                    { "Content-Type": "application/json" }, wsId),
                  body: "{}",
                });
                if (!res.ok) {
                  const d = await res.json().catch(() => null);
                  throw new Error(
                    (d && d.error) || `HTTP ${res.status}`);
                }
                route();
              } catch (err) {
                if (isAbort(err)) return;
                rb.disabled = false;
                showErr(err.message);
              }
            });
            ops.appendChild(rb);
          }
          cells.push(ops);
          return cells;
        })));
    } else {
      tp.appendChild(el("div", "empty", "No API tokens yet."));
    }
    root.appendChild(tp);

    if (ROLE_RANK[myRole] >= ROLE_RANK.admin) {
      const ap = el("div", "panel");
      ap.appendChild(el("h3", null, "Audit log"));
      const audit = await api(
        `/api/workspaces/${wsId}/audit`, { signal, ws: wsId });
      if (audit.items.length) {
        ap.appendChild(table(
          ["Time", "Actor", "Action", "Target"],
          audit.items.map((a) => [
            fmtDate(a.created_at),
            el("span", "mono", (a.actor_id || "").slice(0, 12)),
            a.action,
            el("span", "mono", (a.target_id || "").slice(0, 12)),
          ])));
      } else {
        ap.appendChild(el("div", "empty", "No audit events yet."));
      }
      root.appendChild(ap);
    }

    const pp = el("div", "panel");
    pp.appendChild(el("h3", null, "Change password"));
    const pCur = el("input");
    pCur.type = "password";
    pCur.autocomplete = "current-password";
    pCur.placeholder = "Current password";
    pCur.setAttribute("aria-label", "Current password");
    const pNew = el("input");
    pNew.type = "password";
    pNew.autocomplete = "new-password";
    pNew.placeholder = "New password (12+ chars)";
    pNew.setAttribute("aria-label", "New password");
    const pBtn = el("button", null, "Change password");
    const pStatus = el("div");
    pStatus.setAttribute("aria-live", "polite");
    pBtn.addEventListener("click", async () => {
      pStatus.textContent = "";
      pBtn.disabled = true;
      try {
        const res = await fetch("/api/account/password", {
          method: "POST", credentials: "same-origin", signal,
          headers: wsHeaders(
            { "Content-Type": "application/json" }, wsId),
          body: JSON.stringify({
            current_password: pCur.value, new_password: pNew.value }),
        });
        const data = await res.json().catch(() => null);
        if (!res.ok) {
          throw new Error((data && data.error) || `HTTP ${res.status}`);
        }
        showLogin("signin");
      } catch (err) {
        if (isAbort(err)) return;
        pStatus.className = "notice-err";
        pStatus.textContent = err.message || "Password change failed";
      } finally {
        pBtn.disabled = false;
        pCur.value = ""; pNew.value = "";
      }
    });
    pp.append(pCur, pNew, pBtn, pStatus);
    root.appendChild(pp);
  }
}

function canWrite() {
  return !demoMode() && currentWs
    && ROLE_RANK[currentWs.role] >= ROLE_RANK.member;
}

function canOwner() {
  return !demoMode() && currentWs && currentWs.role === "owner";
}

function docLoc(d) {
  if (d.path) return `${d.path}:${d.line_start || 1}-${d.line_end || ""}`;
  if (d.commit_sha) return `commit ${String(d.commit_sha).slice(0, 10)}`;
  return d.source_id ? `source ${String(d.source_id).slice(0, 24)}` : "";
}

let docOverlay = null;
let docOverlayFocus = null;

function closeDocOverlay() {
  if (docOverlay) {
    docOverlay.remove();
    docOverlay = null;
  }
  if (docOverlayFocus) {
    try { docOverlayFocus.focus(); } catch (_) { /* noop */ }
    docOverlayFocus = null;
  }
}

async function openDoc(id, wsId) {
  const gen = routeVersion;
  const signal = routeCtl ? routeCtl.signal : undefined;
  try {
    const d = await api(
      `/api/memory/documents/${encodeURIComponent(id)}`,
      { ws: wsId, signal });
    if (gen !== routeVersion || (signal && signal.aborted)) return;
    const doc = d.document;
    const panel = el("div", "panel");
    panel.appendChild(el("h3", null, doc.title || doc.id));
    panel.appendChild(el("div", "cap",
      `${doc.kind} · ${docLoc(doc)} · ${doc.id.slice(0, 16)}`));
    const links = el("div", "cap");
    if (doc.kind === "session" && doc.source_id) {
      const a = el("a", null, "open session");
      a.href = `#/sessions/${doc.source_id.split(":")[0]}`;
      links.appendChild(a);
    }
    if (doc.kind === "checkpoint" && doc.source_id) {
      const a = el("a", null, "open checkpoint");
      a.href = `#/checkpoints/${doc.source_id}`;
      links.appendChild(a);
    }
    if (links.childNodes.length) panel.appendChild(links);
    panel.appendChild(el("pre", "code-view",
      doc.text || "(empty)"));
    closeDocOverlay();
    const overlay = el("div", "doc-overlay");
    overlay.setAttribute("role", "dialog");
    overlay.setAttribute("aria-modal", "true");
    overlay.setAttribute("aria-label", "Memory document");
    const close = el("button", "ghost", "Close");
    close.addEventListener("click", closeDocOverlay);
    overlay.addEventListener("click", (e) => {
      if (e.target === overlay) closeDocOverlay(); });
    panel.prepend(close);
    overlay.appendChild(panel);
    docOverlayFocus = document.activeElement;
    document.body.appendChild(overlay);
    docOverlay = overlay;
    close.focus();
  } catch (err) {
    if (isAbort(err)) return;
    if (gen === routeVersion) showErr(err.message);
  }
}

async function vMemory(root, params, signal) {
  const wsId = currentWs && currentWs.id;
  setCrumb("Memory");
  setNav("memory");
  const status = await api("/api/memory/status",
                           { signal, ws: wsId });

  const stat = el("div", "panel");
  stat.appendChild(el("h2", null, "Repository memory"));
  stat.appendChild(el("p", null,
    "Local-first evidence index over recorded sessions, checkpoints," +
    " decisions, and indexed code. Lexical search only unless you" +
    " explicitly run an AI request."));
  const flags = el("div", "cap",
    `documents ${status.documents} · fts5 ${status.fts5}` +
    ` · provider ${status.provider_configured
      ? "configured" : "not configured"}` +
    ` · external AI ${status.external_ai_enabled
      ? "enabled" : "disabled"}`);
  stat.appendChild(flags);
  if (status.indexed_repositories.length) {
    stat.appendChild(el("div", "cap",
      "indexed: " + status.indexed_repositories.map(
        (r) => `${r.repo_id.slice(0, 10)}@` +
               `${String(r.commit_sha).slice(0, 10) || "-"}`).join(", ")));
  }
  if (canWrite()) {
    const idxBtn = el("button", null, "Index captured history");
    idxBtn.addEventListener("click", async () => {
      idxBtn.disabled = true;
      try {
        await api("/api/memory/index", {
          method: "POST", ws: wsId, signal,
          headers: { "Content-Type": "application/json" },
          body: "{}" });
        route();
      } catch (err) {
        if (isAbort(err)) return;
        showErr(err.message);
        idxBtn.disabled = false;
      }
    });
    stat.appendChild(idxBtn);
    stat.appendChild(el("p", "hint small",
      "Re-indexes captured sessions, checkpoints, and decisions" +
      " recorded in this workspace. It does not scan the filesystem" +
      " — code documents and the graph are only built by running" +
      " `partial index` on a repository checkout."));
  }
  if (canOwner()) {
    const lab = el("label", "cap");
    const cb = el("input");
    cb.type = "checkbox";
    cb.checked = !!status.external_ai_enabled;
    cb.disabled = !status.provider_configured
      && !status.external_ai_enabled;
    lab.appendChild(cb);
    lab.appendChild(document.createTextNode(
      " Allow sending bounded evidence to the configured AI provider"
      + (status.provider_configured ? ""
        : " (provider not configured)")));
    cb.addEventListener("change", async () => {
      if (cb.checked && !window.confirm(
        "Enable external AI for this workspace? Bounded evidence"
        + " will be sent to the configured provider on explicit"
        + " requests only. This can incur paid API usage.")) {
        cb.checked = false;
        return;
      }
      try {
        await api("/api/memory/settings", {
          method: "POST", ws: wsId, signal,
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ external_ai_enabled: cb.checked }) });
      } catch (err) {
        if (isAbort(err)) return;
        cb.checked = !cb.checked;
        showErr(err.message);
      }
    });
    stat.appendChild(lab);
  }
  root.appendChild(stat);

  const kind = params.get("kind") || "";
  const q = params.get("q") || "";
  const repoParam = params.get("repo") || "";
  const useSem = params.get("semantic") === "1";
  const semAllowed = status.provider_configured
    && status.external_ai_enabled && canWrite();
  const repos = await repoOptions(signal, wsId);
  const filters = el("div", "filters");
  const qIn = el("input");
  qIn.value = q;
  qIn.placeholder = "Search memory…";
  qIn.className = "wide";
  qIn.setAttribute("aria-label", "Memory search query");
  const rSel = el("select");
  rSel.setAttribute("aria-label", "Repository");
  rSel.appendChild(new Option("All repos", ""));
  for (const r of repos) rSel.appendChild(new Option(r.name, r.id));
  rSel.value = repoParam;
  const go = el("button", "primary", "Search");
  let semCb = null;
  if (semAllowed) {
    semCb = el("input");
    semCb.type = "checkbox";
    semCb.checked = useSem;
    semCb.setAttribute("aria-label", "Semantic search");
  }
  const apply = (over = {}) => {
    const p = new URLSearchParams();
    const qv = qIn.value.trim();
    if (qv) p.set("q", qv);
    const kv = over.kind !== undefined ? over.kind : kind;
    if (kv) p.set("kind", kv);
    if (rSel.value) p.set("repo", rSel.value);
    if (semCb && semCb.checked && semAllowed) {
      if (qv && !window.confirm(
        "Run semantic search? The query is embedded by the" +
        " configured AI provider — a paid external request." +
        " Cancel to run a local lexical search instead.")) {
        semCb.checked = false;
      } else {
        p.set("semantic", "1");
      }
    }
    location.hash = `#/memory?${p}`;
  };
  go.addEventListener("click", () => apply());
  qIn.addEventListener("keydown", (e) => {
    if (e.key === "Enter") apply(); });
  rSel.addEventListener("change", () => apply());

  const kindTabs = el("div", "tabs");
  kindTabs.setAttribute("role", "tablist");
  kindTabs.setAttribute("aria-label", "Document kind");
  for (const [v, l] of [["", "All"], ["code", "Code"],
                        ["session", "Sessions"],
                        ["checkpoint", "Checkpoints"],
                        ["decision", "Decisions"]]) {
    const b = el("button", v === kind ? "active" : null, l);
    b.setAttribute("role", "tab");
    b.setAttribute("aria-selected", v === kind ? "true" : "false");
    b.addEventListener("click", () => apply({ kind: v }));
    kindTabs.appendChild(b);
  }
  root.appendChild(kindTabs);

  const docIn = el("input");
  docIn.placeholder = "Open document by id";
  docIn.setAttribute("aria-label", "Open document by id");
  const docBtn = el("button", null, "Open doc");
  const openById = () => {
    const v = docIn.value.trim();
    if (v) openDoc(v, wsId);
  };
  docBtn.addEventListener("click", openById);
  docIn.addEventListener("keydown", (e) => {
    if (e.key === "Enter") openById(); });
  filters.append(qIn, rSel, go, docIn, docBtn);
  if (q && status.external_ai_enabled && canWrite()) {
    const askBtn = el("button", null,
      "Send selected context to configured AI provider");
    askBtn.addEventListener("click", async () => {
      if (!window.confirm(
        "Send bounded evidence to the configured AI provider?"
        + " This is a paid external request.")) return;
      try {
        const out = await api("/api/workflows", {
          method: "POST", ws: wsId, signal,
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ kind: "ask", query: q, run: true,
                                 repo_id: rSel.value || null }) });
        showErr(`ask submitted: ${out.id.slice(0, 12)} — see Workflows`);
      } catch (err) {
        if (isAbort(err)) return;
        showErr(err.message);
      }
    });
    filters.appendChild(askBtn);
  }
  root.appendChild(filters);
  if (semAllowed) {
    const semLab = el("label", "cap");
    semLab.appendChild(semCb);
    semLab.appendChild(document.createTextNode(
      " Semantic (sends the query embedding to the configured"
      + " provider; paid request — asks for confirmation)"));
    root.appendChild(semLab);
  } else {
    root.appendChild(el("div", "cap",
      "Semantic search unavailable — it needs a configured AI"
      + " provider and the external-AI policy enabled by an owner."
      + " Lexical search never contacts a provider."));
  }

  if (!q) {
    root.appendChild(el("div", "empty",
      "Type a query to search indexed memory. Archived or cited" +
      " documents can still be opened by id above."));
    return;
  }
  let d;
  if (useSem && semAllowed) {
    d = await api("/api/memory/search", {
      method: "POST", signal, ws: wsId,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query: q, kind: kind || null,
                             repo_id: repoParam || null,
                             semantic: true }) });
  } else {
    const qp = new URLSearchParams({ q });
    if (kind) qp.set("kind", kind);
    if (repoParam) qp.set("repo", repoParam);
    d = await api(`/api/memory/search?${qp}`,
                  { signal, ws: wsId });
  }
  const p = el("div", "panel");
  if (useSem && !semAllowed) {
    p.appendChild(el("div", "cap",
      "Semantic search was requested but is unavailable" +
      " (provider or policy); showing local lexical results."));
  }
  if (!d.items.length) {
    p.appendChild(el("div", "empty", `No results for "${q}".`));
  } else {
    p.appendChild(el("div", "cap",
      `${d.items.length} result(s) · mode ${d.mode}`));
    p.appendChild(table(
      ["Title", "Kind", "Location", "Repo"],
      d.items.map((r) => {
        const a = el("a", null, r.title || shortId(r.id));
        a.href = "#/memory";
        a.addEventListener("click", (e) => {
          e.preventDefault(); openDoc(r.id, wsId); });
        return [a, r.kind, docLoc(r),
                String(r.repo_id || "").slice(0, 10)];
      })));
  }
  root.appendChild(p);
}

async function vGraph(root, params, signal) {
  const wsId = currentWs && currentWs.id;
  setCrumb("Code graph");
  setNav("graph");
  const q = params.get("q") || "";
  const sym = params.get("symbol") || "";
  const repos = await repoOptions(signal, wsId);
  const filters = el("div", "filters");
  const qIn = el("input");
  qIn.value = q;
  qIn.placeholder = "Symbol name…";
  qIn.setAttribute("aria-label", "Symbol search");
  const rSel = el("select");
  rSel.setAttribute("aria-label", "Repository");
  rSel.appendChild(new Option("All repos", ""));
  for (const r of repos) rSel.appendChild(new Option(r.name, r.id));
  rSel.value = params.get("repo") || "";
  const go = el("button", "primary", "Search");
  const apply = () => {
    const p = new URLSearchParams();
    if (qIn.value.trim()) p.set("q", qIn.value.trim());
    if (rSel.value) p.set("repo", rSel.value);
    location.hash = `#/graph?${p}`;
  };
  go.addEventListener("click", apply);
  qIn.addEventListener("keydown", (e) => {
    if (e.key === "Enter") apply(); });
  filters.append(qIn, rSel, go);
  root.appendChild(filters);

  const status = await api("/api/memory/status",
                           { signal, ws: wsId });
  root.appendChild(el("div", "cap",
    "Coverage: python AST (definitions, same-file calls, imports);" +
    " other languages lexical inventory only. Dynamic dispatch and" +
    " aliases are not resolved."));

  if (sym) {
    const d = await api(
      `/api/graph/neighbors?id=${encodeURIComponent(sym)}` +
      (rSel.value ? `&repo=${rSel.value}` : ""),
      { signal, ws: wsId });
    const p = el("div", "panel");
    p.appendChild(el("h3", null,
      `${d.symbol.qualified_name} (${d.symbol.kind})`));
    p.appendChild(el("div", "cap",
      `${d.symbol.path}:${d.symbol.line}-${d.symbol.end_line} ·` +
      ` analysis ${d.analysis}`));
    for (const lim of d.limitations || [])
      p.appendChild(el("div", "cap", `⚠ ${lim}`));
    const others = (d.nodes || []).filter(
      (n) => n.id !== d.symbol.id);
    if (others.length) {
      p.appendChild(el("h4", null, `Neighbor nodes (${others.length})`));
      p.appendChild(table(
        ["Symbol", "Kind", "File", "Lines"],
        others.map((n) => {
          const a = el("a", null, n.qualified_name);
          a.href = `#/graph?symbol=${encodeURIComponent(n.id)}` +
            (rSel.value ? `&repo=${rSel.value}` : "");
          return [a, n.kind, n.path, `${n.line}-${n.end_line}`];
        })));
    }
    if (d.edges.length)
      p.appendChild(table(
        ["From", "→", "To", "Kind"],
        d.edges.map((e) => {
          const src = (d.nodes.find((n) => n.id === e.source_id) || {});
          const tgt = e.target_id.startsWith("import:")
            ? { qualified_name: e.target_id.slice(7) }
            : (d.nodes.find((n) => n.id === e.target_id) || {});
          return [src.qualified_name || shortId(e.source_id), "→",
                  tgt.qualified_name || shortId(e.target_id), e.kind];
        })));
    else
      p.appendChild(el("div", "empty",
        "No recorded call/import relationships for this symbol" +
        (d.analysis === "ast"
          ? "."
          : " — this language is lexical inventory only.")));
    root.appendChild(p);
    return;
  }
  if (!q) {
    root.appendChild(el("div", "empty",
      "Search for an indexed symbol to inspect its relationships."));
    return;
  }
  const qp = new URLSearchParams({ q });
  if (rSel.value) qp.set("repo", rSel.value);
  const d = await api(`/api/graph/search?${qp}`, { signal, ws: wsId });
  const p = el("div", "panel");
  if (!d.items.length) {
    p.appendChild(el("div", "empty", `No symbols for "${q}".`));
  } else {
    p.appendChild(table(
      ["Symbol", "Kind", "File", "Lines", ""],
      d.items.map((s) => {
        const a = el("a", null, s.qualified_name);
        a.href = `#/graph?symbol=${encodeURIComponent(s.id)}` +
          (rSel.value ? `&repo=${rSel.value}` : "");
        const imp = el("button", "ghost", "impact");
        imp.setAttribute("aria-label",
          `Impact of ${s.qualified_name}`);
        imp.addEventListener("click", async () => {
          try {
            const d2 = await api(
              `/api/graph/impact?id=${encodeURIComponent(s.id)}` +
              (rSel.value ? `&repo=${rSel.value}` : ""),
              { signal, ws: wsId });
            if (signal.aborted) return;
            const impacted = (d2.impacted || []).filter(
              (n) => n.id !== d2.symbol.id);
            for (const old of p.querySelectorAll(".impact-panel"))
              old.remove();
            const ip = el("div", "panel impact-panel");
            ip.appendChild(el("h3", null,
              `Impact of ${d2.symbol.qualified_name}`));
            ip.appendChild(el("div", "cap",
              `${impacted.length} caller-side symbol(s)` +
              (d2.truncated ? " (truncated at 200)" : "")));
            for (const lim of d2.limitations || [])
              ip.appendChild(el("div", "cap", `⚠ ${lim}`));
            if (impacted.length) {
              ip.appendChild(table(
                ["Symbol", "Kind", "File", "Lines"],
                impacted.map((n) => {
                  const na = el("a", null, n.qualified_name);
                  na.href = `#/graph?symbol=` +
                    `${encodeURIComponent(n.id)}` +
                    (rSel.value ? `&repo=${rSel.value}` : "");
                  return [na, n.kind, n.path,
                          `${n.line}-${n.end_line}`];
                })));
            } else {
              ip.appendChild(el("div", "empty",
                "No caller-side symbols recorded. Static analysis" +
                " only — dynamic dispatch is not resolved."));
            }
            p.appendChild(ip);
          } catch (err) {
            if (!isAbort(err) && !signal.aborted)
              showErr(err.message);
          }
        });
        return [a, s.kind, `${s.path} (${s.analysis})`,
                `${s.line}-${s.end_line}`, imp];
      })));
  }
  root.appendChild(p);
}

async function vDecisions(root, params, signal) {
  const wsId = currentWs && currentWs.id;
  setCrumb("Decisions");
  setNav("decisions");
  const repos = await repoOptions(signal, wsId);
  const filters = el("div", "filters");
  const fSel = el("select");
  fSel.setAttribute("aria-label", "Repository");
  fSel.appendChild(new Option("All repos", ""));
  for (const r of repos) fSel.appendChild(new Option(r.name, r.id));
  fSel.value = params.get("repo") || "";
  fSel.addEventListener("change", () => {
    const p = new URLSearchParams();
    if (fSel.value) p.set("repo", fSel.value);
    location.hash = `#/decisions?${p}`;
  });
  filters.appendChild(fSel);
  root.appendChild(filters);
  const qp = new URLSearchParams();
  if (params.get("repo")) qp.set("repo", params.get("repo"));
  const d = await api(`/api/decisions?${qp}`, { signal, ws: wsId });
  const p = el("div", "panel");
  p.appendChild(el("h2", null, "Decisions"));
  p.appendChild(el("p", "hint small",
    "Decisions are append-only history — recording a decision that" +
    " supersedes another marks the earlier one [superseded]; it is" +
    " never deleted."));
  if (!d.items.length)
    p.appendChild(el("div", "empty", "No decisions recorded."));
  else
    for (const dec of d.items) {
      const card = el("div", "panel");
      card.appendChild(el("h3", null,
        `[${dec.status}] ${dec.title}`));
      card.appendChild(el("p", null, dec.body));
      card.appendChild(el("div", "cap",
        `author ${dec.author} · ${fmtDate(dec.created_at)}` +
        (dec.supersedes
          ? ` · supersedes ${dec.supersedes.slice(0, 12)}` : "")));
      if (dec.source_ids && dec.source_ids.length) {
        const sl = el("div", "cap");
        sl.appendChild(document.createTextNode("sources: "));
        for (const sid of dec.source_ids) {
          const a = el("a", null, sid.slice(0, 12));
          a.href = "#/memory";
          a.addEventListener("click", (e) => {
            e.preventDefault(); openDoc(sid, wsId); });
          sl.appendChild(a);
          sl.appendChild(document.createTextNode(" "));
        }
        card.appendChild(sl);
      }
      p.appendChild(card);
    }
  root.appendChild(p);
  if (canWrite() && !repos.length) {
    root.appendChild(el("div", "cap",
      "Recording decisions requires a registered repository."));
  } else if (canWrite()) {
    const form = el("div", "panel");
    form.appendChild(el("h3", null, "Record a decision"));
    const rSel = el("select");
    rSel.setAttribute("aria-label", "Repository");
    for (const r of repos) rSel.appendChild(new Option(r.name, r.id));
    const tIn = el("input");
    tIn.placeholder = "Title";
    tIn.className = "wide";
    tIn.setAttribute("aria-label", "Decision title");
    const bIn = el("textarea");
    bIn.placeholder = "Body";
    bIn.setAttribute("aria-label", "Decision body");
    const sIn = el("input");
    sIn.placeholder = "Source document ids (comma separated)";
    sIn.className = "wide";
    sIn.setAttribute("aria-label", "Source document ids");
    const supIn = el("input");
    supIn.placeholder = "Supersedes decision id (optional)";
    supIn.className = "wide";
    supIn.setAttribute("aria-label", "Supersedes decision id");
    const btn = el("button", "primary", "Record decision");
    btn.addEventListener("click", async () => {
      btn.disabled = true;
      try {
        await api("/api/decisions", {
          method: "POST", ws: wsId, signal,
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            repo_id: rSel.value, title: tIn.value, body: bIn.value,
            source_ids: sIn.value.split(",").map((s) => s.trim())
              .filter(Boolean),
            supersedes: supIn.value.trim() || null }) });
        route();
      } catch (err) {
        if (isAbort(err)) return;
        showErr(err.message);
        btn.disabled = false;
      }
    });
    form.append(rSel, tIn, bIn, sIn, supIn, btn);
    root.appendChild(form);
  } else {
    root.appendChild(el("div", "cap",
      demoMode()
        ? "Demo workspace is read-only."
        : "Recording decisions requires the member role."));
  }
}

async function vDispatch(root, params, signal) {
  const wsId = currentWs && currentWs.id;
  setCrumb("Dispatches");
  setNav("dispatch");
  const repos = await repoOptions(signal, wsId);
  const filters = el("div", "filters");
  const since = el("input");
  since.type = "date";
  since.value = params.get("since") || "";
  since.setAttribute("aria-label", "Since date");
  const until = el("input");
  until.type = "date";
  until.value = params.get("until") || "";
  until.setAttribute("aria-label", "Until date");
  const rSel = el("select");
  rSel.setAttribute("aria-label", "Repository");
  rSel.appendChild(new Option("All repos", ""));
  for (const r of repos) rSel.appendChild(new Option(r.name, r.id));
  rSel.value = params.get("repo") || "";
  const bIn = el("input");
  bIn.placeholder = "branch";
  bIn.value = params.get("branch") || "";
  bIn.setAttribute("aria-label", "Branch");
  const go = el("button", "primary", "Preview");
  const apply = () => {
    const p = new URLSearchParams();
    if (since.value) p.set("since", since.value);
    if (until.value) p.set("until", until.value);
    if (rSel.value) p.set("repo", rSel.value);
    if (bIn.value.trim()) p.set("branch", bIn.value.trim());
    location.hash = `#/dispatch?${p}`;
  };
  go.addEventListener("click", apply);
  for (const inp of [since, until, bIn])
    inp.addEventListener("keydown", (e) => {
      if (e.key === "Enter") apply(); });
  rSel.addEventListener("change", apply);
  filters.append(since, until, rSel, bIn, go);
  root.appendChild(filters);
  const qp = new URLSearchParams();
  for (const k of ["since", "until", "repo", "branch"])
    if (params.get(k)) qp.set(k, params.get(k));
  const d = await api(`/api/dispatch?${qp}`, { signal, ws: wsId });
  const p = el("div", "panel");
  p.appendChild(el("h3", null, "Recorded activity"));
  p.appendChild(el("p", "hint",
    "Deterministic recap of recorded checkpoints — generated" +
    " locally with no AI call. For an AI-written summary, plan and" +
    " explicitly run a dispatch workflow from the Workflows page."));
  const sc = d.scope || {};
  p.appendChild(el("div", "cap",
    `window ${sc.since || "?"} → ${sc.until || "?"}` +
    ` · repo ${sc.repo_id ? sc.repo_id.slice(0, 10) : "all"}` +
    ` · branch ${sc.branch || "all"}`));
  const srcIds = d.source_ids || [];
  if (srcIds.length === 0)
    p.appendChild(el("div", "empty",
      "No checkpoints recorded in this window."));
  else {
    p.appendChild(el("pre", "code-view", d.markdown));
    const acts = el("div", "filters");
    acts.appendChild(copyBtn(d.markdown));
    const url = URL.createObjectURL(
      new Blob([d.markdown], { type: "text/markdown" }));
    const dl = el("a", "button", "Download markdown");
    dl.href = url;
    dl.download = "partial-dispatch.md";
    acts.appendChild(dl);
    setTimeout(() => URL.revokeObjectURL(url), 60000);
    p.appendChild(acts);
    if (d.truncated)
      p.appendChild(el("div", "cap",
        "⚠ Truncated at 2000 checkpoints — narrow the date window" +
        " or the repo/branch scope."));
    const det = el("details");
    det.appendChild(el("summary", "hint",
      `Source checkpoints (${srcIds.length})`));
    for (const cid of srcIds) {
      const a = el("a", "mono blocklink", cid.slice(0, 12));
      a.href = `#/checkpoints/${cid}`;
      det.appendChild(a);
    }
    p.appendChild(det);
  }
  root.appendChild(p);
}

async function vWorkflows(root, params, signal) {
  const wsId = currentWs && currentWs.id;
  setCrumb("Workflows");
  setNav("workflows");
  const status = await api("/api/memory/status",
                           { signal, ws: wsId });
  const repos = await repoOptions(signal, wsId);
  const filters = el("div", "filters");
  const fSel = el("select");
  fSel.setAttribute("aria-label", "Repository");
  fSel.appendChild(new Option("All repos", ""));
  for (const r of repos) fSel.appendChild(new Option(r.name, r.id));
  fSel.value = params.get("repo") || "";
  fSel.addEventListener("change", () => {
    const p2 = new URLSearchParams();
    if (fSel.value) p2.set("repo", fSel.value);
    location.hash = `#/workflows?${p2}`;
  });
  filters.appendChild(fSel);
  root.appendChild(filters);
  const wqp = new URLSearchParams();
  if (params.get("repo")) wqp.set("repo", params.get("repo"));
  const d = await api(`/api/workflows?${wqp}`, { signal, ws: wsId });
  const p = el("div", "panel");
  p.appendChild(el("h2", null, "Workflow runs"));
  p.appendChild(el("p", "hint small",
    "A workflow run assembles a bounded evidence packet. Planning" +
    " saves the packet locally — no provider or agent is contacted." +
    " Running sends the packet to the configured AI provider after" +
    " your explicit confirmation; no agents run on this server."));
  if (!d.items.length)
    p.appendChild(el("div", "empty", "No workflow runs yet."));
  else
    p.appendChild(table(
      ["Run", "Kind", "Status", "Created"],
      d.items.map((r) => {
        const a = el("a", null, r.id.slice(0, 12));
        a.href = `#/workflows/${r.id}`;
        return [a, r.kind, statusPill(r.status),
                fmtDate(r.created_at)];
      })));
  root.appendChild(p);

  const form = el("div", "panel");
  form.appendChild(el("h3", null, "Plan a workflow"));
  const kindSel = el("select");
  kindSel.setAttribute("aria-label", "Workflow kind");
  for (const k of ["ask", "review", "investigate", "dispatch"])
    kindSel.appendChild(new Option(k, k));
  const wRepo = el("select");
  wRepo.setAttribute("aria-label", "Repository");
  wRepo.appendChild(new Option("All repos", ""));
  for (const r of repos) wRepo.appendChild(new Option(r.name, r.id));
  const qIn = el("input");
  qIn.className = "wide";
  qIn.placeholder = "Question (ask/investigate)";
  qIn.setAttribute("aria-label", "Workflow question");
  const scope = el("div", "filters");
  const wSince = el("input");
  wSince.type = "date";
  wSince.setAttribute("aria-label", "Dispatch since date");
  const wUntil = el("input");
  wUntil.type = "date";
  wUntil.setAttribute("aria-label", "Dispatch until date");
  const wBranch = el("input");
  wBranch.placeholder = "branch";
  wBranch.setAttribute("aria-label", "Dispatch branch");
  const scopeNote = el("span", "hint small",
    "(dispatch scope only)");
  scope.append(wSince, wUntil, wBranch, scopeNote);
  const runCb = el("input");
  runCb.type = "checkbox";
  runCb.setAttribute("aria-label", "Run with AI provider");
  const runLab = el("label", "cap");
  runLab.appendChild(runCb);
  runLab.appendChild(document.createTextNode(
    " Run with AI provider (sends bounded evidence externally;"
    + " paid request)"));
  if (!status.external_ai_enabled || !status.provider_configured) {
    runCb.disabled = true;
    runLab.appendChild(el("div", "cap",
      status.provider_configured
        ? "External AI is disabled; an owner can enable it under"
          + " Memory."
        : "No AI provider is configured on the server"
          + " (PARTIAL_OPENAI_API_KEY)."));
  }
  const btn = el("button", "primary", "Submit");
  btn.addEventListener("click", async () => {
    if (runCb.checked && !window.confirm(
      "Run this workflow with the configured AI provider?"
      + " Bounded evidence is sent externally (paid request)."))
      return;
    btn.disabled = true;
    try {
      const out = await api("/api/workflows", {
        method: "POST", ws: wsId, signal,
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          kind: kindSel.value, query: qIn.value,
          repo_id: wRepo.value || null,
          run: runCb.checked,
          ...(wSince.value ? { since: wSince.value } : {}),
          ...(wUntil.value ? { until: wUntil.value } : {}),
          ...(wBranch.value.trim()
            ? { branch: wBranch.value.trim() } : {}) }) });
      showErr(`workflow ${out.status}: ${out.id.slice(0, 12)}`);
      route();
    } catch (err) {
      if (isAbort(err)) return;
      showErr(err.message);
      btn.disabled = false;
    }
  });
  if (canWrite()) {
    form.append(kindSel, wRepo, qIn, scope, runLab, btn);
    root.appendChild(form);
  } else {
    root.appendChild(el("div", "cap",
      demoMode()
        ? "Demo workspace is read-only."
        : "Starting workflows requires the member role."));
  }
}

async function vWorkflowDetail(root, id, signal) {
  const wsId = currentWs && currentWs.id;
  setCrumb("Workflow run");
  setNav("workflows");
  const d = await api(`/api/workflows/${encodeURIComponent(id)}`,
                      { signal, ws: wsId });
  const r = d.run;
  const p = el("div", "panel");
  const h = el("h2");
  h.appendChild(document.createTextNode(`${r.kind} `));
  h.appendChild(statusPill(r.status));
  p.appendChild(h);
  const semantics = {
    planned: "Planned — a bounded evidence packet was saved" +
      " locally; no external request was made.",
    running: "Running — this page polls until the run finishes.",
    completed: "Completed — the report below was validated against" +
      " the evidence packet.",
    partial: "Partial — a report was produced but one or more" +
      " reviewers/agents failed; see the errors below.",
    error: "Error — the run failed; see the error below.",
    interrupted: "Interrupted — the server or CLI process exited" +
      " before the run finished.",
    imported: "Imported — this run arrived via a bundle and was" +
      " not executed on this server.",
  };
  if (semantics[r.status])
    p.appendChild(el("p", "hint", semantics[r.status]));
  p.appendChild(el("div", "cap",
    `${r.id} · created ${fmtDate(r.created_at)} ·` +
    ` updated ${fmtDate(r.updated_at)} ·` +
    ` sources ${(r.source_ids || []).length}`));
  const rep = r.report || {};
  const det = r.details || {};
  if (rep.error)
    p.appendChild(el("div", "notice-err", `Error: ${rep.error}`));
  for (const w of rep.warnings || [])
    p.appendChild(el("div", "cap", `warning: ${w}`));
  if (r.source_ids && r.source_ids.length) {
    const sl = el("div", "cap");
    sl.appendChild(document.createTextNode("evidence: "));
    for (const sid of r.source_ids) {
      const a = el("a", null, sid.slice(0, 12));
      a.href = "#/memory";
      a.addEventListener("click", (e) => {
        e.preventDefault(); openDoc(sid, wsId); });
      sl.appendChild(a);
      sl.appendChild(document.createTextNode(" "));
    }
    p.appendChild(sl);
  }
  const rerrs = det.reviewer_errors || [];
  if (rerrs.length) {
    p.appendChild(el("h3", null, "Reviewer errors"));
    p.appendChild(table(["Agent", "Error"],
      rerrs.map((e) => [e.agent || "?", e.error || ""])));
  }
  p.appendChild(el("h3", null, "Report"));
  p.appendChild(el("pre", "code-view",
    JSON.stringify(rep, null, 2)));
  if (Object.keys(det).length) {
    const dd = el("details");
    dd.appendChild(el("summary", "hint", "Run details"));
    dd.appendChild(el("pre", "code-view",
      JSON.stringify(det, null, 2)));
    p.appendChild(dd);
  }
  root.appendChild(p);
  if (r.status === "running") {
    const alive = () => !signal.aborted;
    setTimeout(async () => {
      if (!alive()) return;
      try {
        const d2 = await api(
          `/api/workflows/${encodeURIComponent(id)}`,
          { signal, ws: wsId });
        if (!alive()) return;
        if (d2.run.status !== r.status) route();
        else setTimeout(() => { if (alive()) route(); }, 4000);
      } catch (_) { /* noop */ }
    }, 2000);
  }
}

async function route() {
  if (!me) return;
  const version = ++routeVersion;
  if (routeCtl) routeCtl.abort();
  routeCtl = new AbortController();
  const signal = routeCtl.signal;
  const current = () => version === routeVersion && !signal.aborted;
  closeDocOverlay();
  view.textContent = "";
  view.appendChild(el("div", "loading", "Loading…"));
  const root = el("div");
  const { parts, params } = parseHash();
  const page = parts[0] || "overview";
  try {
    if (page === "overview") await vOverview(root, signal);
    else if (page === "repos" && parts[1])
      await vRepoDetail(root, parts[1], params, signal);
    else if (page === "repos") await vRepos(root, signal);
    else if (page === "sessions" && parts[1])
      await vSessionDetail(root, parts[1], params, signal);
    else if (page === "sessions") await vSessions(root, params, signal);
    else if (page === "checkpoints" && parts[1])
      await vCheckpointDetail(root, parts[1], params, signal);
    else if (page === "checkpoints")
      await vCheckpoints(root, params, signal);
    else if (page === "search") await vSearch(root, params, signal);
    else if (page === "memory") await vMemory(root, params, signal);
    else if (page === "graph") await vGraph(root, params, signal);
    else if (page === "decisions")
      await vDecisions(root, params, signal);
    else if (page === "dispatch")
      await vDispatch(root, params, signal);
    else if (page === "workflows" && parts[1])
      await vWorkflowDetail(root, parts[1], signal);
    else if (page === "workflows")
      await vWorkflows(root, params, signal);
    else if (page === "integrations") await vIntegrations(root, signal);
    else if (page === "settings") await vSettings(root, signal);
    else await vOverview(root, signal);
  } catch (err) {
    if (err.name === "AbortError" || !current()) return;
    root.textContent = "";
    const p = el("div", "panel");
    p.appendChild(el("div", "notice-err",
      `Could not load: ${err.message}`));
    root.appendChild(p);
  }
  if (!current()) return;
  view.textContent = "";
  view.appendChild(root);
  view.focus({ preventScroll: true });
}

async function boot() {
  try {
    authStatus = await api("/api/auth/status");
  } catch (_) {
    authStatus = null;
  }
  try {
    me = await api("/api/me");
  } catch (_) {
    me = null;
    showLogin(authStatus && !authStatus.initialized
      ? "setup" : "signin");
    return;
  }
  pickWorkspace();
  showApp();
  demoBanner.hidden = !me.demo;
  logoutBtn.hidden = !!me.demo;
  renderWsSelect();
  document.getElementById("user-label").textContent = me.demo
    ? "Demo workspace"
    : `${me.user.name} · ${currentWs ? currentWs.role : ""}`;
  document.getElementById("sys-status").textContent =
    `v${me.version}${me.demo ? " · demo" : ""}`;
  if (!location.hash) location.hash = "#/overview";
  else route();
}

boot();
