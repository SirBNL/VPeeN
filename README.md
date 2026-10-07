<div align="center">

<img src="assets/banner.png" alt="VPeeN — VPN-grade GUI for the free VeePN extension proxy network" width="100%"/>

# 🛡️ VPeeN

**The free VeePN browser-extension network — for your entire operating system.**

**شبکه رایگان افزونه VeePN — این بار برای کل سیستم‌عامل تو.**

No login · No subscription · No browser needed

[![License: MIT](https://img.shields.io/badge/License-MIT-00d68f.svg?style=for-the-badge&labelColor=0e1526)](LICENSE)
[![Platform](https://img.shields.io/badge/Platform-Windows%20%7C%20macOS%20%7C%20Linux-2f81f7.svg?style=for-the-badge&labelColor=0e1526)](#-quick-start)
[![Python](https://img.shields.io/badge/Python-3.9%2B-ffd43b.svg?style=for-the-badge&labelColor=0e1526)](https://www.python.org/)
[![Release](https://img.shields.io/github/v/release/SirBNL/VPeeN?style=for-the-badge&labelColor=0e1526&color=00d68f)](../../releases)
[![Stars](https://img.shields.io/github/stars/SirBNL/VPeeN?style=for-the-badge&labelColor=0e1526&color=ffcb2f)](../../stargazers)

<img src="docs/demo.gif" alt="VPeeN demo — connect and watch your IP roll to the new one" width="90%"/>

</div>

---

## 💡 Why VPeeN?

The VeePN Chrome extension is free, needs no account, and tunnels browser traffic through a
global proxy network. **VPeeN** speaks the same protocol natively and brings those tunnels to
every app on your machine — wrapped in a clean, modern desktop interface.

- 🖥️ **System-wide** — browsers, games, download managers, CLIs: everything is covered
- 🙈 **Anonymous** — guest tokens only, nothing to sign up for
- 🚪 **Browser-free** — Chrome can stay closed; VPeeN talks to the network directly
- 🧩 **Standard interfaces** — plain SOCKS5 and HTTP proxies on localhost

```mermaid
flowchart LR
    A["🖥️ Your apps"] -->|"SOCKS5 / HTTP on 127.0.0.1"| B["VPeeN"]
    B -->|"secure tunnel"| C["🌍 VeePN free network"]
    C --> D["Internet"]
    style B fill:#3e7af4,color:#ffffff,stroke:#3e7af4
    style A fill:#141d33,color:#eaf1fb,stroke:#22304d
    style C fill:#141d33,color:#eaf1fb,stroke:#22304d
    style D fill:#141d33,color:#eaf1fb,stroke:#22304d
```

## ✨ Features

| | Feature | Details |
|---|---|---|
| ⚡ | **One-click connect** | A big, honest power button with a live pulse while working |
| 🌍 | **Location picker** | Searchable list of all free locations + *Optimal (auto)* mode |
| 🎲 | **IP roll animation** | Watch your IP digits spin and settle on the new one |
| 🕵️ | **IP monitor** | Real IP when offline, exit IP when protected — one click to copy |
| ⏱️ | **Session timer** | Exactly how long you have been protected |
| 🖥️ | **System-wide proxy** | One switch sets your OS proxy and restores it on exit |
| 📜 | **Activity log** | Color-coded, autoscrolling, exportable to a file |
| ⚙️ | **Settings** | Ports, bind address, auto-connect, session reset |
| 🔁 | **Self-healing tunnels** | Round-robin across servers, automatic retry on failure |
| 📦 | **Zero runtime deps** | The core engine is pure Python standard library |

<div align="center">

| Connect | Logs | Settings |
|:---:|:---:|:---:|
| <img src="docs/screenshot-connect.png" width="275"/> | <img src="docs/screenshot-logs.png" width="275"/> | <img src="docs/screenshot-settings.png" width="275"/> |

</div>

## 🚀 Quick Start

### Option 1 — Ready-made binaries

Download from [**Releases**](../../releases):

| File | OS | Run |
|---|---|---|
| `VPeeN-Windows.zip` | Windows 10/11 | unzip → `VPeeN.exe` |
| `VPeeN-macOS.zip` | macOS 10.13+ | unzip → `VPeeN.app` *(right-click → Open, the first time)* |
| `VPeeN-Linux.tar.gz` | Linux x64 | extract → `./VPeeN` |

> Linux tip: if fonts look odd in the bundled build, prefer Option 2 — running from source
> uses your system's font stack and looks perfect.

### Option 2 — Run from source

```bash
git clone https://github.com/SirBNL/VPeeN.git
cd VPeeN
pip install -r requirements.txt
python main.py
```

The app starts, shows your real IP, fetches free locations — press the power button.

### Option 3 — Headless CLI

No GUI needed (servers, routers, scripts):

```bash
python -m vpeen.cli list                 # list free locations
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
3. **Local proxy** — VPeeN listens as a standard SOCKS5 + HTTP proxy on `127.0.0.1`.
4. **Tunneling** — every connection is relayed through an encrypted tunnel to the selected region.
5. **System switch** — optionally flips the OS proxy on (Windows / macOS / GNOME) and restores
   the previous state on exit.

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
├── main.py               # GUI launcher
├── vpeen/
│   ├── gui.py            # CustomTkinter interface (hero / logs / settings)
│   ├── core.py           # background proxy engine + GUI event bridge
│   ├── api.py            # VeePN network client (tokens, locations, servers)
│   ├── upstream.py       # encrypted upstream tunnels
│   ├── localproxy.py     # local SOCKS5 + HTTP servers
│   ├── systemproxy.py    # OS proxy switch (Win / macOS / GNOME)
│   ├── settings.py       # persistent app settings
│   ├── cli.py            # headless command-line mode
│   └── utils.py          # shared helpers
├── assets/               # icon, logo, banner
├── docs/                 # screenshots + demo GIF
└── .github/workflows/    # multi-OS build + release pipeline
```

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
<summary><b>Is this a "real" VPN?</b></summary>
It is a system-wide proxy: TCP traffic of any app can be routed through the tunnel, but there is
no UDP support and no kernel-level device. For browsing, downloading and most daily apps it
behaves exactly like a VPN.
</details>

<details>
<summary><b>Where is my data stored?</b></summary>
Only <code>~/.vpeen/</code> on your machine — cached token, servers and settings. Nothing is sent anywhere else.
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
**کل سیستم‌عامل** باز می‌کنه — با یک رابط گرافیکی تمیز و مدرن، بدون ثبت‌نام و بدون نیاز به کروم.

## ✨ ویژگی‌ها

- ⚡ **اتصال با یک کلیک** — دکمه پاور بزرگ با انیمیشن پالس زنده
- 🌍 **انتخاب موقعیت** — لیست تمام لوکیشن‌های رایگان با جستجو + حالت «بهینه (خودکار)»
- 🎲 **انیمیشن تغییر IP** — ارقام IP موقع اتصال می‌چرخند و روی IP جدید می‌ایستند
- 🕵️ **مانیتور IP** — زمان قطع: IP واقعی تو؛ زمان اتصال: IP خروجی — با یک کلیک کپی می‌شه
- ⏱️ **تایمر جلسه** — دقیقاً می‌گه چقدره که محافظت می‌شی
- 🖥️ **پروکسی سیستم** — با یک سوییچ، پروکسی سیستم‌عامل ست می‌شه و موقع خروج برمی‌گرده
- 📜 **لاگ کامل فعالیت** — رنگی، اسکرول خودکار، قابل ذخیره در فایل
- ⚙️ **تنظیمات** — پورت‌ها، آدرس bind، اتصال خودکار، ریست جلسه
- 🔁 **تانل خودترمیم** — توزیع بار بین سرورها و تلاش مجدد خودکار

## 🚀 نصب و اجرا

**روش ۱ — فایل آماده:** از [Releases](../../releases) نسخه سیستمت رو بگیر:

| فایل | سیستم‌عامل | اجرا |
|---|---|---|
| `VPeeN-Windows.zip` | ویندوز ۱۰/۱۱ | باز کن → `VPeeN.exe` |
| `VPeeN-macOS.zip` | مک ۱۰.۱۳+ | باز کن → `VPeeN.app` *(بار اول راست‌کلیک → Open)* |
| `VPeeN-Linux.tar.gz` | لینوکس ۶۴بیتی | باز کن → `./VPeeN` |

**روش ۲ — اجرا از سورس:**

```bash
git clone https://github.com/SirBNL/VPeeN.git
cd VPeeN
pip install -r requirements.txt
python main.py
```

**روش ۳ — خط فرمان (بدون رابط گرافیکی):**

```bash
python -m vpeen.cli list                  # لیست لوکیشن‌های رایگان
python -m vpeen.cli test nl               # تست کامل زنجیره
python -m vpeen.cli run --region us-va    # SOCKS5 :1080 + HTTP :8080
python -m vpeen.cli export nl             # نمایش کانفیگ خام آپ‌استریم
```

بعد هر برنامه‌ای رو به این آدرس‌ها وصل کن:

```
SOCKS5 :  socks5://127.0.0.1:1080
HTTP   :  http://127.0.0.1:8080
```

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
یک پروکسی سراسریه: ترافیک TCP هر برنامه‌ای از تانل رد می‌شه ولی UDP پشتیبانی نمی‌شه.
برای وب‌گردی، دانلود و اکثر کارهای روزمره دقیقاً مثل VPN رفتار می‌کنه.
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
</div>
