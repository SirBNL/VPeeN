/* VPeeN Bridge popup logic. */
"use strict";

const $ = (id) => document.getElementById(id);
const DEFAULTS = { enabled: false, host: "127.0.0.1", port: 1080 };

const toggle = $("toggle");
const hostEl = $("host");
const portEl = $("port");
const statusEl = $("status");
const hintEl = $("hint");
const dotEl = $("dot");
const footEl = $("foot");

function fmtCfg(c) {
  return `socks5://${c.host}:${c.port}`;
}

async function load() {
  const c = { ...DEFAULTS, ...(await chrome.storage.local.get(["enabled", "host", "port"])) };
  toggle.checked = !!c.enabled;
  hostEl.value = c.host;
  portEl.value = c.port;
  render(c);
}

function render(c) {
  if (c.enabled) {
    statusEl.textContent = "Chrome is routed through VPeeN";
    statusEl.className = "status on";
    hintEl.textContent = `Proxy: ${fmtCfg(c)} — everything except localhost & LAN goes through the tunnel.`;
    dotEl.className = "dot ok";
    footEl.textContent = "Remote DNS active (hostnames resolve on the exit side)";
  } else {
    statusEl.textContent = "Chrome uses its normal connection";
    statusEl.className = "status";
    hintEl.textContent = "Start VPeeN, connect, then flip the switch above.";
    dotEl.className = "dot";
    footEl.textContent = "VPeeN must be connected first";
  }
}

async function save(c) {
  await chrome.storage.local.set(c);
  render(c);
}

toggle.addEventListener("change", () => {
  save({ enabled: toggle.checked });
});

async function applyCfg() {
  const host = (hostEl.value || "").trim() || DEFAULTS.host;
  const port = Math.min(65535, Math.max(1, parseInt(portEl.value, 10) || DEFAULTS.port));
  hostEl.value = host;
  portEl.value = port;
  await save({ host, port });
}

hostEl.addEventListener("change", applyCfg);
portEl.addEventListener("change", applyCfg);

load();
