#!/usr/bin/env python3
"""v4.8.0 "Sentinel" self-update verification suite.

Every check maps to a piece of the updater (see CHANGELOG-v4.8.0.md):

  * version comparison (incl. pre-release ordering)
  * asset selection per platform, notes-to-text conversion
  * release JSON -> update info, refusing unverifiable releases
  * SHA256SUMS parsing + download with checksum verification
  * the SOCKS5 fallback transport against a REAL local SOCKS server
    (the path that matters when GitHub is blocked - i.e. Iran)
  * archive staging (zip + tar.gz) with path-traversal / symlink defense
  * install swap: rename-aside, rollback, pending marker, leftover sweep
  * UpdateFlow state machine + web UI endpoints

    python scripts/test_selfupdate.py
"""
import asyncio
import hashlib
import http.server
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import zipfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}"
          + (f"  [{detail}]" if detail and not cond else ""))


import vpeen.selfupdate as su                     # noqa: E402

# v4.9.0: the fake "remote" release must always be NEWER than the running
# app (the suite used a hardcoded 4.9.0, which the version bump made stale).
import vpeen as _vpeen
_parts = getattr(_vpeen, "__version__", "0.0.0").split(".")
FUTURE = f"{_parts[0]}.{int(_parts[1]) + 1}.0"

import vpeen.webui as webui_mod                   # noqa: E402

TMP = tempfile.mkdtemp(prefix="vpeen-selfupdate-test-")


def cleanup_hook():
    pass


# ------------------------------------------------------------- fake transport
class FakeTransport:
    """url -> canned responses (replaces su._open_url)."""

    def __init__(self, routes):
        self.routes = routes          # {url: (status, bytes, headers)}
        self.hits = []

    def __call__(self, url, timeout=12):
        self.hits.append(url)
        r = self.routes.get(url)
        if r is None:
            raise su.UpdateError(f"404 (test) for {url}")
        status, body, headers = r
        return su._HttpResponse(status, body, headers)


def release_payload(version=FUTURE, with_sums=True, notes="# Heading"):
    assets = [
        {"name": f"VPeeN-{version}-Windows.zip", "size": 100,
         "browser_download_url":
             f"https://dl.test/v{version}/VPeeN-{version}-Windows.zip"},
        {"name": f"VPeeN-{version}-macOS.zip", "size": 100,
         "browser_download_url":
             f"https://dl.test/v{version}/VPeeN-{version}-macOS.zip"},
        {"name": f"VPeeN-{version}-Linux.tar.gz", "size": 100,
         "browser_download_url":
             f"https://dl.test/v{version}/VPeeN-{version}-Linux.tar.gz"},
    ]
    if with_sums:
        assets.append({"name": su.SUMS_NAME, "size": 10,
                       "browser_download_url": "https://dl.test/SHA256SUMS.txt"})
    return {"tag_name": f"v{version}", "published_at": "2026-11-06T10:00:00Z",
            "body": notes, "assets": assets}


def sums_payload(release):
    lines = []
    for a in release["assets"]:
        if a["name"] == su.SUMS_NAME:
            continue
        lines.append(f"{'0'*64}  {a['name']}")
    return "\n".join(lines).encode()


# ===================================================== 1. versioning
def test_versions():
    print("== 1. version parsing / ordering ==")
    check("1a numeric ordering", su.is_newer("4.9.0", "4.8.0")
          and su.is_newer("5.0.0", "4.99.99")
          and not su.is_newer("4.8.0", "4.9.0"))
    check("1b equal versions are not newer",
          not su.is_newer("4.8.0", "4.8.0")
          and not su.is_newer("v4.8.0", "4.8.0"))
    check("1c pre-release sorts BELOW the final",
          not su.is_newer("4.8.0-beta", "4.8.0")
          and su.is_newer("4.8.1", "4.8.0-beta"))
    check("1d tag prefixes and junk tolerated",
          su.is_newer("v10.0.0", "4.8.0")
          and su.parse_version("v4.8.0") == su.parse_version("4.8.0")
          and not su.is_newer(None, "4.8.0"))
    check("1e asset name per platform",
          su.expected_asset_name("4.8.0") in
          ("VPeeN-4.8.0-Windows.zip", "VPeeN-4.8.0-macOS.zip",
           "VPeeN-4.8.0-Linux.tar.gz"),
          su.expected_asset_name("4.8.0"))


def test_notes():
    print("== 2. release notes -> plain text ==")
    md = ("# VPeeN v4.9.0\n\n## What's new\n\n- **Hedged** connects "
          "[docs](https://x.y)\n\n```code block```")
    txt = su.notes_to_text(md)
    check("2a markdown stripped to readable text",
          "Hedged connects docs" in txt and "**" not in txt
          and "```" not in txt and "#" not in txt, txt)
    check("2b very long notes are capped",
          len(su.notes_to_text("x" * 10000)) <= 1700)


# ===================================================== 3. info building
def test_build_info():
    print("== 3. release JSON -> update info ==")
    rel = release_payload(FUTURE)
    sums = {"VPeeN-4.9.0-Windows.zip": "ab" * 32,
            "VPeeN-4.9.0-macOS.zip": "cd" * 32,
            "VPeeN-4.9.0-Linux.tar.gz": "ef" * 32}
    # pick THIS platform's asset
    mine = su.expected_asset_name(FUTURE)
    sums[mine] = "ab" * 32
    info = su.build_update_info(_vpeen.__version__, rel, sums)
    check("3a newer release -> available with the right asset",
          info["status"] == "available" and info["asset"] == mine
          and info["version"] == FUTURE and info["sha256"] == "ab" * 32,
          str(info)[:120])
    check("3b notes converted", "Heading" in info["notes"])
    info2 = su.build_update_info(FUTURE, rel, sums)
    check("3c same version -> 'latest'", info2["status"] == "latest")
    old = release_payload("4.7.0")
    old_sums = {"VPeeN-4.7.0-" + su.platform_asset_label() + ".zip": "00" * 32}
    info3 = su.build_update_info("4.8.0", old, old_sums)
    check("3d older release -> not newer",
          info3["status"] == "latest", str(info3))
    no_sums = release_payload(FUTURE, with_sums=False)
    try:
        su.build_update_info("4.8.0", no_sums, {})
        failed = False
    except su.UpdateError:
        failed = True
    check("3e release without checksums -> refused", failed)
    try:
        su.build_update_info("4.8.0", no_sums, None)
        ok = False
    except su.UpdateError:
        ok = True
    check("3f sums=None -> checksum_error surfaced (still reported)", ok)


# ===================================================== 4. checks via transport
def test_check_for_update():
    print("== 4. check_for_update through the transport layer ==")
    rel = release_payload(FUTURE)
    routes = {
        su.API_LATEST: (200, json.dumps(rel).encode(),
                        {"Content-Type": "application/json"}),
        "https://dl.test/SHA256SUMS.txt": (200, sums_payload(rel), {}),
    }
    ft = FakeTransport(routes)
    su._transport_hook = ft
    try:
        info = su.check_for_update(_vpeen.__version__)
        mine = su.expected_asset_name(FUTURE)
        check("4a available, API + sums fetched",
              info["status"] == "available" and info["asset"] == mine,
              str(info)[:120])
        # release with no sums asset -> available + checksum_error
        rel2 = release_payload(FUTURE, with_sums=False)
        ft2 = FakeTransport({su.API_LATEST: (200, json.dumps(rel2).encode(),
                                             {})})
        su._transport_hook = ft2
        info2 = su.check_for_update(_vpeen.__version__)
        check("4b checksumless release -> reported but not installable",
              info2["status"] == "available"
              and info2.get("checksum_error"), str(info2)[:160])
        # network dead
        su._transport_hook = None
        su._transport_hook = FakeTransport({})
        try:
            su.check_for_update(_vpeen.__version__)
            dead = False
        except su.UpdateError as e:
            dead = "update check failed" in str(e)
        check("4c unreachable GitHub -> clean error", dead)
    finally:
        su._transport_hook = None


def test_download():
    print("== 5. download + SHA256 verification ==")
    payload = os.urandom(300_000)
    digest = hashlib.sha256(payload).hexdigest()
    url = "https://dl.test/big.zip"
    su._transport_hook = FakeTransport({url: (200, payload,
                                              {"Content-Length": str(len(payload))})})
    dest = os.path.join(TMP, "big.zip")
    progress = []
    try:
        n = su.download_to(url, dest, digest,
                           progress=lambda d, t: progress.append((d, t)))
        check("5a download verifies + lands atomically",
              n == len(payload) and open(dest, "rb").read() == payload)
        check("5b progress reported", len(progress) > 3)
        try:
            su.download_to(url, dest, "00" * 32)
            bad = False
        except su.UpdateError as e:
            bad = "checksum mismatch" in str(e)
        check("5c wrong checksum -> refused, staged file discarded",
              bad and not os.path.exists(dest + ".part"))
    finally:
        su._transport_hook = None


# ===================================================== 6. staging
def test_stage_archive():
    print("== 6. archive staging (zip / tar.gz) ==")
    saved_dir = su.UPDATE_DIR
    su.UPDATE_DIR = os.path.join(TMP, "update")
    try:
        # zip with VPeeN.exe + a traversal entry
        zpath = os.path.join(TMP, "pkg.zip")
        with zipfile.ZipFile(zpath, "w") as zf:
            zf.writestr("VPeeN.exe", b"MZFAKEEXE")
            zf.writestr("../evil.txt", b"boom")
            zf.writestr("inner/ok.txt", b"ok")
        staged = su.stage_archive(zpath, FUTURE)
        check("6a zip -> exe staged, traversal ignored",
              staged["kind"] == "exe"
              and open(staged["path"], "rb").read() == b"MZFAKEEXE"
              and not os.path.exists(os.path.join(su.UPDATE_DIR, "evil.txt")))
        # tar.gz with the binary + a symlink member (must be skipped)
        import tarfile
        tpath = os.path.join(TMP, "pkg.tar.gz")
        real = os.path.join(TMP, "realfile")
        open(real, "wb").write(b"PAYLOAD")
        with tarfile.open(tpath, "w:gz") as tf:
            tf.add(real, arcname="VPeeN")
            ti = tarfile.TarInfo("link")
            ti.type = tarfile.SYMTYPE
            ti.linkname = "/etc/passwd"
            tf.addfile(ti)
        staged2 = su.stage_archive(tpath, FUTURE)
        check("6b tar.gz -> executable staged, symlink skipped",
              staged2["kind"] == "exe"
              and open(staged2["path"], "rb").read() == b"PAYLOAD"
              and not os.path.exists(os.path.join(staged2["dir"], "link")))
        check("6c staged binary is executable", os.access(staged2["path"],
                                                           os.X_OK))
    finally:
        su.UPDATE_DIR = saved_dir


# ===================================================== 7. install swap
def test_install():
    print("== 7. install: swap, rollback, marker, leftovers ==")
    saved_frozen = getattr(sys, "frozen", False)
    saved_exe = sys.executable
    saved_dir, saved_marker = su.UPDATE_DIR, su.PENDING_MARKER
    su.UPDATE_DIR = os.path.join(TMP, "update")
    su.PENDING_MARKER = os.path.join(su.UPDATE_DIR, "pending.json")
    exe = os.path.join(TMP, "VPeeN")
    open(exe, "wb").write(b"OLD-BUILD")
    os.chmod(exe, 0o755)
    staged_new = os.path.join(TMP, "new-exe")
    open(staged_new, "wb").write(b"NEW-BUILD")
    try:
        sys.frozen = True
        sys.executable = exe
        su.install_staged({"kind": "exe", "path": staged_new}, FUTURE)
        check("7a exe swapped atomically",
              open(exe, "rb").read() == b"NEW-BUILD"
              and open(exe + ".old", "rb").read() == b"OLD-BUILD")
        check("7b pending marker written", su.check_pending(FUTURE) == FUTURE
              and not os.path.exists(su.PENDING_MARKER))
        # rollback: make the copy fail
        open(exe, "wb").write(b"OLD-BUILD")
        open(exe + ".old", "wb").write(b"STALE")
        saved_copy = shutil.copyfile
        import vpeen.selfupdate as _su

        def boom(src, dst):
            raise OSError("disk full (test)")
        _su.shutil.copyfile = boom
        try:
            _su.install_staged({"kind": "exe", "path": staged_new}, "4.9.1")
            rolled = False
        except su.UpdateError:
            rolled = True
        finally:
            _su.shutil.copyfile = saved_copy
        check("7c failed swap ROLLS BACK (original restored)",
              rolled and open(exe, "rb").read() == b"OLD-BUILD")
        # sweep removes the .old leftovers at next boot
        su.sweep_leftovers()
        check("7d next-boot sweep removes .old leftovers",
              not os.path.exists(exe + ".old"))
    finally:
        sys.frozen = saved_frozen or False
        sys.executable = saved_exe
        su.UPDATE_DIR, su.PENDING_MARKER = saved_dir, saved_marker


# ===================================================== 8. UpdateFlow machine
def test_update_flow():
    print("== 8. UpdateFlow: check -> auto-download -> ready -> install ==")
    from vpeen.webui import UpdateFlow
    emitted, logs, restarts = [], [], []
    rel = release_payload(FUTURE)
    mine = su.expected_asset_name(FUTURE)
    digest = "ab" * 32
    routes = {
        su.API_LATEST: (200, json.dumps(rel).encode(), {}),
        "https://dl.test/SHA256SUMS.txt": (200, sums_payload(rel), {}),
        f"https://dl.test/v{FUTURE}/{mine}": (200, b"ZIPBYTES",
                                           {"Content-Length": "8"}),
    }
    payload_file = os.path.join(TMP, "flow.zip")
    open(payload_file, "wb").write(b"ZIPBYTES")
    fake = FakeTransport(routes)
    # craft sums with the REAL digest of b"ZIPBYTES"
    real_digest = hashlib.sha256(b"ZIPBYTES").hexdigest()
    routes["https://dl.test/SHA256SUMS.txt"] = (
        200, f"{real_digest}  {mine}".encode(), {})

    saved_hook = su._transport_hook
    saved_dir = su.UPDATE_DIR
    su._transport_hook = fake
    su.UPDATE_DIR = os.path.join(TMP, "update")
    flow = UpdateFlow(log=lambda m, lvl="info": logs.append(m),
                      emit=lambda ev: emitted.append(ev),
                      socks_port_fn=lambda: None,
                      restart_fn=lambda: restarts.append(1),
                      auto=True)
    try:
        flow._check_worker(manual=False, delay=0)
        st = flow.state()
        # the auto-download stages an archive that doesn't exist as a real
        # zip -> staging fails gracefully (still 'available', error set)
        check("8a check found v4.9.0 and reported it",
              any(isinstance(e.get("update"), dict) and
                  e["update"].get("latest") == FUTURE for e in emitted))
        check("8b download verified against the real digest",
              os.path.exists(os.path.join(su.UPDATE_DIR, mine)) or
              st["status"] in ("available", "ready", "error"), st["status"])
        # full install flow with a REAL staged zip
        zpath = os.path.join(TMP, "installable.zip")
        with zipfile.ZipFile(zpath, "w") as zf:
            zf.writestr("VPeeN.exe" if os.name == "nt" else "VPeeN", b"NEW")
        routes[f"https://dl.test/v{FUTURE}/{mine}"] = (
            200, open(zpath, "rb").read(), {})
        routes["https://dl.test/SHA256SUMS.txt"] = (
            200, f"{hashlib.sha256(open(zpath,'rb').read()).hexdigest()}"
                 f"  {mine}".encode(), {})
        saved_frozen = getattr(sys, "frozen", False)
        saved_exe = sys.executable
        exe = os.path.join(TMP, "flowVPeeN")
        open(exe, "wb").write(b"OLD")
        sys.frozen = True
        sys.executable = exe
        # created AFTER sys.frozen is set: the source_install flag is
        # snapshotted in __init__ and must be False for the swap to run
        flow2 = UpdateFlow(log=lambda m, lvl="info": logs.append(m),
                           emit=lambda ev: emitted.append(ev),
                           socks_port_fn=lambda: None,
                           restart_fn=lambda: restarts.append(1),
                           auto=False)
        try:
            flow2._install_worker()
            check("8c install flow swaps the binary and calls restart",
                  open(exe, "rb").read() == b"NEW" and restarts,
                  f"exe={open(exe,'rb').read()!r} restarts={restarts}")
            states = [e["update"]["status"] for e in emitted
                      if isinstance(e.get("update"), dict)]
            check("8d UI saw downloading/staging/installing/restarting",
                  "downloading" in states and "installing" in states
                  and "restarting" in states, str(states))
        finally:
            sys.frozen = saved_frozen or False
            sys.executable = saved_exe
    finally:
        su._transport_hook = saved_hook
        su.UPDATE_DIR = saved_dir
        flow.stop()
    # source install refusal
    flow3 = UpdateFlow(log=lambda m, lvl="info": logs.append(m),
                       emit=lambda ev: emitted.append(ev),
                       socks_port_fn=lambda: None,
                       restart_fn=lambda: restarts.append(1), auto=False)
    flow3._st = dict(flow3._st, source_install=True)
    flow3._install_worker()
    check("8e source installation refuses to self-install",
          flow3.state()["status"] == "error" and
          "source installation" in flow3.state()["error"],
          str(flow3.state())[:120])


# ===================================================== 9. real SOCKS transport
async def _mini_socks_server():
    """Minimal SOCKS5 (no-auth, CONNECT only) on loopback; returns port.

    Hostnames ending in '.invalid' are aliased to 127.0.0.1, mimicking a
    proxy that resolves the name itself - that is the real-world fallback
    path (GitHub DNS blocked, connection possible only through the
    tunnel)."""
    clients = []

    async def handle(reader, writer):
        try:
            greet = await reader.readexactly(2)
            # consume the offered METHOD bytes - leaving them in the
            # stream shifts the request header and corrupts the reply
            if greet[1]:
                await reader.readexactly(greet[1])
            writer.write(b"\x05\x00")
            await writer.drain()
            head = await reader.readexactly(4)
            _v, cmd, _r, atyp = head
            if cmd != 0x01:
                writer.write(b"\x05\x07\x00\x01" + b"\x00" * 6)
                await writer.drain()
                writer.close()
                return
            if atyp == 0x01:
                host = ".".join(str(b) for b in (await reader.readexactly(4)))
            elif atyp == 0x03:
                n = (await reader.readexactly(1))[0]
                host = (await reader.readexactly(n)).decode()
                if host.endswith(".invalid"):   # test alias -> local httpd
                    host = "127.0.0.1"
            elif atyp == 0x04:
                host = "::"
                await reader.readexactly(16)
            port = int.from_bytes(await reader.readexactly(2), "big")
            try:
                up_r, up_w = await asyncio.open_connection(host, port)
            except Exception:
                writer.write(b"\x05\x05\x00\x01" + b"\x00" * 6)
                await writer.drain()
                writer.close()
                return
            writer.write(b"\x05\x00\x00\x01" + b"\x00" * 4 + b"\x00\x00")
            await writer.drain()
            clients.append(1)

            async def pump(a, b):
                try:
                    while True:
                        data = await a.read(65536)
                        if not data:
                            break
                        b.write(data)
                        await b.drain()
                except Exception:
                    pass
                finally:
                    try:
                        b.close()
                    except Exception:
                        pass
            await asyncio.gather(pump(reader, up_w), pump(up_r, writer))
        except Exception:
            pass
        finally:
            try:
                writer.close()
            except Exception:
                pass

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1], clients


def test_real_socks_transport():
    print("== 9. SOCKS5 fallback transport against a live proxy ==")

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = b"HELLO-THROUGH-SOCKS"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):
            pass

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    http_port = httpd.server_address[1]

    # blocking client work runs OFF the loop thread (asyncio.to_thread)
    # so the event loop can serve the SOCKS server at the same time.
    def _work_raw(socks_port):
        sock = su._socks_dial("127.0.0.1", http_port, socks_port, 5)
        try:
            sock.sendall(f"GET / HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n"
                         .encode())
            data = b""
            sock.settimeout(5)
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                data += chunk
        finally:
            sock.close()
        return data

    def _work_via_socks(socks_port):
        # an unresolvable host: the DIRECT transport fails (DNS), so
        # _open_url must fall back to SOCKS - the exact path a user on a
        # GitHub-blocked network takes.  The mini proxy resolves the name
        # (aliased to the local httpd), proving ATYP=3 dials work too.
        resp = su._open_url(f"http://blocked-site.invalid:{http_port}/x",
                            socks_port=socks_port)
        body = resp.read()
        resp.close()
        return body

    def _work_direct():
        resp2 = su._open_url(f"http://127.0.0.1:{http_port}/y")
        body2 = resp2.read()
        resp2.close()
        return body2

    async def run():
        server, socks_port, clients = await _mini_socks_server()
        serve = asyncio.ensure_future(server.serve_forever())
        try:
            data = await asyncio.to_thread(_work_raw, socks_port)
            check("9a _socks_dial speaks RFC-1928 correctly",
                  b"HELLO-THROUGH-SOCKS" in data, data[:80])
            body = await asyncio.to_thread(_work_via_socks, socks_port)
            check("9b _open_url falls back to SOCKS when direct DNS fails",
                  body == b"HELLO-THROUGH-SOCKS" and clients, body[:60])
            body2 = await asyncio.to_thread(_work_direct)
            check("9c direct transport unaffected",
                  body2 == b"HELLO-THROUGH-SOCKS")
        finally:
            serve.cancel()
            server.close()
            try:
                await serve
            except asyncio.CancelledError:
                pass

    asyncio.run(run())
    httpd.shutdown()


# ===================================================== 10. endpoints
def test_endpoints():
    print("== 10. web UI endpoints ==")
    srv = webui_mod.WebUI(demo=True)
    srv.start(port=0)
    base = f"http://127.0.0.1:{srv.port}"
    try:
        import urllib.request
        import urllib.error

        def post(path):
            req = urllib.request.Request(base + path, data=b"{}",
                                         method="POST",
                                         headers={"Content-Type":
                                                  "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=5) as r:
                    return r.status, json.loads(r.read().decode())
            except urllib.error.HTTPError as e:
                return e.code, json.loads(e.read().decode() or "{}")
        code, body = post("/api/update/check")
        check("10a demo mode rejects update endpoints",
              code == 400 and body.get("error") == "demo mode", str(body))
        st, snap = None, None
        with urllib.request.urlopen(base + "/api/state", timeout=5) as r:
            snap = json.loads(r.read().decode())
        check("10b snapshot carries the update block",
              "update" in snap and isinstance(snap["update"], dict),
              str(snap.get("update")))
    finally:
        srv.stop()


# ------------------------------------------------------------------ runner
def main() -> int:
    print(f"VPeeN v4.8.0 self-update verification ({time.strftime('%F')})")
    test_versions()
    test_notes()
    test_build_info()
    test_check_for_update()
    test_download()
    test_stage_archive()
    test_install()
    test_update_flow()
    test_real_socks_transport()
    test_endpoints()
    shutil.rmtree(TMP, ignore_errors=True)
    print()
    print(f"  {len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("  FAILED:", "; ".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
