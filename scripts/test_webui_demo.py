#!/usr/bin/env python3
"""End-to-end checks against a LIVE demo-mode web-UI server.

Spins up `vpeen.webui.UiServer(demo=True)` on a loopback port and talks
plain HTTP to it exactly like the bundled front-end does: static assets,
status snapshot, locations, pings, settings validation, connect /
disconnect timeline (with the region switch + pending-reconnect paths),
and the long-poll event stream.

Run:  python3 scripts/test_webui_demo.py
"""
import json
import os
import sys

import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vpeen.webui import WebUI as UiServer  # noqa: E402


def http_get(base, path, timeout=5):
    with urllib.request.urlopen(base + path, timeout=timeout) as r:
        return r.status, r.read()


def http_post(base, path, payload=None, timeout=15):
    data = json.dumps(payload or {}).encode()
    req = urllib.request.Request(base + path, data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        try:
            body = json.loads(body)
        except Exception:
            pass
        return e.code, body


def poll_events(base, timeout=10):
    """One long-poll round: GET /api/poll -> {seq, events:[...]}."""
    try:
        st, body = http_get(base, "/api/poll", timeout=timeout)
        if st != 200:
            return []
        return json.loads(body.decode()).get("events", [])
    except Exception:
        return []


def main():
    results = []

    def check(name, cond, detail=""):
        results.append((name, bool(cond)))
        print(f"  {'PASS' if cond else 'FAIL'}  {name}"
              + (f"  [{detail!r}]" if detail and not cond else ""))

    srv = UiServer(demo=True)
    srv.start(port=0)
    base = f"http://127.0.0.1:{srv.port}"
    try:
        # ---- static assets
        st, body = http_get(base, "/")
        check("GET / serves the app shell",
              st == 200 and b"VPeeN" in body)
        st, css = http_get(base, "/style.css")
        check("style.css served", st == 200 and b"--" in css)
        st, js = http_get(base, "/app.js")
        check("app.js served", st == 200 and b"VPeeN" in js or b"vpeen" in js)

        # ---- state snapshot (demo: disconnected, real IP visible)
        st, body = http_get(base, "/api/state")
        status = json.loads(body.decode())
        check("state JSON parses", st == 200)
        check("demo state is disconnected",
              status.get("phase") == "disconnected")
        check("demo direct IP present",
              status.get("direct_ip") == "78.39.106.14")
        check("demo locations preloaded",
              isinstance(status.get("locations"), list) and
              len(status["locations"]) >= 5)
        check("demo pings preloaded", status.get("pings", {}).get("nl") == 18)
        check("snapshot embeds the session log tail",
              isinstance(status.get("logs"), list))

        # ---- pings endpoint (POST; demo replies from canned data)
        st, resp = http_post(base, "/api/pings", {"regions": ["nl", "gb-lnd"]})
        check("pings endpoint accepted", st == 200 and resp.get("ok") is True)

        # ---- settings validation (200 + ok:false + per-field error text)
        st, resp = http_post(base, "/api/settings",
                             {"socks_port": 70000})
        errs = resp.get("errors") or []
        check("out-of-range port rejected at save time",
              st == 200 and resp.get("ok") is False and
              any("socks_port" in str(e) for e in errs))
        st, resp = http_post(base, "/api/settings", {"tunnel_mtu": 100})
        errs = resp.get("errors") or []
        check("out-of-range MTU rejected at save time",
              st == 200 and resp.get("ok") is False and
              any("tunnel_mtu" in str(e) for e in errs))
        st, resp = http_post(base, "/api/settings", {"socks_port": 10808})
        check("valid settings accepted",
              st == 200 and resp.get("ok") is True)

        # ---- connect timeline (demo mode)
        st, resp = http_post(base, "/api/connect", {"region": "nl"})
        check("connect accepted", st == 200 and resp.get("ok") is True)
        events = poll_events(base)
        kinds = [e.get("t") for e in events]
        check("connecting phase event seen", "phase" in kinds)
        check("log lines stream during connect", any(
            e.get("t") == "log" for e in events))
        deadline = time.monotonic() + 12
        connected = False
        exit_ip = None
        while time.monotonic() < deadline:
            st, body = http_get(base, "/api/state")
            status = json.loads(body.decode())
            if status.get("phase") == "connected":
                connected = True
                exit_ip = status.get("exit_ip")
                break
            time.sleep(0.4)
        check("demo connect reaches connected", connected,
              f"phase={status.get('phase')}")
        check("demo exit IP reported", exit_ip == "185.244.29.219")

        # ---- region switch while connected (demo path)
        st, resp = http_post(base, "/api/connect", {"region": "gb-lnd"})
        check("region switch accepted while connected",
              st == 200 and resp.get("ok") is True)
        time.sleep(1.0)
        st, body = http_get(base, "/api/state")
        status = json.loads(body.decode())
        check("switch keeps the session alive",
              status.get("phase") in ("connecting", "connected"))

        # ---- disconnect
        st, resp = http_post(base, "/api/disconnect", {})
        check("disconnect accepted", st == 200 and resp.get("ok") is True)
        deadline = time.monotonic() + 6
        back = False
        while time.monotonic() < deadline:
            st, body = http_get(base, "/api/state")
            status = json.loads(body.decode())
            if status.get("phase") == "disconnected":
                back = True
                break
            time.sleep(0.3)
        check("disconnect returns to disconnected", back)

        # ---- logs ride along in the snapshot
        check("logs recorded the session",
              len(status.get("logs", [])) >= 3)
    finally:
        srv.stop()

    failed = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} PASS")
    if failed:
        print("FAILED:")
        for n in failed:
            print(f"  - {n}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
