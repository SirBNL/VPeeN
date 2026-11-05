/*
 * VPeeN Bridge - service worker.
 *
 * Applies (or clears) Chrome's proxy configuration from the saved state.
 * With scheme "socks5" Chrome sends HOSTNAMES to the proxy (remote DNS),
 * so browsing through VPeeN resolves names on the exit side - identical
 * behaviour to the desktop app's system-proxy mode.
 *
 * Zero network permissions, no content scripts, no analytics: the
 * extension only talks to chrome.proxy / chrome.storage, both local.
 */
"use strict";

const DEFAULTS = {
  enabled: false,
  host: "127.0.0.1",
  port: 1080,
};

// Match the desktop app's bypass list (vpeen/systemproxy.py BYPASS_LIST):
// loopback + RFC1918 stays DIRECT so local devices keep working.
const BYPASS_LIST = [
  "localhost",
  "127.0.0.1",
  "[::1]",
  "10.*",
  "172.16.*",
  "172.17.*",
  "172.18.*",
  "172.19.*",
  "172.2?.*",
  "172.30.*",
  "172.31.*",
  "192.168.*",
  "<local>",
];

async function getCfg() {
  const stored = await chrome.storage.local.get(["enabled", "host", "port"]);
  return { ...DEFAULTS, ...stored };
}

async function applyProxy() {
  const cfg = await getCfg();
  try {
    if (cfg.enabled) {
      await chrome.proxy.settings.set({
        value: {
          mode: "fixed_servers",
          rules: {
            singleProxy: {
              scheme: "socks5",
              host: String(cfg.host || "127.0.0.1"),
              port: Number(cfg.port) || 1080,
            },
            bypassList: BYPASS_LIST,
          },
        },
        scope: "regular",
      });
    } else {
      await chrome.proxy.settings.clear({ scope: "regular" });
    }
    await chrome.action.setBadgeText({ text: cfg.enabled ? "ON" : "" });
    if (cfg.enabled) {
      await chrome.action.setBadgeBackgroundColor({ color: "#22c55e" });
    }
  } catch (e) {
    await chrome.action.setBadgeText({ text: "!" });
    await chrome.action.setBadgeBackgroundColor({ color: "#ef4444" });
    console.error("VPeeN Bridge: proxy apply failed:", e);
  }
}

chrome.runtime.onInstalled.addListener(applyProxy);
chrome.runtime.onStartup.addListener(applyProxy);
chrome.storage.onChanged.addListener(applyProxy);

export {};
