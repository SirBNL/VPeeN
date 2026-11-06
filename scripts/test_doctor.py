#!/usr/bin/env python3
"""v4.9.0 Doctor (protection self-check) verification suite.

  1. doctor.run_checks() - the pure engine: every state combination the
     snapshot can be in (tunnel/proxy, connected/disconnected, IP match
     or mismatch, kill switch states, DNS settings, bypass list...)
  2. the web layer            - POST /api/doctor against the REAL demo
     server; the report lands in /api/state and as a UI event
  3. the frontend             - the shield button, the modal, the event
     handler and localized titles for every check id

    python scripts/test_doctor.py
"""
import json
import os
import sys
import time
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}"
          + (f"  [{detail}]" if detail and not cond else ""))


import vpeen.doctor as dr                      # noqa: E402
import vpeen.settings as cfgmod                # noqa: E402


def by_id(report):
    return {c["id"]: c for c in report}


BASE = {
    "phase": "connected", "mode": "tunnel",
    "listeners": {"socks_port": 1080, "http_port": 8080},
    "exit_ip": "185.244.29.219", "direct_ip": "78.39.106.14",
    "system_proxy": False, "ks": None,
    "settings": dict(cfgmod.DEFAULTS),
}


def snap(**over):
    s = dict(BASE)
    s["settings"] = dict(BASE["settings"])
    if "settings" in over:
        s["settings"].update(over.pop("settings"))
    s.update(over)
    return s


# ===================================================== 1. the engine
def test_engine():
    print("== 1. run_checks (pure engine) ==")
    # ---- fully healthy tunnel session
    r = by_id(dr.run_checks(snap()))
    check("1a healthy tunnel: listeners ok", r["listeners"]["status"] == "ok")
    check("1b healthy tunnel: IP differs ok",
          r["ip_split"]["status"] == "ok"
          and "185.244.29.219" in r["ip_split"]["detail"])
    check("1c healthy tunnel: routing ok (full-device)",
          r["routing"]["status"] == "ok" and "every app" in
          r["routing"]["detail"])
    check("1d healthy tunnel: DNS ok (relayed)",
          r["dns"]["status"] == "ok")
    check("1e healthy tunnel: kill switch armed ok",
          r["killswitch"]["status"] == "ok")
    check("1f healthy tunnel: QUIC has no bypass path",
          r["quic"]["status"] == "ok")

    # ---- THE leak: exit == direct while connected
    r = by_id(dr.run_checks(snap(exit_ip="78.39.106.14")))
    check("1g IP LEAK detected as FAIL",
          r["ip_split"]["status"] == "fail" and "LEAK" in
          r["ip_split"]["detail"])
    check("1h worst_status picks the fail", dr.worst_status(
        dr.run_checks(snap(exit_ip="78.39.106.14"))) == "fail")

    # ---- proxy mode
    r = by_id(dr.run_checks(snap(mode="proxy")))
    check("1i proxy mode: routing warn (partial coverage)",
          r["routing"]["status"] == "warn")
    check("1j proxy mode: QUIC side channel warn",
          r["quic"]["status"] == "warn" and "QUIC" in r["quic"]["detail"])
    check("1k proxy mode: DNS warn",
          r["dns"]["status"] == "warn")
    check("1l proxy mode: kill switch warns it cannot protect",
          r["killswitch"]["status"] == "warn")

    # ---- kill switch states
    r = by_id(dr.run_checks(snap(ks="lockdown")))
    check("1m lockdown -> kill switch FAIL with guidance",
          r["killswitch"]["status"] == "fail")
    r = by_id(dr.run_checks(snap(ks="healing")))
    check("1n healing -> kill switch WARN", r["killswitch"]["status"] == "warn")
    r = by_id(dr.run_checks(snap(settings={"kill_switch": False})))
    check("1o disabled kill switch -> warn", r["killswitch"]["status"] == "warn")

    # ---- DNS variants
    r = by_id(dr.run_checks(snap(settings={"tunnel_dns": False})))
    check("1p tunnel without DNS relay -> warn",
          r["dns"]["status"] == "warn")

    # ---- disconnected
    r = by_id(dr.run_checks(snap(phase="disconnected", mode="proxy",
                                 listeners={}, exit_ip=None)))
    check("1q disconnected: listeners warn",
          r["listeners"]["status"] == "warn")
    check("1r disconnected: routing reflects the NEXT mode",
          "next connect" in r["routing"]["detail"])
    check("1s disconnected: DNS warn (resolver sees hostnames)",
          r["dns"]["status"] == "warn")

    # ---- system proxy + bypass note
    r = by_id(dr.run_checks(snap(mode="proxy", system_proxy=True,
                                 settings={"bypass_domains":
                                           "*.ir, bank.example.com"})))
    check("1t system proxy on -> ok + bypass count in the detail",
          r["system"]["status"] == "ok"
          and "2 domain" in r["system"]["detail"])
    r = by_id(dr.run_checks(snap(mode="proxy", system_proxy=True)))
    check("1u system proxy on, no bypass -> clean detail",
          r["system"]["status"] == "ok"
          and "bypass" not in r["system"]["detail"])

    # ---- structural invariants
    rep = dr.run_checks(snap())
    ids = [c["id"] for c in rep]
    check("1v report is a stable ordered list of 8 checks",
          ids == ["listeners", "ip_split", "routing", "dns",
                  "killswitch", "system", "quic", "webrtc"], str(ids))
    check("1w every check has id/status/title/detail with valid status",
          all(set(c) >= {"id", "status", "title", "detail"}
              and c["status"] in ("ok", "warn", "fail")
              and c["title"] and isinstance(c["detail"], str)
              for c in rep))
    check("1x WebRTC advisory always present",
          by_id(rep)["webrtc"]["status"] == "warn")
    check("1y summarize() counts correctly",
          dr.summarize(rep) ==
          f"8 checks: "
          f"{sum(1 for c in rep if c['status'] == 'ok')} ok, "
          f"{sum(1 for c in rep if c['status'] == 'warn')} warnings, "
          f"{sum(1 for c in rep if c['status'] == 'fail')} problems",
          dr.summarize(rep))
    check("1z worst_status of an all-ok report is ok",
          dr.worst_status([{"status": "ok"}]) == "ok")


# ===================================================== 2. web layer
def test_web_layer():
    print("== 2. /api/doctor against the real (demo) server ==")
    import vpeen.webui as webui_mod
    ui = webui_mod.WebUI(demo=True, native_window=False)
    ui.start(0)
    base = f"http://127.0.0.1:{ui.port}"
    try:
        def get(path):
            with urllib.request.urlopen(base + path, timeout=10) as r:
                return json.loads(r.read().decode())

        def post(path, body):
            req = urllib.request.Request(
                base + path, data=json.dumps(body).encode(),
                headers={"Content-Type": "application/json"},
                method="POST")
            with urllib.request.urlopen(req, timeout=10) as r:
                return json.loads(r.read().decode())

        # connect in demo mode so the snapshot is "connected"
        check("2a demo connect accepted", post("/api/connect",
                                               {"region": "nl"})["ok"])
        deadline = time.time() + 10
        connected = False
        while time.time() < deadline:
            st = get("/api/state")
            if st.get("phase") == "connected":
                connected = True
                break
            time.sleep(0.25)
        check("2b demo session reached connected", connected)

        check("2c POST /api/doctor returns ok",
              post("/api/doctor", {})["ok"])
        report = None
        deadline = time.time() + 10
        while time.time() < deadline:
            st = get("/api/state")
            if st.get("doctor"):
                report = st["doctor"]
                break
            time.sleep(0.2)
        check("2d the report lands in /api/state", report is not None)
        if report:
            ids = [c["id"] for c in report]
            check("2e the live report carries all 8 checks",
                  set(ids) == {"listeners", "ip_split", "routing", "dns",
                               "killswitch", "system", "quic", "webrtc"},
                  str(ids))
            r = by_id(report)
            check("2f demo: listeners ok (live ports)",
                  r["listeners"]["status"] == "ok")
            check("2g demo: exit IP differs from the real IP -> ok",
                  r["ip_split"]["status"] == "ok"
                  and "185.244.29.219" in r["ip_split"]["detail"])
            check("2h demo: proxy mode warns about partial coverage",
                  r["routing"]["status"] == "warn")
        # the doctor event also flows through the poll stream
        st = get("/api/state")
        since = st.get("seq", 0)
        post("/api/doctor", {})
        ev = None
        deadline = time.time() + 10
        while time.time() < deadline and ev is None:
            with urllib.request.urlopen(
                    f"{base}/api/poll?since={since}", timeout=10) as r2:
                data = json.loads(r2.read().decode())
            for e in data.get("events", []):
                if e.get("t") == "doctor":
                    ev = e
            time.sleep(0.2)
        check("2i the doctor event reaches the poll stream",
              ev is not None and isinstance(ev.get("report"), list)
              and len(ev["report"]) == 8)
        # idempotent / busy guard: rapid double call stays healthy
        post("/api/doctor", {})
        post("/api/doctor", {})
        check("2j rapid double-run does not explode", True)
    finally:
        ui.stop()


# ===================================================== 3. frontend
def test_frontend():
    print("== 3. frontend wiring ==")
    html = open(os.path.join(REPO, "vpeen", "web", "index.html"),
                encoding="utf-8").read()
    js = open(os.path.join(REPO, "vpeen", "web", "app.js"),
                encoding="utf-8").read()
    css = open(os.path.join(REPO, "vpeen", "web", "style.css"),
               encoding="utf-8").read()
    check("3a topbar shield button present",
          'id="btn-doctor"' in html and 'data-i18n-title="title_btn_doctor"'
          in html)
    check("3b doctor modal present with list + run button",
          'id="modal-doctor"' in html and 'id="doctor-list"' in html
          and 'id="btn-doctor-run"' in html)
    check("3c app.js handles the doctor event + renders",
          'case "doctor"' in js and "function renderDoctor" in js
          and "runDoctor" in js)
    check("3d localized titles keyed by check id (dr_<id>)",
          't("dr_" + c.id)' in js)
    check("3e doctor rows styled for all three statuses",
          ".dr-row.ok" in css and ".dr-row.warn" in css
          and ".dr-row.fail" in css)
    # v4.9.1 regression: the modal open-state CSS only listed settings and
    # update, so the doctor modal opened INVISIBLE (opacity:0, no pointer
    # events) and the shield button appeared to do nothing.
    check("3e2 the doctor modal actually becomes visible when opened",
          'body[data-modal="doctor"] #modal-doctor { opacity: 1'
          in css.replace("\n", " ")
          and 'body[data-modal="doctor"] #modal-doctor .modal-card'
          in css.replace("\n", " "))
    for key in ("doctor_title", "doctor_run", "doctor_running",
                "doctor_idle", "doctor_st_ok", "doctor_st_warn",
                "doctor_st_fail", "dr_listeners", "dr_ip_split",
                "dr_routing", "dr_dns", "dr_killswitch", "dr_system",
                "dr_quic", "dr_webrtc"):
        check(f"3f i18n key {key} exists in BOTH languages",
              js.count(f'"{key}"') >= 2, str(js.count(f'"{key}"')))


def main():
    print("VPeeN v4.9.0 doctor verification\n" + "=" * 52)
    test_engine()
    test_web_layer()
    test_frontend()
    print("-" * 52)
    print(f"RESULT: {len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED:", *FAIL, sep="\n  - ")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
