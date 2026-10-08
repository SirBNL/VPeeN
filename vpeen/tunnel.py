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
import threading
import time

from . import tunnel_platforms as plat

ADAPTER_GUID = plat.ADAPTER_GUID

WORKER_HEARTBEAT_TIMEOUT = 12.0     # seconds without PING -> self-teardown
CONNECT_TIMEOUT = 40.0              # GUI wait for worker socket

sys_platform = platform.system().lower()


# ------------------------------------------------------------------ helpers
def _log_file():
    d = plat.CONFIG_DIR
    os.makedirs(d, exist_ok=True)
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
        self.dns_enabled = True
        self.mtu = 1500
        self._tasks: set = set()            # anchored tasks (asyncio keeps
        self._tun2socks_logf = None        # only weak refs - unanchored
                                            # tasks can be GC'd mid-flight)

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
            hello = json.loads(first)
            if hello.get("auth") != self.ctl_token:
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
            self.upstream_ips = list(cfg.get("upstream_ips", []))
            self.dns_enabled = bool(cfg.get("dns", True))
            self._spawn(self._up())
        elif cmd == "down":
            self.last_ping = time.time()
            self._spawn(self._down("requested by app"))
        elif cmd == "upd":
            self.upstream_ips = list(msg.get("upstream_ips", []))
            self._spawn(self._refresh_host_routes())
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

    async def _up(self):
        if self.state == "up":
            await self._down("re-connecting")
        try:
            exe, bindir = binary_paths()
            if not exe:
                raise OSError("tun2socks binary missing")
            if not plat.is_admin():
                raise OSError("worker is not elevated")

            # 0. discover original route BEFORE any changes
            device = self._device_arg()
            if not device:
                raise OSError("could not plan the TUN device")

            # 1. launch tun2socks (it creates the TUN adapter itself).
            #    v4.2.0: its output now goes to tun2socks.log instead of
            #    DEVNULL so failures are actually diagnosable.
            args = [exe, "--device", device,
                    "--proxy", f"socks5://127.0.0.1:{self.socks_port}",
                    "--mtu", str(self.mtu), "--loglevel", "info"]
            bind_if = self._orig_bind_interface()
            if bind_if:
                args += ["--interface", bind_if]
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

            # 2. wait for the ADAPTER to appear.  v4.2.0 fix: the old code
            #    waited for the adapter ADDRESS (198.18.0.1) here, but that
            #    address is only assigned by bring_up() BELOW - so the wait
            #    timed out every single time and tunnel mode never came up.
            await self._wait_adapter()
            journal, ok, err = plat.bring_up(self.mtu, self.upstream_ips)
            if journal is None:
                raise OSError(err or "route setup failed")
            self.journal = journal
            if not ok:
                self.blog(f"route setup incomplete: {err}", "warn")
            # sanity: the address should now be present (netsh/ip are sync)
            await self._verify_address()

            # 3. DNS relay bound to the TUN address (through local SOCKS5)
            if self.dns_enabled:
                from .dnsrelay import DNSRelay
                self.relay = DNSRelay(socks_dial=self._socks_dial,
                                      bind_ip=plat.TUN_IP, port=53,
                                      log=self.blog)
                try:
                    await self.relay.start()
                except OSError as e:
                    self.blog(f"DNS relay failed to bind: {e}", "warn")

            self.state = "up"
            self.broadcast({"ev": "up"})
            self.blog("Tunnel is UP - all traffic now flows through VPeeN.",
                      "ok")
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
        """Server list rotated in the GUI: replace anti-loop host routes."""
        if self.state != "up" or not self.journal:
            return
        try:
            keep = [r for r in self.journal.get("routes", [])
                    if r.get("type") != "route" or "/1" not in r.get("prefix", "")
                    or r.get("mask") == "128.0.0.0"]
            # remove old host routes then add new ones
            host_routes = [r for r in self.journal.get("routes", [])
                           if r.get("type") == "route" and
                           r.get("mask") == "255.255.255.255"]
            for r in host_routes:
                if sys_platform.startswith("win"):
                    plat.run(["route", "delete", r["prefix"], "mask", r["mask"],
                              r["gw"]])
                elif sys_platform == "linux":
                    plat.run(["ip", "route", "del", f"{r['prefix']}/32",
                              "via", r["via"]])
                elif sys_platform == "darwin":
                    plat.run(["route", "-n", "delete", "-host", r["prefix"],
                              r["gw"]])
            fresh = []
            orig = self._orig_route_triple()
            for ip in self.upstream_ips:
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
                    gw, _dev = orig
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
            await asyncio.sleep(2)
            if self.state == "up" and \
                    time.time() - self.last_ping > WORKER_HEARTBEAT_TIMEOUT:
                _flog("GUI heartbeat lost - emergency teardown", "err")
                await self._down("heartbeat lost")
                os._exit(0)          # never linger after the GUI is gone


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
                mtu: int = 1500, dns: bool = True) -> None:
        if self.state in ("starting", "up"):
            return
        threading.Thread(target=self._connect_sync,
                         args=(socks_port, upstream_ips, mtu, dns),
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

    def _connect_sync(self, socks_port, upstream_ips, mtu, dns):
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
                         "mtu": mtu, "dns": dns}
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
    def update_servers(self, upstream_ips: list[str]) -> None:
        if self.state == "up":
            self._send({"cmd": "upd", "upstream_ips": upstream_ips})

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
        i = argv.index("--tunnel-worker")
        port = int(argv[argv.index("--ctl-port") + 1]) if "--ctl-port" in argv else 0
        token = argv[argv.index("--ctl-token") + 1] if "--ctl-token" in argv else ""
        return _worker_main(port, token)
    if "--tunnel-cleanup" in argv:
        i = argv.index("--tunnel-cleanup")
        path = argv[argv.index("--session") + 1] if "--session" in argv else \
            plat.SESSION_PATH
        return _cleanup_main(path)
    print("usage: python -m vpeen.tunnel --tunnel-worker --ctl-port N "
          "--ctl-token T [--tunnel-cleanup --session P]")
    return 2


if __name__ == "__main__":
    sys.exit(main())
