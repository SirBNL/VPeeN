<div align="center">

<img src="assets/banner.png" alt="VPeeN — VPN-grade GUI for the free VeePN extension proxy network" width="100%"/>

# 🛡️ VPeeN

### The free VeePN browser-extension network — for your entire operating system.

**شبکه رایگان افزونه VeePN — این بار برای کل سیستم‌عامل تو.**

`No login` · `No subscription` · `No browser needed` · `Pure-Python core`

[![License: MIT](https://img.shields.io/badge/License-MIT-00d68f.svg?style=for-the-badge&labelColor=0e1526)](LICENSE)
[![Platform](https://img.shields.io/badge/Platform-Windows%20%7C%20macOS%20%7C%20Linux-2f81f7.svg?style=for-the-badge&labelColor=0e1526)](#-quick-start)
[![Python](https://img.shields.io/badge/Python-3.9%2B-ffd43b.svg?style=for-the-badge&labelColor=0e1526)](https://www.python.org/)
[![GUI](https://img.shields.io/badge/GUI-Aurora%20v4.5-2de3a7.svg?style=for-the-badge&labelColor=0e1526)](CHANGELOG-v4.5.0.md)
[![Release](https://img.shields.io/github/v/release/SirBNL/VPeeN?style=for-the-badge&labelColor=0e1526&color=00d68f)](../../releases)
[![Stars](https://img.shields.io/github/stars/SirBNL/VPeeN?style=for-the-badge&labelColor=0e1526&color=ffcb2f)](../../stargazers)

<img src="docs/demo.gif" alt="VPeeN Aurora UI — the aurora-dart cursor connects, watches the handshake, switches to Amsterdam, copies the exit IP and inspects live logs" width="88%"/>

</div>

---

## 💡 Why VPeeN?

The VeePN Chrome extension is free, needs no account, and tunnels browser traffic through a
global proxy network. **VPeeN** speaks the same protocol natively and brings those tunnels to
**every app on your machine** — wrapped in a product-grade desktop interface.

- 🖥️ **System-wide** — browsers, games, download managers, CLIs: everything is covered
- 🙈 **Anonymous** — guest tokens only, nothing to sign up for
- 🚪 **Browser-free** — Chrome can stay closed; VPeeN talks to the network directly
- 🧩 **Standard interfaces** — plain SOCKS5 and HTTP proxies on localhost
- 🎨 **“Aurora” GUI (v4.4)** — an animated power orb, drifting aurora gradients, live
  latency bars and realtime traffic stats — a local web app in a native window
  (pywebview) or your browser. No Tk, no Electron, no heavy frameworks
- ⚡ **“Velocity” networking (v4.5)** — pre-warmed TLS connections cut every proxied
  connection from ~3 round-trips to 1, blackholed server addresses are raced
  Happy-Eyeballs style instead of timing out, servers are auto-ranked fastest-first,
  and repeated DNS lookups are answered from a local cache
- 🆓 **Free-tier honest** — the list shows the 7 locations the free network actually
  serves, with real measured latency; the 178 premium ones are hidden instead of failing

```mermaid
flowchart LR
    A["🖥️ Your apps"] -->|"SOCKS5 / HTTP on 127.0.0.1"| B["VPeeN"]
    B -->|"encrypted tunnel"| C["🌍 VeePN free network"]
    C --> D["Internet"]
    style B fill:#0f766e,color:#d7fbee,stroke:#2de3a7
    style A fill:#0e1526,color:#eaf1fb,stroke:#22304d
    style C fill:#0e1526,color:#eaf1fb,stroke:#22304d
    style D fill:#0e1526,color:#eaf1fb,stroke:#22304d
```

## ✨ Features

| | Feature | Details |
|---|---|---|
| ⚡ | **One-click connect** | A big, honest power orb — pulse while connecting, glow when protected, red arc on error |
| 🌍 | **Location picker** | Flag-tagged list of the free locations, live search, real measured latency bars, “Optimal” auto-pick |
| 📡 | **Realtime stats** | Down/up totals **with live rates**, session timer, connection counters — updated every second |
| 🕵️ | **IP monitor** | Real IP when offline, exit IP when protected — one click to copy |
| 🔒 | **Real VPN Tunnel (TUN)** | Hiddify-style full-system tunnel — every app, zero config (see below) |
| 🖥️ | **System proxy mode** | Classic SOCKS5/HTTP mode with a one-switch OS setup + clean restore on exit |
| 🛡️ | **DNS leak protection** | In tunnel mode DNS is relayed through the tunnel itself |
| 📜 | **Activity log** | Color-coded, autoscrolling, one-click copy — every step of every connection |
| ⚙️ | **Settings** | Ports, bind, tunnel MTU, DNS relay, fallback policy, crash cleanup — all validated on save |
| 🔁 | **Self-healing tunnels** | Round-robin across servers, automatic retry and credential rotation on failure |
| ♿ | **Accessible & smooth** | Full keyboard operation, focus rings, `prefers-reduced-motion` support |
| 📦 | **Zero runtime deps** | The core engine is pure Python standard library — the GUI is plain HTML/CSS/JS |

<div align="center">

**The connect flow** — idle · connecting · protected

<img src="docs/screenshot-main.png" width="32%"/> <img src="docs/screenshot-connecting.png" width="32%"/> <img src="docs/screenshot-connect.png" width="32%"/>

**The app** — locations · logs · settings

<img src="docs/screenshot-locations.png" width="32%"/> <img src="docs/screenshot-logs.png" width="32%"/> <img src="docs/screenshot-settings.png" width="32%"/>

</div>

## 🚀 Quick Start

### Option 1 — Ready-made binaries

Download from [**Releases**](../../releases):

| File | OS | Run |
|---|---|---|
| `VPeeN-Windows.zip` | Windows 10/11 | unzip → `VPeeN.exe` |
| `VPeeN-macOS.zip` | macOS 10.13+ | unzip → `VPeeN.app` — see the [macOS first-launch note](#-macos-first-launch-gatekeeper) |
| `VPeeN-Linux.tar.gz` | Linux x64 | extract → `./VPeeN` |
| `VPeeN-Chrome-Extension.zip` | Chrome 108+ | unzip → [load unpacked](#-chrome-extension--vpeen-bridge) |

> Windows/macOS open the UI in a native desktop window (pywebview); Linux (and any machine
> without a webview backend) opens it in your default browser — same UI, same features.

### 🍏 macOS first launch (Gatekeeper)

VPeeN is free, open-source software — and **not** wrapped in a paid Apple Developer
certificate. On modern macOS (Sequoia and **Tahoe 26**) Gatekeeper answers that with
*“Apple could not verify ‘VPeeN’ is free of malware…”* and no visible Open button.
It is a warning, not a defect — the binary is ad-hoc code-signed since v4.6.0. Two ways
past it:

1. **The two-click way** — try to open the app once, then
   **System Settings → Privacy & Security** → scroll to the bottom →
   **“Open Anyway”**. macOS asks once; after that it opens normally forever.
2. **The one-line way** — Terminal:
   ```bash
   xattr -cr /Applications/VPeeN.app     # (or the path where you unzipped it)
   ```
   This removes the quarantine flag Apple put on the download.

### 🧩 Chrome extension — VPeeN Bridge

Want *only the browser* on the VPN while the rest of the system stays direct — or the
one-click switch while VPeeN runs? **VPeeN Bridge** (ships as
`VPeeN-Chrome-Extension.zip` on every release, source in [`extension/`](extension/)):

1. Unzip it anywhere permanent.
2. `chrome://extensions` → enable **Developer mode** → **Load unpacked** → pick the folder.
3. Click the VPeeN icon → flip **“Route Chrome through VPeeN”**.

Two permissions only (`proxy`, `storage`), remote DNS through the tunnel, and the same
localhost/LAN bypass list as the desktop app. Flip it off and Chrome is instantly direct
again.

### Option 2 — Run from source

```bash
git clone https://github.com/SirBNL/VPeeN.git
cd VPeeN
pip install -r requirements.txt     # optional: native window (pywebview)
python main.py
```

The app starts, shows your real IP, fetches free locations with live latency — press the power orb.

### Option 3 — Headless CLI

No GUI needed (servers, routers, scripts):

```bash
python -m vpeen.cli list                 # list free locations + latency
python -m vpeen.cli test nl              # full chain test: token → server → exit IP
python -m vpeen.cli run --region us-va   # SOCKS5 :1080 + HTTP :8080
python -m vpeen.cli run --best           # auto-pick the optimal region
python -m vpeen.cli export nl            # print the raw upstream config
```

Then point any app at:

```
SOCKS5 :  socks5://127.0.0.1:1080
HTTP   :  http://127.0.0.1:8080
```

## ⚙️ How it works

VPeeN reproduces what the free VeePN extension does inside the browser — but for the whole OS:

1. **Anonymous session** — VPeeN obtains a guest access token, exactly like the extension.
   No account is ever created.
2. **Locations & servers** — the free location list and per-session credentials are fetched
   from VeePN's live infrastructure, so the app keeps working as endpoints evolve.
3. **Local proxy** — VPeeN listens as a standard SOCKS5 + HTTP proxy on `127.0.0.1`
   (a non-loopback bind without credentials is refused outright).
4. **Tunneling** — every connection is relayed through an encrypted tunnel to the selected region.
5. **System switch** — optionally flips the OS proxy on (Windows / macOS / GNOME) and restores
   the previous state on exit.

The GUI itself is a tiny loopback-only web app: a stdlib HTTP server runs the proxy core and
streams events to the UI over a long-poll `/api/poll` endpoint — the interface reacts in
under a second with zero third-party JavaScript.

## 🔒 Tunnel Mode — a real VPN, the Hiddify way

Flip the **VPN Tunnel** switch and VPeeN stops being “just a proxy” and becomes
a **system-wide VPN**: a virtual network adapter takes over all of your traffic — not just the
apps that know about proxies.

```mermaid
flowchart LR
    A["🖥️ ALL apps + DNS"] --> B["🔌 VPeeN TUN adapter"]
    B --> C["tun2socks userspace TCP/IP stack"]
    C -->|"socks5 over loopback"| D["VPeeN core"]
    D -->|"encrypted tunnel"| E["🌍 VeePN network"]
    E --> F["Internet"]
    style D fill:#0f766e,color:#d7fbee,stroke:#2de3a7
    style A fill:#0e1526,color:#eaf1fb,stroke:#22304d
    style B fill:#0e1526,color:#eaf1fb,stroke:#22304d
    style C fill:#0e1526,color:#eaf1fb,stroke:#22304d
    style E fill:#0e1526,color:#eaf1fb,stroke:#22304d
    style F fill:#0e1526,color:#eaf1fb,stroke:#22304d
```

Under the hood this is the same architecture Hiddify / sing-box use: a TUN device
([wintun](https://www.wintun.net/) on Windows, `utun` on macOS, `tun` on Linux) driven by the
open-source [tun2socks](https://github.com/xjasonlyu/tun2socks) engine (MIT), which VPeeN
launches as a properly elevated helper and points at its own local SOCKS5.

### Built to never break your internet

| Safety mechanism | What it means for you |
|---|---|
| 🚫 **No route hijack** | VPeeN adds two `/1` routes instead of replacing your default gateway — disconnect = instant, clean restore |
| 🧭 **Anti-loop routes** | The VPN servers themselves are pinned to your real gateway, so the tunnel can never route into itself |
| 🏠 **LAN stays up** | Local network traffic (printers, NAS, router admin) is untouched |
| 📡 **DNS through the tunnel** | The TUN adapter's DNS points at VPeeN's internal relay — your system DNS settings are never modified |
| 🛑 **Explicit failure, never silent** | If the tunnel drops, you SEE it: a loud warning explains that coverage is now partial — or, with the fallback switch turned off, VPeeN stops instead of quietly downgrading you to proxy mode |
| 💓 **Heartbeat watchdog** | If the GUI dies, the elevated helper notices within seconds and tears everything down |
| 🧹 **Crash journal** | Every route change is journaled to disk; after a hard crash the next launch cleans up automatically |
| ♻️ **Adapter auto-removal** | The wintun adapter lives only as long as the process — kill anything, Windows cleans itself |

> ℹ️ Tunnel mode needs **administrator rights once per connect** (UAC prompt on Windows,
> pkexec / osascript on Linux / macOS) — that is how real VPN apps create a network device.
> Everything else (proxy mode) works without elevation.

| | Proxy mode | 🔒 Tunnel mode |
|---|---|---|
| Coverage | Apps that use the OS proxy / SOCKS5 | **Every app — TCP; DNS is tunneled too. Non-DNS UDP (e.g. QUIC) is not relayed: apps that can, fall back to TCP on their own** |
| Admin rights | not needed | once per connect (UAC) |
| DNS | resolved by your system | relayed through the tunnel |
| Best for | browsers, quick use | full protection, stubborn apps, TCP-heavy apps |

## 🏗️ Build from source

```bash
pip install pyinstaller
pyinstaller vpeen.spec --noconfirm
```

- **Windows** → `dist/VPeeN.exe`  ·  **macOS** → `dist/VPeeN.app`  ·  **Linux** → `dist/VPeeN`

Every push is built for all three platforms by [GitHub Actions](.github/workflows/build.yml);
version tags (`v*`) automatically produce a [release](../../releases) with binaries.

## 📁 Project structure

```
VPeeN/
├── main.py               # GUI launcher (+ tunnel-worker dispatch, crash log)
├── vpeen/
│   ├── gui.py            # desktop window launcher (pywebview → browser fallback)
│   ├── webui.py          # local web-UI server + core/tunnel bridge (loopback only)
│   ├── web/              # the "Aurora" GUI (index.html / style.css / app.js)
│   ├── core.py           # background proxy engine + GUI event bridge
│   ├── api.py            # VeePN network client (tokens, locations, servers)
│   ├── upstream.py       # encrypted upstream tunnels
│   ├── localproxy.py     # local SOCKS5 + HTTP servers
│   ├── tunnel.py         # VPN tunnel orchestration (elevated worker + controller)
│   ├── tunnel_platforms.py  # per-OS TUN routes / DNS / session journal
│   ├── dnsrelay.py       # DNS relay that tunnels DNS through VPeeN
│   ├── systemproxy.py    # OS proxy switch (Win / macOS / GNOME)
│   ├── settings.py       # persistent app settings (atomic writes)
│   ├── cli.py            # headless command-line mode
│   └── utils.py          # shared helpers
├── assets/               # icon, logo, banner, flags/ (92 circular flags)
├── scripts/fetch-binaries.py  # pinned download of tun2socks + wintun (build-time only)
├── docs/                 # screenshots + demo GIF
└── .github/workflows/    # multi-OS build + release pipeline
```

## 🧾 Third-party components

| Component | License | Usage |
|---|---|---|
| [tun2socks](https://github.com/xjasonlyu/tun2socks) v2.7.0 | **MIT** | TUN engine for Tunnel mode (fetched at build time, never at runtime) |
| [wintun](https://www.wintun.net/) 0.14.1 | Wintun Prebuilt Binaries License | signed virtual-adapter driver (Windows, used via its permitted API) |
| [pywebview](https://pywebview.ngole.io/) | BSD-3 | native desktop window for the web GUI (optional; falls back to the browser) |
| Flags | [flagcdn](https://flagcdn.com/) (public domain) | circular country flags |

## ❓ FAQ

<details>
<summary><b>Is an account or subscription required?</b></summary>
No. VPeeN uses the same anonymous guest-token flow as the free browser extension.
</details>

<details>
<summary><b>Which locations are free?</b></summary>
Currently 7 free locations (Amsterdam, Paris, London, Virginia, Oregon, Singapore, Saint Petersburg).
The app fetches the live list — run <code>python -m vpeen.cli list</code> to see it.
</details>

<details>
<summary><b>Is this a “real” VPN?</b></summary>
With <b>Tunnel mode</b> — yes for TCP and DNS: a virtual network adapter routes your system's
TCP traffic through the VPN tunnel with DNS leak protection, exactly like Hiddify/sing-box do it.
Being honest about the limits: the upstream is an HTTPS-CONNECT (TCP-only) proxy, so <b>non-DNS
UDP (QUIC, VoIP, games) is not relayed</b> — QUIC-capable apps fall back to TCP on their own,
others need TCP. In classic proxy mode it is a system-wide SOCKS5/HTTP proxy with the same limits.
Since v4.3.0 the local SOCKS5 also answers <b>UDP ASSOCIATE for DNS</b> (relayed as DNS-over-TCP
through the tunnel), which keeps tun2socks-based setups working.
</details>

<details>
<summary><b>Where is my data stored?</b></summary>
Only <code>~/.vpeen/</code> on your machine — cached token, servers and settings. Nothing is sent
anywhere else. The state file (which contains the access token) is written with <code>0600</code>
permissions and the directory with <code>0700</code> (POSIX), so only your user can read it.
</details>

<details>
<summary><b>What changed in v4.6.0 (Turbo)?</b></summary>
<ul>
<li><b>Pool-aware scheduling (fixed the 1/N bug)</b> — v4.5.0's warm pool only matched the
round-robin cursor's server, so with 3 servers two thirds of every browsing burst still
paid full TLS handshakes. Every connection now takes the newest warm connection,
whatever server it belongs to: <b>3.4× faster connection setup</b> measured on the real
free network.</li>
<li><b>Bigger pool, filled in parallel</b> — 12 warm connections (was 4), refilled in
parallel waves, pre-warmed from the moment you connect — the <i>first</i> page load is
fast now too, not just the tenth.</li>
<li><b>Throughput ranking — the server-side signal</b> — at connect time each top server
is measured with a real 128 KiB download through its own tunnel; the best-bandwidth
server gets the traffic, not merely the lowest-ping one. Re-measured every 60 s.</li>
<li><b>Chrome extension (VPeeN Bridge)</b> — issue #2: a 2-permission MV3 extension that
routes all of Chrome through the local SOCKS5 tunnel with one click. Ships with every
release.</li>
<li><b>macOS Gatekeeper</b> — issue #1: release builds are ad-hoc code-signed and the
README documents the two-click “Open Anyway” path for Sequoia/Tahoe 26.</li>
<li><b>Also</b>: handshake concurrency 8→12, macOS socket buffers (512 KiB), faster
Happy-Eyeballs stagger (150 ms), DNS cache TTL 60 s, `N warm` live in the stats line,
and a fixed v4.5.0 latent bug where the latency map kept stale indices after the
startup reorder.</li>
<li><b>Tests:</b> 118 checks green (25 speed-pass + 59 + 11 + 23) plus a real-network
A/B benchmark. Full list: <a href="CHANGELOG-v4.6.0.md">CHANGELOG-v4.6.0.md</a>.</li>
</ul>
</details>

<details>
<summary><b>What changed in v4.5.0 (Velocity)?</b></summary>
<ul>
<li><b>Pre-warmed TLS connection pool</b> — every proxied connection used to pay TCP +
full TLS handshake + CONNECT (~3 round-trips) before its first byte. The proxy now keeps
TLS-established connections warm in the background, so a client CONNECT costs a single
round-trip. Stale warm connections (silently reaped by the server) are detected instantly
and retried transparently.</li>
<li><b>Happy-Eyeballs address racing</b> — a blackholed server address costs 250 ms
instead of the full 8 s timeout before the next one is tried.</li>
<li><b>Latency-ranked servers</b> — at startup every server in the region is probed
concurrently and the list is reordered fastest-first; the GUI’s per-region ping reports
the region’s best server, not just its first entry.</li>
<li><b>Instant “connected”</b> — the UI flips to connected the moment the local proxies
are live; the exit IP is verified in the background instead of blocking the phase.</li>
<li><b>Throughput & CPU</b> — 256 KiB relay buffers (was 64 KiB), cached SSL contexts
(no CA-bundle reload per connection), TCP_NODELAY + Windows buffer tuning, and a
DNS cache for the SOCKS5 UDP-DNS path.</li>
<li><b>Tests:</b> 17 speed-pass checks + the 23-check live demo-server suite.
Full list: <a href="CHANGELOG-v4.5.0.md">CHANGELOG-v4.5.0.md</a>.</li>
</ul>
</details>

<details>
<summary><b>What changed in v4.4.0 (Aurora)?</b></summary>
<ul>
<li><b>The GUI is a local web app now.</b> A loopback-only server (pure stdlib) runs the proxy
core and streams events to a vanilla HTML/CSS/JS interface — shown in a native pywebview window
or your browser. CustomTkinter, Pillow and the whole Tk runtime are gone: smaller bundles,
faster startup, and a UI Tk could never render (gradients, glow, smooth animations).</li>
<li><b>Live latency + live traffic rates</b> in the location list and stat cards.</li>
<li><b>Audit pass:</b> 12 defects found and fixed in the new bridge/UI — including a demo-data
crash on connect, tunnel mode never reporting the exit country, an 8-second hang on tunnel
disconnect, silent saving of invalid ports/MTU, and HTTP handlers that died mid-response on
bridge errors. Full list with test evidence:
<a href="CHANGELOG-v4.4.0.md">CHANGELOG-v4.4.0.md</a>.</li>
<li><b>Test suites:</b> 27 new end-to-end checks (<code>scripts/test_webui_demo.py</code>) on top
of the existing 59 + 11.</li>
</ul>
</details>

<details>
<summary><b>What changed in v4.3.0 (security pass)?</b></summary>
<ul>
<li><b>Local proxy auth</b> — the CLI (<code>python -m vpeen.cli run</code>) accepts
<code>--proxy-user/--proxy-pass</code>: RFC 1929 for SOCKS5, Basic for HTTP.</li>
<li><b>Bind safety</b> — a non-loopback bind without credentials is refused outright; the GUI is
loopback-only and its Bind field is now actually applied (it used to be stored but ignored).</li>
<li><b>Explicit TUN failure</b> — no more silent fallback to proxy mode: loud warning, or full stop
if you disable the “Fall back to proxy mode” switch in Settings.</li>
<li><b>UDP ASSOCIATE (DNS)</b> — DNS datagrams are relayed through the tunnel; other UDP is dropped
deliberately (the upstream is TCP-only).</li>
<li><b>Secrets hygiene</b> — <code>export</code> masks the upstream password unless
<code>--show-secrets</code>; state files are 0600/0700.</li>
<li><b>API hardening</b> — reserve API domains must be HTTPS with a sane hostname before the Bearer
token is ever sent there, every domain in the rotation is actually tried, and one request can
never stall past a 120s wall-clock budget.</li>
<li><b>macOS restore fidelity</b> — the pre-existing per-service proxy (enabled, host, port) is
backed up and restored exactly, instead of blindly switching proxies off.</li>
</ul>
</details>

<details>
<summary><b>Antivirus flags the binary?</b></summary>
PyInstaller onefile builds are sometimes heuristically flagged. Build from source with
<code>pyinstaller vpeen.spec</code> if you prefer — the code is fully readable right here.
</details>

## ⚠️ Disclaimer

This project is published **for educational and research purposes** — it demonstrates how a
browser-extension proxy client works. Using the free extension network outside the browser may
conflict with VeePN's Terms of Service; you are responsible for how you use this software.
Respect the laws of your country and the networks you connect to.

---

<div align="center" dir="rtl">

# 🇮🇷 راهنمای فارسی

**VPeeN** همون شبکه‌ای رو که افزونه رایگان VeePN توی مرورگر در اختیارت می‌ذاره، برای
**کل سیستم‌عامل** باز می‌کنه — با رابط گرافیکی «Aurora» مدرن و انیمیشنی، بدون ثبت‌نام و بدون نیاز به کروم.

## ✨ ویژگی‌ها

- ⚡ **اتصال با یک کلیک** — اورب پاور بزرگ: موقع اتصال می‌تپه، موقع محافظت می‌درخشه، موقع خطا قرمز می‌شه
- 🌍 **انتخاب موقعیت** — لیست لوکیشن‌های رایگان با پرچم کشور، جستجوی زنده و **پینگ واقعیِ اندازه‌گیری‌شده** برای هر سرور + انتخاب خودکار «بهترین»
- 📡 **آمار زنده** — حجم دانلود/آپلود **با نرخ لحظه‌ای**، تایمر جلسه و شمارنده اتصال‌ها، هر ثانیه آپدیت
- 🕵️ **مانیتور IP** — زمان قطع: IP واقعی تو؛ زمان اتصال: IP خروجی — با یک کلیک کپی می‌شه
- 🔒 **تانل واقعی VPN (حالت TUN)** — تانل کامل مثل Hiddify؛ کل ترافیک سیستم بدون هیچ تنظیماتی
- 🖥️ **حالت پروکسی سیستم** — SOCKS5/HTTP کلاسیک با ست/بازیابی خودکار پروکسی سیستم‌عامل
- 🛡️ **ضد نشت DNS** — در حالت تانل، خود DNS هم از داخل تانل رد می‌شه
- 📜 **لاگ کامل فعالیت** — رنگی، اسکرول خودکار، با یک کلیک کپی
- ⚙️ **تنظیمات** — پورت‌ها، MTU تانل، رله DNS، سیاست fallback، پاک‌سازی بعد از کرش — همه موقع ذخیره اعتبارسنجی می‌شن
- 🔁 **تانل خودترمیم** — توزیع بار بین سرورها و تلاش مجدد خودکار
- ♿ **دسترس‌پذیر** — کاملاً با کیبورد کار می‌کنه و حرکت‌های انیمیشن رو برای کاربران حساس کاهش می‌ده

## 🔒 حالت تانل — یک VPN واقعی، مثل Hiddify

کلید **VPN Tunnel** رو بزن تا VPeeN از «فقط یک پروکسی» به یک **VPN سراسری واقعی** تبدیل بشه:
یک کارت شبکه مجازی کل ترافیک سیستم رو می‌گیره — حتی برنامه‌هایی که مفهوم پروکسی رو نمی‌فهمن.

همون معماری Hiddify و sing-box: آداپتور TUN ([wintun](https://www.wintun.net/) در ویندوز، `utun` در مک،
`tun` در لینوکس) که با موتور متن‌باز [tun2socks](https://github.com/xjasonlyu/tun2socks) (مجوز MIT) راه می‌افته.

### طوری ساخته شده که اینترنتت رو خراب نکنه

| مکانیزم امنیتی | یعنی چی؟ |
|---|---|
| 🚫 **بدون دستکاری روپ پیش‌فرض** | به‌جای عوض‌کردن گیت‌وی سیستم، فقط دو مسیر `/1` اضافه می‌شه؛ قطع اتصال = برگشت فوری و تمیز |
| 🧭 **مسیر ضد حلقه** | IP سرورهای VPN به گیت‌وی اصلی قفل می‌شن تا تانل هیچ‌وقت داخل خودش نیافته |
| 🏠 **شبکه محلی سالم می‌مونه** | ترافیک LAN (پرینتر، NAS، پنل مودم) دست‌نخورده |
| 📡 **DNS از داخل تانل** | DNS آداپتور مجازی به رله داخلی VPeeN اشاره می‌کنه؛ تنظیمات DNS سیستم تو هیچ‌وقت عوض نمی‌شه |
| 🛑 **خطای شفاف، نه سکوت** | اگه تانل بیفته، صریح می‌بینی — یا با خاموش‌کردن سوییچ fallback، VPeeN کلاً می‌ایسته تا مخفی‌وار به پروکسی برگرده |
| 💓 **نگهبان ضربان** | اگه رابط گرافیکی بمیره، هلپر elevated ظرف چند ثانیه همه‌چیز رو برمی‌گردونه |
| 🧹 **ژورنال کرش** | تک‌تک تغییرات مسیرها روی دیسک ثبت می‌شه؛ بعد از کرش سخت، اجرای بعدی خودش پاک می‌کنه |
| ♻️ **حذف خودکار آداپتور** | آداپتور wintun فقط تا وقتی پروسه‌اش زنده‌ست وجود داره |

> ℹ️ حالت تانل به **دسترسی ادمین (یک بار در هر اتصال)** نیاز داره (UAC در ویندوز) — همین قانون برای همه VPN های واقعی برقراره. حالت پروکسی بدون هیچ دسترسی خاصی کار می‌کنه.

| | حالت پروکسی | 🔒 حالت تانل |
|---|---|---|
| پوشش | برنامه‌هایی که پروکسی سیستم رو می‌فهمن | **تمام برنامه‌ها (TCP)** |
| دسترسی ادمین | لازم نیست | یک بار در هر اتصال |
| DNS | با DNS خود سیستم | از داخل تانل |
| مناسب برای | مرورگر، استفاده سریع | محافظت کامل، برنامه‌های سرسخت |

## 🚀 نصب و اجرا

**روش ۱ — فایل آماده:** از [Releases](../../releases) نسخه سیستمت رو بگیر:

| فایل | سیستم‌عامل | اجرا |
|---|---|---|
| `VPeeN-Windows.zip` | ویندوز ۱۰/۱۱ | باز کن → `VPeeN.exe` |
| `VPeeN-macOS.zip` | مک ۱۰.۱۳+ | باز کن → `VPeeN.app` *(برای بار اول، پایین رو بخون)* |
| `VPeeN-Linux.tar.gz` | لینوکس ۶۴بیتی | باز کن → `./VPeeN` |
| `VPeeN-Chrome-Extension.zip` | کروم ۱۰۸+ | باز کن → Load unpacked |

> 🍏 **کاربران مک (Sequoia / Tahoe 26):** چون VPeeN مجانی و متن‌بازه و گواهی پولی Apple Developer
> نداره، اولین بار Gatekeeper با پیام «Apple could not verify…» جلوی اجرا رو می‌گیره — این هشداره،
> نه خرابی. راه‌حل: یک بار سعی کن بازش کنی، بعد برو **System Settings → Privacy & Security**
> و پایین صفحه روی **Open Anyway** بزن؛ از اون به بعد مستقیم باز می‌شه. یا در Terminal:
> `xattr -cr /Applications/VPeeN.app`

**روش ۲ — اجرا از سورس:**

```bash
git clone https://github.com/SirBNL/VPeeN.git
cd VPeeN
pip install -r requirements.txt
python main.py
```

**روش ۳ — خط فرمان (بدون رابط گرافیکی):**

```bash
python -m vpeen.cli list                  # لیست لوکیشن‌های رایگان + پینگ
python -m vpeen.cli test nl               # تست کامل زنجیره
python -m vpeen.cli run --region us-va    # SOCKS5 :1080 + HTTP :8080
python -m vpeen.cli export nl             # نمایش کانفیگ خام آپ‌استریم
```

بعد هر برنامه‌ای رو به این آدرس‌ها وصل کن:

```
SOCKS5 :  socks5://127.0.0.1:1080
HTTP   :  http://127.0.0.1:8080
```

## 🆕 تازه‌های نسخه ۴.۶ (Turbo)

- **رفع باگ ۱/N در استخر گرم** — نسخه قبل فقط وقتی نوبت round-robin روی سرورِ درست می‌افتاد
  از اتصال‌های آماده استفاده می‌کرد؛ با ۳ سرور، دوسومِ هر بَرست مرورگر هنوز هندشیک کامل
  TLS می‌پرداخت! حالا هر اتصال جدیدترین اتصال آماده رو می‌گیره — **راه‌اندازی اتصال ۳.۴ برابر
  سریع‌تر** (اندازه‌گیری‌شده روی شبکه‌ی واقعی)
- **استخر بزرگ‌تر و موازی** — ۱۲ اتصال آماده (قبلاً ۴) که با موج‌های موازی پر می‌شن و از
  لحظه‌ی اتصال شروع به گرم شدن می‌کنن — یعنی **اولین** بارگذاری صفحه هم سریعه، نه فقط دهمین
- **رتبه‌بندی بر اساس توان عبوری واقعی** — برای اولین بار سیگنالِ سمت سرور: موقع اتصال،
  ۱۲۸ کیلوبایت واقعی از داخل تانل هر کاندید دانلود و سرعتش اندازه گرفته می‌شه؛ سروری که
  واقعاً پهنای باند می‌ده برنده‌ست، نه فقط کم‌پینگ‌ترین — و هر ۶۰ ثانیه دوباره سنجیده می‌شه
- **افزونه کروم «VPeeN Bridge»** — فقط با ۲ دسترسی؛ کل کروم رو با یک کلیک از تانل محلی
  VPeeN عبور می‌ده (DNS هم سمت خروجی حل می‌شه)
- **امضای ad-hoc مک + راهنمای Gatekeeper** برای مشکل باز نشدن در Tahoe 26
- و بافرهای سوکت macOS، همزمانی هندشیک ۸→۱۲، DNS کش ۶۰ ثانیه‌ای، نمایش «N warm» زنده
  در آمار و ۱۱۸ تست سبز + بنچمارک واقعی
(<a href="CHANGELOG-v4.6.0.md">فهرست کامل</a>)

## 🆕 تازه‌های نسخه ۴.۵ (Velocity)

- **استخرِ اتصال‌های TLS از پیش گرم** — هر اتصال قبلاً ۳ رفت‌وبرگشت کامل (TCP + TLS + CONNECT)
  هزینه می‌داد؛ حالا اتصال‌های آماده در پس‌زمینه نگه داشته می‌شن و هر اتصال جدید فقط
  ۱ رفت‌وبرگشت می‌خره — چیزی ۳ برابر سریع‌تر برای هر کانکت
- **مسابقه‌ی آدرس‌های سرور (Happy Eyeballs)** — یک آدرس بلاک‌شده دیگه ۸ ثانیه معطلی نمی‌سازه؛
  همه‌ی آدرس‌ها هم‌زمان مسابقه می‌دن و سالم‌ترین در ~۲۵۰ میلی‌ثانیه برنده می‌شه
- **رتبه‌بندی سرورها بر اساس تأخیر واقعی** — سریع‌ترین سرور ریجن همیشه اول انتخاب می‌شه؛
  پینگ هر ریجن هم از بهترین سرورش گزارش می‌شه نه اولین سرورش
- **اتصال آنی** — رابط بلافاصله بعد از بالا اومدن پراکسی‌ها «متصل» می‌شه؛ تأیید IP خروجی
  در پس‌زمینه انجام می‌شه
- **توان عبوری بیشتر** — بافرهای ۲۵۶ کیلوبایتی رله، کش شدن context های TLS،
  کش DNS برای مسیر SOCKS و تنظیمات TCP بهینه
(<a href="CHANGELOG-v4.5.0.md">فهرست کامل</a>)

## 🆕 تازه‌های نسخه ۴.۴ (Aurora)

- **رابط گرافیکی جدید «Aurora»** — به‌جای Tk قدیمی، حالا یک وب‌اپ محلی با انیمیشن‌های نرم،
  گرادیان‌های aurora و اورب پاور متحرک؛ در پنجره بومی (pywebview) یا مرورگر اجرا می‌شه
- **پینگ واقعی و نرخ ترافیک زنده** در لیست لوکیشن‌ها و کارت‌های آمار
- **رفع ۱۲ باگ** در بریج و رابط جدید — از جمله کرش اتصال در داده‌ی دمو، نشان‌داده‌نشدن کشورِ
  خروجی در حالت تانل، ۸ ثانیه معطلی موقع قطع تانل، و ذخیره‌ی بی‌صدای پورت‌های نامعتبر
  (<a href="CHANGELOG-v4.4.0.md">فهرست کامل</a>)
- **۲۷ تست انتها-به-انتهای جدید** روی مانده به ۵۹+۱۱ تست قبلی

## ❓ سوالات متداول

<details>
<summary><b>نیاز به ثبت‌نام یا خرید اشتراک هست؟</b></summary>
نه. VPeeN دقیقاً مثل افزونه رایگان، توکن مهمان ناشناس می‌گیره. هیچ حسابی ساخته نمی‌شه.
</details>

<details>
<summary><b>کدوم لوکیشن‌ها رایگانه؟</b></summary>
الان ۷ لوکیشن رایگانه (آمستردام، پاریس، لندن، ویرجینیا، اورگن، سنگاپور، سن‌پترزبورگ).
لیست زنده رو خود اپ می‌گیره.
</details>

<details>
<summary><b>VPN واقعیه؟</b></summary>
با <b>حالت تانل</b> — آره: یک کارت شبکه مجازی کل ترافیک TCP سیستم رو با حفاظت نشت DNS از تانل رد می‌کنه،
دقیقاً مثل Hiddify و sing-box. در حالت پروکسی کلاسیک، یک پروکسی سراسری SOCKS5/HTTP داری.
</details>

<details>
<summary><b>اطلاعاتم کجا ذخیره می‌شه؟</b></summary>
فقط در <code>~/.vpeen/</code> روی سیستم خودت: توکن کش‌شده، سرورها و تنظیمات.
</details>

## ⚠️ سلب مسئولیت

این پروژه با هدف **آموزش و پژوهش** منتشر شده و نشان می‌دهد کلاینت پروکسی یک افزونه مرورگر چطور کار می‌کند.
استفاده از شبکه رایگان افزونه خارج از مرورگر ممکن است با شرایط استفاده VeePN در تضاد باشد؛ مسئولیت نحوه استفاده با شماست.
قوانین کشور خودتان را رعایت کنید.

</div>

---

<div align="center">
<br/>
<b>⭐ If VPeeN saved you some money, a star is the best thank-you.</b><br/>
<a href="../../stargazers"><img src="https://img.shields.io/badge/%E2%AD%90-Star%20on%20GitHub-ffcb2f?style=for-the-badge&labelColor=0e1526"/></a>
<br/><sub>v4.5.0 “Velocity” · pure-Python core · zero-dependency GUI</sub>
</div>
