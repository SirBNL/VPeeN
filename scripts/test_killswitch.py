#!/usr/bin/env python3
"""v4.9.0 Kill Switch verification suite.

The kill switch must answer ONE question honestly: "when tun2socks dies
mid-session, does traffic stay contained instead of leaking?"  Verified
here at every layer that can be exercised from any OS:

  1. vpeen/killswitch.py      - the containment engine: netsh (win) and
     iptables (linux) rule sequences, idempotent engage/release, stale
     sweep, journal step format
  2. tunnel_platforms         - rebind_tun_routes (the repair surgery
     per OS) and the no-leak ORDER of bring_down/cleanup_from_session
     (containment lifted before routes)
  3. the worker               - detection -> contain -> repair -> heal /
     lockdown state machine, incl. the no-GUI orphan case and the
     heartbeat-loss policy (kill switch ON = keep the blackhole)
  4. plumbing                 - controller forwards ks_* events, the
     bridge maps them to UI events, the setting defaults ON
  5. sweeps                   - boot-time stale-rule + zombie hygiene

    python scripts/test_killswitch.py
"""
import asyncio
import os
import platform as _platform
import queue
import sys
import threading
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}"
          + (f"  [{detail}]" if detail and not cond else ""))


def drive(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class _FakeExit(Exception):
    """Sentinel raised by the patched os._exit so loop semantics match
    reality (a real _exit never returns; a returning fake would fall
    through into code that must stay unreachable)."""


def _patched_exit(code=0):
    raise _FakeExit(code)


async def _noop_async(*a, **k):
    pass


def _down_async(log):
    async def _d(reason):
        log.append(reason)
    return _d


import vpeen.killswitch as ks                      # noqa: E402
import vpeen.tunnel_platforms as plat              # noqa: E402
import vpeen.tunnel as tun_mod                     # noqa: E402
import vpeen.webui as webui_mod                    # noqa: E402
import vpeen.settings as cfgmod                    # noqa: E402


class FakePopen:
    def __init__(self, rc=None):
        self.returncode = rc
        self._rc = rc

    def poll(self):
        return self._rc

    def terminate(self):
        self._rc = 0

    def kill(self):
        self._rc = 9

    def wait(self, timeout=None):
        return self._rc


# ===================================================== 1. containment engine
def test_engine():
    print("== 1. containment engine (fake netsh/iptables) ==")
    saved = (ks._run_hook, ks._which_hook, ks._is_windows, ks._is_linux)
    cmds = []
    live = {"win": False, "v4": False, "v6": False}

    def fake_run(cmd, timeout=25):
        cmds.append(list(cmd))
        j = " ".join(cmd)
        if "add rule" in j:
            live["win"] = True
            return (0, "", "")
        if "delete rule" in j:
            live["win"] = False
            return (0, "", "")
        if "show rule" in j:
            return (0, f"{ks.RULE_TAG}:\n" if live["win"] else "", "")
        fam = "v6" if "ip6tables" in cmd[0] else "v4"
        if " -S OUTPUT" in j:
            return (0, f"-A OUTPUT ... /* {ks.RULE_TAG} */\n"
                    if live[fam] else "", "")
        if " -I OUTPUT" in j or " -D OUTPUT" in j:
            live[fam] = " -I " in j
            return (0, "", "")
        return (0, "", "")

    try:
        ks._run_hook = fake_run

        # ---- Windows path
        ks._is_windows = lambda: True
        ks._is_linux = lambda: False
        cmds.clear()
        step = ks.engage()
        joined = [" ".join(c) for c in cmds]
        check("1a win engage: delete-then-add netsh (no rule stacking)",
              step == {"type": "ks", "engine": "netsh", "families": ["v4"]}
              and any("delete rule" in j for j in joined)
              and any("add rule" in j and "action=block" in j
                      and "dir=out" in j for j in joined), str(joined))
        check("1b every rule carries the fixed tag",
              all(ks.RULE_TAG in j for j in joined if "rule" in j))
        cmds.clear()
        ok = ks.release(step)
        check("1c win release deletes the rule (verified absent)",
              ok is True and live["win"] is False
              and any("delete rule" in " ".join(c) for c in cmds))
        check("1d release(None)/({})/unknown engine are no-ops",
              ks.release(None) is True and ks.release({}) is True
              and ks.release({"type": "ks", "engine": "carrier-pigeon"})
              is True)

        # ---- Windows engage failure
        def boom_add(cmd, timeout=25):
            j = " ".join(cmd)
            if "add rule" in j:
                return (1, "", "The service has not been started (test)")
            if "show rule" in j:
                return (0, "", "")
            return (0, "", "")
        ks._run_hook = boom_add
        check("1e engage failure -> None (caller degrades to repair-only)",
              ks.engage() is None)

        # ---- Linux path (v4 + v6)
        ks._run_hook = fake_run
        ks._is_windows = lambda: False
        ks._is_linux = lambda: True
        found = {"iptables": "/sbin/iptables",
                 "ip6tables": "/sbin/ip6tables"}
        ks._which_hook = lambda n: found.get(n)
        cmds.clear()
        step = ks.engage()
        joined = [" ".join(c) for c in cmds]
        check("1f linux engage: v4+v6 DROP on OUTPUT, loopback exempt",
              step == {"type": "ks", "engine": "iptables",
                       "families": ["v4", "v6"]}
              and any("-I OUTPUT 1 ! -o lo" in j and "-j DROP" in j
                      and cmd[0].endswith("iptables") and ks.RULE_TAG in j
                      for cmd in cmds for j in [" ".join(cmd)])
              and any("-I OUTPUT 1 ! -o lo" in " ".join(cmd)
                      and "ip6tables" in cmd[0] for cmd in cmds),
              str([" ".join(c) for c in cmds]))
        cmds.clear()
        ks.release(step)
        check("1g linux release deletes both families",
              any("-D OUTPUT" in " ".join(c) and c[0].endswith("iptables")
                  for c in cmds)
              and any("-D OUTPUT" in " ".join(c) and "ip6tables" in c[0]
                      for c in cmds)
              and live["v4"] is False and live["v6"] is False)

        # ---- v4 engage failure
        def fail_v4(cmd, timeout=25):
            j = " ".join(cmd)
            if cmd[0].endswith("iptables") and " -I OUTPUT" in j:
                return (2, "", "permission denied (test)")
            return (0, "", "")
        ks._run_hook = fail_v4
        check("1h linux v4 engage failure -> None", ks.engage() is None)

        # ---- availability dispatch
        ks._run_hook = fake_run
        ks._which_hook = lambda n: None
        ok, why = ks.available()
        check("1i linux without iptables reports unavailable",
              ok is False and "iptables" in why)
        ks._which_hook = lambda n: "/bin/" + n if n == "iptables" else None
        ok, _why = ks.available()
        check("1j linux with iptables reports available", ok is True)
        ks._is_windows = lambda: True
        ks._is_linux = lambda: False
        ok, why = ks.available()
        check("1k windows reports available", ok is True and why == "")

        # ---- journal release + stale sweep
        released = []
        saved_rel = ks.release
        ks.release = lambda step: released.append(step) or True
        n = ks.release_from_journal(
            {"ks": [{"type": "ks", "engine": "netsh"}, {"type": "ks"},
                    "garbage", 42]})
        check("1l release_from_journal handles mixed junk",
              n == 2 and len(released) == 2)
        ks.release = saved_rel

        live["win"] = True
        n = ks.sweep_stale()
        check("1m stale win rule is swept away",
              n == 1 and live["win"] is False)
        live["win"] = False
        check("1n sweep with no rules is a clean no-op",
              ks.sweep_stale() == 0)

        # ---- darwin unsupported
        ks._is_windows = lambda: False
        ks._is_linux = lambda: False
        ok, why = ks.available()
        check("1o darwin reports containment unsupported (repair-only)",
              ok is False and "macOS" in why)
    finally:
        ks._run_hook, ks._which_hook, ks._is_windows, ks._is_linux = saved


# ============================================== 2. route rebind + ordering
def test_rebind():
    print("== 2. tunnel_platforms: rebind + no-leak ordering ==")
    saved_run, saved_save = plat.run, plat.save_session
    saved_sys = plat.sys_platform
    saved_clear = plat.clear_session
    saved_lind = plat.linux_down
    cmds = []

    def fake_run(cmd, timeout=25, check=False, input=None):
        cmds.append(list(cmd))
        return (0, "", "")

    try:
        plat.run = fake_run
        plat.save_session = lambda data: None
        plat.clear_session = lambda: None

        # ---- Linux rebind
        plat.sys_platform = "linux"
        cmds.clear()
        journal = {"routes": [
            {"type": "addr", "iface": "vpeen0"},
            {"type": "route", "prefix": "203.0.113.7/32", "via": "1.1.1.1"},
            {"type": "route", "prefix": "0.0.0.0/1", "dev": "vpeen0"},
            {"type": "route", "prefix": "128.0.0.0/1", "dev": "vpeen0"}],
            "ks": []}
        ok, err = plat.rebind_tun_routes(journal)
        joined = [" ".join(c) for c in cmds]
        check("2a linux rebind: addr replace + link up + both /1 routes",
              ok and any("ip addr replace" in j for j in joined)
              and any("ip link set vpeen0 up" in j for j in joined)
              and any("ip route replace 0.0.0.0/1 dev vpeen0" in j
                      for j in joined)
              and any("ip route replace 128.0.0.0/1 dev vpeen0" in j
                      for j in joined), str(joined))
        check("2b rebind does NOT touch the anti-loop host route",
              not any("203.0.113.7" in j for j in joined))

        # ---- Linux rebind failure propagates the OS error
        def fail_route(cmd, timeout=25, check=False, input=None):
            cmds.append(list(cmd))
            if "route replace" in " ".join(cmd):
                return (2, "", "RTNETLINK answers: File exists (test)")
            return (0, "", "")
        plat.run = fail_route
        cmds.clear()
        ok, err = plat.rebind_tun_routes(journal)
        check("2c rebind reports failure with the OS error",
              ok is False and "route replace" in (err or ""), str(err))
        plat.run = fake_run

        # ---- Windows rebind (fake adapter index)
        plat.sys_platform = "win32"
        saved_idx = plat._win_tun_index
        plat._win_tun_index = lambda: 42
        cmds.clear()
        journal = {"routes": [
            {"type": "addr", "iface": "VPeeN"},
            {"type": "route", "prefix": "203.0.113.7",
             "mask": "255.255.255.255", "gw": "1.1.1.1"},
            {"type": "route", "prefix": "0.0.0.0", "mask": "128.0.0.0",
             "gw": "198.18.0.1", "metric": "1", "if": 7},
            {"type": "route", "prefix": "128.0.0.0", "mask": "128.0.0.0",
             "gw": "198.18.0.1", "metric": "1", "if": 7}],
            "ks": []}
        ok, err = plat.rebind_tun_routes(journal)
        joined = [" ".join(c) for c in cmds]
        check("2d win rebind: addr + stale def1 delete + re-add on new idx",
              ok and any("set address" in j and "198.18.0.1" in j
                         for j in joined)
              and any("route delete 0.0.0.0" in j for j in joined)
              and any("route add 0.0.0.0" in j and "if 42" in j
                      for j in joined)
              and any("flushdns" in j for j in joined), str(joined))
        check("2e win rebind updates the journal if-index",
              all(r["if"] == 42 for r in journal["routes"]
                  if r.get("gw") == "198.18.0.1"))
        check("2f win rebind leaves the anti-loop host route alone",
              not any("203.0.113.7" in j for j in joined))
        plat._win_tun_index = lambda: None
        cmds.clear()
        ok, err = plat.rebind_tun_routes(journal)
        check("2g win rebind without an adapter fails cleanly",
              ok is False and "adapter" in (err or ""))
        plat._win_tun_index = saved_idx

        # ---- macOS rebind
        plat.sys_platform = "darwin"
        cmds.clear()
        ok, err = plat.rebind_tun_routes({"routes": [], "ks": []})
        joined = [" ".join(c) for c in cmds]
        check("2h mac rebind: ifconfig + all 8 net chunks",
              ok and any("ifconfig utun9 198.18.0.1" in j for j in joined)
              and sum(1 for j in joined if "route -n add -net" in j) == 8,
              str(joined))

        # ---- bring_down ordering: KS release BEFORE route teardown
        order = []
        saved_relj = ks.release_from_journal
        ks.release_from_journal = lambda j: order.append("ks") or 0
        plat.linux_down = lambda j: order.append("routes")
        plat.sys_platform = "linux"
        plat.bring_down({"routes": [], "ks": [{"type": "ks"}]})
        check("2i bring_down lifts containment BEFORE route teardown",
              order[0] == "ks" and order[1] == "routes", str(order))

        # ---- crash cleanup: KS release + unconditional stale sweep
        order.clear()
        swept = []
        saved_sweep = ks.sweep_stale
        ks.sweep_stale = lambda: swept.append(1) or 0
        plat.cleanup_from_session({"os": "linux", "journal": {}})
        check("2j crash cleanup releases KS + sweeps stale rules",
              order[0] == "ks" and swept == [1])
        ks.sweep_stale = saved_sweep
        ks.release_from_journal = saved_relj
    finally:
        plat.run, plat.save_session = saved_run, saved_save
        plat.sys_platform = saved_sys
        plat.clear_session = saved_clear
        plat.linux_down = saved_lind


# ===================================================== 3. worker state machine
def test_worker_ks():
    print("== 3. worker: detect -> contain -> repair -> heal/lockdown ==")
    saved = {
        "rebind": plat.rebind_tun_routes,
        "binary": tun_mod.binary_paths,
        "exit": os._exit,
    }
    try:
        tun_mod.binary_paths = lambda: ("/fake/tun2socks", "/fake/bin")
        os._exit = _patched_exit

        # ---- 3a-3c: happy repair path
        w = tun_mod.TunnelWorker(0, "tok")
        w.state = "up"
        w.journal = {"routes": [], "ks": []}
        w.KS_REPAIR_WAIT = 0
        events, engaged, released, rebinds = [], [], [], []

        w.broadcast = lambda obj: events.append(obj)
        w.blog = lambda msg, lvl="info": events.append((lvl, msg))
        w._ks_engage_containment = lambda: engaged.append(1) or True
        w._ks_release_containment = lambda: released.append(1)
        w._ks_restart_relay = _noop_async
        w._wait_adapter = _noop_async
        w._verify_address = _noop_async

        def fake_launch():
            w.tun2socks = FakePopen(None)          # alive again
        w._launch_tun2socks = fake_launch
        plat.rebind_tun_routes = \
            lambda j: rebinds.append(1) or (True, None)

        w.tun2socks = FakePopen(137)               # dead
        w.clients.add((object(), object()))        # GUI attached
        drive(w._ks_handle_death())

        ev_names = [e.get("ev") for e in events if isinstance(e, dict)]
        check("3a death -> ks_engaged then ks_healed",
              ev_names == ["ks_engaged", "ks_healed"], str(ev_names))
        check("3b containment engaged, released only AFTER repair",
              engaged == [1] and released == [1] and rebinds == [1])
        check("3c worker stays up + healing flag reset",
              w.state == "up" and w._ks_healing is False)

        # ---- 3d-3f: repair fails -> lockdown, containment STAYS
        events.clear()
        engaged.clear()
        released.clear()
        attempts = []
        w2 = tun_mod.TunnelWorker(0, "tok")
        w2.state = "up"
        w2.journal = {"routes": [], "ks": []}
        w2.KS_REPAIR_WAIT = 0
        w2.broadcast = lambda obj: events.append(obj)
        w2.blog = lambda msg, lvl="info": None
        w2._ks_engage_containment = lambda: engaged.append(1) or True
        w2._ks_release_containment = lambda: released.append(1)
        w2._ks_restart_relay = _noop_async
        w2._wait_adapter = _noop_async
        w2._verify_address = _noop_async

        def dying_launch():
            attempts.append(1)
            w2.tun2socks = FakePopen(137)          # dies again instantly
        w2._launch_tun2socks = dying_launch
        w2.tun2socks = FakePopen(137)
        w2.clients.add((object(), object()))
        drive(w2._ks_handle_death())
        ev_names = [e.get("ev") for e in events if isinstance(e, dict)]
        check("3d unrepairable -> lockdown event exactly once",
              ev_names == ["ks_engaged", "ks_lockdown"], str(ev_names))
        check("3e lockdown KEEPS the containment (never released)",
              engaged == [1] and released == [])
        check("3f exactly 3 repair attempts were made",
              len(attempts) == 3 and w2.KS_REPAIR_TRIES == 3)

        # ---- 3g: no-GUI orphan death -> contain + exit, no repair
        w3 = tun_mod.TunnelWorker(0, "tok")
        w3.state = "up"
        w3.journal = {"routes": [], "ks": []}
        w3.broadcast = lambda obj: None
        w3.blog = lambda msg, lvl="info": None
        exits = []
        os._exit = lambda code=0: exits.append(code) or \
            _patched_exit(code)
        w3._ks_engage_containment = lambda: engaged.append(2) or True
        w3.tun2socks = FakePopen(137)
        w3.clients = set()                       # GUI gone
        try:
            drive(w3._ks_handle_death())
        except _FakeExit:
            pass
        check("3g orphan death: contain + exit, no repair attempts",
              exits == [0] and engaged.count(2) == 1)
        os._exit = _patched_exit

        # ---- 3h: heartbeat loss + KS ON -> exit WITHOUT teardown
        w4 = tun_mod.TunnelWorker(0, "tok")
        w4.state = "up"
        w4.kill_switch = True
        w4.last_ping = time.time() - 999
        downs = []
        w4._down = _down_async(downs)
        w4.broadcast = lambda obj: None
        try:
            drive(w4._watchdog())
        except _FakeExit:
            pass
        check("3h heartbeat loss + KS ON: blackhole kept (no teardown)",
              downs == [], str(downs))

        # ---- 3i: heartbeat loss + KS OFF -> teardown then exit
        w5 = tun_mod.TunnelWorker(0, "tok")
        w5.state = "up"
        w5.kill_switch = False
        w5.last_ping = time.time() - 999
        downs.clear()
        w5._down = _down_async(downs)
        w5.broadcast = lambda obj: None
        try:
            drive(w5._watchdog())
        except _FakeExit:
            pass
        check("3i heartbeat loss + KS OFF: classic teardown then exit",
              downs == ["heartbeat lost"], str(downs))

        # ---- 3j: the watchdog notices a dead child within one poll
        w6 = tun_mod.TunnelWorker(0, "tok")
        w6.state = "up"
        w6.kill_switch = True
        w6.last_ping = time.time()
        w6.tun2socks = FakePopen(139)            # dead child
        spawned = []
        w6._spawn = lambda coro: (spawned.append(1), coro.close())
        w6._down = _down_async([])
        w6.broadcast = lambda obj: None
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(
                asyncio.wait_for(w6._watchdog(), 2.5))
        except (asyncio.TimeoutError, _FakeExit):
            pass
        finally:
            loop.close()
        check("3j watchdog detects the dead child within one poll",
              len(spawned) >= 1 and tun_mod.WORKER_POLL == 1.0)
    finally:
        plat.rebind_tun_routes = saved["rebind"]
        tun_mod.binary_paths = saved["binary"]
        os._exit = saved["exit"]


# ===================================================== 4. plumbing to the UI
def test_plumbing():
    print("== 4. controller/bridge/settings plumbing ==")
    # never touch the developer's real ~/.vpeen/settings.json
    saved_save = cfgmod.save
    cfgmod.save = lambda cfg: None
    try:
        # ---- controller forwards the worker's ks_* events verbatim
        q = queue.Queue()
        ctl = tun_mod.TunnelController(q)
        ctl.log = lambda msg, lvl="info": None
        ctl._dispatch({"ev": "ks_engaged", "rc": 137})
        ctl._dispatch({"ev": "ks_healed"})
        ctl._dispatch({"ev": "ks_lockdown"})
        got = [q.get_nowait() for _ in range(3)]
        check("4a controller forwards ks events with the tunnel type",
              all(g.get("type") == "tunnel" for g in got)
              and got[0].get("phase") == "ks_engaged"
              and got[1].get("phase") == "ks_healed"
              and got[2].get("phase") == "ks_lockdown")

        # ---- bridge maps them onto UI events + ks_state
        class FakeBridge:
            lock = threading.RLock()
            ks_state = None
            phase = "connected"
            conn_mode = "tunnel"
            region = None
            _tunnel_watchdog = None
            _stop_after_tunnel = False
            cfg = {"tun_fallback": False}
            logs = []
            update = None

            class _Core:
                @staticmethod
                def is_busy():
                    return False
            core = _Core()

            def log(self, line, level="info"):
                FakeBridge.logs.append((level, str(line)))

            def emit(self, ev):
                FakeBridge.emitted.append(ev)

            def public_settings(self):
                return dict(self.cfg)

            def request_disconnect(self):
                FakeBridge.emitted.append({"t": "disconnect"})

        FakeBridge.emitted = []
        fb = FakeBridge()
        webui_mod.Bridge._tunnel_event(fb, {"type": "tunnel",
                                            "phase": "ks_engaged"})
        webui_mod.Bridge._tunnel_event(fb, {"type": "tunnel",
                                            "phase": "ks_lockdown"})
        webui_mod.Bridge._tunnel_event(fb, {"type": "tunnel",
                                            "phase": "ks_healed"})
        ks_events = [e for e in FakeBridge.emitted if e.get("t") == "ks"]
        check("4b bridge emits ks UI events and tracks the state",
              [e.get("state") for e in ks_events] ==
              ["healing", "lockdown", "off"] and fb.ks_state is None,
              str(ks_events))
        check("4c lockdown produced an error-level log",
              any(lvl == "err" and "LOCKDOWN" in line
                  for lvl, line in FakeBridge.logs))

        # ---- settings: default ON, patchable through the API
        check("4d kill_switch defaults ON",
              cfgmod.DEFAULTS.get("kill_switch") is True)
        fb2 = FakeBridge()
        fb2.cfg = dict(cfgmod.DEFAULTS)
        errs = webui_mod.Bridge.update_settings(fb2, {"kill_switch": False})
        check("4e update_settings accepts the kill_switch key",
              errs == [] and fb2.cfg["kill_switch"] is False, str(errs))
        errs = webui_mod.Bridge.update_settings(fb2, {"kill_switch": True})
        check("4f update_settings re-enables it",
              errs == [] and fb2.cfg["kill_switch"] is True)

        # ---- the bridge passes the setting into tunnel.connect()
        class FakeTunnelCtl:
            calls = []

            def connect(self, **kw):
                FakeTunnelCtl.calls.append(kw)

        fb3 = FakeBridge()
        fb3.cfg = dict(cfgmod.DEFAULTS)
        fb3.tunnel = FakeTunnelCtl()
        fb3.listeners = {"socks_port": 1080,
                         "upstream_ips": ["203.0.113.9"],
                         "upstream_hosts": {}, "strict_ips": False}
        webui_mod.Bridge._connect_tunnel(fb3)
        check("4g tunnel.connect receives kill_switch from the settings",
              FakeTunnelCtl.calls
              and FakeTunnelCtl.calls[0].get("kill_switch") is True)
        fb3.cfg["kill_switch"] = False
        FakeTunnelCtl.calls.clear()
        webui_mod.Bridge._connect_tunnel(fb3)
        check("4h kill_switch=False is honoured on the next connect",
              FakeTunnelCtl.calls
              and FakeTunnelCtl.calls[0].get("kill_switch") is False)

        # ---- the UI files expose the toggle + the states
        js = open(os.path.join(REPO, "vpeen", "web", "app.js"),
                  encoding="utf-8").read()
        html = open(os.path.join(REPO, "vpeen", "web", "index.html"),
                    encoding="utf-8").read()
        check("4i app.js handles the ks event + lockdown/healing states",
              'case "ks"' in js and "Kill Switch ON" in js
              and "Repairing" in js and "ks: st.ks || null" in js)
        check("4j index.html carries the settings toggle",
              'id="set-killswitch"' in html and "Kill switch" in html)
        check("4k snapshot carries the ks field for late-joining UIs",
              '"ks": self.ks_state' in open(
                  os.path.join(REPO, "vpeen", "webui.py"),
                  encoding="utf-8").read())

        # ---- worker honours the cfg flag from the 'up' command
        w = tun_mod.TunnelWorker(0, "tok")
        check("4l worker defaults kill_switch ON", w.kill_switch is True)
        w.kill_switch = False
        check("4m the flag is a plain per-worker attribute (cfg merge)",
              w.kill_switch is False)
    finally:
        cfgmod.save = saved_save


# ===================================================== 5. sweep hygiene
def test_sweeps():
    print("== 5. boot / cleanup sweeps ==")
    calls = []
    saved_sweep = ks.sweep_stale
    ks.sweep_stale = lambda: calls.append("stale") or 0
    try:
        tun_mod._stale_ks_rules_only()
        check("5a post-up sweep touches only the KS rules (not tun2socks)",
              calls == ["stale"])
    finally:
        ks.sweep_stale = saved_sweep

    runs = []
    saved = (tun_mod.subprocess.run, tun_mod.binary_paths,
             plat._win_tun_index, ks.sweep_stale, tun_mod.sys_platform)
    try:
        tun_mod.subprocess.run = lambda cmd, **kw: runs.append(list(cmd))
        tun_mod.binary_paths = lambda: ("/fake/tun2socks", "/fake/bin")
        ks.sweep_stale = lambda: calls.append("boot-stale") or 0
        calls.clear()
        runs.clear()
        plat._win_tun_index = lambda: 3
        tun_mod.sys_platform = "win32"
        tun_mod._sweep_worker_boot()
        check("5b boot sweep kills the orphan when OUR adapter exists",
              any("taskkill" in " ".join(c) and "tun2socks.exe" in " ".join(c)
                  for c in runs)
              and "boot-stale" in calls)
        runs.clear()
        plat._win_tun_index = lambda: None
        tun_mod._sweep_worker_boot()
        check("5c boot sweep leaves tun2socks alone without our adapter",
              runs == [])
    finally:
        tun_mod.subprocess.run, tun_mod.binary_paths, plat._win_tun_index, \
            ks.sweep_stale, tun_mod.sys_platform = saved


def main():
    print("VPeeN v4.9.0 kill switch verification\n" + "=" * 52)
    test_engine()
    test_rebind()
    test_worker_ks()
    test_plumbing()
    test_sweeps()
    print("-" * 52)
    print(f"RESULT: {len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED:", *FAIL, sep="\n  - ")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
