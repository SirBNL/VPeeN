#!/usr/bin/env python3
"""v4.9.0 split-tunneling (bypass list) verification suite.

The bypass list answers: "can the user keep chosen destinations DIRECT
(e.g. *.ir banking sites) while everything else goes through VPeeN - and
does disconnect restore EXACTLY what was there before?"

  1. parse_bypass            - normalization, wildcards, junk, caps
  2. Windows ProxyOverride   - user list appended to the local-only floor
  3. macOS networksetup      - bypass set per service + backup + restore
  4. GNOME ignore-hosts      - backed up, set, restored verbatim
  5. plumbing                - settings key, /api/settings normalization,
     the web UI passing bypass into core.start, the CLI flag

    python scripts/test_bypass.py
"""
import os
import subprocess
import sys
import threading

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}"
          + (f"  [{detail}]" if detail and not cond else ""))


import vpeen.systemproxy as sp               # noqa: E402
import vpeen.settings as cfgmod              # noqa: E402
import vpeen.webui as webui_mod              # noqa: E402


class FakeState:
    def __init__(self):
        self._d = {}

    def set(self, k, v):
        self._d[k] = v

    def get(self, k, default=None):
        return self._d.get(k, default)


# ===================================================== 1. the parser
def test_parser():
    print("== 1. parse_bypass ==")
    check("1a commas + spaces + semicolons all separate",
          sp.parse_bypass("*.ir bank.example.com;aparat.com") ==
          ["*.ir", "bank.example.com", "aparat.com"])
    check("1b lower-cased + surrounding dots stripped",
          sp.parse_bypass("  EXAMPLE.com. ") == ["example.com"])
    check("1c de-duplicated (order preserved)",
          sp.parse_bypass("a.com b.com a.com A.COM") ==
          ["a.com", "b.com"])
    check("1d junk tokens dropped (separators/quotes/slashes)",
          sp.parse_bypass('a.com "b;com" c/d e\\f') == ["a.com"])
    check("1e empty / None / non-string -> []",
          sp.parse_bypass("") == [] and sp.parse_bypass(None) == []
          and sp.parse_bypass(123) == [])
    check("1f wildcard prefixes survive untouched",
          sp.parse_bypass("*.ir *example.org") == ["*.ir", "*example.org"])
    check("1g a paste-bomb is capped at 200 entries",
          len(sp.parse_bypass(" ".join(f"d{i}.com"
                                       for i in range(5000)))) == 200)
    check("1h Persian IDN domains are accepted",
          "اپل.ir" in sp.parse_bypass("اپل.ir, example.com"))
    check("1i pure punctuation never becomes an entry",
          sp.parse_bypass("... , ; *** -") == [])
    check("1j floor merge keeps loopback first + de-dupes",
          sp._merged_bypass(["*.ir", "localhost"]) ==
          ["localhost", "127.0.0.1", "::1", "*.ir"])


# ===================================================== 2. Windows path
def test_windows():
    print("== 2. Windows ProxyOverride ==")
    saved = (sp.IS_WINDOWS, sp.IS_MACOS, sp._winreg_read, sp._winreg_write,
             sp._wininet_refresh, sp._gnome_available)
    writes = []
    import types
    fake_winreg = types.ModuleType("winreg")
    fake_winreg.REG_SZ = 1
    fake_winreg.REG_DWORD = 4
    sys.modules["winreg"] = fake_winreg
    try:
        sp.IS_WINDOWS = True
        sp.IS_MACOS = False
        sp._winreg_read = lambda name: None
        sp._winreg_write = lambda name, value, typ: \
            writes.append((name, value))
        sp._wininet_refresh = lambda: None
        st = FakeState()
        sp.system_on(st, "127.0.0.1", 8080, 1080,
                     bypass="*.ir, bank.example.com")
        ov = [v for n, v in writes if n == "ProxyOverride"]
        check("2a user bypass lands AFTER the local-only floor",
              ov and ov[0].startswith(sp.BYPASS_LIST)
              and ov[0].endswith("*.ir;bank.example.com"), str(ov))
        writes.clear()
        sp.system_on(st, "127.0.0.1", 8080, 1080)
        ov = [v for n, v in writes if n == "ProxyOverride"]
        check("2b no bypass -> override stays the plain local list",
              ov == [sp.BYPASS_LIST], str(ov))
        check("2c the backup captured the PREVIOUS override (restore path)",
              st.get("system_proxy_backup") is not None)
    finally:
        sp.IS_WINDOWS, sp.IS_MACOS, sp._winreg_read, sp._winreg_write, \
            sp._wininet_refresh, sp._gnome_available = saved
        sys.modules.pop("winreg", None)


# ===================================================== 3. macOS path
def test_macos():
    print("== 3. macOS networksetup ==")
    saved = (sp.IS_WINDOWS, sp.IS_MACOS, sp._gnome_available,
             sp._mac_read_proxy, sp._mac_read_bypass)
    cmds = []
    svc_list = {"ok": True}

    def fake_run(cmd, **kw):
        cmds.append(list(cmd))
        j = " ".join(cmd)
        if "-listallnetworkservices" in j:
            class R:
                returncode = 0
                stdout = "Enabled\nWi-Fi\nEthernet\n*Disabled one\n"
                stderr = ""
            return R()
        if "-getproxybypassdomains" in j:
            class R:
                returncode = 0
                stdout = "old.example.com\n*.local\n"
                stderr = ""
            return R()
        class R:
            returncode = 0
            stdout = "Enabled: No\nServer: \nPort: 0\n"
            stderr = ""
        return R()

    try:
        sp.IS_WINDOWS = False
        sp.IS_MACOS = True
        sp._gnome_available = lambda: False
        real_run = subprocess.run
        subprocess.run = fake_run
        sp._mac_read_proxy = lambda s, k: {"enabled": "No", "host": "",
                                           "port": ""}
        st = FakeState()
        ok = sp.system_on(st, "127.0.0.1", 8080, 1080, bypass="*.ir")
        joined = [" ".join(c) for c in cmds]
        check("3a bypass domains are set per active service",
              any("-setproxybypassdomains Wi-Fi" in j for j in joined)
              and any("-setproxybypassdomains Ethernet" in j
                      for j in joined), str(joined))
        merged = [j for j in joined if "-setproxybypassdomains Wi-Fi" in j]
        check("3b the merged list = loopback floor + user domains",
              merged and "*.ir" in merged[0] and "localhost" in merged[0]
              and "127.0.0.1" in merged[0], str(merged))
        check("3c the ORIGINAL bypass lists were backed up",
              st.get("system_proxy_backup:bypass") ==
              {"Wi-Fi": ["old.example.com", "*.local"],
               "Ethernet": ["old.example.com", "*.local"]},
              str(st.get("system_proxy_backup:bypass")))
        # ---- restore on system_off
        cmds.clear()
        backup = st.get("system_proxy_backup")
        st2 = FakeState()
        st2.set("system_proxy_backup", backup)
        st2.set("system_proxy_backup:bypass",
                st.get("system_proxy_backup:bypass"))
        sp.system_off(st2)
        joined = [" ".join(c) for c in cmds]
        restored = [j for j in joined
                    if "-setproxybypassdomains Wi-Fi" in j]
        check("3d disconnect restores the ORIGINAL bypass lists",
              restored and "old.example.com" in restored[0]
              and "*.local" in restored[0], str(restored[:2]))
        # ---- restore with an EMPTY original -> literal Empty
        st3 = FakeState()
        st3.set("system_proxy_backup", {"services": ["Wi-Fi"],
                                        "detail": {}})
        st3.set("system_proxy_backup:bypass", {"Wi-Fi": []})
        cmds.clear()
        sp.system_off(st3)
        joined = [" ".join(c) for c in cmds]
        check("3e empty original bypass restored as 'Empty'",
              any("-setproxybypassdomains Wi-Fi Empty" in j
                  for j in joined), str(joined))
    finally:
        subprocess.run = real_run
        sp.IS_WINDOWS, sp.IS_MACOS, sp._gnome_available, \
            sp._mac_read_proxy, sp._mac_read_bypass = saved


# ===================================================== 4. GNOME path
def test_gnome():
    print("== 4. GNOME ignore-hosts ==")
    saved = (sp.IS_WINDOWS, sp.IS_MACOS, sp._gnome_available, sp._gsettings)
    sets = []
    try:
        sp.IS_WINDOWS = False
        sp.IS_MACOS = False
        sp._gnome_available = lambda: True

        def fake_gs(args):
            sets.append(list(args))
            return True
        sp._gsettings = fake_gs
        # _gnome_backup shells out to gsettings get - patch it too
        real_run = subprocess.run

        def fake_run(cmd, **kw):
            class R:
                returncode = 0
                stdout = ("['localhost', '127.0.0.0/8']"
                          if "ignore-hosts" in " ".join(cmd)
                          else "'none'")
                stderr = ""
            return R()
        subprocess.run = fake_run
        st = FakeState()
        sp.system_on(st, "127.0.0.1", 8080, 1080, bypass="*.ir")
        ih = [a for a in sets if "ignore-hosts" in a]
        check("4a ignore-hosts replaced with floor + user list",
              ih and "*.ir" in " ".join(ih[-1])
              and "localhost" in " ".join(ih[-1]), str(ih[-1:]))
        check("4b the OLD ignore-hosts value was captured in the backup",
              st.get("system_proxy_backup") is not None and
              st.get("system_proxy_backup")
              .get("org.gnome.system.proxy:ignore-hosts") ==
              "['localhost', '127.0.0.0/8']")
        # ---- restore
        sets.clear()
        st2 = FakeState()
        st2.set("system_proxy_backup", st.get("system_proxy_backup"))
        sp.system_off(st2)
        ih = [a for a in sets if "ignore-hosts" in a]
        check("4c disconnect restores the old ignore-hosts verbatim",
              ih and "['localhost', '127.0.0.0/8']" in " ".join(ih[0]),
              str(ih))
    finally:
        subprocess.run = real_run
        sp.IS_WINDOWS, sp.IS_MACOS, sp._gnome_available, \
            sp._gsettings = saved


# ===================================================== 5. plumbing
def test_plumbing():
    print("== 5. settings / web UI / CLI plumbing ==")
    check("5a DEFAULTS carry an empty bypass_domains",
          cfgmod.DEFAULTS.get("bypass_domains") == "")

    saved_save = cfgmod.save
    cfgmod.save = lambda cfg: None          # never touch ~/.vpeen
    try:
        class FakeBridge:
            lock = threading.RLock()
            update = None
            cfg = dict(cfgmod.DEFAULTS)

            def log(self, *a, **k):
                pass

            def emit(self, *a, **k):
                pass

            def public_settings(self):
                return dict(self.cfg)

        fb = FakeBridge()
        errs = webui_mod.Bridge.update_settings(
            fb, {"bypass_domains": "*.ir, Bank.Example.com, ;;;  'quoted'"})
        check("5b /api/settings normalizes the list (junk dropped)",
              errs == [] and fb.cfg["bypass_domains"] == "*.ir, bank.example.com",
              repr(fb.cfg["bypass_domains"]))
        errs = webui_mod.Bridge.update_settings(fb, {"bypass_domains": ""})
        check("5c clearing the list works", fb.cfg["bypass_domains"] == "")

        # ---- request_connect forwards the parsed list into core.start
        class FakeCore:
            started = []

            def is_busy(self):
                return False

            def start(self, *a, **kw):
                FakeCore.started.append((a, kw))

        fb2 = FakeBridge()
        fb2.demo = False
        fb2.core = FakeCore()
        fb2.selected = "optimal"
        fb2.locations = []
        fb2.region = None
        fb2._pending_reconnect = False
        fb2._pending_tunnel = False
        fb2._stop_after_tunnel = False
        fb2.phase = "disconnected"
        fb2.emit = lambda ev: None
        fb2.log = lambda *a, **k: None
        fb2._region_label = lambda region: "Optimal"
        fb2.cfg = dict(cfgmod.DEFAULTS)
        fb2.cfg.update({"auto_system_proxy": True,
                        "bypass_domains": "*.ir aparat.com"})
        webui_mod.Bridge.request_connect(fb2)
        args, kw = FakeCore.started[0]
        check("5d core.start receives the parsed bypass list",
              args[4] is True and kw.get("bypass") ==
              ["*.ir", "aparat.com"], f"args={args} kw={kw}")
        # ---- bypass is NOT passed in tunnel mode (everything is routed)
        FakeCore.started.clear()
        fb2.cfg["tunnel_enabled"] = True

        class FakeTunnel:
            state = "down"

            def is_available(self):
                return (True, "")

        fb2.tunnel = FakeTunnel()
        fb2.phase = "disconnected"
        webui_mod.Bridge.request_connect(fb2)
        check("5e tunnel mode passes an EMPTY bypass (full tunnel by design)",
              FakeCore.started and FakeCore.started[0][1].get("bypass") == [],
              str(FakeCore.started))
    finally:
        cfgmod.save = saved_save

    # ---- static wiring of the UI + CLI
    html = open(os.path.join(REPO, "vpeen", "web", "index.html"),
                encoding="utf-8").read()
    js = open(os.path.join(REPO, "vpeen", "web", "app.js"),
              encoding="utf-8").read()
    cli = open(os.path.join(REPO, "vpeen", "cli.py"),
               encoding="utf-8").read()
    check("5f settings modal carries the bypass input + placeholder",
          'id="set-bypass"' in html and 'data-i18n-ph="set_bypass_ph"' in html)
    check("5g app.js loads/saves bypass_domains",
          'c.bypass_domains || ""' in js
          and 'bypass_domains: $("set-bypass").value.trim()' in js)
    check("5h CLI exposes --bypass for --set-system runs",
          '"--bypass"' in cli and "parse_bypass" in cli)
    check("5i the dictionary carries bypass strings in both languages",
          '"set_bypass"' in js and '"set_bypass_hint"' in js
          and "لیست عبور مستقیم" in js)


def main():
    print("VPeeN v4.9.0 split-tunneling verification\n" + "=" * 52)
    test_parser()
    test_windows()
    test_macos()
    test_gnome()
    test_plumbing()
    print("-" * 52)
    print(f"RESULT: {len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED:", *FAIL, sep="\n  - ")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
