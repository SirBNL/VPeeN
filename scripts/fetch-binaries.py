#!/usr/bin/env python3
"""Fetch tunnel helper binaries for VPeeN.

Downloads pinned releases of tun2socks (MIT) and wintun into
assets/bin/<platform>/ so PyInstaller can bundle them.  Runs on CI and
locally.  Never downloads anything at app runtime.

Usage:  python scripts/fetch-binaries.py [--only <platform> ...]
"""
import argparse
import hashlib
import io
import os
import sys
import urllib.request
import zipfile

TUN2SOCKS_VERSION = "2.7.0"
WINTUN_VERSION = "0.14.1"

# sha256 of the wintun zip, pinned for supply-chain safety
WINTUN_URL = f"https://www.wintun.net/builds/wintun-{WINTUN_VERSION}.zip"
WINTUN_SHA256 = "07c256185d6ee3652e09fa55c0b673e2624b565e02c4b9091c79ca7d2f24ef51"

TUN2SOCKS_ASSETS = {
    "windows-amd64": "tun2socks-windows-amd64.zip",
    "linux-amd64": "tun2socks-linux-amd64.zip",
    "darwin-amd64": "tun2socks-darwin-amd64.zip",
    "darwin-arm64": "tun2socks-darwin-arm64.zip",
}

BASE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "assets", "bin")


def _fetch(url: str) -> bytes:
    print(f"  GET {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "VPeeN-build"})
    with urllib.request.urlopen(req, timeout=180) as r:
        return r.read()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fetch_tun2socks(platform_key: str) -> None:
    asset = TUN2SOCKS_ASSETS[platform_key]
    url = (f"https://github.com/xjasonlyu/tun2socks/releases/download/"
           f"v{TUN2SOCKS_VERSION}/{asset}")
    blob = _fetch(url)
    zf = zipfile.ZipFile(io.BytesIO(blob))
    members = [n for n in zf.namelist()
               if n.split("/")[-1].startswith("tun2socks")]
    if not members:
        raise RuntimeError(f"binary not found inside {asset} ({zf.namelist()})")
    exe = zf.read(members[0])
    name = "tun2socks.exe" if platform_key.startswith("windows") else "tun2socks"
    out_dir = os.path.join(BASE, platform_key)
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, name)
    with open(out_path, "wb") as f:
        f.write(exe)
    if not platform_key.startswith("windows"):
        os.chmod(out_path, 0o755)
    print(f"  ok  {out_path}  ({len(exe) // (1024 * 1024)} MB, "
          f"sha256={_sha256(exe)[:16]}...)")


def fetch_wintun() -> None:
    blob = _fetch(WINTUN_URL)
    if _sha256(blob) != WINTUN_SHA256:
        raise RuntimeError("wintun zip sha256 mismatch - aborting")
    zf = zipfile.ZipFile(io.BytesIO(blob))
    for arch in ("amd64", "x86", "arm64"):
        member = f"wintun/bin/{arch}/wintun.dll"
        try:
            dll = zf.read(member)
        except KeyError:
            continue
        out_dir = os.path.join(BASE, "wintun", arch)
        os.makedirs(out_dir, exist_ok=True)
        with open(os.path.join(out_dir, "wintun.dll"), "wb") as f:
            f.write(dll)
        with open(os.path.join(BASE, "wintun", "LICENSE.txt"), "wb") as f:
            f.write(zf.read("wintun/LICENSE.txt"))
        print(f"  ok  {out_dir}/wintun.dll  ({len(dll) // 1024} KB)")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*", default=None,
                    help="platform keys: " + ", ".join(TUN2SOCKS_ASSETS))
    ap.add_argument("--skip-wintun", action="store_true")
    args = ap.parse_args()
    wanted = args.only or list(TUN2SOCKS_ASSETS)
    if not args.only and os.environ.get("VPeeN_PLATFORM"):   # CI hint
        wanted = [os.environ["VPeeN_PLATFORM"]]
    print("Fetching tun2socks binaries ...")
    for key in wanted:
        if key not in TUN2SOCKS_ASSETS:
            print(f"  !! unknown platform {key}")
            continue
        fetch_tun2socks(key)
    if not args.skip_wintun:
        print("Fetching wintun ...")
        fetch_wintun()
    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
