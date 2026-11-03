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
                          "failed": ev.get("failed", 0)}
            self.emit({"t": "stats", **self.stats})
        elif kind == "system_proxy":
            self.system_proxy = bool(ev.get("on"))
            self.emit({"t": "system_proxy", "on": self.system_proxy})
        elif kind == "servers_rotated":
            if self.tunnel is not None:
                try:
                    self.tunnel.update_servers(ev.get("upstream_ips", []))
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
                self._connect_tunnel()
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
    def _connect_tunnel(self):
        if self.tunnel is None:
            return
        self.log("Requesting administrator rights to enable the VPN tunnel...")
        try:
            self.tunnel.connect(
                socks_port=self.listeners.get("socks_port",
                                              self.cfg.get("socks_port", 1080)),
                upstream_ips=self.listeners.get("upstream_ips", []),
                mtu=int(self.cfg.get("tunnel_mtu", 1500)),
                dns=bool(self.cfg.get("tunnel_dns", True)))
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
                        "auto_connect"):
                if key in patch:
                    cfg[key] = bool(patch[key])
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
                                  "active": 2, "failed": 0}
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
