# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for VPeeN - builds a windowed one-file executable."""
import os
import sys

from PyInstaller.utils.hooks import collect_all

datas, binaries, hidden = [], [], []
for pkg in ("customtkinter",):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hidden += h
for asset in ("icon.png", "icon.ico", "logo.png"):
    if os.path.exists(os.path.join("assets", asset)):
        datas.append((os.path.join("assets", asset), "assets"))

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
            "CFBundleShortVersionString": "2.0.0",
            "CFBundleVersion": "2.0.0",
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "10.13",
        },
    )
