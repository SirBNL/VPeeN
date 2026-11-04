# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for VPeeN - builds a windowed one-file executable.

Bundles the tunnel helper binaries (tun2socks + wintun.dll) that match the
BUILD machine's platform; CI runs one job per OS, so each release gets the
right set.  Run scripts/fetch-binaries.py first.

v4.4.0: the GUI is a local web app (vpeen/web) shown through pywebview
(Windows/macOS) or the default browser (Linux).  CustomTkinter/Pillow/Tk
are gone: the bundle no longer carries the whole Tk runtime.
"""
import os
import platform
import re
import sys

from PyInstaller.utils.hooks import collect_all

# v4.3.1: read the real version from vpeen/__init__.py.  The macOS bundle
# used to carry a HARD-CODED "4.2.2" while the app itself was 4.3.0 -
# Finder/About showed a stale version for two releases straight.
_version = "0.0.0"
with open(os.path.join("vpeen", "__init__.py"), "r", encoding="utf-8") as _f:
    _m = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', _f.read(), re.M)
    if _m:
        _version = _m.group(1)
print(f"VPeeN bundle version: {_version}")

datas, binaries, hidden = [], [], []   # v4.4.0: web UI (no Tk/CustomTkinter)
# ---- the web GUI assets (index.html / style.css / app.js)
_web_dir = os.path.join("vpeen", "web")
if os.path.isdir(_web_dir):
    for _fn in sorted(os.listdir(_web_dir)):
        if _fn.endswith((".html", ".css", ".js")):
            datas.append((os.path.join(_web_dir, _fn), "vpeen/web"))
else:
    raise SystemExit("!! vpeen/web is missing - cannot bundle the GUI")

# ---- pywebview (optional native window).  Linux builds skip it; the
# frozen app falls back to browser mode at runtime (vpeen.gui probes it).
try:
    import webview  # noqa: F401  - availability probe only
    _have_webview = True
except Exception:
    _have_webview = False
if _have_webview and not sys.platform.startswith("linux"):
    _d, _b, _h = collect_all("webview")
    datas += _d
    binaries += _b
    hidden += _h

# v4.4.0: "uuid" stays pinned as a belt-and-suspenders guard for frozen
# builds (regression v4.1.0: __import__("uuid") is invisible to static
# analysis -> fresh installs failed the locations fetch).
hidden += ["uuid"]
for asset in ("icon.png", "icon.ico", "logo.png"):
    if os.path.exists(os.path.join("assets", asset)):
        datas.append((os.path.join("assets", asset), "assets"))

# ---- circular country flags shown in the location panel
flag_dir = os.path.join("assets", "flags")
if os.path.isdir(flag_dir):
    for fn in sorted(os.listdir(flag_dir)):
        if fn.endswith(".png"):
            datas.append((os.path.join(flag_dir, fn), "assets/flags"))

# ---- tunnel helper binaries for this platform
machine = platform.machine().lower()
system = platform.system().lower()
if system.startswith("windows"):
    key = "windows-amd64" if "64" in machine else "windows-x86"
elif system == "darwin":
    key = "darwin-arm64" if machine in ("arm64", "aarch64") else "darwin-amd64"
else:
    key = "linux-amd64" if "64" in machine else "linux-x86"

tun_name = "tun2socks.exe" if key.startswith("windows") else "tun2socks"
tun_path = os.path.join("assets", "bin", key, tun_name)
if key.startswith("windows"):
    if os.path.exists(tun_path):
        datas.append((tun_path, "assets/bin/" + key))
    else:
        print(f"!! WARNING: {tun_path} missing - tunnel mode unavailable")
    arch = "amd64" if "64" in machine else ("arm64" if "arm" in machine else "x86")
    wt = os.path.join("assets", "bin", "wintun", arch, "wintun.dll")
    if os.path.exists(wt):
        datas.append((wt, "assets/bin/" + key))   # must sit next to tun2socks.exe
    lic = os.path.join("assets", "bin", "wintun", "LICENSE.txt")
    if os.path.exists(lic):
        datas.append((lic, "assets/bin"))
elif os.path.exists(tun_path):
    # POSIX: keep the executable bit by registering it as a PyInstaller binary
    binaries.append((tun_path, "assets/bin/" + key))
    if system == "darwin":
        # ship BOTH architectures; binary_paths() picks at runtime
        other = "darwin-amd64" if key == "darwin-arm64" else "darwin-arm64"
        other_path = os.path.join("assets", "bin", other, tun_name)
        if os.path.exists(other_path):
            binaries.append((other_path, "assets/bin/" + other))
else:
    print(f"!! WARNING: {tun_path} missing - tunnel mode unavailable")

# Icon: per-platform.  v4.4.0 dropped Pillow from the build env, and
# PyInstaller needs a native .icns on macOS (it can no longer auto-convert
# .ico without Pillow -> the v4.4.0 macOS CI build died on exactly that).
# assets/icon.icns is generated from icon.png (see scripts/make_icns.py)
# and committed so CI never needs Pillow for icon conversion again.
if system == "darwin":
    icon = "assets/icon.icns" if os.path.exists("assets/icon.icns") else (
        "assets/icon.png" if os.path.exists("assets/icon.png") else None)
elif system.startswith("windows"):
    icon = "assets/icon.ico" if os.path.exists("assets/icon.ico") else None
else:
    icon = "assets/icon.png" if os.path.exists("assets/icon.png") else None

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hidden,
    hookspath=[],
    runtime_hooks=[],
    excludes=["matplotlib", "numpy", "pandas", "PySide6", "PyQt5",
              "tkinter", "customtkinter", "PIL", "test", "unittest"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="VPeeN",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    icon=icon,
)

if sys.platform == "darwin":
    _bundle_icon = ("assets/icon.icns" if os.path.exists("assets/icon.icns")
                    else icon)
    app = BUNDLE(
        exe,
        name="VPeeN.app",
        icon=_bundle_icon,
        bundle_identifier="com.sirbnl.vpeen",
        info_plist={
            "CFBundleName": "VPeeN",
            "CFBundleDisplayName": "VPeeN",
            "CFBundleShortVersionString": _version,
            "CFBundleVersion": _version,
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "10.13",
        },
    )
