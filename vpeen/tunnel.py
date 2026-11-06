"""
VPeeN Tunnel (TUN/VPN) mode.

Architecture (mirrors how Hiddify's TUN mode works):

    apps -> TUN adapter -> tun2socks (userspace TCP/IP stack, MIT)
                |  socks5://127.0.0.1:<port>   (loopback, same host)
                v
          VPeeN local SOCKS5 -> VeePN HTTPS-CONNECT upstream

Two roles live in this module:

* TunnelWorker  - runs ELEVATED as ``--tunnel-worker``.  Owns tun2socks,
  the adapter address/routes (tunnel_platforms) and the DNS relay
  (dnsrelay).  Controlled over a localhost TCP socket with a random
  token; a GUI watchdog must PING every 2 s or the worker tears
  everything down by itself (never leaves the network broken).
* TunnelController - the GUI-side client.  Spawns the elevated worker
  (UAC / pkexec / osascript), speaks the control protocol, keeps the
  heartbeat and translates worker events into GUI events.

The session journal (tunnel_platforms.save_session) plus "start-up sweep"
guarantee recovery even after a hard crash.
"""
import asyncio
import json
import os
import platform
import secrets
import socket
import subprocess
import sys

from .utils import ensure_private_dir
import threading
import time

from . import tunnel_platforms as plat

ADAPTER_GUID = plat.ADAPTER_GUID

WORKER_HEARTBEAT_TIMEOUT = 12.0     # seconds without PING -> self-teardown
WORKER_IDLE_EXIT = 60.0            # no GUI client + idle -> exit (v4.2.3)
CONNECT_TIMEOUT = 40.0              # GUI wait for worker socket
WORKER_POLL = 1.0                  # v4.9.0: kill-switch liveness cadence

sys_platform = platform.system().lower()


# ------------------------------------------------------------------ helpers
def _log_file():
    d = plat.CONFIG_DIR
    ensure_private_dir(d)
    return os.path.join(d, "tunnel-worker.log")


def _flog(msg, lvl="info"):
    """Append to the worker log file (survives GUI crash)."""
    try:
        with open(_log_file(), "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} [{lvl}] {msg}\n")
    except OSError:
        pass


def platform_key() -> str:
    machine = platform.machine().lower()
    if sys_platform.startswith("win"):
        return "windows-amd64" if "64" in machine or machine == "amd64" \
            else "windows-x86"
    if sys_platform == "darwin":
        return "darwin-arm64" if machine in ("arm64", "aarch64") \
            else "darwin-amd64"
    return "linux-amd64" if "64" in machine else "linux-x86"


def _valid_route_ip(ip: str) -> bool:
    """True for a concrete IPv4 literal usable as a host-route target.

    v4.8.0: the anti-loop contract demands that everything passed to the
    platform route commands is a real IPv4 (hostnames make `ip route`
    fail outright and route.exe resolve inconsistently; IPv6 upstreams
    cannot be protected by this IPv4-only TUN setup)."""
    import ipaddress
    try:
        return ipaddress.ip_address(str(ip).strip()).version == 4
    except (ValueError, AttributeError):
        return False


def binary_paths():
    """Locate tun2socks (+wintun dir) for this platform, frozen or source."""
    key = platform_key()
    name = "tun2socks.exe" if key.startswith("windows") else "tun2socks"
    roots = []
    if getattr(sys, "frozen", False):
        roots.append(getattr(sys, "_MEIPASS", ""))
        roots.append(os.path.dirname(sys.executable))
    roots.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    for root in roots:
        if not root:
            continue
        cand = os.path.join(root, "assets", "bin", key, name)
        if os.path.exists(cand):
            return cand, os.path.join(root, "assets", "bin")
    return None, None


def tunnel_available() -> tuple[bool, str]:
    exe, _ = binary_paths()
    if not exe:
        return False, ("tunnel binaries not found - run "
                       "scripts/fetch-binaries.py or reinstall the release")
    if not sys_platform.startswith("win") and sys_platform not in ("linux", "darwin"):
        return False, f"unsupported platform {sys_platform}"
    return True, ""


# ================================================================ WORKER
class TunnelWorker:
    """Elevated helper: tun2socks lifecycle + routes + DNS relay + watchdog."""

    def __init__(self, ctl_port: int, ctl_token: str):
        self.ctl_port = ctl_port
        self.ctl_token = ctl_token
        self.state = "idle"                 # idle | up
        self.last_ping = time.time()
        self.clients: set = set()           # (reader, writer)
        self.tun2socks: subprocess.Popen | None = None
        self.relay = None
        self.journal = None
        self.socks_port = 1080
        self.upstream_ips: list[str] = []
        self.upstream_hosts: dict = {}        # v4.8.0: hostname -> pinned IPs
        self.strict_ips = False              # v4.8.0: hostnames were seen ->
                                             # an empty IP set is FATAL
        self.dns_enabled = True
        self.mtu = 1500
        self.kill_switch = True              # v4.9.0: leak-proof policy
        self._ks_healing = False             # v4.9.0: repair loop guard
        self._ks_step = None                 # v4.9.0: active containment step
        self.KS_REPAIR_TRIES = 3             # respawn attempts before lockdown
        self.KS_REPAIR_WAIT = 2.0            # pause between attempts
        self._tasks: set = set()            # anchored tasks (asyncio keeps
        self._tun2socks_logf = None        # only weak refs - unanchored
                                            # tasks can be GC'd mid-flight)
        self._up_task = None               # in-flight "up" guard (v4.2.2)
        self._upd_task = None              # v4.8.0: in-flight "upd" guard

    # ------------------------------------------------------------- lifecycle
    def _spawn(self, coro):
        """Create a task that the loop cannot garbage-collect silently."""
        t = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(t)

        def _reap(task):
            self._tasks.discard(task)
            if not task.cancelled():
                exc = task.exception()
                if exc is not None:
                    _flog(f"internal task error: {exc}", "err")

        t.add_done_callback(_reap)
        return t

    async def amain(self):
        server = await asyncio.start_server(self._on_client,
                                            "127.0.0.1", self.ctl_port)
        _flog(f"worker listening on 127.0.0.1:{self.ctl_port}")
        self._spawn(self._watchdog())
        async with server:
            try:
                await server.serve_forever()
            finally:
                await self._down("worker exiting")

    # ---------------------------------------------------------------- comms
    async def _on_client(self, reader, writer):
        try:
            first = await asyncio.wait_for(reader.readline(), 10)
            # v4.3.1: a garbage/empty first line used to raise an UNCAUGHT
            # ValueError (JSONDecodeError) or AttributeError (non-dict JSON)
            # inside the connection callback - asyncio then logged a scary
            # "exception in callback" traceback for every bad probe that
            # touched the control port.
            try:
                hello = json.loads(first)
            except ValueError:
                writer.close()
                return
            if not isinstance(hello, dict) or \
                    hello.get("auth") != self.ctl_token:
                writer.close()
                return
            await self._send(writer, {"ev": "hello"})
            self.clients.add((reader, writer))
            self.last_ping = time.time()
            while True:
                line = await reader.readline()
                if not line:
                    break
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                await self._handle(writer, msg)
        except (asyncio.IncompleteReadError, asyncio.TimeoutError,
                ConnectionError, OSError):
            pass
        finally:
            self.clients.discard((reader, writer))
            try:
                writer.close()
            except Exception:
                pass

    async def _send(self, writer, obj):
        try:
            writer.write((json.dumps(obj) + "\n").encode())
            await writer.drain()
        except (ConnectionError, OSError):
            pass

    def broadcast(self, obj):
        for rd, wr in list(self.clients):
            self._spawn(self._send(wr, obj))

    def blog(self, msg, lvl="info"):
        _flog(msg, lvl)
        self.broadcast({"ev": "log", "level": lvl, "line": msg})

    # ------------------------------------------------------------- commands
    async def _handle(self, writer, msg):
        cmd = msg.get("cmd")
        if cmd == "ping":
            self.last_ping = time.time()
            await self._send(writer, {"ev": "pong"})
        elif cmd == "up":
            self.last_ping = time.time()
            cfg = msg.get("cfg", {})
            self.socks_port = int(cfg.get("socks_port", 1080))
            self.mtu = int(cfg.get("mtu", 1500))
            self.upstream_ips = [ip for ip in cfg.get("upstream_ips", [])
                                if isinstance(ip, str)]
            hosts = cfg.get("upstream_hosts") or {}
            self.upstream_hosts = {str(k): [str(x) for x in v]
                                  for k, v in hosts.items() if v} \
                if isinstance(hosts, dict) else {}
            self.strict_ips = bool(cfg.get("strict_ips", False))
            self.dns_enabled = bool(cfg.get("dns", True))
            self.kill_switch = bool(cfg.get("kill_switch", True))
            # v4.2.2: guard against two "up" commands racing - the old code
            # spawned _up() for each, and two concurrent runs could launch
            # two tun2socks processes and interleave route setup.
            if self._up_task is None or self._up_task.done():
                self._up_task = self._spawn(self._up())
        elif cmd == "down":
            self.last_ping = time.time()
            self._spawn(self._down("requested by app"))
        elif cmd == "upd":
            self.last_ping = time.time()
            self.upstream_ips = [ip for ip in msg.get("upstream_ips", [])
                                if isinstance(ip, str)]
            hosts = msg.get("upstream_hosts") or {}
            if isinstance(hosts, dict):
                self.upstream_hosts = {str(k): [str(x) for x in v]
                                       for k, v in hosts.items() if v}
            # v4.8.0: same guard as "up" - two rotations in flight could
            # interleave route deletes/adds and leave the journal pointing
            # at routes that no longer exist.
            if self._upd_task is None or self._upd_task.done():
                self._upd_task = self._spawn(self._refresh_host_routes())
        elif cmd == "quit":
            await self._down("quit command")
            os._exit(0)

    # ------------------------------------------------------------------- up
    def _tun2socks_log_path(self):
        return os.path.join(plat.CONFIG_DIR, "tun2socks.log")

    def _tun2socks_tail(self, lines=25):
        """Last lines of the tun2socks log - the ONLY way to see why it
        failed (its stderr used to go to DEVNULL, making every failure
        a mystery)."""
        try:
            with open(self._tun2socks_log_path(), "rb") as f:
                data = f.read()
            return b"\n".join(data.splitlines()[-lines:]).decode(
                "utf-8", "replace").strip()
        except Exception:
            return ""

    def _check_tun2socks_alive(self):
        """Raise with the log tail if tun2socks died during startup."""
        if self.tun2socks is not None and self.tun2socks.poll() is not None:
            tail = self._tun2socks_tail()
            msg = f"tun2socks exited early (rc={self.tun2socks.returncode})"
            if tail:
                msg += f"; last output:\n{tail}"
            raise OSError(msg)

    def _close_tun2socks_log(self):
        if self._tun2socks_logf:
            try:
                self._tun2socks_logf.close()
            except Exception:
                pass
            self._tun2socks_logf = None

    def _launch_tun2socks(self):
        """Start tun2socks with the CURRENT worker config (v4.9.0: shared
        by the initial up() and the kill-switch repair path so a respawn
        is byte-identical to the original launch)."""
        exe, _bindir = binary_paths()
        if not exe:
            raise OSError("tun2socks binary missing")
        args = [exe, "--device", self._device_arg(),
                "--proxy", f"socks5://127.0.0.1:{self.socks_port}",
                "--mtu", str(self.mtu), "--loglevel", "info"]
        self.blog(f"starting tun2socks: {' '.join(args)}")
        flags = 0
        if os.name == "nt":
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            self._tun2socks_logf = open(self._tun2socks_log_path(), "ab")
        except Exception:
            self._tun2socks_logf = None
        out = self._tun2socks_logf or subprocess.DEVNULL
        self.tun2socks = subprocess.Popen(
            args, cwd=os.path.dirname(exe), creationflags=flags,
            stdout=out, stderr=out, stdin=subprocess.DEVNULL)

    # ------------------------------------------------ v4.9.0 kill switch
    def _ks_engage_containment(self):
        """Raise the firewall containment (SYNC - run in an executor).

        Returns True when rules are active.  The step is journaled into
        the session file the moment it exists, so even a crash right
        after this line is recovered by the next-boot sweep."""
        from . import killswitch
        try:
            ok, why = killswitch.available()
            if not ok:
                self.blog(f"kill switch: firewall containment unavailable "
                          f"({why}) - relying on fast repair", "warn")
                return False
            step = killswitch.engage()
            if step is None:
                self.blog("kill switch: containment rules could not be "
                          "installed - repair continues without them", "warn")
                return False
            self._ks_step = step
            if self.journal is not None:
                self.journal.setdefault("ks", []).append(step)
                try:
                    plat.save_session({"os": plat.sys_platform,
                                       "journal": self.journal})
                except Exception:
                    pass
            self.blog("kill switch: firewall containment ACTIVE - "
                      "nothing leaves this machine while it repairs", "warn")
            return True
        except Exception as e:
            self.blog(f"kill switch engage failed: {e}", "warn")
            return False

    def _ks_release_containment(self):
        """Lift the containment rules and drop the journal step (SYNC)."""
        from . import killswitch
        try:
            if self._ks_step is not None:
                killswitch.release(self._ks_step)
            self._ks_step = None
            if self.journal is not None and self.journal.get("ks"):
                self.journal["ks"] = []
                try:
                    plat.save_session({"os": plat.sys_platform,
                                       "journal": self.journal})
                except Exception:
                    pass
        except Exception as e:
            _flog(f"kill switch release failed: {e}", "err")

    async def _ks_restart_relay(self):
        """Re-bind the DNS relay onto the reborn adapter address."""
        if not self.dns_enabled:
            return
        from .dnsrelay import DNSRelay
        if self.relay is not None:
            try:
                await self.relay.stop()
            except Exception:
                pass
            self.relay = None
        try:
            self.relay = DNSRelay(socks_dial=self._socks_dial,
                                  bind_ip=plat.TUN_IP, port=53,
                                  log=self.blog, pins=self.upstream_hosts)
            await self.relay.start()
            self.blog("DNS relay re-bound to the repaired adapter.", "ok")
        except OSError as e:
            self.relay = None
            self.blog(f"DNS relay restart failed: {e} - queries fall back "
                      "to the physical resolver (still routed through the "
                      "tunnel)", "warn")

    async def _ks_repair_once(self):
        """One repair attempt: respawn tun2socks, re-bind routes+address,
        restart the relay, prove it STAYS alive.  Raises on failure."""
        if not self.journal:
            raise OSError("no route journal to rebind")
        self._launch_tun2socks()
        await self._wait_adapter()
        loop = asyncio.get_running_loop()
        ok, err = await loop.run_in_executor(
            None, plat.rebind_tun_routes, self.journal)
        if not ok:
            raise OSError(err or "route rebind failed")
        await self._verify_address()
        await self._ks_restart_relay()
        await asyncio.sleep(1.2)          # settle: it must STAY alive
        self._check_tun2socks_alive()

    async def _ks_handle_death(self):
        """tun2socks died mid-session: the kill-switch core path.

        CONTAIN -> REPAIR (up to 3x) -> lift containment when proven, or
        LOCKDOWN (containment stays) when repair fails.  The user's
        traffic is never allowed to silently fall back to the physical
        route: when tun2socks dies the adapter - and with it the hijack
        routes - dies too, and without this path every packet would
        simply leave through the real gateway while the UI still says
        "Protected"."""
        if self._ks_healing or self.state != "up":
            return
        if not self.clients:
            # No GUI attached: this worker is orphaned (the GUI died
            # inside the heartbeat window).  Same policy as heartbeat
            # loss - contain, never leak, exit; the next app start (or
            # the next worker boot) sweeps the rules away.
            rc = self.tun2socks.returncode if self.tun2socks else None
            self.blog(f"tun2socks died (rc={rc}) with no GUI attached - "
                      "engaging containment and exiting (kill switch)",
                      "err")
            self._close_tun2socks_log()
            self.tun2socks = None
            self._ks_engage_containment()      # sync: we exit right after
            os._exit(0)
        self._ks_healing = True
        try:
            rc = self.tun2socks.returncode if self.tun2socks else None
            self._close_tun2socks_log()
            self.tun2socks = None
            self.broadcast({"ev": "ks_engaged", "rc": rc})
            self.blog(f"tun2socks died (rc={rc}) - kill switch ENGAGED: "
                      "outbound traffic is blocked, not leaked", "err")
            loop = asyncio.get_running_loop()
            # AWAITED (not fire-and-forget): the containment must be
            # PROVEN active before the repair starts, and proven lifted
            # before "healed" is broadcast - otherwise the UI could say
            # "Protected" while the block rule is still up.
            await loop.run_in_executor(None, self._ks_engage_containment)
            for attempt in range(1, self.KS_REPAIR_TRIES + 1):
                self.blog(f"kill switch: repairing the tunnel "
                          f"({attempt}/{self.KS_REPAIR_TRIES})...", "warn")
                try:
                    await self._ks_repair_once()
                    # containment lifts ONLY after the hijack is proven
                    # back in place - that ordering is what makes the
                    # repair path leak-free.
                    await loop.run_in_executor(
                        None, self._ks_release_containment)
                    self.broadcast({"ev": "ks_healed"})
                    self.blog("kill switch: tunnel REPAIRED - protection "
                              "restored with no leak window", "ok")
                    return
                except Exception as e:
                    self.blog(f"repair attempt failed: {e}", "warn")
                    await asyncio.sleep(self.KS_REPAIR_WAIT)
            self.broadcast({"ev": "ks_lockdown"})
            self.blog("KILL SWITCH LOCKDOWN: traffic stays BLOCKED - this "
                      "is the safe state. Disconnect to restore your "
                      "normal network, or connect again to retry.", "err")
        finally:
            self._ks_healing = False

    async def _up(self):
        if self.state == "up":
            await self._down("re-connecting")
        try:
            exe, bindir = binary_paths()
            if not exe:
                raise OSError("tun2socks binary missing")
            if not plat.is_admin():
                raise OSError("worker is not elevated")

            # v4.8.0 "Sentinel" - THE anti-loop contract:
            # raising the default-route hijack with an empty/invalid
            # upstream IP set means every upstream dial loops back into
            # the tunnel: "connected" with a dead network.  When the GUI
            # says hostname-based servers were involved (strict_ips), a
            # resolved-IP set that is empty is FATAL - refuse to hijack.
            # And when the list was NON-empty but nothing in it is a
            # usable IPv4 literal (e.g. v6-only upstreams), the hijack
            # would have the same blackhole: refuse that too, always.
            routable = [ip for ip in self.upstream_ips
                        if _valid_route_ip(ip)]
            if not routable and (self.strict_ips or self.upstream_ips):
                raise OSError(
                    "upstream server IPs could not be resolved - refusing "
                    "to raise the tunnel (it would blackhole all traffic); "
                    "try again or use proxy mode")

            # 0. discover original route BEFORE any changes
            device = self._device_arg()
            if not device:
                raise OSError("could not plan the TUN device")

            # 1. launch tun2socks (it creates the TUN adapter itself).
            #    v4.2.0: its output now goes to tun2socks.log instead of
            #    DEVNULL so failures are actually diagnosable.
            #    v4.8.0: --interface is GONE.  The SOCKS server it dials is
            #    loopback (127/8 beats the /1 hijack routes), and binding
            #    its outbound socket to the physical interface was pure
            #    risk on some stacks (a loopback destination through a
            #    bound physical interface is not guaranteed to work).
            #    v4.9.0: the launch lives in _launch_tun2socks() so the
            #    kill-switch repair path can respawn it identically.
            self._launch_tun2socks()

            # 2. wait for the ADAPTER to appear.  v4.2.0 fix: the old code
            #    waited for the adapter ADDRESS (198.18.0.1) here, but that
            #    address is only assigned by bring_up() BELOW - so the wait
            #    timed out every single time and tunnel mode never came up.
            await self._wait_adapter()
            journal, ok, err = plat.bring_up(self.mtu,
                                            [ip for ip in self.upstream_ips
                                             if _valid_route_ip(ip)])
            if journal is None:
                raise OSError(err or "route setup failed")
            self.journal = journal
            # v4.2.2: ok=False means the def1 hijack (or the adapter) failed -
            # traffic would NOT actually be routed through the tunnel, yet the
            # old code still broadcast "up" and the GUI showed "Protected".
            # That is a silent no-VPN lie; fail loudly and tear down instead.
            if not ok:
                raise OSError(f"route setup failed: {err or 'incomplete'}")
            # sanity: the address should now be present (netsh/ip are sync)
            await self._verify_address()

            # 2. DNS relay bound to the TUN address (through local SOCKS5).
            #    v4.8.0 ORDER FIX: the relay must be LISTENING before the
            #    system DNS is pointed at it.  The old order swapped
            #    resolv.conf / macOS DNS inside bring_up and then started
            #    the relay - a bind failure (53 already taken) left the
            #    whole machine with dead DNS while the tunnel was up.
            #    The swap now happens BELOW, only after a live relay exists,
            #    and is journaled exactly like a route so teardown/crash
            #    recovery restores it.
            relay_ok = False
            if self.dns_enabled:
                from .dnsrelay import DNSRelay
                self.relay = DNSRelay(socks_dial=self._socks_dial,
                                      bind_ip=plat.TUN_IP, port=53,
                                      log=self.blog,
                                      pins=self.upstream_hosts)
                try:
                    await self.relay.start()
                    relay_ok = True
                except OSError as e:
                    self.blog(f"DNS relay failed to bind: {e} - system DNS "
                              "stays on the physical resolver (queries "
                              "leak to your ISP, traffic stays protected)",
                              "warn")
                    self.relay = None
            if relay_ok:
                if plat.apply_dns(self.journal):
                    self.blog("System DNS now resolves through the tunnel.",
                              "ok")
                else:
                    self.blog("DNS swap skipped/failed - keeping the physical "
                              "resolver for now.", "warn")

            self.state = "up"
            self.broadcast({"ev": "up"})
            self.blog("Tunnel is UP - all traffic now flows through VPeeN.",
                      "ok")
            # v4.9.0: a dying orphaned worker may have engaged containment
            # AFTER this worker's boot sweep (reconnect race).  Rules can
            # never legitimately belong to THIS fresh tunnel (containment
            # only engages on tun2socks death), so sweep them once more.
            asyncio.get_running_loop().run_in_executor(
                None, _stale_ks_rules_only)
        except Exception as e:
            self.blog(f"Tunnel UP failed: {e}", "err")
            await self._down("failed")
            self.broadcast({"ev": "down", "error": str(e)})

    def _device_arg(self):
        if sys_platform.startswith("win"):
            return f"tun://VPeeN?guid={ADAPTER_GUID}"
        if sys_platform == "linux":
            return f"tun://{plat.TUN_NAME['linux']}"
        if sys_platform == "darwin":
            return plat.TUN_NAME["darwin"]
        return None

    def _orig_bind_interface(self):
        """Interface name for tun2socks --interface (best effort)."""
        try:
            if sys_platform.startswith("win"):
                orig = plat._win_orig_route()
                return orig[1] if orig else None
            if sys_platform == "linux":
                orig = plat._linux_orig_route()
                return orig[1] if orig else None
            if sys_platform == "darwin":
                orig = plat._mac_orig_route()
                return orig[1] if orig else None
        except Exception:
            return None
        return None

    async def _wait_adapter(self, timeout=20):
        """Wait for the TUN adapter (device) to exist - NOT for its address,
        which is assigned later by bring_up().  Fails fast if tun2socks
        dies, including its log tail so the cause is visible."""
        loop = asyncio.get_running_loop()
        t0 = time.time()
        while time.time() - t0 < timeout:
            self._check_tun2socks_alive()
            if await loop.run_in_executor(None, self._adapter_present):
                return
            await asyncio.sleep(0.4)
        self._check_tun2socks_alive()
        name = (plat.TUN_NAME.get("win32") if sys_platform.startswith("win")
                else plat.TUN_NAME.get("linux" if sys_platform == "linux"
                                       else "darwin"))
        raise OSError(f"TUN adapter '{name}' did not appear within {timeout}s "
                      f"(tun2socks may have failed - see tun2socks.log)")

    def _adapter_present(self):
        """True once the TUN *device* exists (address may not be set yet)."""
        try:
            if sys_platform.startswith("win"):
                return plat._win_tun_index() is not None
            if sys_platform == "linux":
                return os.path.exists(
                    f"/sys/class/net/{plat.TUN_NAME['linux']}")
            if sys_platform == "darwin":
                rc, _, _ = plat.run(["ifconfig", plat.TUN_NAME["darwin"]],
                                    timeout=10)
                return rc == 0
        except Exception:
            return False
        return False

    async def _verify_address(self, timeout=8):
        """Post-bring_up sanity check - warn only (netsh/ip are sync, so a
        miss here is unusual)."""
        loop = asyncio.get_running_loop()
        t0 = time.time()
        while time.time() - t0 < timeout:
            if await loop.run_in_executor(None, self._addr_present):
                return
            await asyncio.sleep(0.4)
        self.blog(f"note: address {plat.TUN_IP} not visible yet - "
                  "continuing anyway", "warn")

    def _addr_present(self):
        if sys_platform.startswith("win"):
            rc, out, _ = plat.run(["netsh", "interface", "ipv4", "show",
                                   "addresses", f"name={plat.TUN_NAME['win32']}"])
            return rc == 0 and plat.TUN_IP in out
        if sys_platform == "linux":
            rc, out, _ = plat.run(["ip", "addr", "show",
                                   plat.TUN_NAME["linux"]])
            return rc == 0 and plat.TUN_IP in out
        if sys_platform == "darwin":
            rc, out, _ = plat.run(["ifconfig", plat.TUN_NAME["darwin"]])
            return rc == 0 and plat.TUN_IP in out
        return False

    async def _socks_dial(self, host, port):
        """Open a TCP connection to host:port through the local SOCKS5.
        v4.2.0 fix: parse the reply PROPERLY (the old code consumed the
        2-byte greeting reply + 8 of the 10-byte CONNECT reply, leaving 2
        stray bytes in the stream - the DNS relay then misread them as a
        TCP-DNS length prefix and EVERY query failed)."""
        reader, writer = await asyncio.open_connection("127.0.0.1",
                                                       self.socks_port)
        try:
            target = host.encode()
            writer.write(b"\x05\x01\x00"           # ver 5, 1 method, no-auth
                         b"\x05\x01\x00\x03" + bytes([len(target)]) + target +
                         (port).to_bytes(2, "big"))
            await writer.drain()

            greet = await asyncio.wait_for(reader.readexactly(2), 10)
            if greet[0] != 5:
                raise OSError("bad SOCKS5 greeting reply")
            if greet[1] != 0:
                raise OSError(f"SOCKS5 method rejected (code {greet[1]})")
            rep = await asyncio.wait_for(reader.readexactly(4), 10)
            if rep[0] != 5:
                raise OSError("bad SOCKS5 reply")
            if rep[1] != 0:
                raise OSError(f"SOCKS5 dial failed (code {rep[1]})")
            atyp = rep[3]
            if atyp == 0x01:      # IPv4: BND.ADDR(4) + BND.PORT(2)
                await reader.readexactly(6)
            elif atyp == 0x04:    # IPv6: BND.ADDR(16) + BND.PORT(2)
                await reader.readexactly(18)
            elif atyp == 0x03:    # domain: LEN(1) + ADDR(n) + PORT(2)
                n = (await reader.readexactly(1))[0]
                await reader.readexactly(n + 2)
            else:
                raise OSError(f"unexpected SOCKS5 ATYP {atyp}")
            return reader, writer
        except Exception:
            try:
                writer.close()
            except Exception:
                pass
            raise

    async def _refresh_host_routes(self):
        """Server list rotated in the GUI: replace anti-loop host routes.

        v4.2.2 CRITICAL fix: the old keep-filter dropped the DEFAULT-HIJACK
        routes from the journal on Linux (0.0.0.0/1 + 128.0.0.0/1 have no
        mask) and macOS (the eight net chunks are /8../1 prefixes).  After a
        credential rotation, bring_down() then no longer deleted them, so
        disconnecting left all traffic routed into the dead TUN adapter -
        a total network blackout until reboot.  The journal now only ever
        loses entries that ARE host routes (the ones this refresh replaces)."""
        if self.state != "up" or not self.journal:
            return

        def _is_host_route(r):
            if r.get("type") != "route":
                return False
            if sys_platform.startswith("win"):
                return r.get("mask") == "255.255.255.255"
            if sys_platform == "linux":
                return str(r.get("prefix", "")).endswith("/32")
            if sys_platform == "darwin":
                return r.get("kind") == "host"
            return False

        try:
            routes = self.journal.get("routes", [])
            keep = [r for r in routes if not _is_host_route(r)]
            host_routes = [r for r in routes if _is_host_route(r)]
            for r in host_routes:
                if sys_platform.startswith("win"):
                    plat.run(["route", "delete", r["prefix"], "mask", r["mask"],
                              r["gw"]])
                elif sys_platform == "linux":
                    # journal prefixes already carry /32 - do not double it
                    # (the old f"{r['prefix']}/32" built 'ip/32/32' and the
                    # stale route was never actually deleted)
                    prefix = str(r["prefix"])
                    if not prefix.endswith("/32"):
                        prefix += "/32"
                    if r.get("via"):
                        plat.run(["ip", "route", "del", prefix,
                                  "via", r["via"]])
                    else:
                        plat.run(["ip", "route", "del", prefix])
                elif sys_platform == "darwin":
                    plat.run(["route", "-n", "delete", "-host", r["prefix"],
                              r["gw"]])
            fresh = []
            orig = self._orig_route_triple()
            for ip in self.upstream_ips:
                # v4.8.0: never hand a hostname (or a v6 literal) to the
                # route commands - both silently fail today.
                if not _valid_route_ip(ip):
                    continue
                if orig is None:
                    break
                if sys_platform.startswith("win"):
                    gw, _alias, idx = orig
                    rc, _, _ = plat.run(["route", "add", ip, "mask",
                                         "255.255.255.255", gw, "metric", "1",
                                         "if", str(idx)])
                    if rc == 0:
                        fresh.append({"type": "route", "prefix": ip,
                                      "mask": "255.255.255.255", "gw": gw,
                                      "metric": "1", "if": idx})
                elif sys_platform == "linux":
                    # v4.2.2: _orig_route_triple returns a TRIPLE - the old
                    # 'gw, _dev = orig' raised 'too many values to unpack',
                    # so after a rotation NO fresh host routes were ever
                    # added on Linux and the tunnel silently broke.
                    gw, _dev, _idx = orig
                    rc, _, _ = plat.run(["ip", "route", "add", f"{ip}/32",
                                         "via", gw])
                    if rc == 0:
                        fresh.append({"type": "route", "prefix": f"{ip}/32",
                                      "via": gw})
                elif sys_platform == "darwin":
                    gw, _dev = orig
                    rc, _, _ = plat.run(["route", "-n", "add", "-host", ip, gw])
                    if rc == 0:
                        fresh.append({"type": "route", "kind": "host",
                                      "prefix": ip, "gw": gw})
            self.journal["routes"] = keep + fresh
            plat.save_session({"os": plat.sys_platform, "journal": self.journal})
            self.blog(f"host routes refreshed ({len(fresh)} upstream servers).")
        except Exception as e:
            self.blog(f"host-route refresh failed: {e}", "warn")

    def _orig_route_triple(self):
        if sys_platform.startswith("win"):
            return plat._win_orig_route()
        if sys_platform == "linux":
            orig = plat._linux_orig_route()
            return (orig[0], orig[1], 0) if orig else None
        if sys_platform == "darwin":
            return plat._mac_orig_route()
        return None

    # ----------------------------------------------------------------- down
    async def _down(self, reason):
        self.blog(f"tearing tunnel down ({reason}) ...")
        if self.relay:
            try:
                await self.relay.stop()
            except Exception:
                pass
            self.relay = None
        if self.tun2socks:
            try:
                self.tun2socks.terminate()
                try:
                    self.tun2socks.wait(timeout=6)
                except subprocess.TimeoutExpired:
                    self.tun2socks.kill()
            except Exception:
                pass
            self.tun2socks = None
        if self._tun2socks_logf:
            try:
                self._tun2socks_logf.close()
            except Exception:
                pass
            self._tun2socks_logf = None
        self._ks_step = None          # bring_down() released the rules
        if self.journal:
            try:
                plat.bring_down(self.journal)
            except Exception as e:
                _flog(f"route cleanup error: {e}", "err")
            self.journal = None
        elif plat.load_session():
            # crash leftovers from this worker or an older one
            try:
                plat.cleanup_from_session(plat.load_session())
            except Exception as e:
                _flog(f"orphan cleanup error: {e}", "err")
        if self.state == "up":
            self.state = "idle"
            self.broadcast({"ev": "down"})
            self.blog("Tunnel is DOWN - original network restored.", "ok")

    # ------------------------------------------------------------- watchdog
    async def _watchdog(self):
        while True:
            await asyncio.sleep(WORKER_POLL)
            # v4.9.0 kill switch: liveness of tun2socks while the tunnel
            # is up.  A dead tun2socks means the adapter (and every
            # hijack route on it) is gone and the machine is leaking
            # through the physical gateway RIGHT NOW - hand the case to
            # the kill-switch handler within one poll interval.
            if self.state == "up" and self.tun2socks is not None:
                if self.tun2socks.poll() is not None and not self._ks_healing:
                    self._spawn(self._ks_handle_death())
            if self.state == "up" and \
                    time.time() - self.last_ping > WORKER_HEARTBEAT_TIMEOUT:
                if self.kill_switch:
                    # GUI is gone; the local SOCKS core died with it, so
                    # the still-hijacked routes blackhole everything -
                    # the SAFE state.  Restoring the routes instead (the
                    # pre-4.9 behaviour) would silently send the user's
                    # traffic out through the real network with no app
                    # running and no protection at all.  tun2socks is
                    # deliberately left ALIVE: killing it would destroy
                    # the adapter and drop the hijack (= leak).
                    _flog("GUI heartbeat lost - kill switch: keeping the "
                          "blackhole (network stays OFFLINE, nothing "
                          "leaks; start VPeeN again to restore)", "err")
                    os._exit(0)
                _flog("GUI heartbeat lost - emergency teardown", "err")
                await self._down("heartbeat lost")
                os._exit(0)          # never linger after the GUI is gone
            # v4.2.3: the GUI can also die BEFORE the tunnel ever came up
            # (crash during elevation/startup).  An idle worker with no
            # connected client must not linger forever as an invisible
            # elevated zombie process holding the control port.
            if self.state == "idle" and not self.clients and \
                    time.time() - self.last_ping > WORKER_IDLE_EXIT:
                _flog("no GUI client connected - idle worker exiting", "warn")
                os._exit(0)


# ============================================================= CONTROLLER
class TunnelController:
    """GUI-side manager for the elevated worker (thread-driven)."""

    def __init__(self, events_q):
        self.events = events_q
        self.state = "down"            # down | starting | up | error
        self._proc: subprocess.Popen | None = None
        self._sock: socket.socket | None = None
        self._wfile = None
        self._token = ""
        self._ctl_port = 0
        self._cfg: dict = {}
        self._hb_thread: threading.Thread | None = None
        self._rx_thread: threading.Thread | None = None
        self._lock = threading.Lock()

    # ---------------------------------------------------------------- events
    def _emit(self, **ev):
        ev["type"] = "tunnel"
        self.events.put(ev)

    def log(self, msg, lvl="info"):
        self._emit(ev="log", level=lvl, line=msg)

    # ----------------------------------------------------------- availability
    @staticmethod
    def is_available() -> tuple[bool, str]:
        return tunnel_available()

    @staticmethod
    def is_admin() -> bool:
        return plat.is_admin()

    @staticmethod
    def cleanup_orphans(log=None) -> None:
        """Elevated one-shot cleanup of a crashed session (spawns helper)."""
        session = plat.load_session()
        if not session:
            return
        if plat.is_admin():
            plat.cleanup_from_session(session)
            return
        args = ["--tunnel-cleanup", "--session", plat.SESSION_PATH]
        exe = sys.executable
        if getattr(sys, "frozen", False):
            argv = [exe] + args
        else:
            argv = [exe, "-m", "vpeen.tunnel"] + args
        cwd = (os.path.dirname(sys.executable) if getattr(sys, "frozen", False)
               else os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        if sys_platform.startswith("win"):
            import ctypes
            params = " ".join(f'"{a}"' if " " in a else a for a in argv[1:])
            rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", argv[0],
                                                     params, cwd, 0)
            if rc <= 32:
                raise OSError(f"elevation cancelled (code {rc})")
        elif sys_platform == "darwin":
            script = " ".join("'" + a.replace("'", "'\\''") + "'"
                              for a in argv)
            subprocess.run(["osascript", "-e",
                            f'do shell script "{script}" '
                            f"with administrator privileges"],
                           check=True, timeout=120, capture_output=True)
        else:
            script = " ".join("'" + a.replace("'", "'\\''") + "'"
                              for a in argv)
            subprocess.run(["pkexec", "sh", "-c", script], check=True,
                           timeout=120, capture_output=True)
        if log:
            log("Orphaned tunnel state removed.", "ok")

    # --------------------------------------------------------------- connect
    def connect(self, socks_port: int, upstream_ips: list[str],
                upstream_hosts: "dict[str, list[str]] | None" = None,
                mtu: int = 1500, dns: bool = True,
                strict_ips: bool = False,
                kill_switch: bool = True) -> None:
        if self.state in ("starting", "up"):
            return
        threading.Thread(target=self._connect_sync,
                         args=(socks_port, upstream_ips, upstream_hosts,
                               mtu, dns, strict_ips, kill_switch),
                         daemon=True, name="vpeen-tunnelctl").start()

    def _elevated_spawn(self, args: list[str]) -> None:
        exe = sys.executable
        if getattr(sys, "frozen", False):
            argv = [exe] + args
        else:
            # source run: python -m vpeen.tunnel ...
            argv = [exe, "-m", "vpeen.tunnel"] + args
        cwd = (os.path.dirname(sys.executable) if getattr(sys, "frozen", False)
               else os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        if sys_platform.startswith("win"):
            import ctypes
            params = " ".join(f'"{a}"' if " " in a else a for a in argv[1:])
            rc = ctypes.windll.shell32.ShellExecuteW(
                None, "runas", argv[0], params, cwd, 0)     # 0 = SW_HIDE
            if rc <= 32:
                raise OSError(f"UAC elevation failed (code {rc})")
        elif sys_platform == "darwin":
            script = "nohup " + " ".join(
                "'" + a.replace("'", "'\\''") + "'" for a in argv) + \
                " >/dev/null 2>&1 & echo started"
            subprocess.run(["osascript", "-e",
                            f'do shell script "{script}" '
                            f"with administrator privileges"],
                           check=True, timeout=120,
                           capture_output=True)
        else:
            script = "nohup " + " ".join(
                "'" + a.replace("'", "'\\''") + "'" for a in argv) + \
                " >/dev/null 2>&1 & echo started"
            subprocess.run(["pkexec", "sh", "-c", script],
                           check=True, timeout=120, capture_output=True)

    def _connect_sync(self, socks_port, upstream_ips, upstream_hosts,
                      mtu, dns, strict_ips, kill_switch=True):
        try:
            ok, why = tunnel_available()
            if not ok:
                raise OSError(why)
            self._token = secrets.token_hex(16)
            with socket.socket() as s:
                s.bind(("127.0.0.1", 0))
                self._ctl_port = s.getsockname()[1]
            self.state = "starting"
            self.log("Requesting administrator rights for tunnel mode...")
            self._elevated_spawn(["--tunnel-worker",
                                  "--ctl-port", str(self._ctl_port),
                                  "--ctl-token", self._token])

            deadline = time.time() + CONNECT_TIMEOUT
            sock = None
            while time.time() < deadline and sock is None:
                try:
                    sock = socket.create_connection(("127.0.0.1", self._ctl_port),
                                                    timeout=3)
                except OSError:
                    time.sleep(0.4)
            if sock is None:
                raise OSError("elevated helper did not start "
                              "(cancelled or blocked)")
            sock.settimeout(10)
            self._sock = sock
            self._wfile = sock.makefile("w")
            self._wfile.write(json.dumps({"auth": self._token}) + "\n")
            self._wfile.flush()

            self._cfg = {"socks_port": socks_port, "upstream_ips": upstream_ips,
                         "upstream_hosts": upstream_hosts or {},
                         "strict_ips": bool(strict_ips),
                         "mtu": mtu, "dns": dns,
                         "kill_switch": bool(kill_switch)}
            self._send({"cmd": "up", "cfg": self._cfg})
            self._start_threads()
        except Exception as e:
            self.state = "error"
            self.log(f"Tunnel error: {e}", "err")
            self._emit(phase="error", detail=str(e))
            self._cleanup_sock()

    # ------------------------------------------------------------ transport
    def _send(self, obj):
        with self._lock:
            if self._wfile:
                try:
                    self._wfile.write(json.dumps(obj) + "\n")
                    self._wfile.flush()
                    return True
                except (OSError, ValueError):
                    self._cleanup_sock()
        return False

    def _start_threads(self):
        self._rx_thread = threading.Thread(target=self._rx_loop, daemon=True,
                                           name="vpeen-tunnelrx")
        self._rx_thread.start()
        self._hb_thread = threading.Thread(target=self._hb_loop, daemon=True,
                                           name="vpeen-tunnelhb")
        self._hb_thread.start()

    def _hb_loop(self):
        while self._sock and self.state in ("starting", "up"):
            self._send({"cmd": "ping"})
            time.sleep(2)

    def _rx_loop(self):
        sock = self._sock
        f = None
        if sock:
            f = sock.makefile("r")
        while f and self._sock:
            try:
                line = f.readline()
            except OSError:
                break
            if not line:
                break
            try:
                self._dispatch(json.loads(line))
            except ValueError:
                continue
        if self.state in ("starting", "up"):
            self.state = "down"
            self.log("Tunnel helper connection lost.", "err")
            self._emit(phase="down", detail="helper connection lost")
        self._cleanup_sock()

    def _dispatch(self, msg):
        ev = msg.get("ev")
        if ev == "log":
            self.log(msg.get("line", ""), msg.get("level", "info"))
        elif ev == "up":
            self.state = "up"
            self._emit(phase="up")
        elif ev == "down":
            self.state = "down"
            self._emit(phase="down", detail=msg.get("error", ""))
        elif ev in ("ks_engaged", "ks_healed", "ks_lockdown"):
            # v4.9.0 kill switch: forwarded verbatim so the GUI can show
            # the repair / lockdown state instead of a fake "Protected".
            self._emit(phase=ev, detail=str(msg.get("rc", "")))
        elif ev == "hello":
            pass

    def _cleanup_sock(self):
        try:
            if self._wfile:
                self._wfile.close()
        except OSError:
            pass
        try:
            if self._sock:
                self._sock.close()
        except OSError:
            pass
        self._wfile = None
        self._sock = None

    # -------------------------------------------------------------- actions
    def update_servers(self, upstream_ips: list[str],
                        upstream_hosts: "dict[str, list[str]] | None" = None) -> None:
        if self.state == "up":
            msg = {"cmd": "upd", "upstream_ips": upstream_ips}
            if upstream_hosts:
                msg["upstream_hosts"] = upstream_hosts
            self._send(msg)

    def disconnect(self) -> None:
        if self.state in ("starting", "up"):
            self._send({"cmd": "down"})

    def shutdown_worker(self) -> None:
        if self._sock:
            self._send({"cmd": "quit"})
            time.sleep(0.4)
        self._cleanup_sock()
        self.state = "down"


# ------------------------------------------------------------ worker entry
def _stale_ks_rules_only():
    """Best-effort stale-rule sweep without the tun2socks hygiene (used
    after a fresh tunnel comes up - killing tun2socks there would be
    killing OUR OWN)."""
    try:
        from . import killswitch
        killswitch.sweep_stale()
    except Exception:
        pass


def _sweep_worker_boot():
    """v4.9.0 single-instance hygiene, run elevated at worker boot:

    * stale VPeeN-KS firewall rules are removed (a hard crash inside a
      lockdown could otherwise leave the machine firewalled forever)
    * an ORPHANED tun2socks from a crashed worker is killed, but ONLY
      when our TUN adapter still exists - the orphan holds the adapter
      name, and a fresh tun2socks would fail to create it.  Killing it
      cannot leak: its routes point into a dead tunnel and the journal
      sweep has already replayed them away.
    """
    try:
        from . import killswitch
        killswitch.sweep_stale()
    except Exception:
        pass
    try:
        exe, _bindir = binary_paths()
        if not exe:
            return
        if sys_platform.startswith("win"):
            if plat._win_tun_index() is not None:
                subprocess.run(["taskkill", "/F", "/IM", "tun2socks.exe"],
                               capture_output=True, timeout=10)
        elif sys_platform == "linux":
            if os.path.exists(
                    f"/sys/class/net/{plat.TUN_NAME['linux']}"):
                subprocess.run(["pkill", "-f", exe],
                               capture_output=True, timeout=10)
        elif sys_platform == "darwin":
            rc, _out, _err = plat.run(["ifconfig", plat.TUN_NAME["darwin"]],
                                      timeout=10)
            if rc == 0:
                subprocess.run(["pkill", "-f", exe],
                               capture_output=True, timeout=10)
    except Exception:
        pass


def _arg_value(argv, flag, default=None):
    """v4.3.1: safe flag lookup - argv.index(flag) + 1 raised IndexError
    whenever a --ctl-port/--ctl-token/--session flag arrived without its
    value (e.g. a truncated command line from an elevation prompt)."""
    try:
        i = argv.index(flag)
    except ValueError:
        return default
    if i + 1 < len(argv):
        return argv[i + 1]
    return default


def _worker_main(ctl_port: int, ctl_token: str) -> int:
    # The elevated worker runs the DNS relay (UDP datagram endpoints), which
    # is fragile/unsupported on the default Windows ProactorEventLoop.
    # The small SelectorEventLoop handles everything this worker needs.
    if sys.platform == "win32":
        try:
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        except Exception:
            pass
    # tunnel_platforms logs via print(); the frozen windowed worker has no
    # console, so route platform-level logs into the worker log file too
    plat._log = lambda msg, lvl="info": _flog(msg, lvl)
    _sweep_worker_boot()
    worker = TunnelWorker(ctl_port, ctl_token)
    try:
        asyncio.run(worker.amain())
    except KeyboardInterrupt:
        pass
    return 0


def _cleanup_main(session_path: str) -> int:
    try:
        with open(session_path, "r", encoding="utf-8") as f:
            session = json.load(f)
        plat.cleanup_from_session(session)
    except Exception as e:
        _flog(f"cleanup failed: {e}", "err")
        return 1
    return 0


def main(argv=None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if "--tunnel-worker" in argv:
        try:
            port = int(_arg_value(argv, "--ctl-port", "0"))
        except ValueError:
            port = 0
        token = _arg_value(argv, "--ctl-token", "")
        return _worker_main(port, token)
    if "--tunnel-cleanup" in argv:
        path = _arg_value(argv, "--session", plat.SESSION_PATH)
        return _cleanup_main(path)
    print("usage: python -m vpeen.tunnel --tunnel-worker --ctl-port N "
          "--ctl-token T [--tunnel-cleanup --session P]")
    return 2


if __name__ == "__main__":
    sys.exit(main())
