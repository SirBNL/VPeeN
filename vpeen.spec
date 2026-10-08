# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for VPeeN - builds a windowed one-file executable.

Bundles the tunnel helper binaries (tun2socks + wintun.dll) that match the
BUILD machine's platform; CI runs one job per OS, so each release gets the
right set.  Run scripts/fetch-binaries.py first.
"""
import os
import platform
import sys

from PyInstaller.utils.hooks import collect_all

datas, binaries, hidden = [], [], []
for pkg in ("customtkinter",):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hidden += h
# ImageTk registers the "PyImagingPhoto" Tk command via PIL._tkinter_finder;
# without these hidden imports the frozen app crashes on the first flag icon.
# "uuid" is imported dynamically nowhere anymore, but keep it pinned as a
# belt-and-suspenders guard for frozen builds (regression v4.1.0: the Windows
# build shipped without it because __import__("uuid") is invisible to the
# static analysis -> fresh installs failed the locations fetch with
# "No module named 'uuid'").
hidden += ["PIL.ImageTk", "PIL._tkinter_finder", "uuid"]

# make sure the _tkinter C extension itself is always bundled (some venvs
# keep it in a non-default location, e.g. a patched site-packages); when it
# lives in the normal stdlib lib-dynload PyInstaller already handles it
import importlib.util
_tk_spec = importlib.util.find_spec("_tkinter")
if _tk_spec and _tk_spec.origin and "site-packages" in _tk_spec.origin:
    binaries.append((_tk_spec.origin, "."))
    hidden.append("_tkinter")
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

icon = "assets/icon.ico" if os.path.exists("assets/icon.ico") else None

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hidden,
    hookspath=[],
    runtime_hooks=[],
    excludes=["matplotlib", "numpy", "pandas", "PySide6", "PyQt5", "test", "unittest"],
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
    app = BUNDLE(
        exe,
        name="VPeeN.app",
        icon="assets/icon.ico",
        bundle_identifier="com.sirbnl.vpeen",
        info_plist={
            "CFBundleName": "VPeeN",
            "CFBundleDisplayName": "VPeeN",
            "CFBundleShortVersionString": "4.2.0",
            "CFBundleVersion": "4.2.0",
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "10.13",
        },
    )
