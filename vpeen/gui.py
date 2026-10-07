"""
VPeeN GUI - modern minimal VPN interface (sidebar + blue hero panel +
slide-in location panel), built with CustomTkinter.

Views:  Connect (hero)  |  Logs  |  Settings
Animations: power-button pulse, IP number-roll while connecting,
slide-in location panel, live session timer.
"""
import os
import queue
import random
import sys
import time

import customtkinter as ctk
from PIL import Image, ImageDraw

from . import __app_name__, __version__
from . import settings as cfgmod
from .core import (Core, PHASE_CONNECTED, PHASE_CONNECTING, spawn_quick_task)

# ------------------------------------------------------------------ palette
SIDEBAR_BG = "#0b1226"
SIDEBAR_TXT = "#93a2c4"
SIDEBAR_ACT = "#ffffff"
MAIN_TOP = "#3e7af4"
MAIN_BOT = "#3b74ec"
MAIN_MID = "#3d78f1"
WHITE = "#ffffff"
INK = "#1c2433"
GREY_INK = "#8a94a8"
ACCENT = "#00d68f"
WARN = "#ffb020"
RED = "#ff5c5c"
PILL = "#5b8df9"          # translucent-ish white over blue
PILL_HOVER = "#6f9bfa"
CARD_LIGHT = "#f4f6fb"
LOG_BG = "#0d1428"
LOG_COLORS = {"info": "#93a2c4", "ok": "#00d68f", "warn": "#ffb020", "err": "#ff6b6b"}

MONO = "Consolas" if os.name == "nt" else ("Menlo" if sys.platform == "darwin"
                                           else "DejaVu Sans Mono")
UI_FONT = ("Segoe UI" if os.name == "nt"
           else "Helvetica Neue" if sys.platform == "darwin" else "DejaVu Sans")

OPTIMAL = "Optimal (auto)"


def F(size, weight="normal"):
    return ctk.CTkFont(family=UI_FONT, size=size, weight=weight)


def _asset(name: str) -> str | None:
    roots = [getattr(sys, "_MEIPASS", None),
             os.path.dirname(os.path.dirname(os.path.abspath(__file__)))]
    for root in roots:
        if root:
            p = os.path.join(root, "assets", name)
            if os.path.exists(p):
                return p
    return None


def _gradient(w: int, h: int, top, bot) -> Image.Image:
    base = Image.new("RGB", (1, h))
    for y in range(h):
        t = y / max(h - 1, 1)
        base.putpixel((0, y), tuple(int(a + (b - a) * t) for a, b in zip(top, bot)))
    return base.resize((w, h))


def _icon(kind: str, color, size: int = 26) -> Image.Image:
    """PIL-drawn flat icons for the sidebar (font-independent, crisp)."""
    if isinstance(color, str):
        color = _hexrgb(color)
    img = Image.new("RGBA", (size * 4, size * 4), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    s = size * 4
    lw = max(3, s // 9)
    if kind == "shield":
        pts = [(s * .5, s * .06), (s * .88, s * .2), (s * .88, s * .55),
               (s * .68, s * .82), (s * .5, s * .94), (s * .32, s * .82),
               (s * .12, s * .55), (s * .12, s * .2)]
        d.polygon(pts, outline=color + (255,), width=lw)
        d.line([(s * .34, s * .5), (s * .47, s * .64), (s * .68, s * .36)],
               fill=color + (255,), width=lw)
    elif kind == "logs":
        for i, y in enumerate((.22, .5, .78)):
            wfrac = (.62, .8, .45)[i]
            d.rounded_rectangle((s * .12, s * y - lw, s * (.12 + wfrac), s * y + lw),
                                radius=lw, fill=color + (255,))
    elif kind == "gear":
        import math
        cx = cy = s / 2
        r_out, r_in = s * .40, s * .18
        d.ellipse((cx - r_out, cy - r_out, cx + r_out, cy + r_out),
                  outline=color + (255,), width=lw)
        d.ellipse((cx - r_in, cy - r_in, cx + r_in, cy + r_in),
                  outline=color + (255,), width=lw)
        for k in range(8):
            a = math.pi / 4 * k
            d.line([(cx + r_in * math.cos(a), cy + r_in * math.sin(a)),
                    (cx + r_out * math.cos(a), cy + r_out * math.sin(a))],
                   fill=color + (255,), width=lw)
    return img.resize((size, size), Image.LANCZOS)


def _lerp_color(c1, c2, t):
    a = [int(c1[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(c2[i:i + 2], 16) for i in (1, 3, 5)]
    return "#%02x%02x%02x" % tuple(int(x + (y - x) * t) for x, y in zip(a, b))


class VPeeNApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.cfg = cfgmod.load()
        ctk.set_widget_scaling(1.0)
        ctk.set_window_scaling(1.0)
        ctk.set_appearance_mode("light")

        self.title(f"{__app_name__}  ·  {__version__}")
        self.geometry("980x620")
        self.resizable(False, False)
        self.configure(fg_color=WHITE)

        self.core = Core(insecure=False)
        self.phase = "disconnected"
        self.t_connect = None
        self.direct_ip = None
        self.exit_ip = None
        self.loc_map: dict[str, str | None] = {}
        self.row_order: list[str] = []
        self._scramble_job = None
        self._pulse_job = None
        self._scramble_tick = 0

        self._build_sidebar()
        self._build_views()
        self._select_view("connect")

        self.after(120, self._poll_events)
        self.after(500, self._bootstrap)
        self.after(1000, self._tick)
        if os.environ.get("VPeeN_DEMO") == "1":
            self.after(2500, self._demo_connect)
        if os.environ.get("VPeeN_DEMO_TABS") == "1":
            self.after(19000, lambda: self._select_view("logs"))
            self.after(26000, lambda: self._select_view("settings"))
            self.after(32000, lambda: self._select_view("connect"))
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ================================================================ sidebar
    def _build_sidebar(self):
        sb = ctk.CTkFrame(self, width=132, corner_radius=0, fg_color=SIDEBAR_BG)
        sb.grid(row=0, column=0, sticky="nsw")
        sb.grid_propagate(False)
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        logo_path = _asset("logo.png")
        if logo_path:
            img = Image.open(logo_path)
            self._logo_img = ctk.CTkImage(light_image=img, dark_image=img, size=(46, 46))
            ctk.CTkLabel(sb, image=self._logo_img, text="",
                         fg_color="transparent").place(x=43, y=24)

        ctk.CTkLabel(sb, text=__app_name__, font=F(15, "bold"), text_color=WHITE,
                     fg_color="transparent").place(x=0, y=76, relwidth=1)

        self.nav_btns = {}
        self.nav_icons = {}
        y = 130
        for key, icon, label in (("connect", "shield", "Connect"),
                                 ("logs", "logs", "Logs"),
                                 ("settings", "gear", "Settings")):
            self.nav_icons[(key, False)] = ctk.CTkImage(
                light_image=_icon(icon, SIDEBAR_TXT),
                dark_image=_icon(icon, SIDEBAR_TXT), size=(24, 24))
            self.nav_icons[(key, True)] = ctk.CTkImage(
                light_image=_icon(icon, SIDEBAR_ACT),
                dark_image=_icon(icon, SIDEBAR_ACT), size=(24, 24))
            b = ctk.CTkButton(sb, text=label, image=self.nav_icons[(key, False)],
                              compound="top",
                              font=F(12), text_color=SIDEBAR_TXT,
                              fg_color="transparent", hover_color="#141d3a",
                              corner_radius=12, height=62, width=112,
                              command=lambda k=key: self._select_view(k))
            b.place(x=10, y=y)
            self.nav_btns[key] = b
            y += 74

        self.nav_dot = ctk.CTkLabel(sb, text="●  Offline", font=F(11),
                                    text_color=GREY_INK, fg_color="transparent")
        self.nav_dot.place(x=0, rely=1.0, y=-46, relwidth=1)
        ctk.CTkLabel(sb, text=f"v{__version__}", font=F(11),
                     text_color="#5a6890", fg_color="transparent").place(
            x=0, rely=1.0, y=-26, relwidth=1)

    def _nav_active(self, key):
        for k, b in self.nav_btns.items():
            on = (k == key)
            b.configure(fg_color="#1a2650" if on else "transparent",
                        text_color=SIDEBAR_ACT if on else SIDEBAR_TXT,
                        image=self.nav_icons[(k, on)])

    # ================================================================== views
    def _build_views(self):
        self.views = {}
        for v in ("connect", "logs", "settings"):
            f = ctk.CTkFrame(self, corner_radius=0, fg_color=WHITE)
            f.grid(row=0, column=1, sticky="nsew")
            self.views[v] = f
        self._build_connect_view(self.views["connect"])
        self._build_logs_view(self.views["logs"])
        self._build_settings_view(self.views["settings"])

    def _select_view(self, key):
        for k, f in self.views.items():
            f.grid_remove() if k != key else f.grid()
        self._nav_active(key)

    # ---------------------------------------------------------- connect view
    def _build_connect_view(self, view):
        view.configure(fg_color=MAIN_MID)
        W, H = 848, 620
        grad = _gradient(W, H, _hexrgb(MAIN_TOP), _hexrgb(MAIN_BOT))
        self._bg_img = ctk.CTkImage(light_image=grad, dark_image=grad, size=(W, H))
        bg = ctk.CTkLabel(view, image=self._bg_img, text="")
        bg.place(x=0, y=0)

        # ---- power button
        self.btn_connect = ctk.CTkButton(
            view, text="", width=216, height=216, corner_radius=108,
            fg_color=WHITE, hover_color="#eef3ff",
            border_width=0, command=self._on_connect_toggle)
        self.btn_connect.place(relx=0.5, x=-108, y=64)
        self.pw = ctk.CTkCanvas(self.btn_connect, width=84, height=84,
                                bg=WHITE, highlightthickness=0)
        self.pw.arc = self.pw.create_arc(10, 16, 74, 80, start=310, extent=280,
                                         style="arc", outline="#9aa7b8", width=8)
        self.pw.line = self.pw.create_line(42, 2, 42, 36, fill="#9aa7b8", width=8,
                                           capstyle="round")
        self.pw.place(relx=0.5, rely=0.5, anchor="center")

        self.lbl_status = ctk.CTkLabel(view, text="Not Connected",
                                       font=F(26, "bold"), text_color=WHITE)
        self.lbl_status.place(relx=0.5, rely=0, y=308, anchor="n")
        self.lbl_sub = ctk.CTkLabel(view, text="Your real IP is exposed",
                                    font=F(14), text_color="#cfe0ff")
        self.lbl_sub.place(relx=0.5, rely=0, y=346, anchor="n")

        # ---- location pill
        self.btn_loc = ctk.CTkButton(view, text="  Optimal (auto)  ",
                                     font=F(14, "bold"), text_color=WHITE,
                                     fg_color=PILL, hover_color=PILL_HOVER,
                                     corner_radius=20, height=40,
                                     command=self._toggle_locations)
        self.btn_loc.place(relx=0.5, rely=0, y=396, anchor="n")

        # ---- bottom info bar
        bar = ctk.CTkFrame(view, fg_color="transparent")
        bar.place(x=0, rely=1.0, y=-126, relwidth=1)
        bar.grid_columnconfigure(0, weight=1)

        self.lbl_ip_title = ctk.CTkLabel(bar, text="YOUR IP", font=F(11, "bold"),
                                         text_color="#bcd0ff",
                                         fg_color="transparent")
        self.lbl_ip_title.grid(row=0, column=0, sticky="w", padx=26)
        iprow = ctk.CTkFrame(bar, fg_color="transparent")
        iprow.grid(row=1, column=0, sticky="ew", padx=20, pady=(0, 0))
        self.lbl_ip = ctk.CTkLabel(iprow, text="…", font=ctk.CTkFont(family=MONO,
                                                                   size=22,
                                                                   weight="bold"),
                                   text_color=WHITE, fg_color="transparent")
        self.lbl_ip.grid(row=0, column=0, sticky="w", padx=6)
        self.btn_copy = ctk.CTkButton(iprow, text="COPY", width=64, height=28,
                                      font=F(11, "bold"), text_color=WHITE,
                                      fg_color=PILL, hover_color=PILL_HOVER,
                                      corner_radius=14,
                                      command=self._copy_ip)
        self.btn_copy.grid(row=0, column=1, padx=(14, 0))
        self.lbl_timer = ctk.CTkLabel(iprow, text="", font=ctk.CTkFont(family=MONO,
                                                                      size=16,
                                                                      weight="bold"),
                                      text_color="#cfe0ff", fg_color="transparent")
        self.lbl_timer.grid(row=0, column=2, sticky="e", padx=(20, 6))
        iprow.grid_columnconfigure(3, weight=1)
        self.lbl_stats = ctk.CTkLabel(iprow, text="", font=ctk.CTkFont(family=MONO,
                                                                      size=13),
                                      text_color="#bcd0ff", fg_color="transparent")
        self.lbl_stats.grid(row=1, column=0, columnspan=4, sticky="w", padx=6,
                            pady=(6, 0))

        self.lbl_proto = ctk.CTkLabel(bar, text="SOCKS5 :1080   ·   HTTP :8080",
                                      font=F(11), text_color="#9dbcf8",
                                      fg_color="transparent")
        self.lbl_proto.grid(row=2, column=0, sticky="w", padx=26, pady=(10, 14))

        self._build_loc_panel(view)

    # ----------------------------------------------------- location panel
    def _build_loc_panel(self, view):
        self.loc_panel = ctk.CTkFrame(view, width=300, corner_radius=0,
                                      fg_color=WHITE)
        self.loc_panel.grid_propagate(False)
        ctk.CTkLabel(self.loc_panel, text="Choose location",
                     font=F(15, "bold"), text_color=INK,
                     fg_color="transparent").place(x=20, y=18)
        self.e_search = ctk.CTkEntry(self.loc_panel, placeholder_text="Search…",
                                     width=260, height=36, corner_radius=10,
                                     fg_color=CARD_LIGHT, border_width=0,
                                     text_color=INK, font=F(13))
        self.e_search.place(x=20, y=54)
        self.e_search.bind("<KeyRelease>", lambda _e: self._filter_locations())
        self.loc_list = ctk.CTkScrollableFrame(self.loc_panel, width=284, height=520,
                                               fg_color="transparent")
        self.loc_list.place(x=0, y=100)
        self._loc_open = False

    def _toggle_locations(self):
        if self._loc_open:
            self._slide_panel(0, -1)
        else:
            self.loc_panel.place(relx=1.0, x=0, y=0, relheight=1.0)
            self._loc_open = True
            self._slide_panel(300, 1)

    def _slide_panel(self, step, direction):
        if not self._loc_open:
            return
        self.loc_panel.place_configure(x=step)
        nxt = step - 30 * direction
        if 0 <= nxt <= 300:
            self.after(12, lambda: self._slide_panel(nxt, direction))
        elif direction < 0:
            self.loc_panel.place_forget()
            self._loc_open = False

    def _fill_loc_rows(self, rows):
        for w in self.loc_list.winfo_children():
            w.destroy()
        self.row_order = []
        for label, code, city in rows:
            self.row_order.append(label)
            row = ctk.CTkButton(self.loc_list, text=f"  {label}   ·   {code}",
                                anchor="w", height=48, corner_radius=10,
                                font=F(13), text_color=INK,
                                fg_color=CARD_LIGHT, hover_color="#e6edff",
                                command=lambda l=label: self._pick_location(l))
            row.pack(fill="x", padx=8, pady=4)
        self._filter_locations()

    def _filter_locations(self):
        q = (self.e_search.get() or "").lower()
        for w in self.loc_list.winfo_children():
            try:
                w.grid() if q in w.cget("text").lower() else w.grid_remove()
            except Exception:
                pass

    def _pick_location(self, label):
        self.cfg["last_region"] = self.loc_map.get(label, "")
        cfgmod.save(self.cfg)
        self.btn_loc.configure(text=f"  {label}  ")
        if self._loc_open:
            self._slide_panel(0, -1)
        if self.phase in ("connected", "connecting"):
            self._log_line(f"Region changed to '{label}' - reconnect to apply.",
                           "warn")

    # -------------------------------------------------------------- logs view
    def _build_logs_view(self, view):
        view.configure(fg_color="#f2f5fb")
        ctk.CTkLabel(view, text="Activity log", font=F(20, "bold"), text_color=INK,
                     fg_color="transparent").place(x=26, y=22)
        ctk.CTkButton(view, text="Clear", width=84, height=32, corner_radius=10,
                      fg_color="#e3e9f4", hover_color="#d5def0", text_color=INK,
                      font=F(12, "bold"),
                      command=lambda: self.txt_logs.delete("1.0", "end")
                      ).place(relx=1.0, x=-96 - 92, y=24)
        ctk.CTkButton(view, text="Save as...", width=88, height=32, corner_radius=10,
                      fg_color=MAIN_TOP, hover_color=MAIN_BOT, text_color=WHITE,
                      font=F(12, "bold"), command=self._save_logs
                      ).place(relx=1.0, x=-96, y=24)
        self.txt_logs = ctk.CTkTextbox(view, font=ctk.CTkFont(family=MONO, size=13),
                                       fg_color=LOG_BG, text_color="#c9d6f2",
                                       corner_radius=14, border_width=0, wrap="word",
                                       width=796, height=520)
        self.txt_logs.place(x=26, y=70)
        for tag, color in LOG_COLORS.items():
            try:
                self.txt_logs.tag_config(tag, foreground=color)
            except Exception:
                pass
        self._log_line("VPeeN ready. Waiting for action...", "info")

    # --------------------------------------------------------- settings view
    def _build_settings_view(self, view):
        view.configure(fg_color="#f2f5fb")
        ctk.CTkLabel(view, text="Settings", font=F(20, "bold"), text_color=INK,
                     fg_color="transparent").place(x=26, y=22)

        def card(y, h, title):
            c = ctk.CTkFrame(view, width=796, height=h, corner_radius=14,
                             fg_color=WHITE)
            c.place(x=26, y=y)
            c.pack_propagate(False)
            ctk.CTkLabel(c, text=title, font=F(11, "bold"), text_color=GREY_INK,
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

        c2 = card(246, 132, "BEHAVIOUR")
        self.sw_auto_sys = self._sw(c2, "Set system proxy on connect",
                                    self.cfg.get("auto_system_proxy"), 50)
        self.sw_autoconn = self._sw(c2, "Auto-connect on launch",
                                    self.cfg.get("auto_connect"), 92)

        c3 = card(392, 168, "SESSION")
        ctk.CTkButton(c3, text="Save settings", height=38, width=756, corner_radius=10,
                      fg_color=MAIN_TOP, hover_color=MAIN_BOT, text_color=WHITE,
                      font=F(13, "bold"), command=self._save_settings
                      ).place(x=20, y=44)
        ctk.CTkButton(c3, text="Reset VeePN session  (clear cached token)",
                      height=38, width=756, corner_radius=10, fg_color="#eef1f8",
                      hover_color="#ffe3e3", text_color=INK, font=F(13),
                      command=self._reset_session).place(x=20, y=92)

        ctk.CTkLabel(view, text=f"{__app_name__} v{__version__}  ·  "
                                f"github.com/SirBNL/VPeeN  ·  MIT License",
                     font=F(11), text_color=GREY_INK,
                     fg_color="transparent").place(x=26, rely=1.0, y=-28)

    def _sw(self, card, text, on, y):
        sw = ctk.CTkSwitch(card, text=text, progress_color=ACCENT,
                           button_color=WHITE, fg_color="#dfe6f2",
                           text_color=INK, font=F(13))
        if on:
            sw.select()
        sw.place(x=20, y=y)
        return sw

    # ================================================================ actions
    def _bootstrap(self):
        spawn_quick_task(self.core.events, "locations", insecure=False)
        spawn_quick_task(self.core.events, "direct_ip", insecure=False)
        self._log_line("Fetching locations and your real IP...", "info")
        if self.cfg.get("auto_connect"):
            self.after(1200, self._demo_connect)

    def _demo_connect(self):
        if self.phase == "disconnected":
            self._on_connect_toggle()

    def _selected_region(self):
        return self.loc_map.get(self.btn_loc.cget("text").strip())

    def _on_connect_toggle(self):
        if self.phase == "connecting":
            return
        if self.phase in ("connected", "connecting"):
            self.core.stop()
            self._log_line("Disconnecting...", "info")
            return
        if self.core.is_busy():
            return
        try:
            socks_port = int(self.e_socks.get())
            http_port = int(self.e_http.get())
            if not (0 < socks_port < 65536 and 0 < http_port < 65536):
                raise ValueError
            bind = self.e_bind.get().strip() or "127.0.0.1"
        except ValueError:
            self._log_line("Invalid ports - use numbers between 1 and 65535.", "err")
            return
        set_system = bool(self.sw_auto_sys.get())
        self.cfg["auto_system_proxy"] = set_system
        cfgmod.save(self.cfg)
        region = self._selected_region()
        self._log_line(f"Connecting to '{self.btn_loc.cget('text').strip()}' "
                       f"(SOCKS5 :{socks_port} - HTTP :{http_port})...", "info")
        self.core.start(region, bind, socks_port, http_port, set_system)
        self._set_phase("connecting")

    def _copy_ip(self):
        ip = self.lbl_ip.cget("text")
        if ip and ip not in ("…", "-"):
            self.clipboard_clear()
            self.clipboard_append(ip)
            self.btn_copy.configure(text="COPIED")
            self.after(1200, lambda: self.btn_copy.configure(text="COPY"))

    def _save_settings(self):
        try:
            socks_port = int(self.e_socks.get())
            http_port = int(self.e_http.get())
            if not (0 < socks_port < 65536 and 0 < http_port < 65536):
                raise ValueError
        except ValueError:
            self._log_line("Invalid ports - settings not saved.", "err")
            return
        self.cfg.update({
            "bind": self.e_bind.get().strip() or "127.0.0.1",
            "socks_port": socks_port,
            "http_port": http_port,
            "auto_system_proxy": bool(self.sw_auto_sys.get()),
            "auto_connect": bool(self.sw_autoconn.get()),
        })
        cfgmod.save(self.cfg)
        self.lbl_proto.configure(text=f"SOCKS5 :{socks_port}   ·   HTTP :{http_port}")
        self._log_line("Settings saved.", "ok")

    def _reset_session(self):
        from tkinter import messagebox
        if messagebox.askyesno("Reset session",
                               "Delete the cached VeePN token?\n"
                               "A fresh anonymous token is fetched on next connect."):
            try:
                from .cli import DEFAULT_STATE_PATH
                os.remove(DEFAULT_STATE_PATH)
            except OSError:
                pass
            self._log_line("VeePN session cleared.", "ok")

    def _save_logs(self):
        from tkinter import filedialog
        path = filedialog.asksaveasfilename(defaultextension=".txt",
                                            initialfile="vpeen-log.txt",
                                            filetypes=[("Text", "*.txt")])
        if path:
            with open(path, "w", encoding="utf-8") as f:
                f.write(self.txt_logs.get("1.0", "end"))
            self._log_line(f"Log saved to {path}", "ok")

    # =============================================================== UI state
    def _set_phase(self, phase, detail=None):
        self.phase = phase
        if phase == "connecting":
            self.lbl_status.configure(text="Connecting...")
            self.lbl_sub.configure(text="Negotiating a secure tunnel")
            self.btn_loc.configure(state="disabled")
            self.nav_dot.configure(text="●  Connecting", text_color=WARN)
            self._start_pulse()
            self._start_scramble()
        elif phase == "connected":
            region = detail or self.btn_loc.cget("text").strip()
            self._stop_pulse(connected=True)
            self._stop_scramble(final=self.exit_ip)
            self.lbl_status.configure(text="Protected")
            self.lbl_sub.configure(text=f"Tunnel active - {region}")
            self.btn_loc.configure(state="normal")
            self.nav_dot.configure(text="●  Protected", text_color=ACCENT)
            self.lbl_ip_title.configure(text="EXIT IP (VPN)")
            self.t_connect = time.time()
        elif phase == "disconnected":
            self._stop_pulse(connected=False)
            self._stop_scramble()
            self.lbl_status.configure(text="Not Connected")
            self.lbl_sub.configure(text="Your real IP is exposed")
            self.btn_loc.configure(state="normal")
            self.nav_dot.configure(text="●  Offline", text_color=GREY_INK)
            self.lbl_ip.configure(text=self.direct_ip or "-")
            self.lbl_ip_title.configure(text="YOUR IP")
            self.lbl_timer.configure(text="")
            self.lbl_stats.configure("")
            self.lbl_timer.configure(text="")
            self.lbl_stats.configure(text="")
        elif phase == "error":
            self._stop_pulse(connected=False)
            self._stop_scramble()
            self.lbl_status.configure(text="Something went wrong")
            self.lbl_sub.configure(text=str(detail or "Unknown error"))
            self.nav_dot.configure(text="●  Error", text_color=RED)
            self.lbl_ip.configure(text=self.direct_ip or "-")
            self.lbl_ip_title.configure(text="YOUR IP")

    # ---- pulse animation (power ring breathing)
    def _start_pulse(self):
        self._stop_pulse(connected=False)
        self._pulse_t0 = time.time()
        self._pulse_step()

    def _pulse_step(self):
        if self.phase not in ("connecting", "connected"):
            return
        t = time.time() - self._pulse_t0
        k = (1 - __import__("math").cos(t * 2 * 3.14159 / 1.6)) / 2  # 1.6s cycle
        if self.phase == "connecting":
            base, glow, target = "#9aa7b8", MAIN_TOP, WARN
        else:
            base, glow, target = ACCENT, "#7cffd9", ACCENT
        col = _lerp_color(base, glow, k)
        self.pw.itemconfig(self.pw.arc, outline=col)
        self.pw.itemconfig(self.pw.line, fill=col)
        self._pulse_job = self.after(40, self._pulse_step)

    def _stop_pulse(self, connected):
        if self._pulse_job:
            self.after_cancel(self._pulse_job)
            self._pulse_job = None
        col = ACCENT if connected else "#9aa7b8"
        self.pw.itemconfig(self.pw.arc, outline=col)
        self.pw.itemconfig(self.pw.line, fill=col)

    # ---- IP scramble animation (number roll while connecting)
    def _fake_ip(self, locked, target):
        parts = []
        for i in range(4):
            if i < locked and target:
                parts.append(target.split(".")[i])
            else:
                parts.append(str(random.randint(1, 255)))
        return ".".join(parts)

    def _start_scramble(self):
        self._scramble_tick = 0
        self._scramble_target = None
        self.lbl_ip_title.configure(text="NEW IP")
        self.lbl_ip.configure(text="...")
        self._scramble_job = self.after(50, self._scramble_step)

    def _scramble_step(self):
        self._scramble_tick += 1
        if self._scramble_target:
            locked = min(4, self._scramble_tick // 10)
            self.lbl_ip.configure(text=self._fake_ip(locked, self._scramble_target))
            if locked >= 4:
                self.lbl_ip.configure(text=self._scramble_target)
                self._scramble_job = None
                return
        else:
            self.lbl_ip.configure(text=self._fake_ip(0, None))
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
            self.lbl_ip.configure(text=self.direct_ip or "-")

    # ----------------------------------------------------------------- ticks
    def _tick(self):
        if self.phase == "connected" and self.t_connect:
            secs = int(time.time() - self.t_connect)
            self.lbl_timer.configure(text=f"{secs // 3600:02d}:{secs % 3600 // 60:02d}:"
                                          f"{secs % 60:02d}")
        self.after(1000, self._tick)

    def _log_line(self, line, level="info"):
        ts = time.strftime("%H:%M:%S")
        tag = level if level in LOG_COLORS else "info"
        try:
            self.txt_logs.insert("end", f"[{ts}] {line}\n", tag)
        except Exception:
            self.txt_logs.insert("end", f"[{ts}] {line}\n")
        self.txt_logs.see("end")

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
        elif kind == "phase":
            self._set_phase(ev.get("phase"), ev.get("region") or ev.get("detail"))
        elif kind == "exit_ip":
            self.exit_ip = ev.get("ip")
            if self.phase == "connecting":
                self._scramble_target = self.exit_ip
        elif kind == "stats":
            self.lbl_stats.configure(
                text=f"TX {ev['up'] // 1024} KiB    RX {ev['down'] // 1024} KiB    "
                     f"·    {ev['conns']} connections ({ev['active']} active)")
        elif kind == "direct_ip":
            if ev.get("data"):
                self.direct_ip = ev["data"]
                if self.phase == "disconnected":
                    self.lbl_ip.configure(text=self.direct_ip)
                self._log_line(f"Your real IP: {self.direct_ip}", "ok")
        elif kind == "locations":
            self._fill_locations(ev)

    def _fill_locations(self, ev):
        data = ev.get("data")
        if not data:
            self._log_line(f"Could not fetch locations: {ev.get('error')}", "warn")
            return
        self.loc_map = {OPTIMAL: None}
        rows = [(OPTIMAL, "auto", "best server for you")]
        for l in sorted(data, key=lambda x: x.get("region", "")):
            label = f"{l.get('name', '?')}"
            key = f"{label} [{l.get('region', '?')}]"
            self.loc_map[key] = l.get("region")
            rows.append((label, l.get("countryCode", ""), l.get("region", "")))
        self._fill_loc_rows(rows)
        last = self.cfg.get("last_region", "")
        shown = OPTIMAL
        for lab, code in self.loc_map.items():
            if code == last:
                shown = lab
                break
        self.btn_loc.configure(text=f"  {shown}  ")
        self._log_line(f"{len(data)} free locations loaded.", "ok")

    # ----------------------------------------------------------------- close
    def _on_close(self):
        if self.core.is_busy():
            self.core.stop()
            self.after(400, self.destroy)
        else:
            self.destroy()


def _hexrgb(h):
    return tuple(int(h[i:i + 2], 16) for i in (1, 3, 5))


def run() -> int:
    app = VPeeNApp()
    app.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(run())
