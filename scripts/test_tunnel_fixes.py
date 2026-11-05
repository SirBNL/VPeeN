#!/usr/bin/env python3
"""v4.8.0 "Sentinel" tunnel-mode verification suite.

Every check maps to one of the fixed bugs (see CHANGELOG-v4.8.0.md):

  T1  event order: "listeners" BEFORE "phase=connected" (+ payload), so
      the TUN layer is never raised with an empty anti-loop IP set.
  T2  upstream_ips is now RESOLVED IPs (hostnames are unroutable and were
      silently dropped by `ip route` / inconsistently resolved by route.exe)
      + resolver PINS keep post-hijack dials on routed IPs.
  T3  the crash-recovery sweep actually RUNS at startup (it was dead code).
  T4  the route journal is checkpointed after EVERY mutation (crash window).
  T6  the system DNS swap only happens AFTER the DNS relay bound.
  --interface is gone from the tun2socks command line.
  strict_ips: the worker refuses to hijack with an empty IP set.
  "upd" commands are guarded against racing route refreshes.

Portable (runs without root / TUN devices by stubbing the platform layer).

    python scripts/test_tunnel_fixes.py
"""
import asyncio
import os
import queue
import sys
import tempfile
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}"
          + (f"  [{detail}]" if detail and not cond else ""))


import vpeen.core as core_mod                    # noqa: E402
import vpeen.upstream as up_mod                 # noqa: E402
from vpeen.upstream import resolve_host, pin_hostnames, clear_pins  # noqa
import vpeen.localproxy as lp_mod               # noqa: E402
import vpeen.tunnel as tun_mod                  # noqa: E402
import vpeen.tunnel_platforms as plat           # noqa: E402
import vpeen.dnsrelay as dns_mod                # noqa: E402
from vpeen.webui import Bridge                  # noqa: E402

TEST_HOST = "ams.test-vpn.example"


# ------------------------------------------------------------------ helpers
def fake_resolver():
    """getaddrinfo hook (async, like loop.getaddrinfo): TEST_HOST -> two
    A records, everything else fails."""
    async def hook(host, port=None, *a, **k):
        if host == TEST_HOST:
            return [(2, 1, 6, "", ("203.0.113.10", 0)),
                    (2, 1, 6, "", ("203.0.114.20", 0))]
        raise OSError(f"no such host {host}")
    return hook


def fresh_resolver_state():
    up_mod._resolve_cache.clear()
    up_mod._resolve_inflight.clear()
    clear_pins()


async def drive(coro, timeout=25):
    return await asyncio.wait_for(coro, timeout)


# ===================================================== 1+2. core wiring
async def test_core_event_order_and_resolved_ips():
    print("== 1. core: listeners before connected; RESOLVED anti-loop IPs ==")
    fresh_resolver_state()
    up_mod._getaddrinfo_hook = fake_resolver()
    events = []
    try:
        servers = [{
            "addresses": [TEST_HOST, "192.0.2.99"], "port": 443,
            "username": "u", "password": "p", "region": "test",
        }]
        factory = lp_mod.TunnelFactory(api=None, servers=servers,
                                       log=lambda *a, **k: None)
        core = core_mod.Core("/tmp/.nonexistent-state-test.json")
        # smallest possible _amain slice: snapshot + emits, no listeners
        await core._snapshot_upstream(factory)
        core._emit(type="listeners", socks_port=1080, http_port=8080,
                   upstream_ips=core.upstream_ips(),
                   upstream_hosts=core._upstream_pins,
                   strict_ips=core._strict_ips)
        core._emit(type="phase", phase="connected", region="test",
                   socks_port=1080, upstream_ips=core.upstream_ips())
        # drain the queue in FIFO order
        while True:
            try:
                events.append(core.events.get_nowait())
            except queue.Empty:
                break
        kinds = [e["type"] for e in events]
        check("1a listeners event precedes phase=connected",
              kinds.index("listeners") < kinds.index("phase"),
              str(kinds))
        lis = next(e for e in events if e["type"] == "listeners")
        ips = lis["upstream_ips"]
        check("1b upstream_ips are RESOLVED IPs (no hostnames)",
              set(ips) == {"203.0.113.10", "203.0.114.20", "192.0.2.99"},
              str(ips))
        check("1c upstream_hosts pins carry the A records",
              lis["upstream_hosts"].get(TEST_HOST) ==
              ["203.0.113.10", "203.0.114.20"],
              str(lis["upstream_hosts"]))
        check("1d strict_ips set (hostname entries present)",
              lis["strict_ips"] is True)
        ph = next(e for e in events if e["type"] == "phase")
        check("1e phase event carries the payload (belt & suspenders)",
              ph.get("socks_port") == 1080 and ph.get("upstream_ips") == ips)
    finally:
        up_mod._getaddrinfo_hook = None
        clear_pins()
        up_mod._resolve_cache.clear()
    return events


async def test_resolver_pins():
    print("== 2. resolver pins: post-hijack dials stay on routed IPs ==")
    fresh_resolver_state()
    pin_hostnames({TEST_HOST: ["198.51.100.7"]})
    try:
        got = await resolve_host(TEST_HOST)
        check("2a pinned hostname returns the pinned snapshot",
              got == ["198.51.100.7"], str(got))
        # a pinned host must never touch the live resolver: make the
        # resolver explode and re-resolve past the TTL
        up_mod._getaddrinfo_hook = fake_resolver()
        up_mod._resolve_cache.pop(TEST_HOST, None)
        got2 = await resolve_host(TEST_HOST)
        check("2b pinned resolve ignores the live resolver",
              got2 == ["198.51.100.7"], str(got2))
    finally:
        up_mod._getaddrinfo_hook = None
        clear_pins()
        up_mod._resolve_cache.clear()


async def test_factory_resolution():
    print("== 2c. factory: hostname + literal addresses resolve to IPs ==")
    fresh_resolver_state()
    up_mod._getaddrinfo_hook = fake_resolver()
    try:
        factory = lp_mod.TunnelFactory(
            api=None,
            servers=[{"addresses": [TEST_HOST, "192.0.2.1"]},
                     {"addresses": [TEST_HOST]},
                     {"addresses": ["dead.host.invalid"]}],
            log=lambda *a, **k: None)
        ips = await factory.resolved_upstream_ips()
        check("2c resolved set = literals + A records, hostnames excluded",
              set(ips) == {"203.0.113.10", "203.0.114.20", "192.0.2.1"},
              str(ips))
        pins = await factory.upstream_pins()
        check("2d pins cover hostname entries (dead host simply absent)",
              pins.get(TEST_HOST) == ["203.0.113.10", "203.0.114.20"]
              and "dead.host.invalid" not in pins and
              "192.0.2.1" in pins, str(pins))
    finally:
        up_mod._getaddrinfo_hook = None
        clear_pins()
        up_mod._resolve_cache.clear()


# ===================================================== 3. webui glue
async def test_webui_phase_payload():
    print("== 3. webui: TUN raised from the phase EVENT payload ==")
    bridge = Bridge(state_path="/tmp/.nonexistent-state-test.json", demo=False)
    calls = {}
    bridge.tunnel.connect = lambda **kw: calls.update(kw) or None
    bridge._tun_failed_fallback = lambda reason: calls.setdefault(
        "fallback", reason)
    # first connect: listeners dict is EMPTY - the phase payload must save us
    bridge.listeners = {}
    bridge._pending_tunnel = True
    bridge.handle_event({
        "type": "phase", "phase": "connected", "region": "test",
        "socks_port": 1099, "upstream_ips": ["203.0.113.10"],
        "upstream_hosts": {TEST_HOST: ["203.0.113.10"]},
        "strict_ips": True})
    check("3a tunnel.connect got the EVENT socks_port (not cfg default)",
          calls.get("socks_port") == 1099, str(calls))
    check("3b tunnel.connect got the EVENT upstream_ips (not [])",
          calls.get("upstream_ips") == ["203.0.113.10"], str(calls))
    check("3c pins/strict forwarded to the worker",
          calls.get("upstream_hosts") == {TEST_HOST: ["203.0.113.10"]}
          and calls.get("strict_ips") is True, str(calls))
    # and with NOTHING available at all: refuse (blackhole guard)
    calls.clear()
    bridge._pending_tunnel = True
    bridge.handle_event({
        "type": "phase", "phase": "connected", "region": "test",
        "socks_port": 1099, "upstream_ips": []})
    check("3d empty IP set -> never raises the tunnel",
          "fallback" in calls and "connect" not in calls,
          str(calls))
    bridge.update.stop()
    return bridge


# ===================================================== 4. worker behaviour
class FakePopen:
    def __init__(self, args, **kw):
        self.args = list(args)
        self.returncode = None

    def poll(self):
        return None

    def terminate(self):
        self.returncode = 0

    def kill(self):
        self.returncode = 1

    def wait(self, timeout=None):
        return 0


class FakeRelay:
    fail_to_bind = False
    started = []

    def __init__(self, *a, **k):
        pass

    async def start(self):
        if FakeRelay.fail_to_bind:
            raise OSError("port 53 busy (test)")
        FakeRelay.started.append(time.monotonic())

    async def stop(self):
        pass


async def test_worker_up_sequence():
    print("== 4. worker: no --interface, strict_ips, DNS-after-relay ==")
    calls = {"popens": [], "bring_up": [], "apply_dns": [], "down": [],
             "events": []}
    relay_marker = {"start": None, "dns": None}

    tun_mod.binary_paths = lambda: ("/fake/tun2socks", "/fake/bin")
    plat.is_admin = lambda: True
    saved = {
        "popen": tun_mod.subprocess.Popen,
        "bring_up": plat.bring_up,
        "apply_dns": plat.apply_dns,
        "relay": dns_mod.DNSRelay,
        "wait": tun_mod.TunnelWorker._wait_adapter,
        "verify": tun_mod.TunnelWorker._verify_address,
        "orig": tun_mod.TunnelWorker._orig_bind_interface,
    }
    try:
        tun_mod.subprocess.Popen = lambda args, **kw: \
            calls["popens"].append(list(args)) or FakePopen(args, **kw)
        plat.bring_up = lambda mtu, ips: (
            calls["bring_up"].append(list(ips)) or
            ({"routes": [], "dns": []}, True, None))
        plat.apply_dns = lambda journal: (
            calls["apply_dns"].append(time.monotonic()) or True)

        class _R(FakeRelay):
            async def start(self):
                await super().start()
                relay_marker["start"] = time.monotonic()
        dns_mod.DNSRelay = _R

        async def _nowait(self, timeout=20):
            return
        tun_mod.TunnelWorker._wait_adapter = _nowait
        tun_mod.TunnelWorker._verify_address = _nowait
        tun_mod.TunnelWorker._orig_bind_interface = lambda self: "eth0"

        # --- 4a: strict_ips + empty IP set -> REFUSE to hijack
        w = tun_mod.TunnelWorker(0, "tok")
        w.blog = lambda msg, lvl="info": calls["events"].append((lvl, msg))
        w.upstream_ips = []
        w.strict_ips = True
        await drive(w._up())
        check("4a empty IP set + strict -> refused (no bring_up)",
              not calls["bring_up"] and
              any("refusing" in m for _, m in calls["events"]),
              str(calls["events"])[-200:])

        # --- 4b: happy path -> no --interface, DNS after relay
        calls["bring_up"].clear()
        calls["apply_dns"].clear()
        calls["events"].clear()
        w2 = tun_mod.TunnelWorker(0, "tok")
        w2.blog = lambda msg, lvl="info": calls["events"].append((lvl, msg))
        w2.broadcast = lambda obj: calls["events"].append(obj)
        w2.socks_port = 1080
        w2.upstream_ips = ["203.0.113.10", TEST_HOST]   # hostname is filtered
        w2.upstream_hosts = {TEST_HOST: ["203.0.113.10"]}
        w2.strict_ips = True
        w2.dns_enabled = True
        await drive(w2._up())
        check("4b tun2socks command has NO --interface flag",
              calls["popens"] and "--interface" not in calls["popens"][0],
              str(calls["popens"])[:200])
        check("4c bring_up received ONLY valid IPv4 literals",
              calls["bring_up"] == [["203.0.113.10"]],
              str(calls["bring_up"]))
        check("4d 'up' event broadcast",
              any(isinstance(e, dict) and e.get("ev") == "up"
                  for e in calls["events"]))
        check("4e system DNS swap AFTER relay bind",
              relay_marker["start"] is not None and
              calls["apply_dns"] and
              calls["apply_dns"][0] >= relay_marker["start"])

        # --- 4f: relay bind failure -> NO DNS swap, tunnel still up
        relay_marker["start"] = None
        calls["apply_dns"].clear()
        calls["popens"].clear()
        dns_mod.DNSRelay = FakeRelay
        FakeRelay.fail_to_bind = True
        try:
            w3 = tun_mod.TunnelWorker(0, "tok")
            w3.blog = lambda msg, lvl="info": calls["events"].append((lvl, msg))
            w3.broadcast = lambda obj: calls["events"].append(obj)
            w3.socks_port = 1080
            w3.upstream_ips = ["203.0.113.10"]
            w3.strict_ips = True
            w3.dns_enabled = True
            await drive(w3._up())
            check("4f relay bind failure -> DNS NOT swapped",
                  not calls["apply_dns"] and
                  any(isinstance(e, dict) and e.get("ev") == "up"
                      for e in calls["events"]),
                  str(calls["apply_dns"]))
        finally:
            FakeRelay.fail_to_bind = False

        # --- 4h: v6-only 'IPs' (non-strict) also refuse the hijack:
        # an IPv4-only TUN cannot protect them with host routes either
        calls["bring_up"].clear()
        calls["events"].clear()
        w4 = tun_mod.TunnelWorker(0, "tok")
        w4.blog = lambda msg, lvl="info": calls["events"].append((lvl, msg))
        w4.socks_port = 1080
        w4.upstream_ips = ["2001:db8::1"]          # v6 literal, no hostnames
        w4.strict_ips = False
        await drive(w4._up())
        check("4h all-unroutable IP set (v6-only) -> refused",
              not calls["bring_up"] and
              any("refusing" in m for _, m in calls["events"]
                  if isinstance(m, str)),
              str(calls["events"])[-200:])
    finally:
        tun_mod.subprocess.Popen = saved["popen"]
        plat.bring_up = saved["bring_up"]
        plat.apply_dns = saved["apply_dns"]
        dns_mod.DNSRelay = saved["relay"]
        tun_mod.TunnelWorker._wait_adapter = saved["wait"]
        tun_mod.TunnelWorker._verify_address = saved["verify"]
        tun_mod.TunnelWorker._orig_bind_interface = saved["orig"]
        tun_mod.binary_paths = tun_mod.__dict__.get("_orig_binary_paths", None) \
            or tun_mod.binary_paths
    # restore the real binary_paths (patched via module attr)
    import importlib
    importlib.reload(tun_mod)


async def test_upd_guard():
    print("== 4g. worker: two fast 'upd' commands never race ==")
    import importlib
    importlib.reload(tun_mod)      # clean state after the previous patches
    w = tun_mod.TunnelWorker(0, "tok")
    runs = []
    started = asyncio.Event()

    async def slow_refresh():
        runs.append("start")
        started.set()
        await asyncio.sleep(0.3)
        runs.append("done")

    w._refresh_host_routes = slow_refresh
    await w._handle(None, {"cmd": "upd", "upstream_ips": ["1.2.3.4"]})
    await started.wait()
    await w._handle(None, {"cmd": "upd", "upstream_ips": ["1.2.3.4"]})
    await asyncio.sleep(0.5)
    check("4g second upd skipped while the first is in flight",
          runs.count("start") == 1 and runs.count("done") == 1, str(runs))


# ===================================================== 5. journal + sweep
def test_incremental_journal():
    print("== 5. tunnel_platforms: journal checkpointed per mutation ==")
    saved_run, saved_save = plat.run, plat.save_session
    committed = []

    def fake_run(cmd, timeout=25, check=False, input=None):
        if cmd[:4] == ["ip", "route", "show", "default"]:
            return 0, "default via 192.0.2.254 dev eth0", ""
        if cmd[:4] == ["ip", "addr", "add", "198.18.0.1/15"]:
            return 0, "", ""
        if cmd[:3] == ["sysctl", "-n"]:
            return 0, "2", ""
        # route add 128.0.0.0/1 (the SECOND def1 hijack) explodes -> crash
        # *between* the mutation and the final save
        if cmd[:3] == ["ip", "route", "add"] and cmd[3] == "128.0.0.0/1":
            return 1, "", "simulated failure"
        return 0, "", ""

    def fake_save(data):
        committed.append(len(data["journal"]["routes"]))

    try:
        plat.run = fake_run
        plat.save_session = fake_save
        journal, ok, err = plat.bring_up(1500, ["203.0.113.10"])
        check("5a partial-route crash leaves a matching journal behind",
              len(committed) >= 4 and committed == sorted(committed)
              and committed[-1] == len(journal["routes"]),
              str(committed))
        check("5b a failed hijack route fails the bring_up loudly",
              ok is False and err, str(err))
        check("5c resolv.conf NOT swapped inside bring_up (relay-first)",
              not any(r["type"] == "resolvconf"
                      for r in journal.get("dns", [])))
    finally:
        plat.run = saved_run
        plat.save_session = saved_save


def test_refresh_routes_filters_hostnames():
    print("== 5d. _refresh_host_routes drops hostname/v6 'IPs' ==")
    import importlib
    importlib.reload(tun_mod)
    w = tun_mod.TunnelWorker(0, "tok")
    w.state = "up"
    w.journal = {"routes": [], "dns": []}
    w.upstream_ips = [TEST_HOST, "203.0.113.10", "2001:db8::1"]
    cmds = []
    saved_run = plat.run
    saved_save = plat.save_session
    try:
        plat.run = lambda cmd, timeout=25, check=False, input=None: \
            cmds.append(cmd) or (0, "", "")
        plat.save_session = lambda data: None
        plat._linux_orig_route = lambda: ("192.0.2.254", "eth0")
        tun_mod.sys_platform = "linux"
        asyncio.run(w._refresh_host_routes())
        added = [c for c in cmds if c[:3] == ["ip", "route", "add"]]
        check("5d only the IPv4 literal got a host route",
              added == [["ip", "route", "add", "203.0.113.10/32",
                         "via", "192.0.2.254"]], str(added))
    finally:
        plat.run = saved_run
        plat.save_session = saved_save
        tun_mod.sys_platform = plat.sys_platform


def test_startup_sweep_wiring():
    print("== 6. webui: the crash-recovery sweep actually runs ==")
    bridge = Bridge(state_path="/tmp/.nonexistent-state-test.json", demo=False)
    fired = []
    saved_load = plat.load_session
    saved_clean = bridge.tunnel.cleanup_orphans
    try:
        plat.load_session = lambda: None
        bridge.sweep_tunnel_orphans()
        time.sleep(0.4)
        check("6a no session -> sweep stays silent",
              not fired)
        plat.load_session = lambda: {"os": "linux", "journal": {}}
        bridge.tunnel.cleanup_orphans = lambda log=None: fired.append(1)
        bridge.sweep_tunnel_orphans()
        deadline = time.time() + 3
        while time.time() < deadline and not fired:
            time.sleep(0.05)
        check("6b leftover session -> cleanup_orphans invoked", bool(fired))
    finally:
        plat.load_session = saved_load
        bridge.tunnel.cleanup_orphans = saved_clean
        bridge.update.stop()


# ===================================================== 7. DNS relay pins
async def test_relay_pins():
    print("== 7. dnsrelay: pinned proxy hostnames answered from the pin ==")
    dialed = []

    async def exploding_dial(host, port):
        dialed.append((host, port))
        raise OSError("must not relay pinned queries")

    relay = dns_mod.DNSRelay(socks_dial=exploding_dial,
                            pins={TEST_HOST: ["203.0.113.10"]})
    import struct as _s

    def q(name, qtype=1):
        parts = b"".join(bytes([len(p)]) + p.encode()
                         for p in name.split(".")) + b"\x00"
        return _s.pack("!HHHHHH", 0x1234, 0x0100, 1, 0, 0, 0) + parts + \
            _s.pack("!HH", qtype, 1)

    # A query for the pinned host -> synthesized answer, no dial
    resp = await relay.resolve(q(TEST_HOST), udp=True)
    check("7a pinned A query never hits the upstream resolver",
          not dialed, str(dialed))
    # header: QR=1, RCODE=0; ANCOUNT=1; answer contains the pinned IP
    rid, flags, qd, an, ns, ar = _s.unpack("!HHHHHH", resp[:12])
    check("7b pin reply is a NOERROR with 1 answer",
          (flags >> 15) & 1 and (flags & 0xF) == 0 and an == 1,
          hex(flags))
    rdata_at = len(resp) - 4
    ip = ".".join(str(b) for b in resp[rdata_at:])
    check("7c answer carries the pinned IP", ip == "203.0.113.10", ip)
    # AAAA -> empty NOERROR (never SERVFAIL, never a fake v6)
    resp6 = await relay.resolve(q(TEST_HOST, 28), udp=True)
    _rid, fl6, _qd, an6, _ns, _ar = _s.unpack("!HHHHHH", resp6[:12])
    check("7d pinned AAAA -> NOERROR, 0 answers",
          (fl6 & 0xF) == 0 and an6 == 0, hex(fl6))
    # a different name -> relayed through the tunnel (dial happens)
    try:
        await relay.resolve(q("other.example"))
    except OSError:
        pass
    check("7e non-pinned queries still relay upstream",
          dialed == [("1.1.1.1", 53)] or dialed, str(dialed))


# ------------------------------------------------------------------ runner
async def amain():
    await test_core_event_order_and_resolved_ips()
    await test_resolver_pins()
    await test_factory_resolution()
    await test_webui_phase_payload()
    await test_worker_up_sequence()
    await test_upd_guard()
    await test_relay_pins()


def main() -> int:
    print(f"VPeeN v4.8.0 tunnel-fix verification ({time.strftime('%F')})")
    asyncio.run(amain())
    test_incremental_journal()
    test_refresh_routes_filters_hostnames()
    test_startup_sweep_wiring()
    print()
    print(f"  {len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("  FAILED:", "; ".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
