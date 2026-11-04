/* VPeeN "Aurora" web UI — client logic (vanilla JS, no dependencies).
 *
 * Talks to the local bridge (vpeen/webui.py):
 *   GET  /api/state          initial snapshot
 *   GET  /api/poll?since=N   long-poll UI events
 *   POST /api/connect | /api/disconnect | /api/select | /api/pings
 *   POST /api/settings
 */

"use strict";

const $ = (id) => document.getElementById(id);

/* ------------------------------------------------------------ state */
const S = {
  phase: "disconnected",
  mode: "proxy",
  region: null,
  selected: "optimal",
  locations: [],
  pings: {},
  settings: {},
  direct_ip: null,
  exit_ip: null,
  stats: { up: 0, down: 0, conns: 0, active: 0, failed: 0 },
  connected_since: null,
  seq: 0,
  native_window: true,
  version: "",
  listeners: {},
  lastDown: 0,
  lastUp: 0,
  lastDownTs: 0,
  renderedLocations: "",
  connectMsgTimer: null,
};

const CONNECTING_MSGS = [
  "Requesting anonymous access token…",
  "Fetching the free server list…",
  "Negotiating a secure tunnel…",
  "Verifying the exit IP…",
];

/* ------------------------------------------------------------ helpers */
async function api(path, body) {
  const opts = body
    ? { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body) }
    : {};
  const r = await fetch(path, opts);
  return r.json();
}

function fmtBytes(n) {
  if (!n || n < 1) return "0 B";
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return (i === 0 ? n : n.toFixed(n >= 100 ? 0 : n >= 10 ? 1 : 2)) + " " + u[i];
}

function fmtRate(bps) {
  if (bps == null) return "—";
  if (bps < 1) return "0 B/s";
  return fmtBytes(bps) + "/s";
}

function fmtDur(sec) {
  sec = Math.max(0, Math.floor(sec || 0));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60),
        s = sec % 60;
  const p = (x) => String(x).padStart(2, "0");
  return h > 0 ? `${h}:${p(m)}:${p(s)}` : `${p(m)}:${p(s)}`;
}

function pingLevel(ms) {
  if (ms == null) return 0;
  if (ms < 80) return 4;
  if (ms < 150) return 3;
  if (ms < 250) return 2;
  return 1;
}

function regionLabel(region) {
  if (region === "optimal" || !region) return "Optimal Location";
  const l = S.locations.find((x) => x.region === region);
  return l ? `${l.city}, ${l.country}` : region;
}

function toast(msg, kind = "info", ms = 3400) {
  const box = $("toasts");
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.innerHTML = `<span class="t-dot"></span><span></span>`;
  el.lastElementChild.textContent = msg;
  box.appendChild(el);
  setTimeout(() => {
    el.classList.add("leaving");
    setTimeout(() => el.remove(), 380);
  }, ms);
}

function setBody(phase) {
  document.body.dataset.phase = phase;
  document.body.dataset.mode = S.mode;
}

/* ------------------------------------------------------------ render */
function renderPhase() {
  const phase = S.phase;
  setBody(phase);
  const title = $("status-title");
  const sub = $("status-sub");
  const pill = $("net-label");
  const ring = $("ring-progress");

  if (phase === "connected") {
    const tun = S.mode === "tunnel";
    title.textContent = tun ? "Protected · VPN" : "Protected";
    const label = regionLabel(S.region);
    sub.textContent = tun
      ? `All device traffic is routed through ${label}`
      : `Encrypted tunnel active — ${label}`;
    pill.textContent = "Protected";
    pill.style.color = "#d7fbee";
  } else if (phase === "connecting") {
    title.textContent = "Connecting…";
    sub.textContent = CONNECTING_MSGS[0];
    startConnectingMsgs();
    pill.textContent = "Connecting";
    pill.style.color = "";
  } else if (phase === "error") {
    title.textContent = "Something went wrong";
    sub.textContent = S.lastError || "Unknown error — check the logs";
    pill.textContent = "Error";
    pill.style.color = "#fda4b1";
  } else {
    title.textContent = "Not Protected";
    sub.textContent = S.direct_ip
      ? `Your traffic is exposed (${S.direct_ip}) — press connect to secure it`
      : "Your traffic is exposed — press connect to secure it";
    pill.textContent = "Offline";
    pill.style.color = "";
    stopConnectingMsgs();
  }
  if (phase !== "connecting") stopConnectingMsgs();

  // progress ring dashoffset is CSS-driven via data-phase, but "error"
  // needs the arc visible:
  ring.style.opacity = phase === "error" ? ".9" : "";

  renderSelection();
  renderStats();
}

function startConnectingMsgs() {
  stopConnectingMsgs();
  let i = 1;
  S.connectMsgTimer = setInterval(() => {
    if (S.phase !== "connecting") return stopConnectingMsgs();
    $("status-sub").textContent =
      CONNECTING_MSGS[i % CONNECTING_MSGS.length];
    i++;
  }, 1900);
}
function stopConnectingMsgs() {
  if (S.connectMsgTimer) clearInterval(S.connectMsgTimer);
  S.connectMsgTimer = null;
}

function renderSelection() {
  const city = $("loc-city"), country = $("loc-country");
  if (S.selected === "optimal" || !S.selected) {
    city.textContent = "Optimal Location";
    country.textContent = "Fastest free server";
    setHeroFlag(null);
  } else {
    const l = S.locations.find((x) => x.region === S.selected);
    city.textContent = l ? l.city : S.selected;
    country.textContent = l ? l.country : "";
    setHeroFlag(l ? l.cc : null);
  }
  $("optimal-card").classList.toggle("selected", S.selected === "optimal");
  // card selection states
  document.querySelectorAll(".loc-card").forEach((c) => {
    c.classList.toggle("selected", c.dataset.region === S.selected);
    c.classList.toggle("active-conn",
                       c.dataset.region === S.region && S.phase === "connected");
  });
}

function setHeroFlag(cc) {
  const box = $("loc-flag");
  if (cc) {
    box.innerHTML =
      `<img src="/flags/${encodeURIComponent(cc)}.png" alt="${cc.toUpperCase()}">`;
  } else {
    box.innerHTML =
      `<svg viewBox="0 0 24 24" class="globe" aria-hidden="true">
         <circle cx="12" cy="12" r="9"/>
         <path d="M3 12h18M12 3a13.5 13.5 0 0 1 0 18M12 3a13.5 13.5 0 0 0 0 18"/>
       </svg>`;
  }
}

function renderStats() {
  const st = S.stats || {};
  const down = st.down || 0, up = st.up || 0;
  setStat("stat-down", fmtBytes(down));
  setStat("stat-up", fmtBytes(up));

  // rates (per second deltas)
  const now = Date.now();
  if (S.phase === "connected") {
    if (S.lastDownTs) {
      const dt = (now - S.lastDownTs) / 1000;
      if (dt > 0.4) {
        $("stat-down-note").textContent = fmtRate(Math.max(0, (down - S.lastDown) / dt));
        $("stat-up-note").textContent = fmtRate(Math.max(0, (up - S.lastUp) / dt));
        S.lastDown = down; S.lastUp = up; S.lastDownTs = now;
      }
    } else {
      S.lastDown = down; S.lastUp = up; S.lastDownTs = now;
    }
    const dur = S.connected_since ? (Date.now() / 1000 - S.connected_since) : 0;
    $("stat-time").textContent = fmtDur(dur);
    // v4.6.0: "N warm" shows the pre-warmed TLS pool size - the connections
    // that skip the full handshake and start moving data in one round-trip.
    // v4.7.0: "N rescued" counts hedged connects - handshakes a parallel
    // backup saved from a stalled server (tail-latency protection).
    const warmTxt = st.warm ? ` · ${st.warm} warm` : "";
    const hedgeTxt = st.hedges ? ` · ${st.hedges} rescued` : "";
    $("stat-time-note").textContent =
      `${st.conns || 0} conns · ${st.active || 0} active${warmTxt}${hedgeTxt}`;
    $("stat-ip").textContent = S.exit_ip || "…";
    $("stat-ip-note").textContent = "exit IP · click to copy";
  } else {
    $("stat-down-note").textContent = "";
    $("stat-up-note").textContent = "";
    $("stat-time").textContent = "00:00";
    $("stat-time-note").textContent = "not connected";
    $("stat-ip").textContent = S.direct_ip || "…";
    $("stat-ip-note").textContent = "your real IP";
    S.lastDownTs = 0;
  }
}

function setStat(id, val) {
  const el = $(id);
  if (el.textContent !== val) {
    el.textContent = val;
    el.classList.remove("flash");
    void el.offsetWidth;         // restart animation
    el.classList.add("flash");
  }
}

function renderPings() {
  // optimal card: lowest ping
  const vals = Object.values(S.pings).filter((v) => v != null);
  const best = vals.length ? Math.min(...vals) : null;
  renderPingInto($("optimal-ping"), best);

  document.querySelectorAll(".loc-card").forEach((card) => {
    const ms = S.pings[card.dataset.region];
    renderPingInto(card.querySelector(".lc-ping"), ms);
  });
}

function renderPingInto(box, ms) {
  if (!box) return;
  const bars = box.querySelector(".ping-bars");
  const label = box.querySelector(".ms");
  if (bars) bars.dataset.level = pingLevel(ms);
  if (label) label.textContent = ms == null ? "—" : ms + " ms";
}

function renderLocations() {
  const list = $("loc-list");
  const q = ($("search").value || "").trim().toLowerCase();
  const key = JSON.stringify(S.locations) + "|" + q;
  if (key === S.renderedLocations) { renderPings(); return; }
  S.renderedLocations = key;

  list.textContent = "";

  for (const l of S.locations) {
    const hay = `${l.city} ${l.country} ${l.cc} ${l.region}`.toLowerCase();
    if (q && !hay.includes(q)) continue;

    const card = document.createElement("div");
    card.className = "loc-card";
    card.dataset.region = l.region;
    card.innerHTML = `
      <span class="lc-flag"><img src="/flags/${encodeURIComponent(l.cc)}.png"
          alt="${l.cc.toUpperCase()}"></span>
      <span class="lc-text">
        <span class="lc-city"></span>
        <span class="lc-country"></span>
      </span>
      <span class="lc-ping">
        <span class="ping-bars" data-level="0"><i></i><i></i><i></i><i></i></span>
        <span class="ms">—</span>
      </span>
      <span class="lc-check"><svg viewBox="0 0 24 24"><path d="M20 6L9 17l-5-5"/></svg></span>`;
    card.querySelector(".lc-city").textContent = l.city;
    card.querySelector(".lc-country").textContent = l.country;
    card.addEventListener("click", () => pickLocation(l.region));
    list.appendChild(card);
  }

  if (!list.children.length) {
    const empty = document.createElement("div");
    empty.className = "loc-card";
    empty.style.cursor = "default";
    empty.innerHTML = `<span class="lc-text">
        <span class="lc-city" style="color:var(--text-faint)">No matching locations</span>
        <span class="lc-country"></span></span>`;
    list.appendChild(empty);
  }

  $("free-count").textContent = `${S.locations.length} free`;
  $("list-caption").textContent =
    `Free locations · ${S.locations.length}`;
  renderPings();
  renderSelection();
}

/* ------------------------------------------------------------ actions */
async function togglePower() {
  if (S.phase === "connected" || S.phase === "connecting") {
    api("/api/disconnect", {});
    toast("Disconnecting…", "info", 2200);
  } else {
    const r = await api("/api/connect", { region: S.selected });
    if (!r.ok) toast("Could not start the connection", "err");
  }
}

async function pickLocation(region) {
  const was = S.selected;
  S.selected = region;
  renderSelection();
  await api("/api/select", { region });
  if (S.phase === "connected" || S.phase === "connecting") {
    toast(`Switching to ${regionLabel(region)}…`, "info");
    await api("/api/connect", { region });
  } else {
    toast(`Selected ${regionLabel(region)}`, "ok", 2200);
    closeSheets();
  }
}

function pickOptimal() {
  pickLocation("optimal");
}

/* ------------------------------------------------------------ sheets */
function openSheet(name) {
  document.body.classList.add("sheet-open");
  document.body.dataset.sheet = name;
}
function closeSheets() {
  document.body.classList.remove("sheet-open");
  delete document.body.dataset.sheet;
}
function toggleSheet(name) {
  (document.body.dataset.sheet === name) ? closeSheets() : openSheet(name);
}

function openModal() { document.body.dataset.modal = "settings"; loadSettingsForm(); }
function closeModal() {
  delete document.body.dataset.modal;
  $("set-note").textContent = "";
}

/* ------------------------------------------------------------ logs */
function appendLog(l) {
  const box = $("log-lines");
  const near = box.scrollHeight - box.scrollTop - box.clientHeight < 60;
  const div = document.createElement("div");
  div.className = `log-line ${l.level || "info"}`;
  const ts = document.createElement("span"); ts.className = "log-ts";
  ts.textContent = l.ts || "";
  const lv = document.createElement("span"); lv.className = "log-lv";
  lv.textContent = (l.level || "info").toUpperCase();
  const tx = document.createElement("span"); tx.className = "log-tx";
  tx.textContent = l.line || "";
  div.append(ts, lv, tx);
  box.appendChild(div);
  while (box.children.length > 400) box.firstChild.remove();
  if (near) box.scrollTop = box.scrollHeight;
}

/* ------------------------------------------------------------ settings */
function loadSettingsForm() {
  const c = S.settings;
  $("set-bind").value = c.bind || "127.0.0.1";
  $("set-socks").value = c.socks_port ?? 1080;
  $("set-http").value = c.http_port ?? 8080;
  $("set-autoproxy").checked = !!c.auto_system_proxy;
  $("set-tunneldns").checked = !!c.tunnel_dns;
  $("set-tunfallback").checked = c.tun_fallback !== false;
  $("set-mtu").value = c.tunnel_mtu ?? 1500;
  $("set-sweep").checked = c.tunnel_sweep !== false;
  $("set-note").textContent = "";
}

async function saveSettingsForm() {
  const patch = {
    bind: $("set-bind").value.trim() || "127.0.0.1",
    socks_port: parseInt($("set-socks").value, 10),
    http_port: parseInt($("set-http").value, 10),
    auto_system_proxy: $("set-autoproxy").checked,
    tunnel_dns: $("set-tunneldns").checked,
    tun_fallback: $("set-tunfallback").checked,
    tunnel_mtu: parseInt($("set-mtu").value, 10),
    tunnel_sweep: $("set-sweep").checked,
  };
  const r = await api("/api/settings", patch);
  const note = $("set-note");
  if (r.ok) {
    note.className = "set-note ok";
    note.textContent = "Settings saved.";
    setTimeout(closeModal, 650);
  } else {
    note.className = "set-note";
    note.textContent = (r.errors || ["invalid values"]).join(" · ");
  }
}

function syncBottomSwitches() {
  const c = S.settings || {};
  $("in-system").checked = !!c.auto_system_proxy;
  $("in-tunnel").checked = !!c.tunnel_enabled;
  const tun = $("sw-tunnel");
  tun.title = S.tunnel_available
    ? "Route ALL device traffic through a VPN tunnel (needs admin)"
    : "Tunnel binaries not found on this system — proxy mode only";
}

function updateFootMeta() {
  const c = S.settings || {};
  $("listeners-meta").textContent =
    `SOCKS5 :${c.socks_port ?? 1080} · HTTP :${c.http_port ?? 8080}`;
}

/* ------------------------------------------------------------ events */
function applyEvent(ev) {
  switch (ev.t) {
    case "phase": {
      S.phase = ev.phase;
      if (ev.mode) S.mode = ev.mode;
      if (ev.region) S.region = ev.region;
      if (ev.phase === "connected") {
        if (!S.connected_since) S.connected_since = Date.now() / 1000;
        S.lastDownTs = 0;
        renderPhase();
      } else if (ev.phase === "disconnected") {
        S.region = null;
        S.mode = "proxy";      // v4.4.0: stale data-mode="tunnel" styling
        S.connected_since = null;
        S.exit_ip = null;
        renderPhase();
      } else if (ev.phase === "error") {
        S.lastError = ev.detail || "Unknown error";
        renderPhase();
        toast(S.lastError, "err", 5000);
      } else {
        renderPhase();
      }
      break;
    }
    case "log":
      appendLog(ev);
      if (ev.level === "err") toast(ev.line, "err", 5000);
      break;
    case "stats":
      S.stats = ev;
      renderStats();
      break;
    case "exit_ip":
      S.exit_ip = ev.ip;
      renderStats();
      break;
    case "direct_ip":
      S.direct_ip = ev.ip;
      if (S.phase === "disconnected") renderPhase();
      renderStats();
      break;
    case "locations":
      S.locations = ev.locations || [];
      if (!S.locations.length && ev.error) {
        $("list-caption").textContent = "Free locations";
        $("sheet-foot").textContent = `Could not load locations — ${ev.error}`;
      } else {
        $("sheet-foot").textContent =
          "Premium locations are hidden — unavailable on the free tier.";
      }
      renderLocations();
      renderSelection();
      break;
    case "pings":
      S.pings = ev.pings || {};
      renderPings();
      break;
    case "selected":
      S.selected = ev.region;
      renderSelection();
      break;
    case "settings":
      S.settings = ev.settings || {};
      syncBottomSwitches();
      updateFootMeta();       // v4.4.0: footer showed stale ports after a save
      break;
    case "system_proxy":
      break;
  }
}

/* ------------------------------------------------------------ polling */
let pollBusy = false;
async function poll() {
  if (pollBusy) return;
  pollBusy = true;
  try {
    const r = await api(`/api/poll?since=${S.seq}`);
    if (r.seq != null) S.seq = r.seq;
    (r.events || []).forEach(applyEvent);
  } catch (e) {
    await new Promise((res) => setTimeout(res, 1500));
  } finally {
    pollBusy = false;
  }
  setTimeout(poll, 60);
}

/* ------------------------------------------------------------ boot */
async function boot() {
  let st;
  try {
    st = await api("/api/state");
  } catch (e) {
    document.body.innerHTML =
      `<div style="display:flex;height:100vh;align-items:center;
        justify-content:center;color:#98a4c2;font:14px sans-serif">
        Cannot reach the VPeeN service.</div>`;
    return;
  }
  Object.assign(S, {
    phase: st.phase,
    mode: st.mode,
    region: st.region,
    selected: st.selected || "optimal",
    locations: st.locations || [],
    pings: st.pings || {},
    settings: st.settings || {},
    direct_ip: st.direct_ip,
    exit_ip: st.exit_ip,
    stats: st.stats || {},
    connected_since: st.connected_since,
    native_window: !!st.native_window,
    version: st.version,
    tunnel_available: !!st.tunnel_available,
    seq: st.seq || 0,
  });

  $("ver-meta").textContent = `v${st.version}`;
  updateFootMeta();
  $("log-lines").textContent = "";
  (st.logs || []).forEach(appendLog);

  renderPhase();
  renderLocations();
  syncBottomSwitches();
  poll();

  // quit affordance for browser-fallback mode
  if (!S.native_window) {
    const b = document.createElement("button");
    b.className = "btn ghost";
    b.style.marginRight = "auto";
    b.textContent = "Shut down VPeeN";
    b.addEventListener("click", async () => {
      await api("/api/quit", {});
      toast("Shutting down…", "info", 4000);
    });
    $("btn-settings-cancel").parentNode.insertBefore(b, $("btn-settings-cancel"));
  }
}

/* 1 Hz local ticker: session clock + rate decay */
setInterval(() => {
  if (S.phase === "connected" && S.connected_since) {
    $("stat-time").textContent =
      fmtDur(Date.now() / 1000 - S.connected_since);
  }
  if (S.phase !== "connected") { renderStats(); }
}, 1000);

/* ------------------------------------------------------------ wiring */
$("orb").addEventListener("click", togglePower);
$("orb").addEventListener("keydown", (e) => {
  if (e.key === "Enter" || e.key === " ") { e.preventDefault(); togglePower(); }
});

$("loc-select").addEventListener("click", () => openSheet("servers"));
$("loc-select").addEventListener("keydown", (e) => {
  if (e.key === "Enter") openSheet("servers");
});

$("btn-servers-close").addEventListener("click", closeSheets);
$("scrim").addEventListener("click", closeSheets);
$("btn-refresh").addEventListener("click", async () => {
  toast("Measuring latencies…", "info", 1800);
  await api("/api/pings", {});
});
$("optimal-card").addEventListener("click", pickOptimal);
$("search").addEventListener("input", renderLocations);

$("btn-logs").addEventListener("click", () => toggleSheet("logs"));
$("btn-logs-close").addEventListener("click", closeSheets);
$("btn-logs-copy").addEventListener("click", async () => {
  const txt = Array.from($("log-lines").children)
    .map((l) => `${l.querySelector(".log-ts").textContent} `
              + `${l.querySelector(".log-lv").textContent.padEnd(5)} `
              + `${l.querySelector(".log-tx").textContent}`)
    .join("\n");
  try { await navigator.clipboard.writeText(txt); toast("Log copied", "ok", 2000); }
  catch { toast("Clipboard unavailable", "err"); }
});

$("btn-settings").addEventListener("click", openModal);
$("btn-settings-close").addEventListener("click", closeModal);
$("btn-settings-cancel").addEventListener("click", closeModal);
$("btn-settings-save").addEventListener("click", saveSettingsForm);

$("stat-ip-card").addEventListener("click", async () => {
  const ip = $("stat-ip").textContent;
  if (!ip || ip === "…" || ip === "—") return;
  try { await navigator.clipboard.writeText(ip); toast(`${ip} copied`, "ok", 1800); }
  catch { toast("Clipboard unavailable", "err"); }
});

$("in-system").addEventListener("change", async (e) => {
  S.settings.auto_system_proxy = e.target.checked;
  await api("/api/settings", { auto_system_proxy: e.target.checked });
});
$("in-tunnel").addEventListener("change", async (e) => {
  const on = e.target.checked;
  if (on && !S.tunnel_available) {
    e.target.checked = false;
    toast("Tunnel mode is not available on this system", "err", 4200);
    return;
  }
  S.settings.tunnel_enabled = on;
  await api("/api/settings", { tunnel_enabled: on });
  if (S.phase === "connected" || S.phase === "connecting") {
    // parity with the v4.3 Tk GUI: applying a mode change live reconnects
    toast("Tunnel mode changed — reconnecting…", "info", 2600);
    await api("/api/connect", { region: S.selected });
  } else {
    toast(on ? "VPN tunnel enabled for the next connect"
             : "VPN tunnel disabled", "info", 2600);
  }
});

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    if (document.body.dataset.modal) closeModal();
    else closeSheets();
  }
});

window.addEventListener("error", (e) => {
  console.error(e);
});

boot();
