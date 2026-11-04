#!/usr/bin/env python3
"""VPeeN demo GIF v3 — deterministic, frame-stepped cinematic capture.

Why v3 exists: the v2 screencast recorder dropped frames whenever software
compositing fell behind, so the pointer stuttered and click ripples
vanished mid-animation ("not natural").  v3 records like a film camera
instead: virtual time only advances between frames, every animation is
paused and re-positioned per frame, and every frame is a full-quality
screenshot — the result is mathematically smooth at the GIF frame rate.

  * page time is virtual (Date/timers/RAF overridden before boot)
  * every CSS transition/animation is stepped exactly one frame at a time
    (the aurora backdrop stays parked — GIF size relief)
  * the /api/* feed is intercepted and replayed from a scripted timeline,
    so the handshake, stats ticks and log lines land on exact frames
  * a bespoke cursor ("aurora dart" + glow aura + comet trail + layered
    click ripples) is positioned from Python every frame — no jitter
  * encoded with a 2-pass palette; falls back to coarser dither /
    frame decimation / smaller width if the file would exceed 9.4 MB

Usage: python capture_gif_v3.py [--smoke]
"""
import json
import math
import os
import random
import shutil
import subprocess
import sys
import time

ROOT = "/home/z/my-project/VPeeN"
WEB = os.path.join(ROOT, "vpeen", "web")
FLAGS = os.path.join(ROOT, "assets", "flags")
ICON = os.path.join(ROOT, "assets", "icon.png")
INIT_JS = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "vtclock.js")
FRAMES_DIR = "/tmp/gif3_frames"
GIF = os.path.join(ROOT, "docs", "demo.gif")
GIF_COPY = "/home/z/my-project/download/screenshots-v4.4.0/demo.gif"

FPS = 20.0
DT = 1.0 / FPS
VW, VH = 1180, 780
DSF = 1.5
START = (1218.0, 836.0)          # enters from beyond the bottom-right corner
BG = "0x070B14"
MAX_BYTES = 9_400_000

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
DIRECT_IP = "78.39.106.14"
EXIT_IP_1 = "185.244.29.219"     # optimal (Frankfurt exit)
EXIT_IP_2 = "146.70.96.25"       # Amsterdam
VERSION = "4.4.0"
TS0 = "21:04:52"                 # log timestamp base


# ---------------------------------------------------------------- easing
def e_io(u):
    return u * u * u * (u * (6 * u - 15) + 10)


def e_back(u):
    c1 = 1.70158
    return 1 + (c1 + 1) * (u - 1) ** 3 + c1 * (u - 1) ** 2


def e_out(u):
    return 1 - (1 - u) ** 3


def clamp(v, lo, hi):
    return lo if v < lo else hi if v > hi else v


def lerp(a, b, t):
    return a + (b - a) * t


def tremor(t, sd):
    return (0.50 * math.sin(9.7 * t + sd[0]) + 0.28 * math.sin(21.3 * t + sd[1]),
            0.45 * math.sin(8.3 * t + sd[1]) + 0.28 * math.sin(18.1 * t + sd[0]))


# ---------------------------------------------------------------- segments
def M(sel=None, xy=None, dx=0.0, dy=0.0, dur=1.0, arc=0.0, ease="io"):
    return {"kind": "move", "sel": sel, "xy": xy, "dx": dx, "dy": dy,
            "dur": dur, "arc": arc, "ease": ease}


def H(dur, wob=1.5, click=None, tag=None):
    return {"kind": "hold", "dur": dur, "wob": wob, "click": click,
            "tag": tag}


def build_segments():
    return [
        M("#orb", dx=48, dy=-40, dur=1.45, arc=-0.30, ease="out"),
        H(0.78, wob=2.2, click=0.60, tag="click-orb"),
        M("#stat-ip-card", dur=1.0, arc=0.20, ease="io"),
        H(3.0, wob=2.8),                                # watching handshake
        M("#loc-select", dur=0.85, arc=-0.22, ease="io"),
        H(0.85, wob=1.6, click=0.40, tag="click-loc"),
        # ---- locations sheet -------------------------------------------
        M(".optimal-card", dx=-70, dur=0.8, arc=0.16, ease="back"),
        H(0.68, wob=1.5),
        M(".loc-card[data-region='fr-prs']", dx=-85, dur=0.6, arc=-0.14,
          ease="io"),
        H(0.55, wob=1.3),
        M(".loc-card[data-region='nl']", dx=-85, dur=0.6, arc=0.12,
          ease="io"),
        H(0.62, wob=1.4),
        M(".loc-card[data-region='gb-lnd']", dx=-85, dur=0.52, arc=-0.10,
          ease="io"),
        H(0.46, wob=1.2),
        M(".loc-card[data-region='nl']", dx=-85, dur=0.68, arc=0.20,
          ease="io"),                                   # elegant return arc
        H(0.6, wob=1.5, click=0.50, tag="click-nl"),
        H(0.72, wob=1.8),                               # switching toast
        M("#btn-servers-close", dur=0.88, arc=-0.18, ease="io"),
        H(0.72, wob=1.2, click=0.35, tag="click-close"),
        # ---- connected beauty shot -------------------------------------
        M("#stat-ip-card", dur=0.9, arc=0.18, ease="io"),
        H(1.0, wob=2.0, click=0.62, tag="click-ip"),
        M("#btn-logs", dur=0.88, arc=0.20, ease="back"),
        H(0.72, wob=1.3, click=0.38, tag="click-logs"),
        # ---- logs sheet -------------------------------------------------
        M(".log-lines", dx=60, dy=95, dur=0.95, arc=0.10, ease="io"),
        H(1.05, wob=2.2),
        M("#btn-logs-close", dur=0.72, arc=-0.15, ease="io"),
        H(0.55, wob=1.1, click=0.30, tag="click-logs-close"),
        # ---- exit -------------------------------------------------------
        M(xy=(1215.0, 832.0), dur=1.15, arc=-0.20, ease="io"),
        H(1.0, wob=0.0),
    ]


# ------------------------------------------------------------ path planner
class Plan:
    """Samples the cursor path at exact frame timestamps.

    Targets resolve LAZILY: a segment's selector is resolved the moment
    the segment becomes active, so sheet-internal targets are measured
    while the sheet is actually open (their closed-state bounding boxes
    are off-screen and would silently miss every click).
    """

    def __init__(self, pg):
        self.pg = pg
        self.rng = random.Random(11)
        self.sd = (self.rng.uniform(0, 6.28), self.rng.uniform(0, 6.28))
        self.clicks = []            # [(tag, t_down, t_up)]
        self.pos = {0: START}       # j -> (x, y) at t = j*DT
        self.total = 0.0
        self.N = 0
        self.walks = []
        self._cur = None
        self._sf = 0.0
        self._aura = START

    def resolve(self, sel, dx, dy):
        if sel is None:
            return None
        loc = self.pg.locator(sel).first
        loc.wait_for(state="visible", timeout=9000)
        bb = loc.bounding_box() or {"x": VW / 2, "y": VH / 2,
                                    "width": 10, "height": 10}
        cx = bb["x"] + bb["width"] / 2 + dx
        cy = bb["y"] + bb["height"] / 2 + dy
        return (cx + self.rng.uniform(-4, 4), cy + self.rng.uniform(-3, 3))

    def build(self):
        segs = build_segments()
        t0 = 0.25                     # beat before the pointer glides in
        t = t0
        for s in segs:
            w = {"kind": s["kind"], "t0": t, "t1": t + s["dur"],
                 "seg": s, "p1": None, "anchor": None}
            self.walks.append(w)
            if s["kind"] == "hold" and s.get("click") is not None:
                tc = t + s["dur"] * s["click"]
                self.clicks.append((s.get("tag"), tc, tc + 0.13))
            t += s["dur"]
        self.total = t
        self.N = int(math.ceil(self.total * FPS))
        # cursor opacity envelope
        self.fade_in = (0.02, 0.30)
        fout_start = self.total - 1.55
        self.fade_out = (fout_start, fout_start + 0.85)
        return self

    def _walk_at(self, tj):
        if tj <= self.walks[0]["t0"]:
            return self.walks[0]
        for w in self.walks:
            if w["t0"] <= tj <= w["t1"] + 1e-9:
                return w
        return self.walks[-1]

    def sample(self, j):
        """Compute (and memoize) the cursor position for frame index j.
        Called sequentially by the driver; segment targets are resolved
        here — between frames — with the DOM in the right virtual state."""
        if j in self.pos:
            return self.pos[j]
        tj = j * DT
        w = self._walk_at(tj)
        s = w["seg"]
        if w is not self._cur:
            # segment boundary crossed: capture the live start point and
            # resolve the target right now (sheet is open / element ready)
            self._cur = w
            w["anchor"] = self.pos.get(j - 1, self._aura)
            if w["kind"] == "move":
                w["p1"] = (self.resolve(s["sel"], s["dx"], s["dy"])
                            or s["xy"])
        if w["kind"] == "move":
            p0, p1 = w["anchor"], w["p1"]
            u = clamp((tj - w["t0"]) / (w["t1"] - w["t0"]), 0.0, 1.0)
            e = {"io": e_io, "back": e_back, "out": e_out}[s["ease"]](u)
            mx, my = (p0[0] + p1[0]) / 2, (p0[1] + p1[1]) / 2
            ddx, ddy = p1[0] - p0[0], p1[1] - p0[1]
            dist = math.hypot(ddx, ddy) or 1.0
            ux, uy = ddx / dist, ddy / dist
            ctrl = (mx - uy * s["arc"] * dist, my + ux * s["arc"] * dist)
            ex, ey = tremor(tj, self.sd)
            x = ((1 - e) ** 2 * p0[0] + 2 * (1 - e) * e * ctrl[0]
                 + e * e * p1[0] + ex)
            y = ((1 - e) ** 2 * p0[1] + 2 * (1 - e) * e * ctrl[1]
                 + e * e * p1[1] + ey)
        else:
            anchor = w["anchor"]
            wob = s["wob"]
            amp = 1.0
            if s.get("click") is not None:
                tc = w["t0"] + s["dur"] * s["click"]
                if tc - 0.16 <= tj:
                    amp = 0.25
            if tj >= w["t1"]:
                x, y = anchor
            else:
                phi = (s["dur"] * 3.1, s["dur"] * 2.3)
                ex, ey = tremor(tj, self.sd)
                x = anchor[0] + wob * amp * math.sin(
                    2 * math.pi * (tj - w["t0"]) / 3.1 + phi[0]) + ex * 0.6
                y = anchor[1] + wob * amp * math.sin(
                    2 * math.pi * (tj - w["t0"]) / 2.3 + phi[1]) + ey * 0.6
        self.pos[j] = (x, y)
        return self.pos[j]

    def _pos_at(self, t):
        """Interpolated cursor position at any timestamp."""
        j = int(t / DT)
        if j < 0:
            return self.pos[0]
        while j not in self.pos and j > 0:
            j -= 1
        if j + 1 in self.pos and j in self.pos:
            frac = t / DT - j
            p0, p1 = self.pos[j], self.pos[j + 1]
            return (p0[0] + (p1[0] - p0[0]) * frac,
                    p0[1] + (p1[1] - p0[1]) * frac)
        return self.pos.get(j, self.pos[0])

    def _click_pos(self, td):
        return self._pos_at(td)

    # -- per-frame cursor state ------------------------------------------
    def frame(self, k):
        """k = 0 .. N-1 → timestamp t = (k+1)*DT (post-tick time)."""
        j = k + 1
        t = j * DT
        x, y = self.sample(j)

        # speed + smoothed speed factor
        px, py = self.sample(j - 1)
        sp = math.hypot(x - px, y - py) / DT
        sf_prev = getattr(self, "_sf", 0.0)
        sf = lerp(sf_prev, clamp(sp / 950.0, 0.0, 1.0), 0.30)
        self._sf = sf

        # aura (lagging glow)
        ax, ay = getattr(self, "_aura", (x, y))
        k1 = 1 - math.exp(-DT * 7.5)
        ax, ay = ax + (x - ax) * k1, ay + (y - ay) * k1
        self._aura = (ax, ay)

        # press amount with a springy release
        press, down, up = 0.0, False, False
        for _, td, tu in self.clicks:
            if td <= t < tu:
                press = min(1.0, (t - td) / 0.05)
                down = down or (self.pos and j > 0 and
                                (j - 1) * DT < td <= t)
            elif t >= tu:
                u = clamp((t - tu) / 0.22, 0.0, 1.0)
                if u < 1.0:
                    press = max(press, (1 - e_out(u)) * 0.92)
            if (j - 1) * DT < td <= t:
                down = True
            if (j - 1) * DT < tu <= t:
                up = True

        ps = 1 - 0.24 * press + 0.05 * math.sin(
            clamp((t - self._last_up(t)) / 0.22, 0, 1) * math.pi) \
            if press > 0 else 1.0

        # lean into motion + press tilt
        vx = (x - px) / DT
        tilt = clamp(vx / 1500.0, -1, 1) * 5.0 * sf - 4.2 * press

        # comet trail (interpolated for a dense, elegant streak)
        g = []
        if sf > 0.05:
            for m in range(1, 9):
                gx, gy = self._pos_at(t - m * 0.035)
                op = 0.62 * sf * (1 - m / 9.5)
                sc = (1.18 - m * 0.135) * (0.72 + 0.28 * sf)
                g.append([round(gx, 2), round(gy, 2),
                          round(op, 3), round(sc, 3)])
        while len(g) < 8:
            g.append(None)

        # click ripples
        fx = []
        for _, td, tu in self.clicks:
            age = t - td
            if -1e-9 <= age <= 0.85:
                e = {"x": round(self._click_pos(td)[0], 2),
                     "y": round(self._click_pos(td)[1], 2)}
                pa = clamp(age / 0.55, 0.0, 1.0)
                if pa < 1.0:
                    e["as"] = 0.12 + 1.62 * e_out(pa)
                    e["ao"] = round((1 - pa) ** 1.35, 3)
                    e["ab"] = 4.2 * (1 - pa) + 0.7
                pb = clamp((age - 0.08) / 0.48, 0.0, 1.0)
                if 0.0 < pb < 1.0:
                    e["bs"] = 0.28 + 1.45 * e_out(pb)
                    e["bo"] = round((1 - pb) ** 1.6 * 0.95, 3)
                    e["bb"] = 3.0 * (1 - pb) + 0.6
                pf = clamp(age / 0.27, 0.0, 1.0)
                if pf < 1.0:
                    e["fs"] = 1.30 - 0.85 * e_out(pf)
                    e["fo"] = round((1 - pf) ** 1.2, 3)
                fx.append(e)
        while len(fx) < 3:
            fx.append(None)

        # aura bloom on click
        bloom = 0.0
        for _, td, _ in self.clicks:
            ba = t - td
            if 0 <= ba < 0.55:
                bloom = max(bloom, 0.6 * (1 - ba / 0.55))

        o = clamp((t - self.fade_in[0]) /
                  (self.fade_in[1] - self.fade_in[0]), 0, 1)
        o *= 1 - clamp((t - self.fade_out[0]) /
                       (self.fade_out[1] - self.fade_out[0]), 0, 1)

        cur = {
            "o": round(o, 3),
            "x": round(x, 2), "y": round(y, 2),
            "ps": round(max(0.7, ps), 4), "tilt": round(tilt, 2),
            "ax": round(ax, 2), "ay": round(ay, 2),
            "ak": round(1 + 0.22 * sf + bloom * 0.28, 4),
            "ao": round(clamp(0.55 + 0.45 * sf + bloom, 0, 1) * o, 3),
            "g": g, "fx": fx,
        }
        return {"cur": cur, "down": down, "up": up, "t": t}

    def _last_up(self, t):
        last = t - 1.0
        for _, _, tu in self.clicks:
            if tu <= t:
                last = tu
        return last

    def click_time(self, tag):
        for tg, td, _ in self.clicks:
            if tg == tag:
                return td
        raise KeyError(tag)


# ------------------------------------------------------------- API script
class Feed:
    """Scripted /api/* responses + a deterministic event timeline.

    Events are NOT delivered through /api/poll — that roundtrip races
    the screenshot compositor and produces double-exposed text.  The
    driver instead applies due events synchronously via applyEvent()
    at the exact frame, before ticking the virtual clock."""

    def __init__(self, plan=None):
        self.plan = plan
        self.rng = random.Random(42)
        self.events = []            # [(t, ev)] sorted lazily
        self.seq = 0
        self.vt = 0.0
        self.delivered = 0

    def at(self, t, ev):
        self.events.append((t, ev))

    def _ts(self, t):
        secs = 21 * 3600 + 4 * 60 + 52 + int(t)
        return "%02d:%02d:%02d" % (secs // 3600, (secs % 3600) // 60,
                                   secs % 60)

    def _log(self, t, line, level="info"):
        self.at(t, {"t": "log", "line": line, "level": level,
                    "ts": self._ts(t)})

    def schedule_connect(self, t, region, exit_ip, fast=False):
        """Handshake timeline mirroring the real demo bridge."""
        label = ("Optimal Location" if region in (None, "optimal")
                 else "Amsterdam, Netherlands")
        self.at(t + 0.04, {"t": "phase", "phase": "connecting",
                           "region": region})
        self._log(t + 0.06, f"Connecting to '{label}' (demo mode)...")
        if fast:
            self._log(t + 0.55, "Requesting anonymous access token "
                                "(POST /v3/launch/) ...")
            self._log(t + 1.25, "Acquired 3 upstream server(s).", "ok")
            tc = t + 1.85
        else:
            self._log(t + 1.15, "Requesting anonymous access token "
                                "(POST /v3/launch/) ...")
            self._log(t + 2.10, "Acquired 3 upstream server(s).", "ok")
            self._log(t + 2.95, "Local listeners up | SOCKS5 127.0.0.1:1080 |"
                                " HTTP 127.0.0.1:8080", "ok")
            tc = t + 3.70
        self.at(tc, {"t": "phase", "phase": "connected",
                     "region": None if region == "optimal" else region,
                     "mode": "proxy"})
        self.at(tc + 0.02, {"t": "exit_ip", "ip": exit_ip})
        self._log(tc + 0.04, f"Connected - exit IP: {exit_ip}", "ok")
        # traffic + relay logs until the end of the scenario
        down = up = 0
        j = 1
        while True:
            ts = tc + j * 1.0
            if ts > self.plan.total:
                break
            down += int(1.30e6 + 0.65e6 * self.rng.random())
            up += int(0.30e6 + 0.14e6 * self.rng.random())
            self.at(ts, {"t": "stats", "up": up, "down": down,
                         "conns": min(11, 5 + j // 2), "active": 2 + j % 3,
                         "failed": 0})
            j += 1
        relays = [
            "relay 104.18.32.7:443 → upstream-1 (22 ms)",
            "relay 151.101.1.140:443 → upstream-2 (35 ms)",
            "relay 142.250.185.78:443 → upstream-3 (29 ms)",
            "relay 13.107.42.16:443 → upstream-1 (24 ms)",
        ]
        j = 0
        while True:
            ts = tc + 1.3 + j * 2.35
            if ts > self.plan.total:
                break
            self._log(ts, relays[j % len(relays)])
            j += 1
        self.events.sort(key=lambda x: x[0])
        return tc

    # -- route handlers --------------------------------------------------
    def state(self):
        return {
            "phase": "disconnected", "mode": "proxy", "region": None,
            "selected": "optimal", "locations": DEMO_LOCATIONS,
            "pings": DEMO_PINGS,
            "settings": {"bind": "127.0.0.1", "socks_port": 1080,
                         "http_port": 8080, "auto_system_proxy": True,
                         "tunnel_dns": False, "tunnel_fallback": True,
                         "tunnel_mtu": 1500, "tunnel_sweep": True},
            "direct_ip": DIRECT_IP, "exit_ip": None,
            "stats": {"up": 0, "down": 0, "conns": 0, "active": 0,
                      "failed": 0},
            "connected_since": None, "native_window": True,
            "version": VERSION, "tunnel_available": False,
            "seq": 0,
            "logs": [{"ts": self._ts(0.05), "level": "ok",
                      "line": f"VPeeN v{VERSION} web UI ready."},
                     {"ts": self._ts(0.08), "level": "ok",
                      "line": f"Your real IP: {DIRECT_IP}"}],
        }

    def poll(self):
        # events are delivered synchronously by the driver, never here
        self.seq += 1
        return {"seq": self.seq, "events": []}

    def due(self, t):
        """Events with time <= t, in order — applied via applyEvent."""
        due = []
        while (self.delivered < len(self.events)
               and self.events[self.delivered][0] <= t + 1e-9):
            due.append(self.events[self.delivered][1])
            self.delivered += 1
        return due


# ---------------------------------------------------------------- routing
CT = {
    "html": "text/html; charset=utf-8",
    "js": "text/javascript; charset=utf-8",
    "css": "text/css; charset=utf-8",
    "png": "image/png",
    "json": "application/json",
}


def setup_routes(pg, feed):
    def handler(route, request):
        url = request.url
        path = url.split("://", 1)[1].split("/", 1)
        p = "/" + (path[1] if len(path) > 1 else "")
        p = p.split("?")[0]
        if p == "/api/state":
            route.fulfill(content_type=CT["json"],
                          body=json.dumps(feed.state()))
        elif p == "/api/poll":
            route.fulfill(content_type=CT["json"],
                          body=json.dumps(feed.poll()))
        elif p == "/api/connect":
            # the handshake is pre-scheduled deterministically from the
            # known click frames; just acknowledge
            route.fulfill(content_type=CT["json"], body='{"ok": true}')
        elif p == "/api/select":
            route.fulfill(content_type=CT["json"], body='{"ok": true}')
        elif p == "/api/disconnect":
            route.fulfill(content_type=CT["json"], body='{"ok": true}')
        elif p == "/api/pings":
            route.fulfill(content_type=CT["json"],
                          body=json.dumps({"ok": True}))
        elif p == "/api/settings":
            route.fulfill(content_type=CT["json"], body='{"ok": true}')
        elif p == "/api/quit":
            route.fulfill(content_type=CT["json"], body='{"ok": true}')
        elif p in ("/", "/index.html"):
            _file(route, os.path.join(WEB, "index.html"), CT["html"])
        elif p == "/app.js":
            _file(route, os.path.join(WEB, "app.js"), CT["js"])
        elif p == "/style.css":
            _file(route, os.path.join(WEB, "style.css"), CT["css"])
        elif p == "/icon.png":
            _file(route, ICON, CT["png"])
        elif p.startswith("/flags/"):
            cc = p[len("/flags/"):-len(".png")]
            if cc.isalpha() and len(cc) == 2:
                _file(route, os.path.join(FLAGS, cc + ".png"), CT["png"])
            else:
                route.fulfill(status=404, body="")
        else:
            route.fulfill(status=404, body="")

    def _file(route, path, ctype):
        try:
            with open(path, "rb") as fh:
                route.fulfill(content_type=ctype, body=fh.read())
        except OSError:
            route.fulfill(status=404, body="")

    pg.route("**/*", handler)


# ---------------------------------------------------------------- encode
CACHE = "/tmp/gif3_cache940"
PAL8 = "/tmp/gif3_pal8"
STABILITY_FALLBACKS = (10, 14)      # escalate if the file is too big


def encode_pipeline(frames_dir, out, stability=10):
    """scale -> 255-colour palette -> palette-mapped frames -> custom
    delta/transparency GIF writer (scripts/write_gif.py).

    The custom writer stores each frame as its changed rect with
    unchanged pixels transparent, plus temporal stabilization (slow
    drift freezes) with a dilation ghost-guard around hard changes —
    roughly half the size of ffmpeg's paletteuse at equal quality.
    """
    import importlib
    for d in (CACHE, PAL8):
        os.makedirs(d, exist_ok=True)
        for f in os.listdir(d):
            os.unlink(os.path.join(d, f))
    n = len([f for f in os.listdir(frames_dir) if f.endswith(".png")])
    dur = n / FPS
    fades = (f"fade=t=in:st=0:d=0.25:color={BG},"
             f"fade=t=out:st={max(0.0, dur - 0.35):.3f}:d=0.35:"
             f"color={BG}")
    for cmd in (
        ["ffmpeg", "-y", "-framerate", str(FPS), "-i",
         f"{frames_dir}/f%04d.png", "-vf", "scale=940:622:flags=lanczos",
         f"{CACHE}/f%04d.png"],
        ["ffmpeg", "-y", "-framerate", str(FPS), "-i",
         f"{CACHE}/f%04d.png", "-vf",
         "palettegen=max_colors=255:stats_mode=diff", "/tmp/gif3_pal.png"],
        ["ffmpeg", "-y", "-framerate", str(FPS), "-i",
         f"{CACHE}/f%04d.png", "-i", "/tmp/gif3_pal.png", "-lavfi",
         f"[0:v]{fades}[s];[s][1:v]paletteuse=dither=bayer:"
         "bayer_scale=5", "-pix_fmt", "pal8", "-start_number", "0",
         f"{PAL8}/f%04d.png"],
    ):
        subprocess.run(cmd, check=True, capture_output=True)

    wg = importlib.import_module("write_gif")
    importlib.reload(wg)
    from PIL import Image
    files = sorted(f for f in os.listdir(PAL8) if f.endswith(".png"))
    frames = [Image.open(os.path.join(PAL8, f)).copy() for f in files]
    w, h = frames[0].size
    palette = list(frames[0].getpalette())
    palette[wg.TIDX * 3:wg.TIDX * 3 + 3] = [7, 11, 20]
    size, _ = wg.write_gif(frames, palette, out, w, h, stability=stability)
    return size, dur


def freeze_background(pg):
    """REJECTED (kept for the record): baking the aurora backdrop into a
    static <img> sounds ideal — deterministic, fast — but the huge opaque
    layer re-orders compositing above it, and every subsequent text
    swap renders double-exposed (old + new glyphs superimposed forever).
    Do not call this; the GIF encoder's temporal stabilization absorbs
    the aurora's per-frame re-raster noise instead."""
    raise RuntimeError("freeze_background corrupts text rendering — "
                       "see docstring")


# ---------------------------------------------------------------- driver
def main():
    smoke = "--smoke" in sys.argv
    from playwright.sync_api import sync_playwright

    t_start = time.time()
    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--force-color-profile=srgb"])
        ctx = browser.new_context(viewport={"width": VW, "height": VH},
                                  device_scale_factor=DSF)
        pg = ctx.new_page()
        feed = Feed()
        setup_routes(pg, feed)
        pg.add_init_script(path=INIT_JS)
        pg.goto("http://127.0.0.1:7788/", wait_until="domcontentloaded")
        pg.wait_for_selector("#orb", timeout=15000)
        pg.wait_for_selector(".loc-card", timeout=15000)
        pg.wait_for_timeout(600)
        pg.evaluate("() => __stepAnims()")      # park the aurora blobs
        # NOTE: no background freeze — a static backdrop layer corrupts
        # text re-rastering (double-exposed glyphs on every text swap)

        plan = Plan(pg).build()
        feed.plan = plan
        # pre-schedule the connects from the known click frames:
        t_orb = plan.click_time("click-orb") + 0.13 + 0.03
        feed.schedule_connect(t_orb, "optimal", EXIT_IP_1)
        t_nl = plan.click_time("click-nl") + 0.13 + 0.03
        feed.schedule_connect(t_nl, "nl", EXIT_IP_2, fast=True)
        print(f"plan: {plan.N} frames, {plan.total:.2f}s, "
              f"{len(plan.clicks)} clicks", flush=True)

        N = plan.N if not smoke else min(plan.N, int(6.0 * FPS))
        os.makedirs(FRAMES_DIR, exist_ok=True)
        for f in os.listdir(FRAMES_DIR):
            os.unlink(os.path.join(FRAMES_DIR, f))
        nframes = 0
        checks = {
            "click-orb": "document.body.dataset.phase !== 'disconnected'",
            "click-loc": "document.body.classList.contains('sheet-open')",
            "click-nl": ("document.querySelector("
                         "'.loc-card[data-region=\\'nl\\']')"
                         ".classList.contains('selected')"),
            "click-close": "!document.body.classList.contains('sheet-open')",
            "click-ip": "document.querySelectorAll('.toast').length > 0",
            "click-logs": "document.body.dataset.sheet === 'logs'",
        }
        done_checks = set()
        pg.mouse.move(*START)

        for k in range(N):
            st = plan.frame(k)
            pg.mouse.move(st["cur"]["x"], st["cur"]["y"])
            if st["down"]:
                pg.mouse.down()
            if st["up"]:
                pg.mouse.up()
            # deliver due events synchronously — a poll roundtrip would
            # race the screenshot compositor (double-exposed text)
            due = feed.due(st["t"])
            if due:
                pg.evaluate("evs => evs.forEach(e => applyEvent(e))", due)
            pg.evaluate(f"__tick({DT})")
            feed.vt = st["t"]
            pg.wait_for_timeout(20)
            pg.evaluate("st => __frame(st)", st["cur"])
            with open(f"{FRAMES_DIR}/f{nframes:04d}.png", "wb") as fh:
                fh.write(pg.screenshot(type="png"))
            nframes += 1

            # deferred UI assertions (a few frames after each click)
            for tg, td, _ in plan.clicks:
                if tg in checks and tg not in done_checks \
                        and st["t"] > td + 0.5:
                    ok = pg.evaluate(f"() => !!({checks[tg]})")
                    print(f"  check {tg}: {'OK' if ok else 'FAILED!'}",
                          flush=True)
                    done_checks.add(tg)
            if k % 40 == 0:
                print(f"  frame {k}/{N} ({(time.time()-t_start):.0f}s)",
                      flush=True)
        browser.close()

    n = nframes
    print(f"captured {n} frames "
          f"({(time.time()-t_start):.0f}s wall)", flush=True)
    if n < 40:
        print("ERROR: too few frames")
        return 1
    if "--capture-only" in sys.argv:
        return 0

    size, dur = encode_pipeline(FRAMES_DIR, GIF, stability=10)
    print(f"v1 (stab=10): {size/1e6:.2f} MB / {dur:.1f}s", flush=True)
    for stab in STABILITY_FALLBACKS:
        if size <= MAX_BYTES:
            break
        size, dur = encode_pipeline(FRAMES_DIR, GIF, stability=stab)
        print(f"fallback (stab={stab}): {size/1e6:.2f} MB", flush=True)

    shutil.copyfile(GIF, GIF_COPY)
    print(f"GIF: {GIF} ({os.path.getsize(GIF)/1e6:.2f} MB, ~{dur:.1f}s)",
          flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
