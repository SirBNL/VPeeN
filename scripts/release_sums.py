#!/usr/bin/env python3
"""VPeeN release tool: rebuild SHA256SUMS.txt for the latest release.

Surgical by design:
  * downloads every asset of the latest release and hashes it
  * every entry ALREADY in SHA256SUMS.txt must match the live asset,
    otherwise the tool REFUSES to touch anything (that is a tamper or
    corruption signal, not something to paper over)
  * assets missing from the sums get their line APPENDED - existing
    lines are never rewritten
  * if the sums asset is missing entirely it is recreated from all assets

Set DRY_RUN=1 (or --dry-run) to only print the plan.  Uses GITHUB_TOKEN
from the environment when present.  Standard library only.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import urllib.request

REPO = "SirBNL/VPeeN"
API = f"https://api.github.com/repos/{REPO}"
SUMS_NAME = "SHA256SUMS.txt"


def _headers(binary: bool) -> dict:
    h = {"Accept": "application/vnd.github+json"}
    tok = os.environ.get("GITHUB_TOKEN")
    if tok:
        h["Authorization"] = f"Bearer {tok}"
    if binary:
        h["Accept"] = "application/octet-stream"
    return h


def _get(url: str, binary: bool = False) -> bytes | dict:
    req = urllib.request.Request(url, headers=_headers(binary))
    with urllib.request.urlopen(req) as r:
        raw = r.read()
    if binary:
        return raw
    return json.loads(raw)


def main() -> int:
    dry = "--dry-run" in sys.argv or os.environ.get("DRY_RUN") == "1"

    rel = _get(f"{API}/releases/latest")
    tag = rel.get("tag_name", "?")
    assets = [a for a in rel.get("assets", []) if isinstance(a, dict)]
    names = [a["name"] for a in assets]
    print(f"latest release: {tag} ({len(assets)} assets)")

    if SUMS_NAME not in names:
        print(f"!! {SUMS_NAME} is MISSING from the release - "
              "the self-updater refuses updates without it")
        sums: dict[str, str] = {}
        sums_lines: list[str] = []
        order: list[str] = []
    else:
        sums_asset = next(a for a in assets if a["name"] == SUMS_NAME)
        text = _get(sums_asset["browser_download_url"], binary=True).decode()
        sums_lines = [l for l in text.splitlines() if l.strip()]
        sums = {}
        order = []
        for line in sums_lines:
            parts = line.split()
            if len(parts) >= 2 and re.fullmatch(r"[0-9a-fA-F]{64}", parts[0]):
                sums[parts[-1]] = parts[0].lower()
                order.append(parts[-1])

    # hash every asset
    live: dict[str, str] = {}
    for a in assets:
        if a["name"] == SUMS_NAME:
            continue
        blob = _get(a["browser_download_url"], binary=True)
        live[a["name"]] = hashlib.sha256(blob).hexdigest()

    # existing entries must match reality - otherwise refuse
    for name, recorded in sums.items():
        if name not in live:
            print(f"REFUSING: sums entry '{name}' has no such asset on the release")
            return 1
        if live[name] != recorded:
            print(f"REFUSING: '{name}' live hash {live[name]} != recorded "
                  f"{recorded} - asset tampered or corrupted, fix it manually")
            return 1
    print(f"verified: {len(sums)} existing entries match the live assets")

    missing = [n for n in live if n not in sums]
    if not missing and SUMS_NAME in names:
        print(f"nothing to do: {SUMS_NAME} already covers all {len(live)} assets")
        return 0

    for n in missing:
        print(f"  + {live[n]}  {n}")
    new_text = "\n".join(sums_lines + [f"{live[n]}  {n}" for n in missing]) + "\n"

    if dry:
        print(f"DRY RUN - would upload this {SUMS_NAME}:\n{new_text}")
        return 0

    # upload: delete the old asset, then POST the new one (uploads must
    # run from an environment that can reach uploads.github.com - a
    # GitHub Actions runner always can; some sandboxes cannot).
    rid = rel["id"]
    if SUMS_NAME in names:
        old = next(a for a in assets if a["name"] == SUMS_NAME)
        req = urllib.request.Request(f"{API}/releases/assets/{old['id']}",
                                     method="DELETE",
                                     headers=_headers(False))
        with urllib.request.urlopen(req) as r:
            r.read()
        print(f"deleted old {SUMS_NAME} asset")

    boundary = "vpeensums"
    body = (f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="name"\r\n\r\n{SUMS_NAME}\r\n'
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{SUMS_NAME}"\r\n'
            f"Content-Type: text/plain\r\n\r\n").encode() + new_text.encode() \
        + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        f"https://uploads.github.com/repos/{REPO}/releases/{rid}",
        data=body, method="POST",
        headers={**_headers(False),
                 "Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req) as r:
        created = json.loads(r.read())
    print(f"uploaded {SUMS_NAME} (asset id {created.get('id')}) covering "
          f"{len(live)} assets")
    print(new_text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
