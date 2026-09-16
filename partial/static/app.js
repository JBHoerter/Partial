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

async function fetchBlob(path, filename, wsId) {
  try {
    const res = await fetch(path, {
      credentials: "same-origin", headers: wsHeaders(null, wsId) });
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
      ["Session", "Agent", "Status", "Updated"],
      sessions.items.map((s) => [
        sessionLink(s), agentPill(s.agent), statusPill(s.status),
        fmtDate(s.updated_at),
      ])));
  } else {
    sp.appendChild(emptySetup());
  }
  root.appendChild(sp);

  const cp = el("div", "panel");
  cp.appendChild(el("h2", null, "Recent checkpoints"));
  if (cps.items.length) {
    cp.appendChild(table(
      ["Commit", "Message", "Branch", "Captured"],
      cps.items.map((c) => [
        checkpointLink(c), c.message || "—", c.branch || "—",
        fmtDate(c.created_at),
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
      ["Name", "Remote", "Registered"],
      d.items.map((r) => {
        const a = el("a", null, r.name);
        a.href = `#/repos/${r.id}`;
        return [a, el("span", "mono", r.remote || "—"),
                fmtDate(r.created_at)];
      })));
  }
  root.appendChild(p);
}

async function vRepoDetail(root, id, params, signal) {
  const wsId = currentWs && currentWs.id;
  setCrumb("Repository");
  setNav("repos");
  const d = await api(`/api/repos/${id}`, { signal, ws: wsId });

  const head = el("div", "panel");
  head.appendChild(el("h2", null, d.repository.name));
  const meta = el("div", null);
  meta.appendChild(el("span", "mono",
    d.repository.remote || "local only"));
  head.appendChild(meta);
  root.appendChild(head);

  const branch = params.get("branch") || "";
  const tab = params.get("tab") === "checkpoints"
    ? "checkpoints" : "sessions";
  const offset = parseInt(params.get("offset") || "0", 10) || 0;
  const limit = 50;

  const nav = (over) => {
    const p = new URLSearchParams();
    if (over.branch !== undefined ? over.branch : branch)
      p.set("branch", over.branch !== undefined ? over.branch : branch);
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
  filter.appendChild(sel);
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
  if (tab === "checkpoints") {
    const cps = await api(`/api/checkpoints?${qp}`, { signal, ws: wsId });
    if (cps.items.length) {
      panel.appendChild(table(
        ["Commit", "Message", "Branch", "Captured"],
        cps.items.map((c) => [checkpointLink(c), c.message || "—",
                              c.branch || "—", fmtDate(c.created_at)])));
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
        ["Session", "Agent", "Status", "Branch", "Updated"],
        ss.items.map((s) => [sessionLink(s), agentPill(s.agent),
                             statusPill(s.status), s.branch || "—",
                             fmtDate(s.updated_at)])));
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
      ["Session", "Agent", "Status", "Branch", "Model", "Updated"],
      d.items.map((s) => [
        sessionLink(s), agentPill(s.agent), statusPill(s.status),
        s.branch || "—", s.model || "Not reported",
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
  if (s.branch) meta.appendChild(el("span", "pill", `⎇ ${s.branch}`));
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
        credentials: "same-origin",
        headers: wsHeaders(null, wsId) });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const text = await res.text();
      copyH.textContent =
        (await copyText(text)) ? "Copied" : "Copy failed";
    } catch (_) {
      copyH.textContent = "Copy failed";
    }
    setTimeout(() => { copyH.textContent = "Copy handoff"; }, 1500);
  });
  const dlH = el("button", "button", "Download handoff");
  dlH.addEventListener("click", () =>
    fetchBlob(`/api/sessions/${id}/handoff`, "partial-handoff.md",
      wsId));
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
      location.hash = `#/sessions/${id}${p.size ? "?" + p : ""}`;
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
  const apply = () => {
    const p = new URLSearchParams();
    if (rSel.value) p.set("repo", rSel.value);
    if (bIn.value.trim()) p.set("branch", bIn.value.trim());
    location.hash = `#/checkpoints?${p}`;
  };
  rSel.addEventListener("change", apply);
  bIn.addEventListener("keydown", (e) => {
    if (e.key === "Enter") apply(); });
  const go = el("button", null, "Filter");
  go.addEventListener("click", apply);
  filters.append(rSel, bIn, go);
  root.appendChild(filters);

  const qp = new URLSearchParams({ limit: String(limit),
                                   offset: String(offset) });
  if (repo) qp.set("repo", repo);
  if (branch) qp.set("branch", branch);
  const d = await api(`/api/checkpoints?${qp}`, { signal, ws: wsId });
  const p = el("div", "panel");
  p.appendChild(el("h2", null, "Checkpoints"));
  if (!d.items.length) {
    p.appendChild(el("div", "empty", "No checkpoints captured yet."));
  } else {
    p.appendChild(table(
      ["Commit", "Message", "Branch", "Sessions", "Captured"],
      d.items.map((c) => [
        checkpointLink(c), c.message || "—", c.branch || "—",
        String((c.session_ids || []).length), fmtDate(c.created_at),
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

function renderDiff(diff, files) {
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
    for (const line of sec.lines) {
      const span = el("span");
      if (line.startsWith("+") && !line.startsWith("+++")) {
        span.className = "d-add";
      } else if (line.startsWith("-") && !line.startsWith("---")) {
        span.className = "d-del";
      } else if (line.startsWith("@@")) {
        span.className = "d-hunk";
      } else if (line.startsWith("diff ") || line.startsWith("index ") ||
                 line.startsWith("---") || line.startsWith("+++")) {
        span.className = "d-head";
      }
      span.textContent = line + "\n";
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
  root.appendChild(head);

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
        ["Session", "Agent", "Status", "Link"],
        d.sessions.map((s) => {
          const link = (c.links || []).find(
            (l) => l.session_id === s.id);
          return [sessionLink(s), agentPill(s.agent),
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
    p.appendChild(renderDiff(c.diff, c.files));
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
          credentials: "same-origin",
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
        credentials: "same-origin",
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
    fetchBlob("/api/export", "partial-export.json", wsId));
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
          method: "POST", credentials: "same-origin",
          headers: wsHeaders(
            { "Content-Type": "application/json" }, wsId),
          body: JSON.stringify({ name: nwName.value.trim() }),
        });
        const data = await res.json().catch(() => null);
        if (!res.ok) {
          throw new Error((data && data.error) || `HTTP ${res.status}`);
        }
        me = await api("/api/me", {
          headers: wsHeaders(null, wsId) });
        pickWorkspace();
        renderWsSelect();
        nwStatus.className = "notice-ok";
        nwStatus.textContent = "Workspace created.";
        nwName.value = "";
      } catch (err) {
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
          method: "POST", credentials: "same-origin",
          headers: wsHeaders(
            { "Content-Type": "application/json" }, wsId),
          body: JSON.stringify({ token: tok, email: me.user.email }),
        });
        const data = await res.json().catch(() => null);
        if (!res.ok) {
          throw new Error((data && data.error) || `HTTP ${res.status}`);
        }
        me = await api("/api/me", {
          headers: wsHeaders(null, wsId) });
        pickWorkspace();
        renderWsSelect();
        ajStatus.className = "notice-ok";
        ajStatus.textContent =
          "Invitation accepted — select the workspace above.";
      } catch (err) {
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
                method: "PATCH", credentials: "same-origin",
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
                method: "DELETE", credentials: "same-origin",
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
              method: "POST", credentials: "same-origin",
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
            method: "POST", credentials: "same-origin",
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
                  method: "DELETE", credentials: "same-origin",
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
          method: "POST", credentials: "same-origin",
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

async function route() {
  if (!me) return;
  const version = ++routeVersion;
  if (routeCtl) routeCtl.abort();
  routeCtl = new AbortController();
  const signal = routeCtl.signal;
  const current = () => version === routeVersion && !signal.aborted;
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
