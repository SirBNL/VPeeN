"""
VPeeN GUI - modern VPN-style interface built with CustomTkinter.

Tabs:
  * Connect  - location picker, big connect button, IP cards, timer, system proxy
  * Logs     - full color-coded activity log
  * Settings - ports, theme, behaviour, session management
"""
import os
import queue
import sys
import time

import customtkinter as ctk
from PIL import Image

from . import __app_name__, __version__
from . import settings as cfgmod
from .core import (Core, PHASE_CONNECTED, PHASE_CONNECTING, spawn_quick_task)

# ---------------------------------------------------------------- palette
BG = "#0e1526"
CARD = "#141d33"
CARD2 = "#182340"
BORDER = "#22304d"
ACCENT = "#00d68f"
ACCENT_HOVER = "#00b377"
RED = "#ff5c5c"
RED_HOVER = "#e04848"
AMBER = "#ffb020"
TEXT = "#eaf1fb"
GREY = "#8ea0bd"

LOG_COLORS = {"info": "#8ea0bd", "ok": "#00d68f", "warn": "#ffb020", "err": "#ff5c5c"}

MONO = "Consolas" if os.name == "nt" else ("Menlo" if sys.platform == "darwin"
                                           else "DejaVu Sans Mono")
UI_FONT = ("Segoe UI" if os.name == "nt"
           else "Helvetica Neue" if sys.platform == "darwin" else "DejaVu Sans")


def F(size, weight="normal"):
    """Uniform UI font across platforms (Tk falls back badly without this)."""
    return ctk.CTkFont(family=UI_FONT, size=size, weight=weight)


OPTIMAL = "Optimal (auto)"


def _asset(name: str) -> str | None:
    """Locate an asset both in dev tree and inside PyInstaller bundles."""
    roots = [getattr(sys, "_MEIPASS", None),
             os.path.dirname(os.path.dirname(os.path.abspath(__file__)))]
    for root in roots:
        if root:
            p = os.path.join(root, "assets", name)
            if os.path.exists(p):
                return p
    return None


class VPeeNApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.cfg = cfgmod.load()
        ctk.set_widget_scaling(1.0)
        ctk.set_window_scaling(1.0)
        ctk.set_appearance_mode(self.cfg.get("theme", "Dark"))

        self.title(f"{__app_name__} v{__version__}")
        self.geometry("940x660")
        self.minsize(880, 620)
        self.configure(fg_color=BG)

        self.core = Core(insecure=False)
        self.phase = "disconnected"
        self.t_connect = None
        self.direct_ip = None
        self.exit_ip = None
        self.loc_map: dict[str, str | None] = {}
        self.locations_loaded = False

        self._build_header()
        self._build_tabs()
        self._load_icon()

        self.after(120, self._poll_events)
        self.after(500, self._bootstrap)
        self.after(1000, self._tick)

        if os.environ.get("VPeeN_DEMO") == "1":
            self.after(2500, self._demo_connect)
        if os.environ.get("VPeeN_DEMO_TABS") == "1":
            # automated tour for documentation screenshots
            self.after(19000, lambda: self.tabs.set("Logs"))
            self.after(26000, lambda: self.tabs.set("Settings"))
            self.after(32000, lambda: self.tabs.set("Connect"))

        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------- visuals
    def _load_icon(self):
        ico = _asset("icon.ico")
        png = _asset("icon.png")
        if os.name == "nt" and ico:
            try:
                self.iconbitmap(ico)
                return
            except Exception:
                pass
        if png:
            try:
                self.iconphoto(True, self._tk_photo(64))
            except Exception:
                pass

    def _tk_photo(self, size: int):
        from PIL import ImageTk
        img = Image.open(_asset("icon.png")).resize((size, size), Image.LANCZOS)
        self._tkicon = ImageTk.PhotoImage(img, master=self)
        return self._tkicon

    def _build_header(self):
        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.grid(row=0, column=0, sticky="ew", padx=18, pady=(14, 4))
        bar.grid_columnconfigure(1, weight=1)

        logo_path = _asset("logo.png")
        if logo_path:
            img = Image.open(logo_path)
            self._logo = ctk.CTkImage(light_image=img, dark_image=img, size=(42, 42))
            ctk.CTkLabel(bar, image=self._logo, text="").grid(row=0, column=0, padx=(0, 10))

        ctk.CTkLabel(bar, text=f"{__app_name__}", font=F(26, "bold"),
                     text_color=TEXT).grid(row=0, column=1, sticky="w")
        ctk.CTkLabel(bar, text=f"v{__version__}", font=F(13),
                     text_color=GREY).grid(row=0, column=1, sticky="w", padx=(110, 0),
                                           pady=(10, 0))

        self.pill = ctk.CTkLabel(bar, text="●  Disconnected",
                                 font=F(13, "bold"),
                                 text_color=GREY, fg_color=CARD, corner_radius=14,
                                 padx=14, pady=6)
        self.pill.grid(row=0, column=2, sticky="e")

    def _build_tabs(self):
        self.tabs = ctk.CTkTabview(self, fg_color=BG, segmented_button_fg_color=CARD,
                                   segmented_button_selected_color=CARD2,
                                   segmented_button_selected_hover_color=CARD2,
                                   text_color=TEXT)
        self.tabs.grid(row=1, column=0, sticky="nsew", padx=18, pady=(6, 14))
        self.grid_rowconfigure(1, weight=1)
        self.grid_columnconfigure(0, weight=1)
        for t in ("Connect", "Logs", "Settings"):
            self.tabs.add(t)
        self._build_connect_tab(self.tabs.tab("Connect"))
        self._build_logs_tab(self.tabs.tab("Logs"))
        self._build_settings_tab(self.tabs.tab("Settings"))

    # ----------------------------------------------------------- connect tab
    def _build_connect_tab(self, tab):
        tab.grid_columnconfigure(0, weight=1)

        # --- location card
        loc = ctk.CTkFrame(tab, fg_color=CARD, corner_radius=16, border_width=1,
                           border_color=BORDER)
        loc.grid(row=0, column=0, sticky="ew", padx=8, pady=(10, 0))
        ctk.CTkLabel(loc, text="LOCATION", font=F(12, "bold"),
                     text_color=GREY).grid(row=0, column=0, sticky="w", padx=18, pady=(12, 0))
        self.menu_loc = ctk.CTkOptionMenu(
            loc, values=["Loading..."], width=320, height=36,
            fg_color=CARD2, button_color=CARD2, button_hover_color=BORDER,
            text_color=TEXT, font=F(14),
            dropdown_fg_color=CARD2, dropdown_text_color=TEXT,
            dropdown_hover_color=BORDER, command=self._on_region)
        self.menu_loc.set("Loading...")
        self.menu_loc.grid(row=1, column=0, sticky="w", padx=18, pady=(4, 14))
        ctk.CTkButton(loc, text="Refresh", width=110, height=36, fg_color=CARD2,
                      hover_color=BORDER, text_color=TEXT,
                      command=self._refresh_locations).grid(row=1, column=1, padx=(6, 18),
                                                            pady=(4, 14))
        loc.grid_columnconfigure(2, weight=1)

        # --- big connect button
        mid = ctk.CTkFrame(tab, fg_color="transparent")
        mid.grid(row=1, column=0, pady=(26, 18))
        mid.grid_rowconfigure(0, weight=1)
        mid.grid_columnconfigure(0, weight=1)
        self.btn_connect = ctk.CTkButton(
            mid, text="", width=210, height=210, corner_radius=105,
            fg_color=ACCENT,
            hover_color=ACCENT_HOVER,
            command=self._on_connect_toggle)
        self.btn_connect.grid(row=0, column=0)
        self.btn_text = ctk.CTkLabel(mid, text="CONNECT", font=F(17, "bold"),
                                     text_color="#06251b", fg_color=ACCENT)
        # float the caption over the circular button (CTk grows buttons with text)
        self.btn_text.place(in_=self.btn_connect, relx=0.5, rely=0.5, anchor="center")
        self.btn_text.bind("<Button-1>", lambda _e: self._on_connect_toggle())
        self.lbl_status = ctk.CTkLabel(mid, text="Not connected",
                                       font=F(15), text_color=GREY)
        self.lbl_status.grid(row=1, column=0, pady=(16, 0))

        # --- info cards
        cards = ctk.CTkFrame(tab, fg_color="transparent")
        cards.grid(row=2, column=0, sticky="ew", padx=8)
        for i in range(3):
            cards.grid_columnconfigure(i, weight=1, uniform="c")

        self.card_real = self._info_card(cards, 0, "REAL IP", "...")
        self.card_exit = self._info_card(cards, 1, "EXIT IP (VPN)", "—")
        self.card_time = self._info_card(cards, 2, "DURATION", "00:00:00")

        # --- bottom bar
        bar = ctk.CTkFrame(tab, fg_color="transparent")
        bar.grid(row=3, column=0, sticky="ew", padx=16, pady=(16, 14))
        bar.grid_columnconfigure(1, weight=1)
        self.sw_system = ctk.CTkSwitch(
            bar, text="System-wide proxy", command=self._on_system_switch,
            progress_color=ACCENT, button_color=TEXT, fg_color=CARD2,
            font=F(13), text_color=TEXT)
        self.sw_system.select() if self.cfg.get("auto_system_proxy") else self.sw_system.deselect()
        self.sw_system.grid(row=0, column=0, sticky="w")
        self.lbl_stats = ctk.CTkLabel(bar, text="TX 0 KiB    RX 0 KiB    ·   0 connections",
                                      font=ctk.CTkFont(family=MONO, size=13),
                                      text_color=GREY)
        self.lbl_stats.grid(row=0, column=2, sticky="e")

    def _info_card(self, parent, col, title, value):
        card = ctk.CTkFrame(parent, fg_color=CARD, corner_radius=16, border_width=1,
                            border_color=BORDER)
        card.grid(row=0, column=col, sticky="ew", padx=8)
        ctk.CTkLabel(card, text=title, font=F(12, "bold"),
                     text_color=GREY).pack(anchor="w", padx=18, pady=(14, 0))
        lbl = ctk.CTkLabel(card, text=value,
                           font=ctk.CTkFont(family=MONO, size=17, weight="bold"),
                           text_color=TEXT)
        lbl.pack(anchor="w", padx=18, pady=(2, 16))
        return lbl

    # -------------------------------------------------------------- logs tab
    def _build_logs_tab(self, tab):
        tab.grid_rowconfigure(1, weight=1)
        tab.grid_columnconfigure(0, weight=1)

        bar = ctk.CTkFrame(tab, fg_color="transparent")
        bar.grid(row=0, column=0, sticky="ew", pady=(10, 6))
        ctk.CTkButton(bar, text="Clear", width=90, height=30, fg_color=CARD2,
                      hover_color=BORDER, text_color=TEXT,
                      command=lambda: self.txt_logs.delete("1.0", "end")
                      ).pack(side="right", padx=(8, 0))
        ctk.CTkButton(bar, text="Save as...", width=110, height=30, fg_color=CARD2,
                      hover_color=BORDER, text_color=TEXT,
                      command=self._save_logs).pack(side="right")
        ctk.CTkLabel(bar, text="ACTIVITY LOG", font=F(12, "bold"),
                     text_color=GREY).pack(side="left")

        self.txt_logs = ctk.CTkTextbox(tab, font=ctk.CTkFont(family=MONO, size=13),
                                       fg_color=CARD, text_color=TEXT, corner_radius=16,
                                       border_width=1, border_color=BORDER, wrap="word")
        self.txt_logs.grid(row=1, column=0, sticky="nsew", pady=(0, 10))
        for tag, color in LOG_COLORS.items():
            try:
                self.txt_logs.tag_config(tag, foreground=color)
            except Exception:
                pass
        self._log_line("VPeeN ready. Waiting for action...", "info")

    # --------------------------------------------------------- settings tab
    def _build_settings_tab(self, tab):
        tab.grid_rowconfigure(0, weight=1)
        tab.grid_columnconfigure(0, weight=1)
        scroll = ctk.CTkScrollableFrame(tab, fg_color="transparent")
        scroll.grid(row=0, column=0, sticky="nsew")
        scroll.grid_columnconfigure(0, weight=1)

        card1 = self._settings_card(scroll, 0, "LOCAL PROXY")
        ctk.CTkLabel(card1, text="Bind address", text_color=GREY,
                     font=F(13)).grid(row=1, column=0, sticky="w",
                                                     padx=16, pady=8)
        self.e_bind = ctk.CTkEntry(card1, width=180, fg_color=CARD2, border_color=BORDER,
                                   text_color=TEXT)
        self.e_bind.insert(0, self.cfg.get("bind", "127.0.0.1"))
        self.e_bind.grid(row=1, column=1, sticky="e", padx=16, pady=8)
        ctk.CTkLabel(card1, text="SOCKS5 port", text_color=GREY,
                     font=F(13)).grid(row=2, column=0, sticky="w",
                                                     padx=16, pady=8)
        self.e_socks = ctk.CTkEntry(card1, width=180, fg_color=CARD2, border_color=BORDER,
                                    text_color=TEXT)
        self.e_socks.insert(0, str(self.cfg.get("socks_port", 1080)))
        self.e_socks.grid(row=2, column=1, sticky="e", padx=16, pady=8)
        ctk.CTkLabel(card1, text="HTTP port", text_color=GREY,
                     font=F(13)).grid(row=3, column=0, sticky="w",
                                                     padx=16, pady=8)
        self.e_http = ctk.CTkEntry(card1, width=180, fg_color=CARD2, border_color=BORDER,
                                   text_color=TEXT)
        self.e_http.insert(0, str(self.cfg.get("http_port", 8080)))
        self.e_http.grid(row=3, column=1, sticky="e", padx=16, pady=(8, 16))
        card1.grid_columnconfigure(1, weight=1)

        card2 = self._settings_card(scroll, 1, "BEHAVIOUR")
        ctk.CTkLabel(card2, text="Theme", text_color=GREY,
                     font=F(13)).grid(row=1, column=0, sticky="w",
                                                     padx=16, pady=8)
        self.menu_theme = ctk.CTkOptionMenu(card2, width=180, height=32, values=[
            "Dark", "Light", "System"], fg_color=CARD2, button_color=CARD2,
            button_hover_color=BORDER, text_color=TEXT,
            dropdown_fg_color=CARD2, dropdown_text_color=TEXT,
            dropdown_hover_color=BORDER,
            command=lambda _v: self._apply_theme())
        self.menu_theme.set(self.cfg.get("theme", "Dark"))
        self.menu_theme.grid(row=1, column=1, sticky="e", padx=16, pady=8)
        self.sw_auto_sys = self._settings_switch(card2, 2, "Set system proxy on connect",
                                                 self.cfg.get("auto_system_proxy"))
        self.sw_autoconn = self._settings_switch(card2, 3, "Auto-connect on launch",
                                                 self.cfg.get("auto_connect"))

        card3 = self._settings_card(scroll, 2, "SESSION")
        ctk.CTkButton(card3, text="Save settings", height=36, fg_color=ACCENT,
                      hover_color=ACCENT_HOVER, text_color="#06251b",
                      font=F(13, "bold"),
                      command=self._save_settings).grid(row=1, column=0, sticky="ew",
                                                        padx=16, pady=(4, 8))
        ctk.CTkButton(card3, text="Reset VeePN session  (clear cached token)",
                      height=36, fg_color=CARD2, hover_color=RED, text_color=TEXT,
                      command=self._reset_session).grid(row=2, column=0, sticky="ew",
                                                        padx=16, pady=8)
        ctk.CTkButton(card3, text="Open config folder", height=36, fg_color=CARD2,
                      hover_color=BORDER, text_color=TEXT,
                      command=self._open_cfg).grid(row=3, column=0, sticky="ew",
                                                   padx=16, pady=(8, 16))
        ctk.CTkLabel(scroll, text=f"{__app_name__} v{__version__}  ·  "
                                  f"github.com/SirBNL/VPeeN  ·  MIT License",
                     font=F(12), text_color=GREY).grid(row=3, column=0, sticky="ew",
                                                       pady=(14, 6))

    def _settings_card(self, parent, row, title):
        card = ctk.CTkFrame(parent, fg_color=CARD, corner_radius=16, border_width=1,
                            border_color=BORDER)
        card.grid(row=row, column=0, sticky="ew", padx=8, pady=(10, 0))
        card.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(card, text=title, font=F(12, "bold"),
                     text_color=GREY).grid(row=0, column=0, sticky="w", padx=16,
                                           pady=(12, 2))
        return card

    def _settings_switch(self, card, row, text, on: bool):
        sw = ctk.CTkSwitch(card, text=text, progress_color=ACCENT, button_color=TEXT,
                           fg_color=CARD2, text_color=TEXT, font=F(13))
        if on:
            sw.select()
        sw.grid(row=row, column=0, sticky="ew", padx=16, pady=8)
        return sw

    # ------------------------------------------------------------ behaviours
    def _bootstrap(self):
        spawn_quick_task(self.core.events, "locations", insecure=False)
        spawn_quick_task(self.core.events, "direct_ip", insecure=False)
        self._log_line("Fetching locations and your real IP...", "info")
        if self.cfg.get("auto_connect"):
            self.after(1200, self._demo_connect)

    def _refresh_locations(self):
        self.menu_loc.configure(values=["Loading..."])
        self.menu_loc.set("Loading...")
        spawn_quick_task(self.core.events, "locations", insecure=False)

    def _apply_theme(self):
        mode = self.menu_theme.get()
        ctk.set_appearance_mode(mode)
        self.cfg["theme"] = mode
        cfgmod.save(self.cfg)

    def _on_region(self, label):
        self.cfg["last_region"] = self.loc_map.get(label, "")
        cfgmod.save(self.cfg)

    def _selected_region(self) -> str | None:
        return self.loc_map.get(self.menu_loc.get())

    def _demo_connect(self):
        if self.phase == "disconnected":
            self._on_connect_toggle()

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
        region_label = self.menu_loc.get()
        self._log_line(f"Connecting to '{region_label}' "
                       f"(SOCKS5 :{socks_port} · HTTP :{http_port})...", "info")
        self.core.start(region, bind, socks_port, http_port, set_system)
        self._set_phase("connecting")

    def _on_system_switch(self):
        self.cfg["auto_system_proxy"] = bool(self.sw_system.get())
        cfgmod.save(self.cfg)
        self.sw_auto_sys.select() if self.sw_system.get() else self.sw_auto_sys.deselect()

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
            "theme": self.menu_theme.get(),
            "auto_system_proxy": bool(self.sw_auto_sys.get()),
            "auto_connect": bool(self.sw_autoconn.get()),
        })
        cfgmod.save(self.cfg)
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

    def _open_cfg(self):
        from .settings import CONFIG_DIR
        try:
            if sys.platform == "win32":
                os.startfile(CONFIG_DIR)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                os.system(f'open "{CONFIG_DIR}" &')
            else:
                os.system(f'xdg-open "{CONFIG_DIR}" &')
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

    # ------------------------------------------------------------- UI state
    def _set_phase(self, phase, detail=None):
        self.phase = phase
        if phase == "connecting":
            self.btn_connect.configure(state="disabled", fg_color=AMBER,
                                       hover_color=AMBER)
            self.btn_text.configure(text="...", fg_color=AMBER)
            self.pill.configure(text="●  Connecting...", text_color=AMBER)
            self.lbl_status.configure(text="Handshaking with VeePN network...",
                                      text_color=AMBER)
            self.card_exit.configure(text="...")
        elif phase == "connected":
            region = detail or self.menu_loc.get()
            self.btn_connect.configure(state="normal", fg_color=RED,
                                       hover_color=RED_HOVER)
            self.btn_text.configure(text="DISCONNECT", fg_color=RED)
            self.pill.configure(text="●  Connected", text_color=ACCENT)
            self.lbl_status.configure(text=f"Tunnel active — {region}",
                                      text_color=ACCENT)
            self.t_connect = time.time()
        elif phase == "disconnected":
            self.btn_connect.configure(state="normal", fg_color=ACCENT,
                                       hover_color=ACCENT_HOVER)
            self.btn_text.configure(text="CONNECT", fg_color=ACCENT)
            self.pill.configure(text="●  Disconnected", text_color=GREY)
            self.lbl_status.configure(text="Not connected", text_color=GREY)
            self.t_connect = None
            self.exit_ip = None
            self.card_exit.configure(text="—")
            self.card_time.configure(text="00:00:00")
        elif phase == "error":
            self.btn_connect.configure(state="normal", fg_color=ACCENT,
                                       hover_color=ACCENT_HOVER)
            self.btn_text.configure(text="CONNECT", fg_color=ACCENT)
            self.pill.configure(text="●  Error", text_color=RED)
            self.lbl_status.configure(text=str(detail or "Unknown error"), text_color=RED)
            self.t_connect = None

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
            self.card_exit.configure(text=self.exit_ip or "—")
        elif kind == "stats":
            self.lbl_stats.configure(
                text=f"TX {ev['up'] // 1024} KiB    RX {ev['down'] // 1024} KiB    ·   "
                     f"{ev['conns']} connections ({ev['active']} active)")
        elif kind == "direct_ip":
            if ev.get("data"):
                self.direct_ip = ev["data"]
                self.card_real.configure(text=self.direct_ip)
                self._log_line(f"Your real IP: {self.direct_ip}", "ok")
            else:
                self.card_real.configure(text="unavailable")
                self._log_line(f"Could not fetch real IP: {ev.get('error')}", "warn")
        elif kind == "locations":
            self._fill_locations(ev)

    def _fill_locations(self, ev):
        data = ev.get("data")
        if not data:
            self._log_line(f"Could not fetch locations: {ev.get('error')}", "warn")
            self.menu_loc.configure(values=["unavailable"])
            self.menu_loc.set("unavailable")
            return
        self.loc_map = {OPTIMAL: None}
        for l in sorted(data, key=lambda x: x.get("region", "")):
            label = f"{l.get('name', '?')}  [{l.get('region', '?')}]"
            self.loc_map[label] = l.get("region")
        labels = list(self.loc_map.keys())
        self.menu_loc.configure(values=labels)
        last = self.cfg.get("last_region", "")
        chosen = OPTIMAL
        for lab, code in self.loc_map.items():
            if code == last:
                chosen = lab
                break
        self.menu_loc.set(chosen)
        self.locations_loaded = True
        self._log_line(f"{len(data)} free locations loaded.", "ok")

    # ----------------------------------------------------------------- ticks
    def _tick(self):
        if self.phase == "connected" and self.t_connect:
            secs = int(time.time() - self.t_connect)
            self.card_time.configure(text=f"{secs // 3600:02d}:{secs % 3600 // 60:02d}:"
                                          f"{secs % 60:02d}")
        self.after(1000, self._tick)

    # ----------------------------------------------------------------- close
    def _on_close(self):
        if self.core.is_busy():
            self.core.stop()
            self.after(400, self.destroy)
        else:
            self.destroy()


def run() -> int:
    app = VPeeNApp()
    app.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(run())
