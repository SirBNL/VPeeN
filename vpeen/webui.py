"""
VPeeN web-UI bridge + local server (standard library only).

The desktop GUI is a small local web application: this module runs the
proxy core (vpeen.core) + tunnel controller behind a loopback-only HTTP
endpoint and serves the static UI from vpeen/web/.  The window itself is
opened by vpeen.gui (pywebview when available, otherwise the default
browser).

Protocol (all JSON, same origin, 127.0.0.1 only):
    GET  /                    index.html (+ /app.js /style.css /icon.png)
    GET  /flags/<cc>.png      circular country flag from assets/flags
    GET  /api/state           full snapshot (phase, locations, pings, ...)
    GET  /api/poll?since=N    long-poll: UI events after sequence N
    POST /api/connect         {"region": "optimal" | "<region>"}
    POST /api/disconnect      {}
    POST /api/select          {"region": ...}       (no connect)
    POST /api/pings           {}                    (refresh latencies)
    POST /api/settings        {partial settings}
    POST /api/update/check   {}   check GitHub for a newer release (v4.8.0)
    POST /api/update/install {}   download+verify+install+restart (v4.8.0)
    POST /api/quit            {}                    (browser-fallback mode)

Connect semantics mirror the v4.2-4.3 Tk GUI: proxy mode always starts
first; when tunnel mode is requested the TUN layer is raised after the
core reports "connected" (pending_tunnel), with a 45 s watchdog that
falls back to proxy mode unless tun_fallback is disabled.
"""
import json
import os
import queue
import re
import socket
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import __app_name__, __version__
from . import settings as cfgmod
from .cli import DEFAULT_STATE_PATH, validate_bind_security
from .core import Core, spawn_quick_task

OPTIMAL = "optimal"

# country-code -> display name (for the free set and future additions)
COUNTRY = {
    "ae": "United Arab Emirates", "al": "Albania", "am": "Armenia",
    "ar": "Argentina", "at": "Austria", "au": "Australia", "az": "Azerbaijan",
    "ba": "Bosnia and Herzegovina", "bd": "Bangladesh", "be": "Belgium",
    "bg": "Bulgaria", "bh": "Bahrain", "br": "Brazil", "by": "Belarus",
    "ca": "Canada", "ch": "Switzerland", "cl": "Chile", "co": "Colombia",
    "cr": "Costa Rica", "cy": "Cyprus", "cz": "Czechia", "de": "Germany",
    "dk": "Denmark", "dz": "Algeria", "ec": "Ecuador", "ee": "Estonia",
    "eg": "Egypt", "es": "Spain", "fi": "Finland", "fr": "France",
    "gb": "United Kingdom", "ge": "Georgia", "gr": "Greece",
    "gt": "Guatemala", "hk": "Hong Kong", "hr": "Croatia", "hu": "Hungary",
    "id": "Indonesia", "ie": "Ireland", "il": "Israel", "in": "India",
    "iq": "Iraq", "is": "Iceland", "it": "Italy", "jp": "Japan",
    "ke": "Kenya", "kh": "Cambodia", "kr": "South Korea", "kz": "Kazakhstan",
    "la": "Laos", "lt": "Lithuania", "lu": "Luxembourg", "lv": "Latvia",
    "ma": "Morocco", "md": "Moldova", "mk": "North Macedonia",
    "mm": "Myanmar", "mx": "Mexico", "my": "Malaysia", "ng": "Nigeria",
    "nl": "Netherlands", "no": "Norway", "np": "Nepal", "nz": "New Zealand",
    "om": "Oman", "pa": "Panama", "pe": "Peru", "ph": "Philippines",
    "pk": "Pakistan", "pl": "Poland", "pt": "Portugal", "py": "Paraguay",
    "qa": "Qatar", "ro": "Romania", "rs": "Serbia", "ru": "Russia",
    "sa": "Saudi Arabia", "se": "Sweden", "sg": "Singapore", "sk": "Slovakia",
    "sv": "El Salvador", "th": "Thailand", "tn": "Tunisia", "tr": "Turkey",
    "tw": "Taiwan", "ua": "Ukraine", "us": "United States", "uy": "Uruguay",
    "uz": "Uzbekistan", "ve": "Venezuela", "vn": "Vietnam", "za": "South Africa",
}

# VPeeN_DEMO_DATA=1 -> deterministic offline feed (screenshots/preview).
# v4.4.0: entries now carry "country" too - _region_label() used to crash
# with KeyError on the demo set (demo connect returned HTTP 500 and the
# UI stayed stuck in "connecting" forever).
DEMO_LOCATIONS = [
    {"region": "fr-prs", "city": "Paris", "cc": "fr", "country": "France"},
    {"region": "nl", "city": "Amsterdam", "cc": "nl", "country": "Netherlands"},
    {"region": "gb-lnd", "city": "London", "cc": "gb", "country": "United Kingdom"},
    {"region": "ru-spb", "city": "Saint Petersburg", "cc": "ru", "country": "Russia"},
    {"region": "sg", "city": "Singapore", "cc": "sg", "country": "Singapore"},
    {"region": "us-va", "city": "Virginia", "cc": "us", "country": "United States"},
    {"region": "us-or", "city": "Oregon", "cc": "us", "country": "United States"},
]
DEMO_PINGS = {"nl": 18, "gb-lnd": 31, "fr-prs": 42, "ru-spb": 86,
              "sg": 194, "us-va": 118, "us-or": 173}
DEMO_DIRECT_IP = "78.39.106.14"
DEMO_EXIT_IP = "185.244.29.219"

_FLAG_RE = re.compile(r"^[a-z]{2}$")
_STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
    "/icon.png": (os.path.join("..", "..", "assets", "icon.png"),
                    "image/png"),
}


def _asset(*parts) -> str | None:
    """Locate a bundled asset (works from source and frozen builds)."""
    roots = [getattr(sys, "_MEIPASS", None),
             os.path.dirname(os.path.dirname(os.path.abspath(__file__)))]
    for root in roots:
        if root:
            p = os.path.join(root, "assets", *parts)
            if os.path.exists(p):
                return p
    return None


def _web_dir() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    p = os.path.join(here, "web")
    if os.path.isdir(p):                      # source tree / datas bundle
        return p
    frozen = getattr(sys, "_MEIPASS", None)   # onefile fallback
    if frozen and os.path.isdir(os.path.join(frozen, "vpeen", "web")):
        return os.path.join(frozen, "vpeen", "web")
    return here


# =====================================================================
# v4.8.0 "Sentinel": self-update flow (web UI side)
# =====================================================================
class UpdateFlow:
    """Drives check -> download -> stage -> install -> restart.

    Everything runs on background threads; every state change is emitted
    as {"t": "update", ...} so the sheet, the footer chip and the toast
    stay live.  The DISK swap only happens after the archive passed its
    SHA256 check, and the restart only after a clean disconnect (tunnel
    down, system proxy restored, core stopped)."""

    RECHECK_INTERVAL = 6 * 3600      # auto re-check while the app runs

    def __init__(self, log, emit, socks_port_fn, restart_fn, auto=True):
        self.log = log
        self.emit = emit
        self.socks_port_fn = socks_port_fn     # -> int | None (live SOCKS)
        self.restart_fn = restart_fn           # clean shutdown + execv
        self.auto = bool(auto)
        self._lock = threading.Lock()
        self._st = {"status": "idle", "current": __version__, "latest": "",
                    "tag": "", "notes": "", "size": 0, "progress": 0,
                    "error": "", "checksum_error": "",
                    "source_install": not getattr(sys, "frozen", False)}
        self._timer = None
        self._busy = False

    # -------------------------------------------------------------- state
    def state(self) -> dict:
        with self._lock:
            return dict(self._st)

    def _set(self, **patch):
        with self._lock:
            self._st.update(patch)
            st = dict(self._st)
        self.emit({"t": "update", "update": st})

    # ---------------------------------------------------------------- api
    def start_check(self, manual: bool = False, delay: float = 0.0):
        if self._busy:
            return
        threading.Thread(target=self._check_worker,
                         args=(manual, delay), daemon=True,
                         name="vpeen-update-check").start()

    def start_install(self):
        if self._busy:
            return
        threading.Thread(target=self._install_worker, daemon=True,
                         name="vpeen-update-install").start()

    def stop(self):
        if self._timer is not None:
            self._timer.cancel()

    # ---------------------------------------------------------- internals
    def _check_worker(self, manual, delay):
        from . import selfupdate
        if delay:
            time.sleep(delay)
        self._busy = True
        try:
            self._set(status="checking", error="")
            if manual:
                self.log("Checking GitHub for a newer release...", "info")
            try:
                info = selfupdate.check_for_update(
                    __version__, socks_port=self.socks_port_fn())
            except selfupdate.UpdateError as e:
                self._set(status="error", error=str(e))
                if manual:
                    self.log(f"Update check failed: {e}", "warn")
                return
            if info.get("status") == "latest":
                self._set(status="latest", latest=info.get("latest", ""))
                if manual:
                    self.log(f"You are on the newest release "
                             f"(v{info.get('latest', '?')}).", "ok")
                return
            # available
            self._set(status="available", latest=info.get("version", ""),
                      tag=info.get("tag", ""), notes=info.get("notes", ""),
                      size=info.get("size", 0), error="",
                      checksum_error=info.get("checksum_error", ""))
            self.log(f"Update available: v{info.get('version', '?')} "
                     f"({info.get('size', 0) // (1024 * 1024)} MB).", "ok")
            if self.auto:
                self._auto_download(info)
        finally:
            self._busy = False
            self._rearm()

    def _auto_download(self, info):
        """Pre-fetch the update in the background so the one click is
        INSTANT (v4.8.0: automatically detect + install; ONE button moves
        the app to the new version).  Never auto-restarts - the restart is
        the user's single button."""
        from . import selfupdate
        if not info.get("sha256"):
            # unverifiable release (missing/partial SHA256SUMS.txt): report
            # it, but never silently download an unverified archive.
            self._set(status="available", error=info.get("checksum_error",
                                                          ""))
            return
        try:
            self._fetch_and_stage(info)
            self._set(status="ready", progress=100)
            self.log(f"v{info['version']} is downloaded and verified - "
                     "one click to switch.", "ok")
        except selfupdate.UpdateError as e:
            self._set(status="available", progress=0, error=str(e))
            self.log(f"Background download failed: {e}", "warn")
        except Exception as e:
            self._set(status="available", progress=0, error=str(e))
            self.log(f"Background download failed: {e}", "warn")

    def _fetch_and_stage(self, info) -> dict:
        from . import selfupdate
        # SAFETY (v4.8.0 "Sentinel" core promise): a build without a
        # SHA256 pin from the release's SHA256SUMS.txt is NEVER fetched,
        # never staged, never installed - no exceptions, no fallback to
        # "trust the CDN".
        if not info.get("sha256"):
            raise selfupdate.UpdateError(
                info.get("checksum_error") or
                "no verified checksum for this asset - refusing to install")
        dest = os.path.join(selfupdate.UPDATE_DIR, info["asset"])
        total = info.get("size", 0)

        def _prog(done, tot):
            pct = (done * 100 // tot) if tot else \
                (done * 100 // max(total, 1))
            if pct % 5 == 0:                      # throttle UI events
                self._set(progress=min(pct, 99), status="downloading")

        self._set(status="downloading", progress=0, error="")
        selfupdate.download_to(info["url"], dest, info.get("sha256", ""),
                               socks_port=self.socks_port_fn(),
                               progress=_prog, expected_size=total)
        self._set(status="staging", progress=100)
        return selfupdate.stage_archive(dest, info["version"])

    def _install_worker(self):
        from . import selfupdate
        if self._busy:
            return
        self._busy = True
        try:
            if self.state().get("source_install"):
                raise selfupdate.UpdateError(
                    "this is a source installation - update with "
                    "'git pull' (or download a release)")
            try:
                info = selfupdate.check_for_update(
                    __version__, socks_port=self.socks_port_fn())
            except selfupdate.UpdateError as e:
                raise selfupdate.UpdateError(f"re-check failed: {e}")
            if info.get("status") != "available":
                self._set(status="latest" if info.get("status") == "latest"
                          else "idle")
                return
            version = info["version"]
            staged = self._fetch_and_stage(info)
            self._set(status="installing", latest=version)
            self.log("Installing the verified build...", "info")
            selfupdate.install_staged(staged, version)
            self._set(status="restarting", latest=version)
            self.log(f"Installed v{version} - restarting VPeeN now.", "ok")
            time.sleep(1.0)                 # let the UI render the message
            self.restart_fn()
        except selfupdate.UpdateError as e:
            self._set(status="error", error=str(e))
            self.log(f"Update failed: {e}", "err")
        except Exception as e:               # absolute guard
            self._set(status="error", error=str(e))
            self.log(f"Update failed unexpectedly: {e}", "err")
        finally:
            self._busy = False

    def _rearm(self):
        if not self.auto:
            return
        if self._timer is not None:
            self._timer.cancel()
        self._timer = threading.Timer(self.RECHECK_INTERVAL,
                                      lambda: self.start_check(False))
        self._timer.daemon = True
        self._timer.start()


class Bridge:
    """Owns Core + TunnelController and feeds the web UI.

    All cross-thread state lives behind self.lock; the UI polls
    /api/state (initial) and /api/poll (live events)."""

    def __init__(self, state_path: str = DEFAULT_STATE_PATH,
                 insecure: bool = False, demo: bool = False):
        self.demo = demo
        self.state_path = state_path
        self.cfg = cfgmod.load()
        self.core = Core(state_path, insecure)
        self.tunnel = None if demo else self._make_tunnel()
        self.update = None if demo else self._make_update()
        self.lock = threading.RLock()
        self.seq = 0
        self.ui_events = deque(maxlen=400)     # [(seq, ev), ...]
        self.wakeup = threading.Event()
        self.logs = deque(maxlen=500)
        self.phase = "disconnected"
        self.conn_mode = "proxy"               # proxy | tunnel
        self.region = None                     # actually connected region
        self.selected = self.cfg.get("last_region") or OPTIMAL
        self.exit_ip = None
        self.direct_ip = None
        self.stats = {"up": 0, "down": 0, "conns": 0, "active": 0, "failed": 0}
        self.connected_since = None
        self.listeners = {}
        self.system_proxy = False
        self.locations = []                    # free locations, normalized
        self.locations_error = None
        self.pings = {}
        self._pending_reconnect = False
        self._pending_tunnel = False
        self._stop_after_tunnel = False
        self._tunnel_watchdog = None
        self._ping_task = None
        self._stopping = False
        self._demo_gen = 0                   # v4.4.0: demo timeline guard
        self.done = threading.Event()          # set => whole app should exit
        self._pump = threading.Thread(target=self._pump_loop, daemon=True,
                                      name="vpeen-bridge")
        self._pump.start()
        self.log(f"{__app_name__} v{__version__} web UI ready.", "ok")

    # ------------------------------------------------------------- plumbing
    def _make_tunnel(self):
        try:
            from .tunnel import TunnelController
            return TunnelController(self.core.events)
        except Exception:
            return None

    def sweep_tunnel_orphans(self):
        """v4.8.0 "Sentinel": the crash-recovery sweep that NEVER existed.

        tunnel_sweep was a settings toggle and TunnelController.cleanup_orphans
        existed - but nothing ever CALLED it, so a crash (or force-quit) while
        the TUN layer was up left the def1 hijack routes behind: the user's
        whole network stayed bricked until a manual route cleanup or reboot.
        Runs in a background thread; only prompts for elevation when a
        session journal is actually present (a crashed session)."""
        if self.demo or self.tunnel is None:
            return
        if not bool(self.cfg.get("tunnel_sweep", True)):
            return

        def _run():
            try:
                from . import tunnel_platforms as plat
                if not plat.load_session():
                    return          # nothing to clean - stay silent
                self.log("Leftover tunnel state from a crashed session "
                         "detected - cleaning it up (admin rights may be "
                         "requested)...", "warn")
                self.tunnel.cleanup_orphans(log=self.log)
            except Exception as e:
                self.log(f"tunnel sweep failed: {e}", "warn")

        threading.Thread(target=_run, daemon=True,
                         name="vpeen-tunnelsweep").start()

    # ---------------------------------------------------- v4.8.0 self-update
    def _make_update(self):
        return UpdateFlow(
            log=self.log,
            emit=self.emit,
            socks_port_fn=self._live_socks_port,
            restart_fn=self.restart_for_update,
            auto=bool(self.cfg.get("auto_update", True)))

    def _live_socks_port(self):
        """The live SOCKS port, but only while the core is actually up.

        The update check falls back to the local SOCKS5 when GitHub is
        unreachable directly (it often is, on exactly the networks that
        need a VPN) - a stale port would just add a doomed attempt."""
        if not self.core.is_busy():
            return None
        try:
            port = (self.listeners.get("socks_port")
                    or int(self.cfg.get("socks_port", 1080)))
            return int(port)
        except (TypeError, ValueError):
            return None

    def boot_maintenance(self):
        """v4.8.0 boot sequence (background threads, never blocks the
        server startup): sweep self-update leftovers, verify a pending
        swap marker, sweep orphaned tunnel state, then auto-check."""
        try:
            from . import selfupdate
            selfupdate.sweep_leftovers()
            expected = selfupdate.check_pending(__version__)
            if expected is not None:
                if expected == __version__:
                    self.log(f"Self-update to v{expected} verified - you are "
                             "on the new build.", "ok")
                else:
                    self.log(f"Update expected v{expected} but running "
                             f"v{__version__} - the swap may have rolled "
                             "back; check the logs.", "warn")
        except Exception as e:
            self.log(f"update sweep failed: {e}", "warn")
        self.sweep_tunnel_orphans()
        if self.update is not None and \
                bool(self.cfg.get("auto_update", True)):
            self.update.start_check(manual=False, delay=3.0)

    def restart_for_update(self):
        """Clean restart after a staged swap (called by UpdateFlow).

        Disconnect first so the TUN layer, its routes and any system-proxy
        setting are restored BEFORE the process image is replaced - a
        restart that leaks routes is exactly the crash this version
        fixed."""
        self.log("Restarting VPeeN to activate the new version...", "ok")
        try:
            self.request_disconnect()
        except Exception:
            pass
        if self._tunnel_watchdog is not None:
            self._tunnel_watchdog.cancel()
        deadline = time.time() + 6
        while self.core.is_busy() and time.time() < deadline:
            time.sleep(0.1)
        self._stopping = True
        self.done.set()
        self.wakeup.set()
        time.sleep(0.6)          # let the last UI poll render the state
        from . import selfupdate
        selfupdate.restart()    # never returns on success

    def log(self, line, level="info"):
        ts = time.strftime("%H:%M:%S")
        with self.lock:
            self.logs.append({"ts": ts, "level": level, "line": str(line)})
        self.emit({"t": "log", "line": str(line), "level": level, "ts": ts})

    def emit(self, ev: dict):
        with self.lock:
            self.seq += 1
            self.ui_events.append((self.seq, ev))
        self.wakeup.set()

    def _pump_loop(self):
        """Drain the core event queue forever (single consumer)."""
        while not self._stopping:
            try:
                ev = self.core.events.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self.handle_event(ev)
            except Exception as e:               # never let the pump die
                self.log(f"bridge: error handling event: {e}", "warn")

    # ---------------------------------------------------------- event logic
    def handle_event(self, ev: dict):
        kind = ev.get("type")
        if kind == "log":
            ts = time.strftime("%H:%M:%S")
            with self.lock:
                self.logs.append({"ts": ts,
                                  "level": ev.get("level", "info"),
                                  "line": ev["line"]})
            self.emit({"t": "log", "line": ev["line"],
                       "level": ev.get("level", "info"), "ts": ts})
        elif kind == "tunnel":
            self._tunnel_event(ev)
        elif kind == "phase":
            self._on_phase(ev)
        elif kind == "listeners":
            self.listeners = ev
        elif kind == "exit_ip":
            self.exit_ip = ev.get("ip")
            self.emit({"t": "exit_ip", "ip": self.exit_ip})
        elif kind == "stats":
            self.stats = {"up": ev.get("up", 0), "down": ev.get("down", 0),
                          "conns": ev.get("conns", 0),
                          "active": ev.get("active", 0),
                          "failed": ev.get("failed", 0),
                          "warm": ev.get("warm", 0),
                          "hedges": ev.get("hedges", 0)}
            self.emit({"t": "stats", **self.stats})
        elif kind == "system_proxy":
            self.system_proxy = bool(ev.get("on"))
            self.emit({"t": "system_proxy", "on": self.system_proxy})
        elif kind == "servers_rotated":
            if self.tunnel is not None:
                try:
                    self.tunnel.update_servers(
                        ev.get("upstream_ips", []),
                        ev.get("upstream_hosts") or {})
                except Exception:
                    pass
        elif kind == "direct_ip":
            self.direct_ip = ev.get("data")
            if self.direct_ip:
                self.log(f"Your real IP: {self.direct_ip}", "ok")
            self.emit({"t": "direct_ip", "ip": self.direct_ip})
        elif kind == "locations":
            self._fill_locations(ev)
        elif kind == "pings":
            data = ev.get("data")
            if isinstance(data, dict) and data:
                with self.lock:
                    self.pings.update(data)
                self.emit({"t": "pings", "pings": dict(self.pings)})

    def _on_phase(self, ev: dict):
        ph = ev.get("phase")
        if ph == "connected":
            # capture the region BEFORE raising the TUN layer.  The first
            # Aurora draft returned early here, so in tunnel mode self.region
            # stayed None and the connected UI / fallback messages showed
            # "Optimal Location" instead of the real exit country.
            with self.lock:
                self.region = ev.get("region")
            if self._pending_tunnel and self.tunnel is not None:
                # stay "connecting" until the TUN layer is up (see
                # _connect_tunnel); the UI is finalized by tunnel events
                self._pending_tunnel = False
                self._connect_tunnel(ev)
                return
            if self.conn_mode == "tunnel":
                return          # tunnel event drives the UI
            with self.lock:
                self.phase = "connected"
                self.connected_since = time.time()
            self.emit({"t": "phase", "phase": "connected",
                       "region": self.region, "mode": "proxy"})
        elif ph == "disconnected":
            with self.lock:
                self.phase = "disconnected"
                self.connected_since = None
                self.conn_mode = "proxy"
                self.stats = {"up": 0, "down": 0, "conns": 0, "active": 0,
                              "failed": 0}
            self.emit({"t": "phase", "phase": "disconnected"})
            if self._pending_reconnect:
                self._pending_reconnect = False
                threading.Thread(target=self._reconnect_kick, daemon=True,
                                 name="vpeen-reconnect").start()
        else:   # connecting / error
            detail = ev.get("detail")
            with self.lock:
                self.phase = ph
            self.emit({"t": "phase", "phase": ph, "detail": detail})

    # ----------------------------------------------------------- tunnel glue
    def _connect_tunnel(self, ev: dict | None = None):
        if self.tunnel is None:
            return
        ev = ev or {}
        # v4.8.0 "Sentinel": read from the PHASE EVENT first, then the
        # listeners event, and only then the config.  The old code read
        # self.listeners - which on the FIRST connect was still {} because
        # "phase=connected" used to arrive BEFORE "listeners" (fixed in
        # core.py) - so the tunnel worker got an EMPTY upstream_ips list,
        # installed no anti-loop host routes, and hijacked the default
        # route anyway: "connected" with a dead network.
        socks_port = (ev.get("socks_port")
                      or self.listeners.get("socks_port")
                      or int(self.cfg.get("socks_port", 1080)))
        upstream_ips = (ev.get("upstream_ips")
                        or self.listeners.get("upstream_ips") or [])
        upstream_hosts = (ev.get("upstream_hosts")
                          or self.listeners.get("upstream_hosts") or {}) or {}
        strict_ips = bool(ev.get("strict_ips",
                                 self.listeners.get("strict_ips", False)))
        if not upstream_ips:
            # belt-and-suspenders: never raise the TUN layer without the
            # anti-loop IP set (the worker double-checks, but the GUI can
            # stop it earlier with a clearer message).
            self._tun_failed_fallback("no upstream IPs resolved - "
                                      "not raising the tunnel (blackhole "
                                      "guard)")
            return
        self.log("Requesting administrator rights to enable the VPN tunnel...")
        try:
            self.tunnel.connect(
                socks_port=socks_port,
                upstream_ips=upstream_ips,
                upstream_hosts=upstream_hosts,
                mtu=int(self.cfg.get("tunnel_mtu", 1500)),
                dns=bool(self.cfg.get("tunnel_dns", True)),
                strict_ips=strict_ips)
        except Exception as e:
            self._tun_failed_fallback(f"tunnel start failed: {e}")
            return
        if self._tunnel_watchdog is not None:
            self._tunnel_watchdog.cancel()
        # if the tunnel never comes up, fall back to proxy mode (45 s)
        self._tunnel_watchdog = threading.Timer(
            45.0, lambda: self._tun_state_not("up",
                                              "tunnel not confirmed within 45s"))
        self._tunnel_watchdog.daemon = True
        self._tunnel_watchdog.start()

    def _tun_state_not(self, state, reason):
        if self.phase == "connecting" and \
                (self.tunnel is None or self.tunnel.state != state):
            self._tun_failed_fallback(reason)

    def _tun_failed_fallback(self, reason: str):
        self.log(f"Tunnel mode unavailable: {reason}", "warn")
        if self.cfg.get("tun_fallback", True):
            self.log("Falling back to proxy mode (browsers only).", "warn")
            with self.lock:
                self.conn_mode = "proxy"
                self.phase = "connected"
                self.connected_since = time.time()
            self.emit({"t": "phase", "phase": "connected",
                       "region": self.region, "mode": "proxy",
                       "fallback": True})
        else:
            self.log("tun_fallback is disabled - disconnecting "
                     "(kill-switch behaviour).", "err")
            self.request_disconnect()

    def _tunnel_event(self, ev: dict):
        sub = ev.get("ev")
        if sub == "log":
            self.log(ev.get("line", ""), ev.get("level", "info"))
            return
        ph = ev.get("phase")
        if ph == "up":
            if self._tunnel_watchdog is not None:
                self._tunnel_watchdog.cancel()
                self._tunnel_watchdog = None
            with self.lock:
                self.conn_mode = "tunnel"
                self.phase = "connected"
                self.connected_since = time.time()
            self.log("VPN tunnel is up - all traffic is protected.", "ok")
            self.emit({"t": "phase", "phase": "connected",
                       "region": self.region, "mode": "tunnel"})
        elif ph in ("down", "error"):
            detail = ev.get("detail") or ""
            was_mode = self.conn_mode
            with self.lock:
                self.conn_mode = "proxy"
            if self._stop_after_tunnel:
                # a disconnect is in flight: stop the proxy core NOW.
                # v4.4.0: this used to just `return` and wait for the 8 s
                # watchdog, so a tunnel-mode disconnect hung the UI on
                # "Disconnecting..." for eight full seconds.
                self._stop_after_tunnel = False
                if self.core.is_busy():
                    self.core.stop()
                return
            if was_mode == "tunnel":
                self.log(f"VPN tunnel went down ({detail}).", "warn")
                if self.cfg.get("tun_fallback", True):
                    self.log("Traffic continues through proxy mode "
                             "(browsers only).", "warn")
                    self.emit({"t": "phase", "phase": "connected",
                               "region": self.region, "mode": "proxy",
                               "fallback": True})
                else:
                    self.request_disconnect()
            else:
                # failed during "starting" - proxy fallback / kill-switch
                self._tun_failed_fallback(detail or "tunnel failed")

    # ------------------------------------------------------------ locations
    def _fill_locations(self, ev: dict):
        """v4.3.2: FREE locations only - the 178 premium locations were
        removed (the free/anonymous API cannot serve them; selecting one
        just produced HTTP 422)."""
        data = ev.get("data")
        if not data:
            self.locations_error = ev.get("error") or "no data"
            self.log(f"Could not fetch locations: {self.locations_error}",
                     "warn")
            self.emit({"t": "locations", "locations": [],
                       "error": self.locations_error})
            return
        rows = []
        for l in data:
            if l.get("proxyType") != 0:
                continue
            cc = (l.get("countryCode") or "").lower()
            phrases = l.get("phrases") or []
            rows.append({
                "region": l.get("region", ""),
                "city": l.get("name", ""),
                "cc": cc,
                "country": COUNTRY.get(cc) or (phrases[1] if len(phrases) > 1
                                               else cc.upper()),
            })
        try:
            rows.sort(key=lambda r: r["region"])
        except Exception:
            pass
        hidden = len(data) - len(rows)
        with self.lock:
            self.locations = rows
            self.locations_error = None
        self.log(f"{len(rows)} free locations available"
                 + (f" ({hidden} premium hidden)" if hidden else ""), "ok")
        self.emit({"t": "locations", "locations": rows})
        self.refresh_pings()

    def refresh_pings(self):
        regions = [r["region"] for r in self.locations]
        if not regions:
            return
        if self._ping_task and self._ping_task.is_alive():
            return
        self._ping_task = spawn_quick_task(self.core.events, "pings",
                                           self.state_path, False,
                                           {"regions": regions})

    def refresh_locations(self):
        spawn_quick_task(self.core.events, "locations", self.state_path, False)

    def refresh_direct_ip(self):
        spawn_quick_task(self.core.events, "direct_ip", self.state_path, False)

    # ------------------------------------------------------------ connect
    def request_connect(self, region: str | None = None):
        if region:
            self.select_region(region)
        if self.phase in ("connected", "connecting") or self.core.is_busy():
            # treat as a switch: disconnect now, reconnect when down.
            # v4.4.0: this check must run BEFORE the demo branch - demo
            # mode used to fall straight into _demo_connect() which
            # no-ops when connected, so switching regions while
            # connected silently did NOTHING in demo mode.
            self._pending_reconnect = True
            self.request_disconnect()
            return
        if self.demo:
            self._demo_connect()
            return
        cfg = self.cfg
        try:
            socks_port = int(cfg.get("socks_port", 1080))
            http_port = int(cfg.get("http_port", 8080))
            if not (0 < socks_port < 65536 and 0 < http_port < 65536):
                raise ValueError
            if socks_port == http_port:
                raise ValueError
        except ValueError:
            self.log("Invalid ports (must differ and be 1-65535) - "
                     "check Settings.", "err")
            return
        want_tunnel = bool(cfg.get("tunnel_enabled")) and self.tunnel is not None
        target = None if self.selected == OPTIMAL else self.selected
        label = self._region_label(self.selected)
        mode = "tunnel" if want_tunnel else "proxy"
        self.log(f"Connecting to '{label}' ({mode} mode)...")
        self._stop_after_tunnel = False
        self._pending_tunnel = want_tunnel
        set_system = bool(cfg.get("auto_system_proxy", False)) and not want_tunnel
        bind = (cfg.get("bind") or "127.0.0.1").strip() or "127.0.0.1"
        problem = validate_bind_security(bind, None, None)
        if problem:
            self.log(problem, "err")
            return
        with self.lock:
            self.phase = "connecting"
        self.emit({"t": "phase", "phase": "connecting",
                   "region": self.selected})
        self.core.start(target, bind, socks_port, http_port, set_system)

    def request_disconnect(self):
        if self.demo:
            self._demo_disconnect()
            return
        self._stop_after_tunnel = True
        tun = self.tunnel
        if tun is not None and tun.state in ("starting", "up"):
            self.log("Disconnecting tunnel...")
            try:
                tun.disconnect()
            except Exception as e:
                self.log(f"tunnel disconnect error: {e}", "warn")
            t = threading.Timer(8.0, self._tunnel_down_watchdog)
            t.daemon = True
            t.start()
        elif self.core.is_busy():
            self.log("Disconnecting...")
            self.core.stop()
        else:
            # nothing left to stop - v4.4.0: if a reconnect was requested
            # here (switch while the core had already exited), fire the
            # kick immediately instead of leaving _pending_reconnect set
            # forever (the connect would silently never happen).
            self._stop_after_tunnel = False
            if self._pending_reconnect:
                self._pending_reconnect = False
                threading.Thread(target=self._reconnect_kick, daemon=True,
                                 name="vpeen-reconnect").start()

    def _tunnel_down_watchdog(self):
        # tunnel never reported back - force the core stop
        if self._stop_after_tunnel and self.phase in ("connected", "connecting"):
            self.core.stop()

    def _reconnect_kick(self, tries: int = 0):
        try:
            busy = self.core.is_busy()
        except Exception:
            busy = False
        if busy and tries < 25:
            time.sleep(0.25)
            self._reconnect_kick(tries + 1)
            return
        if self.phase == "disconnected":
            self.log("Reconnecting with the new settings...")
            self.request_connect()

    def select_region(self, region: str):
        with self.lock:
            self.selected = region or OPTIMAL
            self.cfg["last_region"] = "" if region == OPTIMAL else region
            cfgmod.save(self.cfg)
        self.emit({"t": "selected", "region": self.selected})

    # -------------------------------------------------------------- settings
    def update_settings(self, patch: dict) -> list[str]:
        """Merge + persist; returns a list of validation errors."""
        errors = []
        cfg = self.cfg
        with self.lock:
            for key in ("bind",):
                if key in patch:
                    v = str(patch[key]).strip() or "127.0.0.1"
                    problem = validate_bind_security(v, None, None)
                    if problem:
                        # the GUI offers no proxy-auth fields (unlike the
                        # CLI), so steer the user to loopback explicitly
                        errors.append(problem + " (the GUI cannot set proxy "
                                      "auth - use 127.0.0.1, or share via "
                                      "the CLI)")
                    else:
                        cfg["bind"] = v
            for key in ("socks_port", "http_port"):
                if key in patch:
                    # v4.4.0: range check. Out-of-range ports used to be
                    # saved silently and only explode later at connect
                    # time ("check Settings" with no clue what is wrong).
                    try:
                        v = int(patch[key])
                        if not (0 < v < 65536):
                            raise ValueError
                        cfg[key] = v
                    except (TypeError, ValueError):
                        errors.append(f"{key}: must be a number 1-65535")
            if "tunnel_mtu" in patch:
                try:
                    v = int(patch["tunnel_mtu"])
                    if not (576 <= v <= 65535):
                        raise ValueError
                    cfg["tunnel_mtu"] = v
                except (TypeError, ValueError):
                    errors.append("tunnel_mtu: must be a number 576-65535")
            for key in ("auto_system_proxy", "tunnel_enabled", "tunnel_dns",
                        "tunnel_sweep", "tun_fallback", "save_logs",
                        "auto_connect", "auto_update"):
                if key in patch:
                    cfg[key] = bool(patch[key])
                if key == "auto_update" and self.update is not None:
                    # apply immediately so the periodic check honours it
                    self.update.auto = bool(cfg[key])
            if errors:
                return errors
            if cfg.get("socks_port") == cfg.get("http_port"):
                errors.append("SOCKS and HTTP ports must differ")
                return errors
            cfgmod.save(cfg)
        self.log("Settings saved.", "ok")
        self.emit({"t": "settings", "settings": self.public_settings()})
        return errors

    def public_settings(self) -> dict:
        return {k: self.cfg.get(k) for k in cfgmod.DEFAULTS}

    # ------------------------------------------------------------- snapshot
    def snapshot(self, native_window: bool) -> dict:
        with self.lock:
            return {
                "version": __version__,
                "app": __app_name__,
                "native_window": native_window,
                "phase": self.phase,
                "mode": self.conn_mode,
                "region": self.region,
                "selected": self.selected,
                "locations": list(self.locations),
                "locations_error": self.locations_error,
                "pings": dict(self.pings),
                "settings": self.public_settings(),
                "direct_ip": self.direct_ip,
                "exit_ip": self.exit_ip,
                "stats": dict(self.stats),
                "connected_since": self.connected_since,
                "listeners": dict(self.listeners),
                "system_proxy": self.system_proxy,
                "tunnel_available": self._tunnel_available(),
                "update": (self.update.state() if self.update is not None
                           else {"status": "disabled"}),
                "seq": self.seq,
                "logs": list(self.logs)[-60:],
            }

    def _tunnel_available(self) -> bool:
        if self.demo or self.tunnel is None:
            return False
        try:
            ok, _why = self.tunnel.is_available()
            return bool(ok)
        except Exception:
            return False

    def events_since(self, since: int) -> list[dict]:
        with self.lock:
            return [ev for seq, ev in self.ui_events if seq > since]

    def stop(self):
        self._stopping = True
        self.done.set()
        self.wakeup.set()
        if self.update is not None:
            try:
                self.update.stop()
            except Exception:
                pass
        try:
            self.request_disconnect()
        except Exception:
            pass
        if self._tunnel_watchdog is not None:
            self._tunnel_watchdog.cancel()
        # give the core a moment to tear down cleanly
        deadline = time.time() + 5
        while self.core.is_busy() and time.time() < deadline:
            time.sleep(0.1)

    # ------------------------------------------------------------ demo mode
    def _demo_connect(self):
        with self.lock:
            if self.phase in ("connected", "connecting"):
                return
            self.phase = "connecting"
            self._demo_gen += 1        # v4.4.0: cancels any older timeline
            gen = self._demo_gen
        self.emit({"t": "phase", "phase": "connecting",
                   "region": self.selected})
        self.log(f"Connecting to '{self._region_label(self.selected)}' "
                 "(demo mode)...")

        def timeline():
            # v4.4.0: every stage checks the generation counter.  Disconnecting
            # during the fake handshake used to leave the timeline running:
            # ~4 s later it flipped the UI back to "connected" all by itself.
            def alive():
                return gen == self._demo_gen and self.phase == "connecting"
            time.sleep(1.2)
            if not alive():
                return
            self.log("Requesting anonymous access token (POST /v3/launch/) ...")
            time.sleep(1.0)
            if not alive():
                return
            self.log("Acquired 3 upstream server(s).", "ok")
            time.sleep(0.9)
            if not alive():
                return
            self.log("Local listeners up | SOCKS5 127.0.0.1:1080 | "
                     "HTTP 127.0.0.1:8080", "ok")
            time.sleep(0.8)
            if not alive():
                return
            with self.lock:
                if gen != self._demo_gen:
                    return
                self.phase = "connected"
                self.region = (None if self.selected == OPTIMAL
                               else self.selected)
                self.connected_since = time.time()
                self.exit_ip = DEMO_EXIT_IP
                self.stats = {"up": 0, "down": 0, "conns": 0, "active": 0,
                              "failed": 0}
                self.listeners = {"socks_port": 1080, "http_port": 8080}
            self.log(f"Connected - exit IP: {DEMO_EXIT_IP}", "ok")
            self.emit({"t": "exit_ip", "ip": DEMO_EXIT_IP})
            self.emit({"t": "phase", "phase": "connected",
                       "region": self.region, "mode": "proxy"})
            # simulated traffic
            down, up = 0, 0
            while self.phase == "connected" and gen == self._demo_gen:
                time.sleep(1.0)
                down += 180000 + int(90000 * (0.5 + abs(hash(time.time())) % 97) / 97)
                up += 42000 + int(18000 * (0.5 + abs(hash(down)) % 89) / 89)
                with self.lock:
                    self.stats = {"up": up, "down": down, "conns": 6,
                                  "active": 2, "failed": 0, "warm": 9,
                                  "hedges": 2}
                self.emit({"t": "stats", **self.stats})

        threading.Thread(target=timeline, daemon=True,
                         name="vpeen-demo").start()

    def _demo_disconnect(self):
        with self.lock:
            self._demo_gen += 1        # cancel any in-flight fake handshake
            self.phase = "disconnected"
            self.connected_since = None
            self.exit_ip = None
            self.stats = {"up": 0, "down": 0, "conns": 0, "active": 0,
                          "failed": 0}
        self.log("Disconnected.", "info")
        self.emit({"t": "phase", "phase": "disconnected"})
        # v4.4.0: demo mode has no core, so _on_phase never fires - a region
        # switch while connected would just disconnect and never reconnect.
        if self._pending_reconnect:
            self._pending_reconnect = False
            threading.Thread(target=self._reconnect_kick, daemon=True,
                             name="vpeen-reconnect").start()

    def _region_label(self, region: str) -> str:
        if region == OPTIMAL or not region:
            return "Optimal Location"
        for l in self.locations:
            if l["region"] == region:
                # .get() everywhere: a location row without "country"
                # must never be able to crash a connect request again
                city = l.get("city") or region
                country = l.get("country")
                return f"{city}, {country}" if country else city
        return region


class WebUI:
    """Loopback HTTP server hosting the bridge."""

    def __init__(self, state_path: str = DEFAULT_STATE_PATH,
                 insecure: bool = False, demo: bool | None = None,
                 native_window: bool = False):
        if demo is None:
            demo = os.environ.get("VPeeN_DEMO_DATA") == "1"
        self.bridge = Bridge(state_path, insecure, demo=demo)
        self.native_window = native_window
        self._httpd = None
        self._thread = None
        self.port = 0

    def start(self, port: int = 0):
        bridge = self.bridge
        native_window = self.native_window
        web_dir = _web_dir()

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            server_version = "VPeeN/" + __version__

            def _send(self, code, body: bytes, ctype: str,
                      cache: str = "no-store"):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", cache)
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def _json(self, obj, code=200):
                self._send(code, json.dumps(obj, ensure_ascii=False)
                           .encode("utf-8"),
                           "application/json; charset=utf-8")

            def _body(self) -> dict:
                n = int(self.headers.get("Content-Length") or 0)
                if n <= 0 or n > 65536:
                    return {}
                try:
                    return json.loads(self.rfile.read(n).decode("utf-8"))
                except Exception:
                    return {}

            def log_message(self, fmt, *args):      # quiet server
                pass

            # -------------------------------------------------------- static
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                if path.startswith("/api/"):
                    return self._api_get(path)
                if path.startswith("/flags/"):
                    cc = path[len("/flags/"):].split(".")[0].lower()
                    if not _FLAG_RE.match(cc):
                        return self._send(404, b"no", "text/plain")
                    fp = _asset("flags", f"{cc}.png")
                    if not fp:
                        return self._send(404, b"no flag", "text/plain")
                    with open(fp, "rb") as f:
                        return self._send(200, f.read(), "image/png",
                                          cache="public, max-age=86400")
                entry = _STATIC.get(path)
                if entry is None:
                    return self._send(404, b"not found", "text/plain")
                rel, ctype = entry
                fp = os.path.normpath(os.path.join(web_dir, rel))
                if not os.path.isfile(fp):
                    return self._send(404, b"missing asset", "text/plain")
                with open(fp, "rb") as f:
                    return self._send(200, f.read(), ctype,
                                      cache="no-cache")

            # ----------------------------------------------------------- api
            def _api_get(self, path):
                if path == "/api/state":
                    return self._json(bridge.snapshot(native_window))
                if path == "/api/poll":
                    q = self.path.split("?", 1)
                    since = 0
                    if len(q) == 2:
                        m = re.search(r"(?:^|&)since=(\d+)", q[1])
                        if m:
                            since = int(m.group(1))
                    events = bridge.events_since(since)
                    if not events:                      # long-poll up to 6 s
                        bridge.wakeup.clear()
                        bridge.wakeup.wait(6.0)
                        events = bridge.events_since(since)
                    return self._json({"seq": bridge.seq, "events": events})
                return self._json({"error": "unknown endpoint"}, 404)

            def _bridge_call(self, fn, *args) -> bool:
                """v4.4.0: an exception inside a bridge method used to kill
                the handler thread mid-response - the browser saw a dropped
                connection instead of an error (this is exactly how the demo
                KeyError surfaced).  Returns True when fn completed; False
                after sending a JSON 500."""
                try:
                    fn(*args)
                    return True
                except Exception as e:
                    import traceback
                    bridge.log(f"internal error in {fn.__name__}: {e}",
                               "err")
                    traceback.print_exc()
                    self._json({"ok": False,
                                "error": f"internal error: {e}"}, 500)
                    return False

            def do_POST(self):
                path = self.path.split("?", 1)[0]
                body = self._body()
                if path == "/api/connect":
                    if self._bridge_call(bridge.request_connect,
                                         body.get("region") or None):
                        return self._json({"ok": True})
                    return
                if path == "/api/disconnect":
                    if self._bridge_call(bridge.request_disconnect):
                        return self._json({"ok": True})
                    return
                if path == "/api/select":
                    if self._bridge_call(bridge.select_region,
                                         body.get("region") or OPTIMAL):
                        return self._json({"ok": True})
                    return
                if path == "/api/pings":
                    if self._bridge_call(self._do_pings):
                        return self._json({"ok": True})
                    return
                if path == "/api/settings":
                    try:
                        errors = bridge.update_settings(body)
                    except Exception as e:
                        import traceback
                        bridge.log(f"internal error in update_settings: "
                                   f"{e}", "err")
                        traceback.print_exc()
                        return self._json({"ok": False,
                                            "error": f"internal error: {e}"},
                                           500)
                    return self._json({"ok": not errors, "errors": errors})
                if path == "/api/quit":
                    self._json({"ok": True})
                    bridge.log("Shut down requested from the UI.", "info")
                    threading.Thread(target=bridge.stop, daemon=True).start()
                    return
                if path == "/api/update/check":
                    if bridge.update is None:
                        return self._json({"ok": False,
                                            "error": "demo mode"}, 400)
                    bridge.update.start_check(manual=True)
                    return self._json({"ok": True})
                if path == "/api/update/install":
                    if bridge.update is None:
                        return self._json({"ok": False,
                                            "error": "demo mode"}, 400)
                    if bridge.update.state().get("source_install"):
                        return self._json({"ok": False,
                                            "error": "source installation - "
                                                     "update with git pull"},
                                           400)
                    bridge.update.start_install()
                    return self._json({"ok": True})
                return self._json({"error": "unknown endpoint"}, 404)

            def _do_pings(self):
                if bridge.demo:
                    with bridge.lock:
                        bridge.pings = dict(DEMO_PINGS)
                    bridge.emit({"t": "pings", "pings": dict(DEMO_PINGS)})
                else:
                    if not bridge.locations:
                        bridge.refresh_locations()
                    else:
                        bridge.refresh_pings()

        # bind loopback only; port 0 -> ephemeral
        for attempt in range(20):
            try:
                self._httpd = ThreadingHTTPServer(("127.0.0.1", port or 0),
                                                  Handler)
                break
            except OSError:
                if port:
                    raise
        if self._httpd is None:
            raise OSError("could not bind the local UI server")
        self._httpd.daemon_threads = True
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever,
                                         daemon=True, name="vpeen-http")
        self._thread.start()
        # initial data load (locations -> pings, plus the real IP)
        if self.bridge.demo:
            with self.bridge.lock:
                self.bridge.locations = [dict(l) for l in DEMO_LOCATIONS]
                self.bridge.pings = dict(DEMO_PINGS)
                self.bridge.direct_ip = DEMO_DIRECT_IP
            self.bridge.emit({"t": "locations",
                              "locations": self.bridge.locations})
            self.bridge.emit({"t": "pings", "pings": dict(DEMO_PINGS)})
            self.bridge.emit({"t": "direct_ip", "ip": DEMO_DIRECT_IP})
        else:
            # v4.8.0 "Sentinel" boot sequence: sweep update leftovers from a
            # previous self-update, verify a pending swap, sweep orphaned
            # tunnel state from a crashed session, THEN auto-check GitHub.
            self.bridge.boot_maintenance()
            self.bridge.refresh_locations()
            self.bridge.refresh_direct_ip()
        return self

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/"

    def stop(self):
        try:
            self.bridge.stop()
        finally:
            if self._httpd is not None:
                threading.Thread(target=self._httpd.shutdown,
                                 daemon=True).start()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
