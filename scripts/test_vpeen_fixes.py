#!/usr/bin/env python3
"""Verification suite for the VPeeN v4.3.1 bugfix pass.

Each test maps to one of the fixed bugs (see CHANGELOG-v4.3.1.md).
Portable: run from anywhere, it locates the repo root relative to itself.

    python scripts/test_vpeen_fixes.py
"""
import asyncio
import inspect
import ipaddress
import json
import os
import stat
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  [{detail}]" if detail and not cond else ""))


print("== 1. tunnel_platforms: missing import (CRITICAL) ==")
import vpeen.tunnel_platforms as plat  # noqa: E402
try:
    tmpd = tempfile.mkdtemp()
    _orig_dir, _orig_path = plat.CONFIG_DIR, plat.SESSION_PATH
    plat.CONFIG_DIR = tmpd                      # keep the real home clean
    plat.SESSION_PATH = os.path.join(tmpd, "tunnel_session.json")
    plat.save_session({"os": "linux", "journal": {"routes": []}})
    check("save_session() no longer raises NameError", True)
    loaded = plat.load_session()
    check("session journal round-trips to disk", loaded is not None
          and loaded.get("journal") == {"routes": []})
    plat.CONFIG_DIR, plat.SESSION_PATH = _orig_dir, _orig_path
except Exception as e:
    check("save_session() no longer raises NameError", False, repr(e))

print("== 2. api.py: duplicated block removed ==")
import vpeen.api as api_mod  # noqa: E402
src = inspect.getsource(api_mod)
check("valid_api_domain defined exactly once",
      src.count("def valid_api_domain") == 1)
check("MAX_DOMAIN_ATTEMPTS defined exactly once",
      src.count("MAX_DOMAIN_ATTEMPTS = ") == 1)
check("_API_DOMAIN_RE defined exactly once",
      src.count("_API_DOMAIN_RE = re.compile") == 1)
for dead in ("import base64", "import random"):
    check(f"dead import gone: {dead!r}", dead not in src)

print("== 3. api.py: --insecure now reaches urlopen ==")
sig = inspect.signature(api_mod._http_request)
check("_http_request has an insecure parameter", "insecure" in sig.parameters)
check("_http_request_async has an insecure parameter",
      "insecure" in inspect.signature(api_mod._http_request_async).parameters)
from vpeen.api import VeePNApi  # noqa: E402
from vpeen.utils import State  # noqa: E402
st = State(os.path.join(tempfile.mkdtemp(), "state.json"))
VeePNApi(st, insecure_tls=True)
req_src = inspect.getsource(VeePNApi._request)
launch_src = inspect.getsource(VeePNApi.ensure_token)
check("_request passes insecure=self.insecure_tls",
      "insecure=self.insecure_tls" in req_src)
check("ensure_token passes insecure=self.insecure_tls",
      "insecure=self.insecure_tls" in launch_src)
check("dead _ssl_context method removed",
      "def _ssl_context" not in src)

print("== 4. api.py: IPv6 IP detection ==")
from vpeen.api import _IPV6_TOKEN_RE, _IPV4_RE  # noqa: E402


def extract(s):
    try:
        return str(ipaddress.ip_address(s.strip()))
    except ValueError:
        pass
    m = _IPV4_RE.search(s)
    if m:
        return m.group(1)
    m = _IPV6_TOKEN_RE.search(s)
    if m:
        try:
            return str(ipaddress.ip_address(m.group(1)))
        except ValueError:
            return None
    return None


check("IPv4 in plain text", extract("1.2.3.4") == "1.2.3.4")
check("IPv6 compressed", extract("2606:4700::6810:84e5") == "2606:4700::6810:84e5")
check("IPv6 full form", extract("2001:0db8:85a3:0000:0000:8a2e:0370:7334")
      == "2001:db8:85a3::8a2e:370:7334")
check("time-like token rejected", extract("logged at 12:34:56 today") is None)
check("MAC-like token rejected", extract("mac aa:bb:cc:dd:ee:ff") is None)

print("== 5. upstream.py: IPv6 exit-IP parsing ==")
import vpeen.upstream as up  # noqa: E402
up_src = inspect.getsource(up._exit_ip_via)
check("IPv6 token regex used in _exit_ip_via", "_IPV6_TOKEN_RE" in up_src)

print("== 6. dnsrelay: TCP keeps full answer, UDP truncates ==")
from vpeen.dnsrelay import DNSRelay, _UDPProto  # noqa: E402

QUERY = (b"\xab\xcd\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00"
         b"\x07example\x03com\x00\x00\x01\x00\x01")


class FakeSocks:
    def __init__(self, size):
        self.size = size

    async def __call__(self, host, port):
        outer = self

        class R:
            async def readexactly(self, n):
                if n == 2:
                    return outer.size.to_bytes(2, "big")
                return b"A" * outer.size

        class W:
            def write(self, d):
                pass

            async def drain(self):
                pass

            def close(self):
                pass
        return R(), W()


async def dns_case(size, udp):
    relay = DNSRelay(socks_dial=FakeSocks(size), log=lambda *a, **k: None)
    return len(await relay.resolve(QUERY, udp=udp))


check("TCP path returns the full 1500-byte answer",
      asyncio.run(dns_case(1500, udp=False)) == 1500)
check("UDP path truncates to a 512-byte TC stub",
      asyncio.run(dns_case(1500, udp=True)) == 512)
check("UDP path leaves small answers intact",
      asyncio.run(dns_case(100, udp=True)) == 100)


async def cache_case():
    relay = DNSRelay(socks_dial=FakeSocks(1500), log=lambda *a, **k: None)
    await relay.resolve(QUERY)                      # populate cache
    tcp_hit = len(await relay.resolve(QUERY, udp=False))
    udp_hit = len(await relay.resolve(QUERY, udp=True))
    return tcp_hit, udp_hit


tcp_hit, udp_hit = asyncio.run(cache_case())
check("cache hit: TCP still gets 1500 bytes", tcp_hit == 1500)
check("cache hit: UDP gets truncated stub", udp_hit == 512)
udp_src = inspect.getsource(_UDPProto._handle)
check("UDP handler calls resolve(udp=True)", "udp=True" in udp_src)

print("== 7. tunnel.py: robust control-protocol parsing ==")
import vpeen.tunnel as tun  # noqa: E402
oc = inspect.getsource(tun.TunnelWorker._on_client)
check("hello JSON parse wrapped in try/except",
      "except ValueError" in oc)
check("hello validated as dict", "isinstance(hello, dict)" in oc)
check("_arg_value helper exists", hasattr(tun, "_arg_value"))
check("main() uses _arg_value",
      '_arg_value(argv, "--ctl-port"' in inspect.getsource(tun.main))
check("_arg_value returns default on missing flag",
      tun._arg_value(["--tunnel-worker"], "--ctl-port", "7") == "7")
check("_arg_value returns default on dangling flag",
      tun._arg_value(["--ctl-port"], "--ctl-port", "7") == "7")
check("_arg_value returns value when present",
      tun._arg_value(["--ctl-port", "42"], "--ctl-port", "7") == "42")
# NOTE: we deliberately do NOT execute tun.main(["--tunnel-worker", ...])
# here - it would start a real control server.  The source-level guarantee
# (only _arg_value touches argv indexes now) covers the IndexError case.
check("main() reads flags only through _arg_value (no bare indexing)",
      "argv.index(" not in inspect.getsource(tun.main))

print("== 8. utils.State: cross-instance lost-update fix ==")
tmp = tempfile.mkdtemp()
sp = os.path.join(tmp, "state.json")
a = State(sp)
a.set("udid", "fixed-udid")                # instance A writes a key
b = State(sp)                              # B loads after A's write
b.set("reserve_domains", ["https://x.example"])   # B writes a different key
disk = json.load(open(sp))
check("A's key survives B's save (merge-on-save)",
      disk.get("udid") == "fixed-udid", json.dumps(disk))
check("B's own key is on disk",
      disk.get("reserve_domains") == ["https://x.example"])

print("== 9. utils.ensure_private_dir: tightening loose dirs ==")
loose = os.path.join(tmp, "loose_dir")
os.makedirs(loose)
os.chmod(loose, 0o775)
from vpeen.utils import ensure_private_dir  # noqa: E402
ensure_private_dir(loose)
mode = stat.S_IMODE(os.stat(loose).st_mode)
check("pre-existing 0775 dir tightened to 0700", mode == 0o700, oct(mode))
tight = os.path.join(tmp, "tight_dir")
ensure_private_dir(tight)
check("fresh dir is 0700", stat.S_IMODE(os.stat(tight).st_mode) == 0o700)

print("== 10. gui/cli cleanups ==")
# v4.4.0: the Tk GUI was replaced by the web UI; the connect/state-machine
# logic the old checks guarded now lives in vpeen/webui.py (Bridge).
gui_src = open(os.path.join(REPO, "vpeen", "gui.py")).read()
bridge_src = open(os.path.join(REPO, "vpeen", "webui.py")).read()
check("PHASE_CONNECTING import removed", "PHASE_CONNECTING" not in gui_src)
check("dead free_n removed", "free_n" not in gui_src)
check("ports must differ - connect path",
      "socks_port == http_port" in bridge_src)
check("ports must differ - settings path",
      'cfg.get("socks_port") == cfg.get("http_port")' in bridge_src)
check("pending_tunnel consumed after use",
      "self._pending_tunnel = False" in bridge_src)
cli_src = open(os.path.join(REPO, "vpeen", "cli.py")).read()
check("cli: unused __version__ import removed",
      "from . import __version__" not in cli_src)
check("cli: no-subcommand prints help",
      "parser.print_help()" in cli_src)

print("== 11. systemproxy: macOS disabled services filtered ==")
sp_src = open(os.path.join(REPO, "vpeen", "systemproxy.py")).read()
check("starred services filtered", 'not s.startswith("*")' in sp_src)

print("== 12. main.py / spec / version ==")
main_src = open(os.path.join(REPO, "main.py")).read()
check("worker writes last-run-worker.log", "last-run-worker.log" in main_src)
spec_src = open(os.path.join(REPO, "vpeen.spec")).read()
check("spec reads version dynamically", "_m.group(1)" in spec_src)
check("info_plist uses the dynamic version",
      '"CFBundleShortVersionString": _version' in spec_src
      and '"CFBundleVersion": _version' in spec_src)
import vpeen  # noqa: E402
check("version bumped to 4.5.0", vpeen.__version__ == "4.5.0")

print("== 13. import sweep (no import cycles) ==")
for mod in ("vpeen.api", "vpeen.upstream", "vpeen.localproxy", "vpeen.dnsrelay",
            "vpeen.tunnel_platforms", "vpeen.tunnel", "vpeen.systemproxy",
            "vpeen.settings", "vpeen.cli", "vpeen.core", "vpeen.utils"):
    try:
        __import__(mod)
        check(f"import {mod}", True)
    except Exception as e:
        check(f"import {mod}", False, repr(e))

print()
print(f"RESULT: {len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILED:", *FAIL, sep="\n  - ")
sys.exit(1 if FAIL else 0)
