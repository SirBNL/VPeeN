"""
VPeeN GUI - a faithful, modern VPN desktop look (VeePN-style).

Layout:  blue sidebar (icon+label nav)  |  hero panel (power button,
toggles, location pill, IP row, status strip)  |  always-visible
location panel (search, tabs, flag rows, sort).
"""
import os
import queue
import random
import sys
import threading
import time

import customtkinter as ctk
from PIL import Image, ImageDraw, ImageTk

from . import __app_name__, __version__
from . import settings as cfgmod
from .core import Core, PHASE_CONNECTING, spawn_quick_task

# ------------------------------------------------------------------ palette
WHITE = "#ffffff"
SIDEBAR_BG = "#3b63dc"
SIDEBAR_ACT = "#2e51c0"
MAIN_TOP = "#4287f5"
MAIN_BOT = "#3d78ee"
MAIN_MID = "#4080f2"
STRIP_BG = "#2f55c2"
ROW_SEL = "#2e55c6"
ROW_HOVER = "#3d6fe8"
ROW_DIV = "#3a6ce4"
TAB_DIM = "#bdd0ff"
SUBTLE = "#d7e4ff"
FAINT = "#bcd0ff"
TRACK = "#3460d8"
ACCENT = "#22c55e"
ORANGE = "#ff9f0a"
WARN = "#ffb020"
RED = "#ff5c5c"
INK = "#1c2433"
GREY_INK = "#8a94a8"
CARD_LIGHT = "#f4f6fb"
PAGE_BG = "#eef2fa"
LOG_BG = "#0d1428"
LOG_COLORS = {"info": "#93a2c4", "ok": "#00d68f", "warn": "#ffb020",
              "err": "#ff6b6b"}
POWER_IDLE = "#c9cfdc"
POWER_ON = "#22c55e"

MONO = "Consolas" if os.name == "nt" else ("Menlo" if sys.platform == "darwin"
                                           else "DejaVu Sans Mono")
UI_FONT = ("Segoe UI" if os.name == "nt"
           else "Helvetica Neue" if sys.platform == "darwin" else "DejaVu Sans")

OPTIMAL = "Optimal Location"

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


def F(size, weight="normal"):
    return ctk.CTkFont(family=UI_FONT, size=size, weight=weight)


def _asset(*parts: str) -> str | None:
    roots = [getattr(sys, "_MEIPASS", None),
             os.path.dirname(os.path.dirname(os.path.abspath(__file__)))]
    for root in roots:
        if root:
            p = os.path.join(root, "assets", *parts)
            if os.path.exists(p):
                return p
    return None


def _hexrgb(h):
    return tuple(int(h[i:i + 2], 16) for i in (1, 3, 5))


def _lerp_color(c1, c2, t):
    a, b = _hexrgb(c1), _hexrgb(c2)
    return "#%02x%02x%02x" % tuple(int(x + (y - x) * t) for x, y in zip(a, b))


def _gradient(w: int, h: int, top, bot) -> Image.Image:
    base = Image.new("RGB", (1, h))
    for y in range(h):
        t = y / max(h - 1, 1)
        base.putpixel((0, y), tuple(int(a + (b - a) * t)
                                    for a, b in zip(_hexrgb(top), _hexrgb(bot))))
    return base.resize((w, h))


# ------------------------------------------------------------------- icons
def _icon(kind: str, color: str, size: int = 24) -> Image.Image:
    """Thin-stroke, modern PIL-drawn icons (font-independent)."""
    col = _hexrgb(color)
    s = size * 4
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    lw = max(3, s // 13)

    def L(*pts, w=None, fill=None):
        d.line(pts, fill=col + (255,), width=w or lw, joint="curve")

    if kind == "shield":                       # VPN / connect
        d.arc((s * .16, s * .08, s * .84, s * .78), 180, 360,
              fill=col + (255,), width=lw)
        L((s * .16, s * .43), (s * .16, s * .60), (s * .5, s * .92),
          (s * .84, s * .60), (s * .84, s * .43))
        L((s * .35, s * .50), (s * .46, s * .63), (s * .66, s * .38),
          w=int(lw * 1.15))
    elif kind == "globe":                      # tunnel
        d.ellipse((s * .10, s * .10, s * .90, s * .90),
                  outline=col + (255,), width=lw)
        d.ellipse((s * .33, s * .10, s * .67, s * .90),
                  outline=col + (255,), width=int(lw * .8))
        L((s * .12, s * .38), (s * .88, s * .38), w=int(lw * .8))
        L((s * .12, s * .62), (s * .88, s * .62), w=int(lw * .8))
    elif kind == "logs":
        for i, y in enumerate((.24, .50, .76)):
            wfrac = (.58, .80, .42)[i]
            x0 = s * .12
            d.rounded_rectangle((x0, s * y - lw * .55,
                                 s * (.12 + wfrac), s * y + lw * .55),
                                radius=lw * .55, fill=col + (255,))
    elif kind == "gear":
        import math
        cx = cy = s / 2
        r1, r2 = s * .38, s * .16
        for k in range(8):
            a = math.pi / 4 * k + math.pi / 8
            x1, y1 = cx + r1 * math.cos(a), cy + r1 * math.sin(a)
            d.rounded_rectangle((x1 - lw * .6, y1 - lw * .6,
                                 x1 + lw * .6, y1 + lw * .6),
                                radius=lw * .5, fill=col + (255,))
            d.line((cx + r2 * math.cos(a), cy + r2 * math.sin(a),
                    (r2 + (r1 - r2) * .9) * math.cos(a) + cx,
                    (r2 + (r1 - r2) * .9) * math.sin(a) + cy),
                   fill=col + (255,), width=int(lw * 1.5))
        d.ellipse((cx - r1, cy - r1, cx + r1, cy + r1),
                  outline=col + (255,), width=lw)
        d.ellipse((cx - r2, cy - r2, cx + r2, cy + r2),
                  outline=col + (255,), width=lw)
    elif kind == "search":
        r = s * .30
        d.ellipse((s * .5 - r, s * .5 - r - s * .06,
                   s * .5 + r, s * .5 + r - s * .06),
                  outline=col + (255,), width=lw)
        d.line((s * .71, s * .71, s * .88, s * .88),
               fill=col + (255,), width=int(lw * 1.3))
    elif kind == "copy":
        d.rounded_rectangle((s * .30, s * .30, s * .86, s * .86),
                            radius=s * .10, outline=col + (255,), width=lw)
        d.rounded_rectangle((s * .14, s * .14, s * .64, s * .64),
                            radius=s * .10, outline=col + (255,), width=lw)
    elif kind == "check":
        L((s * .18, s * .52), (s * .42, s * .74), (s * .82, s * .28),
          w=int(lw * 1.3))
    elif kind == "lock":
        d.arc((s * .30, s * .08, s * .70, s * .52), 180, 360,
              fill=col + (255,), width=lw)
        L((s * .30, s * .30), (s * .30, s * .44))
        L((s * .70, s * .30), (s * .70, s * .44))
        d.rounded_rectangle((s * .22, s * .42, s * .78, s * .86),
                            radius=s * .10, outline=col + (255,), width=lw)
    elif kind == "bolt":
        pts = [(s * .56, s * .08), (s * .26, s * .56), (s * .47, s * .56),
               (s * .42, s * .92), (s * .74, s * .40), (s * .52, s * .40)]
        d.polygon(pts, fill=col + (255,))
    elif kind == "info":
        d.ellipse((s * .10, s * .10, s * .90, s * .90),
                  outline=col + (255,), width=lw)
        d.ellipse((s * .46, s * .24, s * .54, s * .32), fill=col + (255,))
        d.rounded_rectangle((s * .46, s * .42, s * .54, s * .76),
                            radius=lw * .5, fill=col + (255,))
    elif kind == "bars":                       # signal bars (free locations)
        for i, hfrac in enumerate((.35, .62, .92)):
            x = s * (.16 + i * .26)
            d.rounded_rectangle((x, s * (1 - .12) - s * hfrac,
                                 x + s * .16, s * .88),
                                radius=s * .04, fill=col + (255,))
    return img.resize((size, size), Image.LANCZOS)


class IconCache:
    def __init__(self):
        self._cache = {}

    def get(self, kind, color, size):
        key = (kind, color, size)
        if key not in self._cache:
            self._cache[key] = ImageTk.PhotoImage(_icon(kind, color, size))
        return self._cache[key]


ICONS = IconCache()


class FlagCache:
    """Circular country flags with a green badge for free locations."""

    def __init__(self):
        self._cache = {}

    def get(self, cc: str, size: int = 30, free: bool = False):
        key = (cc.lower(), size, free)
        if key in self._cache:
            return self._cache[key]
        path = _asset("flags", f"{cc.lower()}.png")
        d = size * 4
        if path:
            try:
                img = Image.open(path).convert("RGBA")
                w, h = img.size
                side = min(w, h)
                img = img.crop(((w - side) // 2, (h - side) // 2,
                                (w + side) // 2, (h + side) // 2))
            except Exception:
                img = None
        else:
            img = None
        if img is None:
            img = Image.new("RGBA", (d, d), (0, 0, 0, 0))
            dr = ImageDraw.Draw(img)
            dr.ellipse((0, 0, d - 1, d - 1), fill=_hexrgb("#6c87c8") + (255,))
        else:
            img = img.resize((d, d), Image.LANCZOS)
        mask = Image.new("L", (d, d), 0)
        ImageDraw.Draw(mask).ellipse((0, 0, d - 1, d - 1), fill=255)
        img.putalpha(mask)
        ring = ImageDraw.Draw(img)
        ring.ellipse((0, 0, d - 1, d - 1), outline=(255, 255, 255, 210),
                     width=max(2, d // 40))
        if free:
            r = d // 6
            cx, cy = d - r - d // 16, d - r - d // 16
            ring.ellipse((cx - r - d // 28, cy - r - d // 28,
                          cx + r + d // 28, cy + r + d // 28),
                         fill=_hexrgb(MAIN_MID) + (255,))
            ring.ellipse((cx - r, cy - r, cx + r, cy + r),
                         fill=_hexrgb(ACCENT) + (255,))
        photo = ImageTk.PhotoImage(img.resize((size, size), Image.LANCZOS))
        self._cache[key] = photo
        return photo


FLAGS = FlagCache()


class VPeeNApp(ctk.CTk):
    SB_W = 104          # sidebar width
    RP_W = 308          # right (location) panel width
    W, H = 1000, 620

    def __init__(self):
        super().__init__()
        self.cfg = cfgmod.load()
        ctk.set_widget_scaling(1.0)
        ctk.set_window_scaling(1.0)
        ctk.set_appearance_mode("light")

        self.title(f"{__app_name__}  ·  {__version__}")
        self.geometry(f"{self.W}x{self.H}")
        self.resizable(False, False)
        self.configure(fg_color=MAIN_MID)

        self.core = Core(insecure=False)
        from .tunnel import TunnelController
        self.tunnel = TunnelController(self.core.events)
        self.phase = "disconnected"
        self.conn_mode = "proxy"        # proxy | tunnel
        self.t_connect = None
        self.direct_ip = None
        self.exit_ip = None
        self.pending_tunnel = False
        self.stop_after_tunnel = False
        self._pending_reconnect = False   # v4.2.3 reconnect after stop
        self._stopping = False
        self._last_error = None         # v4.2.2: keep the error visible

        # location model
        self.loc_rows = []              # [{label,country,city,cc,free,region}]
        self.loc_tab = "all"            # all | free
        self.loc_sort = "alpha"         # alpha | fastest
        self.loc_query = ""
        self.loc_selected = None        # region or None (= optimal)
        self.loc_hover = None
        self._row_geo = []
        self._pings = {}                # region -> ms (fastest sort)

        self._scramble_job = None
        self._pulse_job = None
        self._scramble_tick = 0
        self._scramble_target = None
        self._pulse_t0 = 0

        self._build_sidebar()
        self._build_views()
        self._select_view("vpn")

        self.after(120, self._poll_events)
        self.after(500, self._bootstrap)
        self.after(1000, self._tick)
        if os.environ.get("VPeeN_DEMO") == "1":
            self.after(2500, lambda: self._on_connect_toggle(auto=True))
        if os.environ.get("VPeeN_DEMO_TABS") == "1":
            self.after(19000, lambda: self._select_view("logs"))
            self.after(26000, lambda: self._select_view("settings"))
            self.after(32000, lambda: self._select_view("vpn"))
        if os.environ.get("VPeeN_DEMO_OPEN") == "1":
            self.after(400, self._reveal_loc_panel)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ================================================================ sidebar
    def _build_sidebar(self):
        sb = ctk.CTkFrame(self, width=self.SB_W, corner_radius=0,
                          fg_color=SIDEBAR_BG)
        sb.grid(row=0, column=0, sticky="nsw")
        sb.grid_propagate(False)
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        logo_path = _asset("logo.png")
        if logo_path:
            img = Image.open(logo_path)
            self._logo_img = ctk.CTkImage(light_image=img, dark_image=img,
                                          size=(44, 44))
            ctk.CTkLabel(sb, image=self._logo_img, text="",
                         fg_color="transparent").place(x=30, y=26)
        ctk.CTkLabel(sb, text=__app_name__, font=F(14, "bold"),
                     text_color=WHITE, fg_color="transparent").place(
            x=0, y=74, relwidth=1)

        self.nav_btns = {}
        self.nav_icons = {}
        y = 132
        for key, icon, label in (("vpn", "shield", "VPN"),
                                 ("logs", "logs", "Logs"),
                                 ("settings", "gear", "Settings")):
            self.nav_icons[(key, False)] = ICONS.get(icon, "#dbe6ff", 26)
            self.nav_icons[(key, True)] = ICONS.get(icon, "#ffffff", 26)
            b = ctk.CTkButton(sb, text=label,
                              image=self.nav_icons[(key, False)],
                              compound="top", font=F(12),
                              text_color="#c8d7ff",
                              fg_color="transparent",
                              hover_color=SIDEBAR_ACT, corner_radius=12,
                              height=60, width=88,
                              command=lambda k=key: self._select_view(k))
            b.place(x=8, y=y)
            self.nav_btns[key] = b
            y += 72

        self.nav_dot = ctk.CTkLabel(sb, text="●  Offline", font=F(10),
                                    text_color="#aebfe8",
                                    fg_color="transparent")
        self.nav_dot.place(x=0, rely=1.0, y=-42, relwidth=1)
        ctk.CTkLabel(sb, text=f"v{__version__}", font=F(10),
                     text_color="#8fa4e0", fg_color="transparent").place(
            x=0, rely=1.0, y=-24, relwidth=1)

    def _nav_active(self, key):
        for k, b in self.nav_btns.items():
            on = (k == key)
            b.configure(fg_color=SIDEBAR_ACT if on else "transparent",
                        text_color="#ffffff" if on else "#c8d7ff",
                        image=self.nav_icons[(k, on)])

    # ================================================================== views
    def _build_views(self):
        self.views = {}
        for v in ("vpn", "logs", "settings"):
            f = ctk.CTkFrame(self, corner_radius=0, fg_color=PAGE_BG)
            f.grid(row=0, column=1, sticky="nsew")
            self.views[v] = f
        self._build_connect_view(self.views["vpn"])
        self._build_logs_view(self.views["logs"])
        self._build_settings_view(self.views["settings"])

    def _select_view(self, key):
        for k, f in self.views.items():
            f.grid_remove() if k != key else f.grid()
        self._nav_active(key)

    # ============================================================ connect view
    def _build_connect_view(self, view):
        view.configure(fg_color=MAIN_MID)
        cw = self.W - self.SB_W - self.RP_W
        self._cw = cw

        # hero = center area left of the always-visible location panel
        hero = ctk.CTkFrame(view, width=cw, height=self.H, corner_radius=0,
                            fg_color="transparent")
        hero.place(x=0, y=0)
        hero.pack_propagate(False)
        hero.grid_propagate(False)

        grad = _gradient(cw, self.H, MAIN_TOP, MAIN_BOT)
        self._bg_photo = ImageTk.PhotoImage(grad)

        # single canvas for gradient + power button (no seams)
        self.cv = ctk.CTkCanvas(hero, width=cw, height=self.H,
                                bg=MAIN_MID, highlightthickness=0, bd=0)
        self.cv.place(x=0, y=0)
        self.cv.create_image(0, 0, image=self._bg_photo, anchor="nw",
                             tags="bg")

        # ---- power button (white circle + thin glyph, like the reference)
        d = 178
        cxc = cw // 2
        cy = 38 + d // 2
        self.pw = self.cv                       # keep legacy attribute name
        self.pw.ring = self.cv.create_oval(cxc - d // 2, cy - d // 2,
                                           cxc + d // 2, cy + d // 2,
                                           fill=WHITE, outline="",
                                           tags="pw")
        self.pw.arc = self.cv.create_arc(cxc - 34, cy - 34, cxc + 34, cy + 34,
                                         start=300, extent=300, style="arc",
                                         outline=POWER_IDLE, width=8,
                                         tags="pw")
        self.pw.line = self.cv.create_line(cxc, cy - 42, cxc, cy - 6,
                                           fill=POWER_IDLE, width=8,
                                           capstyle="round", tags="pw")
        self.cv.tag_bind("pw", "<Button-1>",
                         lambda _e: self._on_connect_toggle())
        self.cv.tag_bind("pw", "<Enter>", lambda _e: self._pw_hover(True))
        self.cv.tag_bind("pw", "<Leave>", lambda _e: self._pw_hover(False))

        self.lbl_status = ctk.CTkLabel(hero, text="Not Connected",
                                       font=F(23, "bold"), text_color=WHITE)
        self.lbl_status.place(relx=0.5, rely=0, y=242, anchor="n")
        self.lbl_sub = ctk.CTkLabel(hero, text="Your real IP is exposed",
                                    font=F(13), text_color=SUBTLE)
        self.lbl_sub.place(relx=0.5, rely=0, y=280, anchor="n")

        # ---- toggle rows (System Proxy / VPN Tunnel) - reference style
        row1 = ctk.CTkFrame(hero, fg_color="transparent")
        row1.place(relx=0.5, rely=0, y=322, anchor="n")
        ctk.CTkLabel(row1, text="System Proxy", font=F(13),
                     text_color=WHITE, fg_color="transparent").pack(
            side="left", padx=(0, 8))
        ctk.CTkLabel(row1, image=ICONS.get("info", "#bcd0ff", 14), text="",
                     fg_color="transparent").pack(side="left", padx=(0, 10))
        self.sw_proxy = ctk.CTkSwitch(
            row1, text="", width=44, progress_color=ORANGE,
            button_color=WHITE, button_hover_color="#f2f2f2",
            fg_color=TRACK, command=self._on_proxy_toggle)
        self.sw_proxy.select() if self.cfg.get("auto_system_proxy") else None
        self.sw_proxy.pack(side="left")

        row2 = ctk.CTkFrame(hero, fg_color="transparent")
        row2.place(relx=0.5, rely=0, y=360, anchor="n")
        ctk.CTkLabel(row2, text="VPN Tunnel (all traffic)", font=F(13),
                     text_color=WHITE, fg_color="transparent").pack(
            side="left", padx=(0, 8))
        ctk.CTkLabel(row2, image=ICONS.get("info", "#bcd0ff", 14), text="",
                     fg_color="transparent").pack(side="left", padx=(0, 10))
        self.sw_tunnel = ctk.CTkSwitch(
            row2, text="", width=44, progress_color=ORANGE,
            button_color=WHITE, button_hover_color="#f2f2f2",
            fg_color=TRACK, command=self._on_tunnel_switch)
        if self.cfg.get("tunnel_enabled"):
            self.sw_tunnel.select()
        self.sw_tunnel.pack(side="left")

        # ---- location pill (flag + name) - like "Optimal Location" bottom
        pill = ctk.CTkFrame(hero, fg_color=TRACK, corner_radius=20,
                            height=40)
        pill.place(relx=0.5, rely=0, y=428, anchor="n")
        self.pill_flag = ctk.CTkLabel(pill, text="", width=30, height=30,
                                      fg_color="transparent")
        self.pill_flag.pack(side="left", padx=(14, 6), pady=5)
        self.btn_loc = ctk.CTkLabel(pill, text=OPTIMAL, font=F(13, "bold"),
                                    text_color=WHITE, fg_color="transparent")
        self.btn_loc.pack(side="left")
        ctk.CTkLabel(pill, text="⌄", font=F(14, "bold"),
                     text_color="#cfe0ff",
                     fg_color="transparent").pack(side="left", padx=(6, 14))
        self.pill = pill

        # ---- IP row (mono + copy) like the reference bottom bar
        iprow = ctk.CTkFrame(hero, fg_color="transparent")
        iprow.place(relx=0.5, rely=0, y=492, anchor="n")
        self.lbl_ip = ctk.CTkLabel(iprow, text="IP: --", font=ctk.CTkFont(
            family=MONO, size=15, weight="bold"), text_color=WHITE,
            fg_color="transparent")
        self.lbl_ip.pack(side="left")
        self.btn_copy = ctk.CTkButton(
            iprow, text="", image=ICONS.get("copy", "#dbe6ff", 15),
            width=30, height=26, corner_radius=8, fg_color=TRACK,
            hover_color="#3a63d8", command=self._copy_ip)
        self.btn_copy.pack(side="left", padx=(10, 0))

        self.lbl_stats = ctk.CTkLabel(iprow, text="", font=ctk.CTkFont(
            family=MONO, size=11), text_color=FAINT,
            fg_color="transparent")
        self.lbl_stats.pack(side="left", padx=(22, 0))

        # ---- bottom status strip (replaces the promo banner)
        strip = ctk.CTkFrame(hero, fg_color=STRIP_BG, corner_radius=0,
                             height=34)
        strip.place(x=0, rely=1.0, y=-34, relwidth=1, anchor="sw")
        self.strip_dot = ctk.CTkLabel(strip, text="●  Offline", font=F(11),
                                      text_color="#aebfe8",
                                      fg_color="transparent")
        self.strip_dot.pack(side="left", padx=16)
        self.strip_info = ctk.CTkLabel(
            strip, text=self._strip_text(), font=F(10),
            text_color="#9db8f2", fg_color="transparent")
        self.strip_info.pack(side="right", padx=16)
        self.lbl_timer = ctk.CTkLabel(strip, text="", font=ctk.CTkFont(
            family=MONO, size=11, weight="bold"), text_color=WHITE,
            fg_color="transparent")
        self.lbl_timer.pack(side="right", padx=8)

        self._build_loc_panel(view, self.W - self.SB_W, self.H)

    # ----------------------------------------------------- location panel
    def _build_loc_panel(self, view, width, height):
        x0 = width - self.RP_W
        panel = ctk.CTkFrame(view, width=self.RP_W, height=height,
                             corner_radius=0, fg_color=MAIN_MID)
        panel.place(x=x0, y=0)
        panel.grid_propagate(False)
        panel.pack_propagate(False)
        self.loc_panel = panel

        # subtle vertical divider like the reference
        ctk.CTkFrame(view, width=2, height=height, corner_radius=0,
                     fg_color="#3a6ce4").place(x=x0 - 2, y=0)

        # ---- search pill (white, rounded, magnifier)
        sp = ctk.CTkFrame(panel, width=self.RP_W - 32, height=40,
                          corner_radius=20, fg_color=WHITE)
        sp.place(x=16, y=16)
        sp.pack_propagate(False)
        ctk.CTkLabel(sp, image=ICONS.get("search", "#9aa3b8", 18), text="",
                     fg_color="transparent").pack(side="left", padx=(12, 6))
        self.e_search = ctk.CTkEntry(
            sp, placeholder_text="Location", border_width=0, height=36,
            fg_color="transparent", text_color=INK,
            placeholder_text_color="#9aa3b8", font=F(13))
        self.e_search.pack(side="left", fill="x", expand=True, padx=(0, 10))
        self.e_search.bind("<KeyRelease>", lambda _e: self._on_search())

        # ---- tabs: Locations | Free
        tabs = ctk.CTkFrame(panel, fg_color="transparent", width=self.RP_W - 32,
                            height=34)
        tabs.place(x=16, y=64)
        self.tab_btns = {}
        self.tab_unders = {}
        for i, (key, label) in enumerate((("all", "Locations"),
                                          ("free", "Free"))):
            b = ctk.CTkLabel(tabs, text=label, font=F(13, "bold"),
                             text_color=WHITE if key == "all" else TAB_DIM,
                             fg_color="transparent", cursor="hand2")
            b.place(x=i * 110, y=0)
            b.bind("<Button-1>", lambda _e, k=key: self._switch_tab(k))
            self.tab_btns[key] = b
            u = ctk.CTkFrame(tabs, width=72, height=3, corner_radius=2,
                             fg_color=WHITE)
            self.tab_unders[key] = u
        self._place_tab_underscore()

        # ---- header: count + sort dropdown
        self.lbl_count = ctk.CTkLabel(panel, text="— locations", font=F(11),
                                      text_color=TAB_DIM,
                                      fg_color="transparent")
        self.lbl_count.place(x=16, y=102)
        self.btn_sort = ctk.CTkLabel(panel, text="Alphabet  ⌄", font=F(11),
                                     text_color=TAB_DIM, fg_color="transparent",
                                     cursor="hand2")
        self.btn_sort.place(relx=1.0, x=-16, y=102, anchor="ne")
        self.btn_sort.bind("<Button-1>", lambda _e: self._cycle_sort())

        # ---- canvas list (fast, reference-accurate rows)
        self.loc_canvas = ctk.CTkCanvas(panel, width=self.RP_W - 20,
                                        bg=MAIN_MID, highlightthickness=0,
                                        bd=0)
        self.loc_canvas.place(x=8, y=126, width=self.RP_W - 20,
                              height=height - 126 - 12)
        self.loc_scroll = ctk.CTkScrollbar(
            panel, command=self.loc_canvas.yview, width=6,
            height=height - 160,
            button_color="#ffffff", button_hover_color="#e6ecff",
            fg_color="transparent")
        self.loc_scroll.place(x=width - 9, y=130)
        self.loc_canvas.configure(yscrollcommand=self.loc_scroll.set)
        self.loc_frame = ctk.CTkFrame(self.loc_canvas, fg_color=MAIN_MID,
                                      corner_radius=0)
        self.loc_canvas.create_window((0, 0), window=self.loc_frame,
                                      anchor="nw", tags="inner")
        self.loc_canvas.bind("<Button-1>", self._loc_click)
        self.loc_canvas.bind("<Motion>", self._loc_motion)
        self.loc_canvas.bind("<Leave>",
                             lambda _e: self._loc_motion_clear())
        for seq in ("<Button-4>", "<Button-5>"):     # linux wheel
            self.loc_canvas.bind(seq, self._loc_wheel)
        self._wheelbound = False
        self.loc_canvas.bind("<Enter>", self._bind_wheel)
        self.loc_canvas.bind("<Leave>", self._unbind_wheel)

    def _bind_wheel(self, _e=None):
        if not self._wheelbound:
            self.loc_canvas.bind_all("<MouseWheel>", self._loc_wheel)
            self._wheelbound = True

    def _unbind_wheel(self, _e=None):
        if self._wheelbound:
            self.loc_canvas.unbind_all("<MouseWheel>")
            self._wheelbound = False

    def _place_tab_underscore(self):
        for key, b in self.tab_btns.items():
            if key == self.loc_tab:
                b.configure(text_color=WHITE)
                x = b.winfo_x() if b.winfo_ismapped() else \
                    0 if key == "all" else 110
                self.tab_unders[key].place(x=x, y=26)
            else:
                b.configure(text_color=TAB_DIM)
                self.tab_unders[key].place_forget()

    def _switch_tab(self, key):
        if key == self.loc_tab:
            return
        self.loc_tab = key
        self._place_tab_underscore()
        self._render_loc_list()

    def _cycle_sort(self):
        self.loc_sort = "fastest" if self.loc_sort == "alpha" else "alpha"
        self.btn_sort.configure(text=("Fastest  ⌄" if self.loc_sort == "fastest"
                                      else "Alphabet  ⌄"))
        self._render_loc_list()

    def _on_search(self):
        self.loc_query = (self.e_search.get() or "").lower().strip()
        self._render_loc_list()

    # ---- list rendering -------------------------------------------------
    def _visible_rows(self):
        rows = self.loc_rows
        if self.loc_tab == "free":
            rows = [r for r in rows if r["free"]]
        q = self.loc_query
        if q:
            rows = [r for r in rows if q in r["country"].lower()
                    or q in r["city"].lower()]
        if self.loc_sort == "fastest" and self._pings:
            rows = sorted(rows, key=lambda r: (0 if r["free"] else 1,
                                               self._pings.get(r["region"], 9e9)))
        return rows

    def _render_loc_list(self):
        c = self.loc_canvas
        c.delete("all")
        self._row_geo = []
        rows = self._visible_rows()
        free_n = sum(1 for r in self.loc_rows if r["free"])
        self.lbl_count.configure(text=("-" if not self.loc_rows else
                                       f"{len(rows)} of {len(self.loc_rows)}"
                                       " locations"))
        h = 50
        y = 2
        width = self.RP_W - 20
        for i, r in enumerate(rows):
            sel = (r["region"] == self.loc_selected)
            hov = (i == self.loc_hover)
            if sel or hov:
                c.create_rectangle(6, y + 1, 6 + width - 26, y + h - 1,
                                   fill=ROW_SEL if sel else ROW_HOVER,
                                   outline="", tags=(f"r{i}", "row"))
            # flag (or bolt badge for optimal)
            if r["region"] is None:
                img = self._optimal_badge()
                c.create_image(20, y + h // 2, image=img, tags=(f"r{i}", "row"))
            else:
                img = FLAGS.get(r["cc"], 30, free=r["free"])
                c.create_image(21, y + h // 2, image=img,
                               tags=(f"r{i}", "row"))
            c.create_text(46, y + h // 2 - 9, anchor="w", text=r["country"],
                          font=F(12, "bold"),
                          fill="#ffffff" if not hov else "#f2f6ff",
                          tags=(f"r{i}", "row"))
            c.create_text(46, y + h // 2 + 9, anchor="w", text=r["city"],
                          font=F(10), fill=TAB_DIM, tags=(f"r{i}", "row"))
            if r["region"] is None:
                pass
            elif r["free"]:
                c.create_image(6 + width - 44, y + h // 2,
                               image=ICONS.get("bars", "#e8f0ff", 22),
                               tags=(f"r{i}", "row"))
            else:
                c.create_image(6 + width - 44, y + h // 2,
                               image=ICONS.get("lock", "#9db8f2", 20),
                               tags=(f"r{i}", "row"))
            if sel:
                c.create_image(6 + width - 70, y + h // 2,
                               image=ICONS.get("check", "#7CFFC4", 18),
                               tags=(f"r{i}", "row"))
            y += h
        c.configure(scrollregion=(0, 0, width, y + 4))

    def _optimal_badge(self):
        if not hasattr(self, "_opt_img"):
            img = Image.new("RGBA", (120, 120), (0, 0, 0, 0))
            d = ImageDraw.Draw(img)
            d.ellipse((4, 4, 116, 116), fill=(255, 255, 255, 38),
                      outline=(255, 255, 255, 200), width=3)
            bolt = _icon("bolt", "#ffffff", 60)
            img.alpha_composite(bolt, (30, 30))
            self._opt_img = ImageTk.PhotoImage(img.resize((32, 32),
                                                          Image.LANCZOS))
        return self._opt_img

    def _row_at(self, x, y):
        i = (self.loc_canvas.canvasy(y) - 2) // 50
        rows = self._visible_rows()
        if 0 <= i < len(rows):
            return int(i)
        return None

    def _loc_motion(self, e):
        i = self._row_at(e.x, e.y)
        if i != self.loc_hover:
            self.loc_hover = i
            self._render_loc_list()

    def _loc_motion_clear(self):
        if self.loc_hover is not None:
            self.loc_hover = None
            self._render_loc_list()

    def _loc_click(self, e):
        i = self._row_at(e.x, e.y)
        if i is None:
            return
        r = self._visible_rows()[i]
        self._pick_location(r)

    def _loc_wheel(self, e):
        delta = -1 if (getattr(e, "delta", 0) > 0 or e.num == 4) else 1
        self.loc_canvas.yview_scroll(delta, "units")
        return "break"

    def _pick_location(self, row):
        # v4.2.1: premium locations were clickable and silently connected to
        # a region the free API cannot serve (HTTP 422 -> 'Something went
        # wrong').  Refuse them with a clear message instead.
        if not row["free"] and row["region"] is not None:
            self._log_line(f"'{row['country']}' is a Premium location - "
                           f"only the flags with the speed-bars icon are "
                           f"free.", "warn")
            return
        self.loc_selected = row["region"]
        self.cfg["last_region"] = row["region"] or ""
        cfgmod.save(self.cfg)
        self._render_loc_list()
        self._update_pill()
        label = row["country"] if row["region"] else OPTIMAL
        if self.phase in ("connected", "connecting"):
            self._log_line(f"Location changed to '{label}' - reconnecting...",
                           "warn")
            self._reconnect()
        else:
            self._log_line(f"Location selected: {label}")

    def _update_pill(self):
        row = self._selected_row()
        if row is None:
            self.btn_loc.configure(text=OPTIMAL)
            self.pill_flag.configure(image=self._optimal_badge())
            return
        self.btn_loc.configure(text=row["country"])
        try:
            self.pill_flag.configure(image=FLAGS.get(row["cc"], 30,
                                                     free=row["free"]))
        except Exception:
            self.pill_flag.configure(image="")

    def _selected_row(self):
        if self.loc_selected is None:
            return None
        for r in self.loc_rows:
            if r["region"] == self.loc_selected:
                return r
        return None

    def _reveal_loc_panel(self):
        self._log_line("Location panel ready.")

    # ================================================================ actions
    def _bootstrap(self):
        spawn_quick_task(self.core.events, "locations", insecure=False)
        spawn_quick_task(self.core.events, "direct_ip", insecure=False)
        self._log_line("Fetching locations and your real IP...", "info")
        self._startup_sweep()
        if self.cfg.get("auto_connect"):
            self.after(1200, lambda: self._on_connect_toggle(auto=True))

    def _startup_sweep(self):
        """Detect a crashed tunnel session and offer/perform cleanup."""
        from .tunnel_platforms import load_session
        if not (self.cfg.get("tunnel_sweep") and load_session()):
            return
        self._log_line("Previous tunnel session ended uncleanly - "
                       "cleaning up leftover routes...", "warn")
        threading.Thread(target=self._sweep_sync, daemon=True,
                         name="vpeen-sweep").start()

    def _sweep_sync(self):
        try:
            from .tunnel import TunnelController
            TunnelController.cleanup_orphans(self._log_line)
            self._log_line("Leftover tunnel state cleaned up.", "ok")
        except Exception as e:
            self._log_line(f"Auto-cleanup failed ({e}) - use Settings > "
                           f"'Clean up tunnel state'.", "err")

    def _strip_text(self):
        mode = "Tunnel" if self.conn_mode == "tunnel" else "Proxy"
        return (f"SOCKS5 127.0.0.1:{self.cfg.get('socks_port', 1080)}"
                f"  ·  HTTP 127.0.0.1:{self.cfg.get('http_port', 8080)}"
                f"  ·  {mode}")

    def _on_proxy_toggle(self):
        self.cfg["auto_system_proxy"] = bool(self.sw_proxy.get())
        cfgmod.save(self.cfg)
        if self.phase == "connected" and self.conn_mode == "proxy":
            self._log_line("System proxy setting applies on next connect.",
                           "info")

    def _on_tunnel_switch(self):
        want = bool(self.sw_tunnel.get())
        if want:
            ok, why = self._tunnel_available()
            if not ok:
                self.sw_tunnel.deselect()
                self._log_line(f"Tunnel mode unavailable: {why}", "err")
                return
        self.cfg["tunnel_enabled"] = want
        cfgmod.save(self.cfg)
        if self.phase in ("connected", "connecting"):
            self._log_line("Tunnel mode changed - reconnecting to apply...",
                           "warn")
            self._reconnect()

    def _tunnel_available(self):
        from .tunnel import TunnelController
        return TunnelController.is_available()

    def _on_connect_toggle(self, auto=False):
        # v4.2.3: the power button used to be IGNORED while "connecting" -
        # a stalled connect (slow network, API backoff) left the user
        # clicking a dead button with no escape but killing the process.
        # core.stop() now cancels the asyncio task directly (~1s) and the
        # tunnel watchdogs handle the tunnel side, so the button is a
        # reliable cancel at every phase.
        if self.phase in ("connected", "connecting"):
            self._disconnect_sequence()
            return
        if self.core.is_busy():
            return
        try:
            socks_port = int(self.cfg.get("socks_port", 1080))
            http_port = int(self.cfg.get("http_port", 8080))
            if not (0 < socks_port < 65536 and 0 < http_port < 65536):
                raise ValueError
        except ValueError:
            self._log_line("Invalid ports - check Settings.", "err")
            return
        want_tunnel = bool(self.sw_tunnel.get())
        region = self.loc_selected
        label = (self._selected_row() or {}).get("country", OPTIMAL)
        self._stopping = False
        self.stop_after_tunnel = False
        self._log_line(f"Connecting to '{label}' "
                       f"({'tunnel' if want_tunnel else 'proxy'} mode)...",
                       "info")
        self.pending_tunnel = want_tunnel
        # v4.2.1: the 'auto system proxy' setting used to be dead - start()
        # was hardcoded to False, so proxy mode never configured the OS and
        # users had to set it by hand every time.
        set_system = bool(self.cfg.get("auto_system_proxy", False)) \
            and not want_tunnel      # tunnel mode routes everything itself
        self.core.start(region, "127.0.0.1", socks_port, http_port, set_system)
        self._set_phase("connecting")

    def _reconnect(self):
        if self.phase in ("connected", "connecting"):
            self._disconnect_sequence(reconnect=True)

    def _disconnect_sequence(self, reconnect=False):
        self.stop_after_tunnel = reconnect
        self._stopping = True
        # v4.2.3: remember that a reconnect was requested - after the core
        # reports 'disconnected' the connect is re-issued automatically.
        # (The old flow logged "reconnecting..." but NOTHING re-invoked
        # the connect: changing the location or the tunnel mode while
        # connected left the app silently disconnected.)
        self._pending_reconnect = reconnect
        if self.tunnel.state in ("starting", "up"):
            self._log_line("Disconnecting tunnel...", "info")
            self.tunnel.disconnect()
            self.after(8000, self._tunnel_down_watchdog)
        elif self.core.is_busy():
            self._log_line("Disconnecting...", "info")
            self.core.stop()
        elif reconnect:
            # nothing to stop - re-issue the connect right away
            self._pending_reconnect = False
            self.after(300, self._reconnect_kick)

    def _reconnect_kick(self, tries=0):
        """Re-issue the connect once the core thread is really gone."""
        if not self._pending_reconnect:
            return
        try:
            busy = self.core.is_busy()
        except Exception:
            busy = False
        if busy and tries < 25:
            self.after(200, lambda: self._reconnect_kick(tries + 1))
            return
        self._pending_reconnect = False
        if self.phase == "disconnected":
            self._log_line("Reconnecting with the new settings...", "info")
            self._on_connect_toggle()

    def _tunnel_down_watchdog(self):
        # if the tunnel never reported back, force the core stop
        if self.stop_after_tunnel and self.phase in ("connected", "connecting"):
            self.core.stop()

    # ------------------------------------------------------------ phases/UI
    def _set_phase(self, phase, detail=None):
        self.phase = phase
        if phase == "connecting":
            self._last_error = None
            self.lbl_status.configure(text="Connecting...")
            self.lbl_sub.configure(text="Negotiating a secure tunnel")
            self.nav_dot.configure(text="●  Connecting", text_color=WARN)
            self.strip_dot.configure(text="●  Connecting", text_color=WARN)
            self._start_pulse()
            self._start_scramble()
        elif phase == "connected":
            region = detail or "optimal"
            self._stop_pulse(connected=True)
            self._stop_scramble(final=self.exit_ip)
            title = "Protected"
            sub = f"Tunnel active - {region}"
            if self.conn_mode == "tunnel":
                title = "Protected (Tunnel)"
                sub = f"VPN mode - all traffic via {region}"
            self.lbl_status.configure(text=title)
            self.lbl_sub.configure(text=sub)
            self.nav_dot.configure(text="●  Protected", text_color=ACCENT)
            self.strip_dot.configure(text="●  Protected", text_color=ACCENT)
            self.lbl_ip.configure(text=f"IP: {self.exit_ip or '--'}")
            self.t_connect = time.time()
            self.strip_info.configure(text=self._strip_text())
        elif phase == "disconnected":
            self._stop_pulse(connected=False)
            self._stop_scramble()
            self.conn_mode = "proxy"
            self.lbl_status.configure(text="Not Connected")
            # v4.2.2: the core emits phase=error and then phase=disconnected,
            # so the error used to be visible for one 120ms poll tick and then
            # overwritten by "Your real IP is exposed" - the user never saw
            # WHY the connect failed.  Keep the last error on screen.
            if self._last_error:
                self.lbl_sub.configure(text=f"Last error: {self._last_error}",
                                       text_color=RED)
            else:
                self.lbl_sub.configure(text="Your real IP is exposed",
                                       text_color=SUBTLE)
            self.nav_dot.configure(text="●  Offline", text_color="#aebfe8")
            self.strip_dot.configure(text="●  Offline", text_color="#aebfe8")
            self.lbl_ip.configure(text=f"IP: {self.direct_ip or '--'}")
            self.lbl_timer.configure(text="")
            self.lbl_stats.configure(text="")
            self.t_connect = None
            self.strip_info.configure(text=self._strip_text())
        elif phase == "error":
            self._stop_pulse(connected=False)
            self._stop_scramble()
            self._last_error = str(detail or "Unknown error")
            self.lbl_status.configure(text="Something went wrong")
            self.lbl_sub.configure(text=str(detail or "Unknown error"))
            self.nav_dot.configure(text="●  Error", text_color=RED)
            self.strip_dot.configure(text="●  Error", text_color=RED)

    def _pw_hover(self, on):
        color = "#eef3ff" if on else WHITE
        self.pw.itemconfig(self.pw.ring, fill=color)

    # ---- pulse animation
    def _start_pulse(self):
        self._stop_pulse(connected=False)
        self._pulse_t0 = time.time()
        self._pulse_step()

    def _pulse_step(self):
        if self.phase not in ("connecting", "connected"):
            return
        import math
        t = time.time() - self._pulse_t0
        k = (1 - math.cos(t * 2 * math.pi / 1.6)) / 2
        if self.phase == "connecting":
            base, glow = POWER_IDLE, WARN
        else:
            base, glow = POWER_ON, "#7cffd9"
        col = _lerp_color(base, glow, k)
        try:
            self.pw.itemconfig(self.pw.arc, outline=col)
            self.pw.itemconfig(self.pw.line, fill=col)
        except Exception:
            return
        self._pulse_job = self.after(40, self._pulse_step)

    def _stop_pulse(self, connected):
        if self._pulse_job:
            self.after_cancel(self._pulse_job)
            self._pulse_job = None
        col = POWER_ON if connected else POWER_IDLE
        try:
            self.pw.itemconfig(self.pw.arc, outline=col)
            self.pw.itemconfig(self.pw.line, fill=col)
        except Exception:
            pass

    # ---- IP scramble animation
    def _fake_ip(self, locked, target):
        parts = []
        dotted = bool(target) and target.count(".") == 3   # v4.2.3: IPv6 exits
        for i in range(4):
            if i < locked and dotted:
                parts.append(target.split(".")[i])
            else:
                parts.append(str(random.randint(1, 255)))
        return ".".join(parts)

    def _start_scramble(self):
        self._scramble_tick = 0
        self._scramble_target = None
        self.lbl_ip.configure(text="IP: ...")
        self._scramble_job = self.after(50, self._scramble_step)

    def _scramble_step(self):
        self._scramble_tick += 1
        if self._scramble_target:
            locked = min(4, self._scramble_tick // 10)
            self.lbl_ip.configure(
                text=f"IP: {self._fake_ip(locked, self._scramble_target)}")
            if locked >= 4:
                self.lbl_ip.configure(text=f"IP: {self._scramble_target}")
                self._scramble_job = None
                return
        else:
            self.lbl_ip.configure(text=f"IP: {self._fake_ip(0, None)}")
        self._scramble_job = self.after(50, self._scramble_step)

    def _stop_scramble(self, final=None):
        if self._scramble_job:
            self.after_cancel(self._scramble_job)
            self._scramble_job = None
        if final:
            self._scramble_target = final
            self._scramble_tick = 0
            self._scramble_job = self.after(50, self._scramble_step)
        elif not self._scramble_job:
            self.lbl_ip.configure(text=f"IP: {self.direct_ip or '--'}")

    def _copy_ip(self):
        txt = self.lbl_ip.cget("text").replace("IP: ", "")
        if txt and txt != "--":
            self.clipboard_clear()
            self.clipboard_append(txt)
            self.btn_copy.configure(image=ICONS.get("check", "#7CFFC4", 15))
            self.after(1200, lambda: self.btn_copy.configure(
                image=ICONS.get("copy", "#dbe6ff", 15)))

    # -------------------------------------------------------------- logs view
    def _build_logs_view(self, view):
        view.configure(fg_color=PAGE_BG)
        ctk.CTkLabel(view, text="Activity log", font=F(20, "bold"),
                     text_color=INK, fg_color="transparent").place(x=26, y=22)
        ctk.CTkButton(view, text="Clear", width=84, height=32,
                      corner_radius=10, fg_color="#e3e9f4",
                      hover_color="#d5def0", text_color=INK,
                      font=F(12, "bold"),
                      command=lambda: self.txt_logs.delete("1.0", "end")
                      ).place(relx=1.0, x=-96 - 92, y=24)
        ctk.CTkButton(view, text="Save as...", width=88, height=32,
                      corner_radius=10, fg_color=MAIN_TOP,
                      hover_color=MAIN_BOT, text_color=WHITE,
                      font=F(12, "bold"), command=self._save_logs
                      ).place(relx=1.0, x=-96, y=24)
        self.txt_logs = ctk.CTkTextbox(
            view, font=ctk.CTkFont(family=MONO, size=13),
            fg_color=LOG_BG, text_color="#c9d6f2", corner_radius=14,
            border_width=0, wrap="word", width=680, height=520)
        self.txt_logs.place(x=26, y=70)
        for tag, color in LOG_COLORS.items():
            try:
                self.txt_logs.tag_config(tag, foreground=color)
            except Exception:
                pass
        self._log_line("VPeeN ready. Waiting for action...", "info")

    def _log_line(self, line, level="info"):
        ts = time.strftime("%H:%M:%S")
        tag = level if level in LOG_COLORS else "info"
        try:
            self.txt_logs.insert("end", f"[{ts}] {line}\n", tag)
        except Exception:
            pass
        try:
            self.txt_logs.see("end")
        except Exception:
            pass

    def _save_logs(self):
        from tkinter import filedialog
        path = filedialog.asksaveasfilename(defaultextension=".txt",
                                            initialfile="vpeen-log.txt",
                                            filetypes=[("Text", "*.txt")])
        if path:
            with open(path, "w", encoding="utf-8") as f:
                f.write(self.txt_logs.get("1.0", "end"))
            self._log_line(f"Log saved to {path}", "ok")

    # --------------------------------------------------------- settings view
    def _build_settings_view(self, view):
        view.configure(fg_color=PAGE_BG)
        ctk.CTkLabel(view, text="Settings", font=F(20, "bold"),
                     text_color=INK, fg_color="transparent").place(x=26, y=22)

        def card(y, h, title):
            c = ctk.CTkFrame(view, width=680, height=h, corner_radius=14,
                             fg_color=WHITE)
            c.place(x=26, y=y)
            c.pack_propagate(False)
            ctk.CTkLabel(c, text=title, font=F(11, "bold"),
                         text_color=GREY_INK,
                         fg_color="transparent").place(x=20, y=12)
            return c

        c1 = card(64, 168, "LOCAL PROXY")
        ctk.CTkLabel(c1, text="Bind address", text_color=INK, font=F(13),
                     fg_color="transparent").place(x=20, y=46)
        self.e_bind = ctk.CTkEntry(c1, width=170, height=34, corner_radius=9,
                                   fg_color=CARD_LIGHT, border_width=0,
                                   text_color=INK)
        self.e_bind.insert(0, self.cfg.get("bind", "127.0.0.1"))
        self.e_bind.place(relx=1.0, x=-190, y=42)
        ctk.CTkLabel(c1, text="SOCKS5 port", text_color=INK, font=F(13),
                     fg_color="transparent").place(x=20, y=90)
        self.e_socks = ctk.CTkEntry(c1, width=170, height=34, corner_radius=9,
                                    fg_color=CARD_LIGHT, border_width=0,
                                    text_color=INK)
        self.e_socks.insert(0, str(self.cfg.get("socks_port", 1080)))
        self.e_socks.place(relx=1.0, x=-190, y=86)
        ctk.CTkLabel(c1, text="HTTP port", text_color=INK, font=F(13),
                     fg_color="transparent").place(x=20, y=134)
        self.e_http = ctk.CTkEntry(c1, width=170, height=34, corner_radius=9,
                                   fg_color=CARD_LIGHT, border_width=0,
                                   text_color=INK)
        self.e_http.insert(0, str(self.cfg.get("http_port", 8080)))
        self.e_http.place(relx=1.0, x=-190, y=130)

        c2 = card(246, 176, "TUNNEL (VPN MODE)")
        self.sw_tdns = self._sw(c2, "Route DNS through the tunnel (no leaks)",
                                self.cfg.get("tunnel_dns"), 44)
        self.sw_tsweep = self._sw(c2, "Auto-clean after a crashed session",
                                  self.cfg.get("tunnel_sweep"), 80)
        ctk.CTkLabel(c2, text="MTU", text_color=INK, font=F(13),
                     fg_color="transparent").place(x=20, y=120)
        self.e_mtu = ctk.CTkEntry(c2, width=90, height=32, corner_radius=9,
                                  fg_color=CARD_LIGHT, border_width=0,
                                  text_color=INK)
        self.e_mtu.insert(0, str(self.cfg.get("tunnel_mtu", 1500)))
        self.e_mtu.place(x=90, y=118)
        ctk.CTkButton(c2, text="Clean up tunnel state", height=32,
                      width=180, corner_radius=9, fg_color="#eef1f8",
                      hover_color="#ffe3e3", text_color=INK, font=F(12),
                      command=self._manual_cleanup).place(relx=1.0, x=-200,
                                                          y=118)

        c3 = card(436, 132, "BEHAVIOUR")
        self.sw_auto_sys = self._sw(c3, "Set system proxy on connect",
                                    self.cfg.get("auto_system_proxy"), 44)
        self.sw_autoconn = self._sw(c3, "Auto-connect on launch",
                                    self.cfg.get("auto_connect"), 84)

        ctk.CTkButton(view, text="Save settings", height=38, width=680,
                      corner_radius=10, fg_color=MAIN_TOP,
                      hover_color=MAIN_BOT, text_color=WHITE,
                      font=F(13, "bold"), command=self._save_settings
                      ).place(x=26, y=548)

        ctk.CTkLabel(view, text=f"{__app_name__} v{__version__}  ·  "
                                f"github.com/SirBNL/VPeeN  ·  MIT  ·  "
                                f"bundles tun2socks (MIT) + wintun",
                     font=F(10), text_color=GREY_INK,
                     fg_color="transparent").place(x=26, rely=1.0, y=-24)

    def _sw(self, card, text, on, y):
        sw = ctk.CTkSwitch(card, text=text, progress_color=ACCENT,
                           button_color=WHITE, fg_color="#dfe6f2",
                           text_color=INK, font=F(13))
        if on:
            sw.select()
        sw.place(x=20, y=y)
        return sw

    def _manual_cleanup(self):
        threading.Thread(target=self._sweep_sync, daemon=True).start()

    def _save_settings(self):
        try:
            socks_port = int(self.e_socks.get())
            http_port = int(self.e_http.get())
            mtu = int(self.e_mtu.get())
            if not (0 < socks_port < 65536 and 0 < http_port < 65536
                    and 500 <= mtu <= 65000):
                raise ValueError
        except ValueError:
            self._log_line("Invalid values - settings not saved.", "err")
            return
        self.cfg.update({
            "bind": self.e_bind.get().strip() or "127.0.0.1",
            "socks_port": socks_port,
            "http_port": http_port,
            "tunnel_dns": bool(self.sw_tdns.get()),
            "tunnel_sweep": bool(self.sw_tsweep.get()),
            "tunnel_mtu": mtu,
            "auto_system_proxy": bool(self.sw_auto_sys.get()),
            "auto_connect": bool(self.sw_autoconn.get()),
        })
        cfgmod.save(self.cfg)
        self.strip_info.configure(text=self._strip_text())
        self._log_line("Settings saved.", "ok")

    # ====================================================== tunnel orchestration
    def _tunnel_event(self, ev):
        phase = ev.get("phase")
        if phase == "up":
            self.conn_mode = "tunnel"
            self._log_line("VPN tunnel is up - all traffic now protected.",
                           "ok")
            self._set_phase("connected",
                            (self._selected_row() or {}).get("city", "optimal"))
            self.strip_info.configure(text=self._strip_text())
        elif phase == "down":
            self._log_line("Tunnel is down - network restored.", "ok")
            if self.stop_after_tunnel or self._stopping:
                self.stop_after_tunnel = False
                self._stopping = False
                self.core.stop()
            elif self.phase == "connected":
                # tunnel dropped unexpectedly - fall back to proxy mode
                self.conn_mode = "proxy"
                self.strip_info.configure(text=self._strip_text())
                self._log_line("Fell back to proxy mode.", "warn")
            elif self.phase == "connecting":
                # v4.2.2: the tunnel died while connecting (helper lost,
                # worker error without an explicit error event) - the proxy
                # listeners ARE up at this point, so land in proxy mode
                # instead of spinning in "Connecting..." forever.
                self.conn_mode = "proxy"
                self._log_line("Tunnel did not come up - continuing in "
                               "proxy mode.", "warn")
                self._set_phase("connected", self._region_label())
        elif phase == "error":
            if self.phase == "connecting":
                # v4.2.2: ALWAYS fall back here.  The old code required
                # exit_ip to be known; when the upstream check had failed the
                # UI stayed stuck in "Connecting..." forever.
                self._log_line(f"Tunnel failed ({ev.get('detail')}) - "
                               f"staying in proxy mode.", "err")
                self.conn_mode = "proxy"
                self._set_phase("connected", self._region_label())
            else:
                self._log_line(f"Tunnel error: {ev.get('detail')}", "err")

    def _region_label(self):
        return (self._selected_row() or {}).get("city") or "optimal"

    # ----------------------------------------------------------------- ticks
    def _tick(self):
        if self.phase == "connected" and self.t_connect:
            secs = int(time.time() - self.t_connect)
            self.lbl_timer.configure(text=f"{secs // 3600:02d}:"
                                          f"{secs % 3600 // 60:02d}:"
                                          f"{secs % 60:02d}")
        self.after(1000, self._tick)

    # ------------------------------------------------------------ event pump
    def _poll_events(self):
        try:
            while True:
                ev = self.core.events.get_nowait()
                self._handle_event(ev)
        except queue.Empty:
            pass
        self.after(120, self._poll_events)

    def _handle_event(self, ev: dict):
        kind = ev.get("type")
        if kind == "log":
            self._log_line(ev["line"], ev.get("level", "info"))
        elif kind == "tunnel":
            self._tunnel_event(ev)
        elif kind == "phase":
            ph = ev.get("phase")
            if ph == "connected" and self.pending_tunnel:
                # wait for the tunnel event to finalize the UI
                self.phase = "connecting"
                self._connect_tunnel()
                return
            if ph == "connected" and self.conn_mode == "tunnel":
                return      # tunnel event will drive the UI
            self._set_phase(ph, ev.get("region") or ev.get("detail"))
            if ph == "disconnected" and self._pending_reconnect:
                # v4.2.3: a location/tunnel-mode change asked for a
                # reconnect - fire it now that the core has stopped.
                self.after(300, self._reconnect_kick)
        elif kind == "listeners":
            self._listeners = ev
        elif kind == "exit_ip":
            self.exit_ip = ev.get("ip")
            if self.phase == "connecting":
                self._scramble_target = self.exit_ip
        elif kind == "stats":
            self.lbl_stats.configure(
                text=f"TX {ev['up'] // 1024} KiB   RX {ev['down'] // 1024} KiB"
                     f"   ·   {ev['conns']} conns ({ev['active']})")
        elif kind == "direct_ip":
            if ev.get("data"):
                self.direct_ip = ev["data"]
                if self.phase == "disconnected":
                    self.lbl_ip.configure(text=f"IP: {self.direct_ip}")
                self._log_line(f"Your real IP: {self.direct_ip}", "ok")
        elif kind == "locations":
            self._fill_locations(ev)
        elif kind == "pings":
            if isinstance(ev.get("data"), dict) and ev["data"]:
                self._pings.update(ev["data"])
                if self.loc_sort == "fastest":
                    self._render_loc_list()
        elif kind == "servers_rotated":
            self.tunnel.update_servers(ev.get("upstream_ips", []))

    def _connect_tunnel(self):
        lst = getattr(self, "_listeners", None) or {}
        self._log_line("Requesting administrator rights to enable the "
                       "VPN tunnel...", "info")
        self.tunnel.connect(
            socks_port=lst.get("socks_port", self.cfg.get("socks_port", 1080)),
            upstream_ips=lst.get("upstream_ips", []),
            mtu=int(self.cfg.get("tunnel_mtu", 1500)),
            dns=bool(self.cfg.get("tunnel_dns", True)))
        # safety: if the tunnel never comes up, still show proxy-mode success.
        # v4.2.2: 45s (was 30s) - the elevated helper is allowed 40s by its
        # own CONNECT_TIMEOUT (a UAC prompt easily takes that long), so the
        # old 30s watchdog pre-empted a perfectly healthy pending prompt.
        self.after(45000, self._tunnel_up_watchdog)

    def _tunnel_up_watchdog(self):
        if self.phase == "connecting" and self.tunnel.state != "up":
            self._log_line("Tunnel not confirmed - continuing in proxy mode.",
                           "warn")
            self.conn_mode = "proxy"
            self._set_phase("connected", self._region_label())

    def _fill_locations(self, ev):
        data = ev.get("data")
        if not data:
            self._log_line(f"Could not fetch locations: {ev.get('error')}",
                           "warn")
            return
        rows = [{"label": OPTIMAL, "country": OPTIMAL, "city": "best server "
                 "for you", "cc": "", "free": True, "region": None}]
        for l in sorted(data, key=lambda x: (COUNTRY.get(
                (x.get("countryCode") or "").lower(), "zz"),
                x.get("name", ""))):
            cc = (l.get("countryCode") or "").lower()
            rows.append({
                "country": COUNTRY.get(cc, l.get("region", cc.upper())),
                "city": l.get("name", "?"),
                "cc": cc, "free": l.get("proxyType") == 0,
                "region": l.get("region"),
                "label": l.get("name", "?"),
            })
        self.loc_rows = rows
        last = self.cfg.get("last_region", "")
        if last:
            for r in rows:
                if r["region"] == last:
                    self.loc_selected = last
                    break
        self._render_loc_list()
        self._update_pill()
        self._log_line(f"{len(data)} locations loaded "
                       f"({sum(1 for r in rows if r['free']) - 1} free).",
                       "ok")
        # v4.2.1: probe real latencies for the free regions so 'Fastest'
        # sorts by measured connect time (and region server lists get cached)
        free_regions = sorted({r["region"] for r in rows
                               if r["free"] and r["region"]})
        if free_regions:
            spawn_quick_task(self.core.events, "pings",
                             payload={"regions": free_regions[:10]})

    # ----------------------------------------------------------------- close
    def _on_close(self):
        try:
            if self.tunnel.state in ("starting", "up"):
                self.tunnel.shutdown_worker()
            if self.core.is_busy():
                self.core.stop()
                # v4.2.1: destroying after a fixed 400ms could kill the core
                # thread mid-restore (leaving the OS proxy or routes half
                # applied).  Wait - bounded - for a clean stop.
                self.after(150, self._wait_core_gone, 0)
                return
        except Exception:
            pass
        self.destroy()

    def _wait_core_gone(self, waited_ms: int):
        try:
            if self.core.is_busy() and waited_ms < 3000:
                self.after(150, self._wait_core_gone, waited_ms + 150)
                return
        except Exception:
            pass
        self.destroy()


def run() -> int:
    app = VPeeNApp()
    app.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(run())



