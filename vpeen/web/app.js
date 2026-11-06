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

/* -------------------------------------------------------- i18n (v4.9.0)
 * English + Persian (فارسی).  The dictionary is pure JSON (string
 * values, {placeholder} templates) so tests can extract and verify it.
 * fa flips the whole UI to RTL (see the [dir="rtl"] rules in style.css).
 */
const I18N = {
  "en": {
    "brand_tag": "FREE",
    "status_not_protected": "Not Protected",
    "status_protected": "Protected",
    "status_protected_vpn": "Protected · VPN",
    "status_connecting": "Connecting…",
    "status_error": "Something went wrong",
    "error_unknown": "Unknown error — check the logs",
    "sub_exposed": "Your traffic is exposed — press connect to secure it",
    "sub_exposed_ip": "Your traffic is exposed ({ip}) — press connect to secure it",
    "sub_tunnel_active": "All device traffic is routed through {label}",
    "sub_encrypted": "Encrypted tunnel active — {label}",
    "ks_lockdown_title": "Kill Switch ON",
    "ks_lockdown_sub": "All traffic is blocked — nothing leaked. Reconnect or disconnect.",
    "ks_healing_title": "Repairing…",
    "ks_healing_sub": "Kill switch engaged — traffic is blocked while the tunnel repairs itself",
    "pill_protected": "Protected",
    "pill_connecting": "Connecting",
    "pill_error": "Error",
    "pill_offline": "Offline",
    "pill_blocked": "Blocked",
    "pill_repairing": "Repairing",
    "connecting_msg_1": "Requesting anonymous access token…",
    "connecting_msg_2": "Fetching the free server list…",
    "connecting_msg_3": "Negotiating a secure tunnel…",
    "connecting_msg_4": "Verifying the exit IP…",
    "optimal_location": "Optimal Location",
    "optimal_sub": "Fastest free server",
    "stat_down": "Downloaded",
    "stat_up": "Uploaded",
    "stat_session": "Session",
    "stat_ip": "IP Address",
    "note_not_connected": "not connected",
    "note_real_ip": "your real IP",
    "note_exit_ip": "exit IP · click to copy",
    "note_conns": "{c} conns · {a} active",
    "note_warm": " · {n} warm",
    "note_rescued": " · {n} rescued",
    "switch_system": "System Proxy",
    "switch_tunnel": "VPN Tunnel",
    "sheet_locations": "Locations",
    "sheet_logs": "Connection Logs",
    "search_ph": "Search locations",
    "free_count": "{n} free",
    "list_caption": "Free locations · {n}",
    "no_match": "No matching locations",
    "foot_premium": "Premium locations are hidden — unavailable on the free tier.",
    "foot_locations_err": "Could not load locations — {err}",
    "toast_disconnecting": "Disconnecting…",
    "toast_conn_fail": "Could not start the connection",
    "toast_switching": "Switching to {label}…",
    "toast_selected": "Selected {label}",
    "toast_update_available": "Update available: v{v} — one click to install",
    "toast_restarting_upd": "Restarting VPeeN to the new version…",
    "toast_installing_upd": "Downloading & installing the update…",
    "toast_upd_fail": "Could not start the update",
    "toast_shutdown_btn": "Shut down VPeeN",
    "toast_shutting_down": "Shutting down…",
    "toast_measuring": "Measuring latencies…",
    "toast_log_copied": "Log copied",
    "toast_clipboard": "Clipboard unavailable",
    "toast_ip_copied": "{ip} copied",
    "toast_tunnel_na": "Tunnel mode is not available on this system",
    "toast_tunnel_changed": "Tunnel mode changed — reconnecting…",
    "toast_tunnel_on": "VPN tunnel enabled for the next connect",
    "toast_tunnel_off": "VPN tunnel disabled",
    "settings_title": "Settings",
    "group_interface": "Interface",
    "group_local": "Local proxy",
    "group_tunnel": "VPN tunnel",
    "group_updates": "Updates",
    "set_lang": "Language",
    "set_lang_hint": "English / فارسی",
    "set_bind": "Bind address",
    "set_socks": "SOCKS5 port",
    "set_http": "HTTP port",
    "set_autoproxy": "Auto system proxy",
    "set_autoproxy_hint": "applies on next connect",
    "set_bypass": "Bypass list",
    "set_bypass_hint": "these addresses connect direct, without the proxy",
    "set_bypass_ph": "e.g. *.ir, bank.example.com",
    "set_tunneldns": "Route DNS through the tunnel",
    "set_tunneldns_hint": "prevents DNS leaks",
    "set_tunfallback": "Fall back to proxy mode",
    "set_tunfallback_hint": "if the tunnel cannot start",
    "set_killswitch": "Kill switch",
    "set_killswitch_hint": "if the tunnel breaks, block everything instead of leaking",
    "set_mtu": "Tunnel MTU",
    "set_sweep": "Clean up crashed sessions",
    "set_sweep_hint": "sweep leftover state on start",
    "set_autoupdate": "Automatic updates",
    "set_autoupdate_hint": "check GitHub on start + every 6 h, pre-download",
    "btn_cancel": "Cancel",
    "btn_save": "Save changes",
    "settings_saved": "Settings saved.",
    "invalid_values": "invalid values",
    "upd_title": "Update VPeeN",
    "upd_check": "Check now",
    "upd_install": "Update & Restart",
    "upd_restart_to": "Restart to v{v}",
    "upd_idle": "No update check has run yet.",
    "upd_checking": "Checking GitHub for a newer release…",
    "upd_latest": "You are on the newest release (v{v}).",
    "upd_available": "v{v} is available ({size}). It downloads automatically; one click switches you over.",
    "upd_available_src": "v{v} is available. This is a source installation — update with 'git pull'.",
    "upd_downloading": "Downloading v{v}… {p}%",
    "upd_staging": "Verifying & unpacking the new build…",
    "upd_ready": "v{v} is downloaded and CHECKSUM-VERIFIED. One click switches you to it.",
    "upd_installing": "Installing v{v}…",
    "upd_restarting": "Installed v{v} — restarting VPeeN now.",
    "upd_error": "Update error: {e}",
    "upd_disabled": "Update checks are disabled in this build.",
    "title_stat_down": "Total data downloaded through the tunnel",
    "title_stat_up": "Total data uploaded through the tunnel",
    "title_stat_session": "Current session duration",
    "title_stat_ip": "Click to copy",
    "title_switch_system": "Set the system-wide proxy automatically when connecting (browsers and apps that honour it)",
    "title_switch_tunnel": "Route ALL device traffic through a VPN tunnel (needs admin)",
    "title_btn_logs": "Connection logs",
    "title_btn_doctor": "Protection self-check",
    "title_btn_settings": "Settings",
    "title_btn_refresh": "Refresh latencies",
    "title_btn_close": "Close",
    "title_btn_copy": "Copy log",
    "title_btn_ver": "VPeeN update",
    "title_optimal": "Let VPeeN pick the fastest free server",
    "error_cannot_reach": "Cannot reach the VPeeN service.",
    "btn_close": "Close",
    "doctor_title": "Protection Check",
    "doctor_run": "Run check",
    "doctor_running": "Checking…",
    "doctor_idle": "Run the check to see how protected you really are.",
    "doctor_st_ok": "OK",
    "doctor_st_warn": "Warning",
    "doctor_st_fail": "Problem",
    "doctor_done_ok": "All good.",
    "doctor_done_warn": "Some points need your attention.",
    "doctor_done_fail": "Problems found — see the red items.",
    "dr_listeners": "Local proxy listeners",
    "dr_ip_split": "Exit IP differs from your real IP",
    "dr_routing": "Traffic routing",
    "dr_dns": "DNS privacy",
    "dr_killswitch": "Kill switch",
    "dr_system": "System proxy applied",
    "dr_quic": "QUIC / UDP side channel",
    "dr_webrtc": "WebRTC advisory"
  },
  "fa": {
    "brand_tag": "رایگان",
    "status_not_protected": "بدون محافظت",
    "status_protected": "محافظت‌شده",
    "status_protected_vpn": "محافظت‌شده · VPN",
    "status_connecting": "در حال اتصال…",
    "status_error": "مشکلی پیش آمد",
    "error_unknown": "خطای نامشخص — لاگ‌ها را ببینید",
    "sub_exposed": "ترافیکتان در معرض است — برای امن‌سازی، وصل شوید",
    "sub_exposed_ip": "ترافیکتان در معرض است ({ip}) — برای امن‌سازی، وصل شوید",
    "sub_tunnel_active": "تمام ترافیک دستگاه از {label} عبور می‌کند",
    "sub_encrypted": "تونل رمزنگاری‌شده فعال است — {label}",
    "ks_lockdown_title": "کیل‌سوئیچ فعال",
    "ks_lockdown_sub": "تمام ترافیک مسدود شده — هیچ چیزی لو نرفته. دوباره وصل شوید یا قطع کنید.",
    "ks_healing_title": "در حال تعمیر…",
    "ks_healing_sub": "کیل‌سوئیچ درگیر شده — ترافیک تا تعمیر تونل مسدود است",
    "pill_protected": "محافظت‌شده",
    "pill_connecting": "در حال اتصال",
    "pill_error": "خطا",
    "pill_offline": "آفلاین",
    "pill_blocked": "مسدود",
    "pill_repairing": "در حال تعمیر",
    "connecting_msg_1": "درخواست توکن دسترسی ناشناس…",
    "connecting_msg_2": "دریافت فهرست سرورهای رایگان…",
    "connecting_msg_3": "برقراری تونل امن…",
    "connecting_msg_4": "بررسی IP خروجی…",
    "optimal_location": "بهترین لوکیشن",
    "optimal_sub": "سریع‌ترین سرور رایگان",
    "stat_down": "دانلود",
    "stat_up": "آپلود",
    "stat_session": "نشست",
    "stat_ip": "آدرس IP",
    "note_not_connected": "وصل نیستید",
    "note_real_ip": "IP واقعی شما",
    "note_exit_ip": "IP خروجی · برای کپی کلیک کنید",
    "note_conns": "{c} اتصال · {a} فعال",
    "note_warm": " · {n} آماده",
    "note_rescued": " · {n} نجات‌یافته",
    "switch_system": "پراکسی سیستم",
    "switch_tunnel": "تونل VPN",
    "sheet_locations": "لوکیشن‌ها",
    "sheet_logs": "لاگ‌های اتصال",
    "search_ph": "جست‌وجوی لوکیشن",
    "free_count": "{n} رایگان",
    "list_caption": "لوکیشن‌های رایگان · {n}",
    "no_match": "لوکیشن منطبقی نیست",
    "foot_premium": "لوکیشن‌های پرمیوم مخفی‌اند — در پلن رایگان در دسترس نیستند.",
    "foot_locations_err": "لوکیشن‌ها بارگیری نشد — {err}",
    "toast_disconnecting": "در حال قطع اتصال…",
    "toast_conn_fail": "اتصال آغاز نشد",
    "toast_switching": "در حال تغییر به {label}…",
    "toast_selected": "{label} انتخاب شد",
    "toast_update_available": "به‌روزرسانی موجود است: v{v} — با یک کلیک نصب کنید",
    "toast_restarting_upd": "در حال ری‌استارت VPeeN روی نسخهٔ جدید…",
    "toast_installing_upd": "در حال دانلود و نصب به‌روزرسانی…",
    "toast_upd_fail": "به‌روزرسانی آغاز نشد",
    "toast_shutdown_btn": "خاموش‌کردن VPeeN",
    "toast_shutting_down": "در حال خاموش‌شدن…",
    "toast_measuring": "در حال اندازه‌گیری پینگ‌ها…",
    "toast_log_copied": "لاگ کپی شد",
    "toast_clipboard": "کلیپ‌بورد در دسترس نیست",
    "toast_ip_copied": "{ip} کپی شد",
    "toast_tunnel_na": "حالت تونل روی این سیستم در دسترس نیست",
    "toast_tunnel_changed": "حالت تونل تغییر کرد — در حال اتصال مجدد…",
    "toast_tunnel_on": "تونل VPN برای اتصال بعدی فعال شد",
    "toast_tunnel_off": "تونل VPN غیرفعال شد",
    "settings_title": "تنظیمات",
    "group_interface": "رابط کاربری",
    "group_local": "پراکسی محلی",
    "group_tunnel": "تونل VPN",
    "group_updates": "به‌روزرسانی",
    "set_lang": "زبان",
    "set_lang_hint": "English / فارسی",
    "set_bind": "آدرس Bind",
    "set_socks": "پورت SOCKS5",
    "set_http": "پورت HTTP",
    "set_autoproxy": "پراکسی خودکار سیستم",
    "set_autoproxy_hint": "در اتصال بعدی اعمال می‌شود",
    "set_bypass": "لیست عبور مستقیم",
    "set_bypass_hint": "این آدرس‌ها مستقیم وصل می‌شوند، بدون پراکسی",
    "set_bypass_ph": "مثلاً *.ir, bank.example.com",
    "set_tunneldns": "DNS از داخل تونل عبور کند",
    "set_tunneldns_hint": "جلوی لو رفتن DNS را می‌گیرد",
    "set_tunfallback": "در صورت شکست، به حالت پراکسی برگرد",
    "set_tunfallback_hint": "اگر تونل بالا نیاید",
    "set_killswitch": "کیل‌سوئیچ",
    "set_killswitch_hint": "اگر تونل قطع شد، همه‌چیز مسدود شود نه لو رفتن",
    "set_mtu": "MTU تونل",
    "set_sweep": "پاک‌سازی نشست‌های کرش‌شده",
    "set_sweep_hint": "در شروع، باقی‌مانده‌ها پاک شوند",
    "set_autoupdate": "به‌روزرسانی خودکار",
    "set_autoupdate_hint": "در شروع + هر ۶ ساعت چک گیت‌هاب، دانلود از پیش",
    "btn_cancel": "انصراف",
    "btn_save": "ذخیرهٔ تغییرات",
    "settings_saved": "تنظیمات ذخیره شد.",
    "invalid_values": "مقادیر نامعتبر",
    "upd_title": "به‌روزرسانی VPeeN",
    "upd_check": "همین حالا چک کن",
    "upd_install": "به‌روزرسانی و ری‌استارت",
    "upd_restart_to": "ری‌استارت به v{v}",
    "upd_idle": "هنوز چک به‌روزرسانی انجام نشده است.",
    "upd_checking": "در حال بررسی گیت‌هاب برای نسخهٔ جدیدتر…",
    "upd_latest": "شما روی جدیدترین نسخه هستید (v{v}).",
    "upd_available": "نسخهٔ v{v} موجود است ({size}). خودکار دانلود می‌شود؛ با یک کلیک جابه‌جا می‌شوید.",
    "upd_available_src": "نسخهٔ v{v} موجود است. این یک نصب سورس است — با «git pull» به‌روزرسانی کنید.",
    "upd_downloading": "در حال دانلود v{v}… {p}٪",
    "upd_staging": "در حال راستی‌آزمایی و باز کردن نسخهٔ جدید…",
    "upd_ready": "نسخهٔ v{v} دانلود و از نظر چک‌سام تأیید شده. با یک کلیک به آن جابه‌جا شوید.",
    "upd_installing": "در حال نصب v{v}…",
    "upd_restarting": "نسخهٔ v{v} نصب شد — در حال ری‌استارت VPeeN.",
    "upd_error": "خطای به‌روزرسانی: {e}",
    "upd_disabled": "چک به‌روزرسانی در این نسخه غیرفعال است.",
    "title_stat_down": "مجموع دادهٔ دانلود‌شده از داخل تونل",
    "title_stat_up": "مجموع دادهٔ آپلود‌شده از داخل تونل",
    "title_stat_session": "مدت نشست جاری",
    "title_stat_ip": "برای کپی کلیک کنید",
    "title_switch_system": "هنگام اتصال، پراکسی سیستم به‌طور خودکار تنظیم شود (مرورگرها و برنامه‌هایی که آن را رعایت می‌کنند)",
    "title_switch_tunnel": "تمام ترافیک دستگاه از تونل VPN عبور کند (نیاز به ادمین)",
    "title_btn_logs": "لاگ‌های اتصال",
    "title_btn_doctor": "خودآزمایی محافظت",
    "title_btn_settings": "تنظیمات",
    "title_btn_refresh": "تازه‌سازی پینگ‌ها",
    "title_btn_close": "بستن",
    "title_btn_copy": "کپی لاگ",
    "title_btn_ver": "به‌روزرسانی VPeeN",
    "title_optimal": "بگذار VPeeN سریع‌ترین سرور رایگان را انتخاب کند",
    "error_cannot_reach": "ارتباط با سرویس VPeeN برقرار نشد.",
    "btn_close": "بستن",
    "doctor_title": "خودآزمایی محافظت",
    "doctor_run": "انجام بررسی",
    "doctor_running": "در حال بررسی…",
    "doctor_idle": "بررسی را اجرا کنید تا ببینید واقعاً چقدر محافظت‌شده‌اید.",
    "doctor_st_ok": "سالم",
    "doctor_st_warn": "هشدار",
    "doctor_st_fail": "مشکل",
    "doctor_done_ok": "همه‌چیز خوب است.",
    "doctor_done_warn": "چند مورد به توجه شما نیاز دارد.",
    "doctor_done_fail": "مشکل پیدا شد — موارد قرمز را ببینید.",
    "dr_listeners": "شنونده‌های پراکسی محلی",
    "dr_ip_split": "IP خروجی با IP واقعی فرق دارد",
    "dr_routing": "مسیریابی ترافیک",
    "dr_dns": "حریم خصوصی DNS",
    "dr_killswitch": "کیل‌سوئیچ",
    "dr_system": "پراکسی سیستم اعمال شده",
    "dr_quic": "کانال جانبی QUIC / UDP",
    "dr_webrtc": "نکتهٔ WebRTC"
  }
};

let LANG = "en";

function t(key, vars) {
  const dict = I18N[LANG] || I18N.en;
  let s = Object.prototype.hasOwnProperty.call(dict, key)
    ? dict[key] : I18N.en[key];
  if (s == null) return key;
  if (vars) {
    for (const k of Object.keys(vars)) {
      s = s.split("{" + k + "}").join(String(vars[k]));
    }
  }
  return s;
}

function langDir(lang) { return lang === "fa" ? "rtl" : "ltr"; }

function applyI18n() {
  document.querySelectorAll("[data-i18n]").forEach((el) => {
    el.textContent = t(el.dataset.i18n);
  });
  document.querySelectorAll("[data-i18n-ph]").forEach((el) => {
    el.placeholder = t(el.dataset.i18nPh);
  });
  document.querySelectorAll("[data-i18n-title]").forEach((el) => {
    el.title = t(el.dataset.i18nTitle);
    if (el.hasAttribute("aria-label")) {
      el.setAttribute("aria-label", t(el.dataset.i18nTitle));
    }
  });
}

function setLang(lang) {
  LANG = I18N[lang] ? lang : "en";
  document.documentElement.lang = LANG;
  document.documentElement.dir = langDir(LANG);
  document.body.classList.toggle("rtl", LANG === "fa");
  applyI18n();
  renderPhase();
  renderSelection();
  renderStats();
  renderLocations();
  renderUpdate();
  renderDoctor();
  updateFootMeta();
  syncBottomSwitches();
}

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
  ks: null,                       /* v4.9.0: null | "healing" | "lockdown" */
  doctor: null,                   /* v4.9.0: last self-check report */
  update: { status: "idle" },
  updateToasted: false,
  lastDown: 0,
  lastUp: 0,
  lastDownTs: 0,
  renderedLocations: "",
  connectMsgTimer: null,
};

const CONNECTING_MSG_KEYS = [
  "connecting_msg_1",
  "connecting_msg_2",
  "connecting_msg_3",
  "connecting_msg_4",
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
  if (region === "optimal" || !region) return t("optimal_location");
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

  // v4.9.0 kill switch: honest states for a broken/repaired tunnel
  if (S.ks === "lockdown") {
    title.textContent = t("ks_lockdown_title");
    sub.textContent = t("ks_lockdown_sub");
    pill.textContent = t("pill_blocked");
    pill.style.color = "#fda4b1";
  } else if (S.ks === "healing") {
    title.textContent = t("ks_healing_title");
    sub.textContent = t("ks_healing_sub");
    pill.textContent = t("pill_repairing");
    pill.style.color = "#fcd34d";
    renderSelection();
    renderStats();
    return;
  } else if (phase === "connected") {
    const tun = S.mode === "tunnel";
    title.textContent = tun ? t("status_protected_vpn") : t("status_protected");
    const label = regionLabel(S.region);
    sub.textContent = tun
      ? t("sub_tunnel_active", { label })
      : t("sub_encrypted", { label });
    pill.textContent = t("pill_protected");
    pill.style.color = "#d7fbee";
  } else if (phase === "connecting") {
    title.textContent = t("status_connecting");
    sub.textContent = t(CONNECTING_MSG_KEYS[0]);
    startConnectingMsgs();
    pill.textContent = t("pill_connecting");
    pill.style.color = "";
  } else if (phase === "error") {
    title.textContent = t("status_error");
    sub.textContent = S.lastError || t("error_unknown");
    pill.textContent = t("pill_error");
    pill.style.color = "#fda4b1";
  } else {
    title.textContent = t("status_not_protected");
    sub.textContent = S.direct_ip
      ? t("sub_exposed_ip", { ip: S.direct_ip })
      : t("sub_exposed");
    pill.textContent = t("pill_offline");
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
      t(CONNECTING_MSG_KEYS[i % CONNECTING_MSG_KEYS.length]);
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
    city.textContent = t("optimal_location");
    country.textContent = t("optimal_sub");
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
    const warmTxt = st.warm ? t("note_warm", { n: st.warm }) : "";
    const hedgeTxt = st.hedges ? t("note_rescued", { n: st.hedges }) : "";
    $("stat-time-note").textContent =
      t("note_conns", { c: st.conns || 0, a: st.active || 0 }) + warmTxt + hedgeTxt;
    $("stat-ip").textContent = S.exit_ip || "…";
    $("stat-ip-note").textContent = t("note_exit_ip");
  } else {
    $("stat-down-note").textContent = "";
    $("stat-up-note").textContent = "";
    $("stat-time").textContent = "00:00";
    $("stat-time-note").textContent = t("note_not_connected");
    $("stat-ip").textContent = S.direct_ip || "…";
    $("stat-ip-note").textContent = t("note_real_ip");
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
  const key = JSON.stringify(S.locations) + "|" + q + "|" + LANG;
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
        <span class="lc-city" style="color:var(--text-faint)">${t("no_match")}</span>
        <span class="lc-country"></span></span>`;
    list.appendChild(empty);
  }

  $("free-count").textContent = t("free_count", { n: S.locations.length });
  $("list-caption").textContent =
    t("list_caption", { n: S.locations.length });
  // v4.9.0: keep the sheet footer in the active language even when the
  // locations EVENT predates the page load (boot snapshot path)
  if (S.locations.length) {
    $("sheet-foot").textContent = t("foot_premium");
  }
  renderPings();
  renderSelection();
}

/* ------------------------------------------------------------ actions */
async function togglePower() {
  if (S.phase === "connected" || S.phase === "connecting") {
    api("/api/disconnect", {});
    toast(t("toast_disconnecting"), "info", 2200);
  } else {
    const r = await api("/api/connect", { region: S.selected });
    if (!r.ok) toast(t("toast_conn_fail"), "err");
  }
}

async function pickLocation(region) {
  const was = S.selected;
  S.selected = region;
  renderSelection();
  await api("/api/select", { region });
  if (S.phase === "connected" || S.phase === "connecting") {
    toast(t("toast_switching", { label: regionLabel(region) }), "info");
    await api("/api/connect", { region });
  } else {
    toast(t("toast_selected", { label: regionLabel(region) }), "ok", 2200);
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

function openModal(which = "settings") {
  document.body.dataset.modal = which;
  if (which === "settings") loadSettingsForm();
  if (which === "update") renderUpdate();
}
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
  $("set-autoupdate").checked = c.auto_update !== false;
  $("set-killswitch").checked = c.kill_switch !== false;   // v4.9.0
  $("set-lang").value = (c.ui_lang === "fa") ? "fa" : "en";  // v4.9.0
  $("set-bypass").value = c.bypass_domains || "";           // v4.9.0
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
    auto_update: $("set-autoupdate").checked,
    kill_switch: $("set-killswitch").checked,               // v4.9.0
    ui_lang: ($("set-lang").value === "fa") ? "fa" : "en", // v4.9.0
    bypass_domains: $("set-bypass").value.trim(),            // v4.9.0
  };
  const r = await api("/api/settings", patch);
  const note = $("set-note");
  if (r.ok) {
    note.className = "set-note ok";
    note.textContent = t("settings_saved");
    setLang(patch.ui_lang);        // apply the language right away
    setTimeout(closeModal, 650);
  } else {
    note.className = "set-note";
    note.textContent = (r.errors || [t("invalid_values")]).join(" · ");
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

/* ------------------------------------------------------- update (v4.8.0) */
const UPD_LABEL = {
  idle: () => t("upd_idle"),
  checking: () => t("upd_checking"),
  latest: (u) => t("upd_latest", { v: u.latest || u.current }),
  available: (u) => u.source_install
    ? t("upd_available_src", { v: u.latest })
    : t("upd_available", { v: u.latest, size: fmtBytes(u.size) }),
  downloading: (u) => t("upd_downloading",
                         { v: u.latest, p: u.progress || 0 }),
  staging: () => t("upd_staging"),
  ready: (u) => t("upd_ready", { v: u.latest }),
  installing: (u) => t("upd_installing", { v: u.latest }),
  restarting: (u) => t("upd_restarting", { v: u.latest }),
  error: (u) => t("upd_error", { e: u.error || "?" }),
  disabled: () => t("upd_disabled"),
};

function renderUpdate() {
  const u = S.update || {};
  const st = u.status || "idle";
  const chipDot = $("upd-dot");
  if (chipDot) {
    chipDot.hidden = !("available" === st || "downloading" === st
      || "staging" === st || "ready" === st);
  }
  if (document.body.dataset.modal !== "update") return;   // modal closed
  $("upd-cur").textContent = `v${u.current || S.version || "?"}`;
  $("upd-new").textContent = `v${u.latest || "?"}`;
  const stateEl = $("upd-state");
  const lab = UPD_LABEL[st] || UPD_LABEL.idle;
  stateEl.textContent = typeof lab === "function" ? lab(u) : lab;
  stateEl.className = "upd-state" + ((st === "error") ? " err" : "");

  const prog = $("upd-progress");
  prog.hidden = !(st === "downloading" || st === "staging");
  $("upd-fill").style.width = `${u.progress || 0}%`;
  $("upd-pct").textContent = `${u.progress || 0}%`;

  const notes = $("upd-notes");
  notes.hidden = !u.notes;
  notes.textContent = u.notes || "";

  const errEl = $("upd-error");
  const errText = [u.error, u.checksum_error].filter(Boolean).join(" · ");
  errEl.hidden = !errText;
  errEl.textContent = errText;

  const btn = $("btn-update-install");
  const canInstall = (st === "available" || st === "ready" || st === "error")
    && !u.source_install;
  btn.disabled = !canInstall;
  btn.textContent = (st === "ready")
    ? t("upd_restart_to", { v: u.latest }) : t("upd_install");
  $("btn-update-check").disabled = (st === "checking" || st === "downloading"
    || st === "staging" || st === "installing" || st === "restarting");
}

async function updateCheckNow() {
  renderUpdate();
  try { await api("/api/update/check", {}); } catch { /* state arrives via events */ }
}

async function updateInstallNow() {
  const u = S.update || {};
  toast(u.status === "ready" ? t("toast_restarting_upd")
    : t("toast_installing_upd"), "info", 4200);
  try { await api("/api/update/install", {}); }
  catch (e) { toast(t("toast_upd_fail"), "err", 4200); }
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
        $("list-caption").textContent = t("list_caption", { n: 0 });
        $("sheet-foot").textContent = t("foot_locations_err",
                                        { err: ev.error });
      } else {
        $("sheet-foot").textContent = t("foot_premium");
      }
      renderLocations();
      renderSelection();
      break;
    case "pings":
      S.pings = ev.pings || {};
      renderPings();
      break;
    case "ks":                         /* v4.9.0 kill switch */
      S.ks = (ev.state === "healing") ? "healing"
           : (ev.state === "lockdown") ? "lockdown" : null;
      renderPhase();
      break;
    case "doctor":                     /* v4.9.0 self-check */
      S.doctor = ev.report || null;
      renderDoctor();
      break;
    case "selected":
      S.selected = ev.region;
      renderSelection();
      break;
    case "settings":
      S.settings = ev.settings || {};
      syncBottomSwitches();
      updateFootMeta();       // v4.4.0: footer showed stale ports after a save
      setLang((S.settings.ui_lang === "fa") ? "fa" : "en");
      break;
    case "system_proxy":
      break;
    case "update":                       /* v4.8.0 "Sentinel" self-update */
      S.update = ev.update || {};
      renderUpdate();
      if ((S.update.status === "available" || S.update.status === "ready")
          && !S.updateToasted) {
        S.updateToasted = true;
        toast(t("toast_update_available", { v: S.update.latest }),
              "ok", 5200);
      }
      break;
  }
}

/* ------------------------------------------------ doctor (v4.9.0) */
function renderDoctor() {
  const list = $("doctor-list");
  if (!list) return;
  const state = $("doctor-state");
  const note = $("doctor-note");
  const report = S.doctor;
  list.textContent = "";
  if (!report || !report.length) {
    if (state) state.textContent = t("doctor_idle");
    if (note) note.textContent = "";
    return;
  }
  let worst = "ok";
  for (const c of report) {
    if (c.status === "fail") worst = "fail";
    else if (c.status === "warn" && worst !== "fail") worst = "warn";
  }
  if (state) {
    state.textContent = t(worst === "fail" ? "doctor_done_fail"
                            : worst === "warn" ? "doctor_done_warn"
                            : "doctor_done_ok");
    state.className = "upd-state" + (worst === "fail" ? " err" : "");
  }
  const order = { fail: 0, warn: 1, ok: 2 };
  for (const c of [...report].sort((a, b) =>
      (order[a.status] ?? 3) - (order[b.status] ?? 3))) {
    const row = document.createElement("div");
    row.className = "dr-row " + c.status;
    const title = document.createElement("div");
    title.className = "dr-title";
    title.textContent = I18N[LANG] &&
      I18N[LANG]["dr_" + c.id] ? t("dr_" + c.id) : c.title;
    const st = document.createElement("span");
    st.className = "dr-st " + c.status;
    st.textContent = t("doctor_st_" + c.status);
    title.prepend(st);
    const detail = document.createElement("div");
    detail.className = "dr-detail";
    detail.setAttribute("dir", "auto");
    detail.textContent = c.detail || "";
    row.append(title, detail);
    list.appendChild(row);
  }
  if (note) {
    note.textContent = "";
    note.className = "set-note";
  }
}

async function runDoctor() {
  renderDoctor();
  const state = $("doctor-state");
  if (state) state.textContent = t("doctor_running");
  try {
    await api("/api/doctor", {});
  } catch (e) { /* the poller picks the report up */ }
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
        ${t("error_cannot_reach")}</div>`;
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
    ks: st.ks || null,
    update: st.update || { status: "idle" },
    seq: st.seq || 0,
  });

  $("ver-meta").textContent = `v${st.version}`;
  renderUpdate();
  updateFootMeta();
  $("log-lines").textContent = "";
  (st.logs || []).forEach(appendLog);

  setLang((st.settings && st.settings.ui_lang === "fa") ? "fa" : "en");
  renderPhase();
  renderLocations();
  syncBottomSwitches();
  S.doctor = st.doctor || null;
  renderDoctor();
  poll();

  // quit affordance for browser-fallback mode
  if (!S.native_window) {
    const b = document.createElement("button");
    b.className = "btn ghost";
    b.style.marginRight = "auto";
    b.textContent = t("toast_shutdown_btn");
    b.addEventListener("click", async () => {
      await api("/api/quit", {});
      toast(t("toast_shutting_down"), "info", 4000);
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
  toast(t("toast_measuring"), "info", 1800);
  await api("/api/pings", {});
});
$("optimal-card").addEventListener("click", pickOptimal);
$("search").addEventListener("input", renderLocations);

$("btn-logs").addEventListener("click", () => toggleSheet("logs"));
$("btn-logs-close").addEventListener("click", closeSheets);

/* v4.9.0: protection self-check modal */
$("btn-doctor").addEventListener("click", () => {
  openModal("doctor");
  renderDoctor();
  if (!S.doctor) runDoctor();
});
$("btn-doctor-close").addEventListener("click", closeModal);
$("btn-doctor-close2").addEventListener("click", closeModal);
$("btn-doctor-run").addEventListener("click", runDoctor);
$("btn-logs-copy").addEventListener("click", async () => {
  const txt = Array.from($("log-lines").children)
    .map((l) => `${l.querySelector(".log-ts").textContent} `
              + `${l.querySelector(".log-lv").textContent.padEnd(5)} `
              + `${l.querySelector(".log-tx").textContent}`)
    .join("\n");
  try { await navigator.clipboard.writeText(txt); toast(t("toast_log_copied"), "ok", 2000); }
  catch { toast(t("toast_clipboard"), "err"); }
});

$("btn-settings").addEventListener("click", () => openModal("settings"));
$("btn-settings-close").addEventListener("click", closeModal);
$("btn-settings-cancel").addEventListener("click", closeModal);
$("btn-settings-save").addEventListener("click", saveSettingsForm);

/* ---------------------------- update (v4.8.0) ------------------------- */
$("btn-ver").addEventListener("click", () => openModal("update"));
$("btn-update-close").addEventListener("click", closeModal);
$("btn-update-check").addEventListener("click", updateCheckNow);
$("btn-update-install").addEventListener("click", updateInstallNow);

$("stat-ip-card").addEventListener("click", async () => {
  const ip = $("stat-ip").textContent;
  if (!ip || ip === "…" || ip === "—") return;
  try { await navigator.clipboard.writeText(ip); toast(t("toast_ip_copied", { ip }), "ok", 1800); }
  catch { toast(t("toast_clipboard"), "err"); }
});

$("in-system").addEventListener("change", async (e) => {
  S.settings.auto_system_proxy = e.target.checked;
  await api("/api/settings", { auto_system_proxy: e.target.checked });
});
$("in-tunnel").addEventListener("change", async (e) => {
  const on = e.target.checked;
  if (on && !S.tunnel_available) {
    e.target.checked = false;
    toast(t("toast_tunnel_na"), "err", 4200);
    return;
  }
  S.settings.tunnel_enabled = on;
  await api("/api/settings", { tunnel_enabled: on });
  if (S.phase === "connected" || S.phase === "connecting") {
    // parity with the v4.3 Tk GUI: applying a mode change live reconnects
    toast(t("toast_tunnel_changed"), "info", 2600);
    await api("/api/connect", { region: S.selected });
  } else {
    toast(on ? t("toast_tunnel_on") : t("toast_tunnel_off"), "info", 2600);
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
