"""
System-wide proxy configuration with automatic backup / restore.

* Windows : HKCU Internet Settings (ProxyEnable / ProxyServer / ProxyOverride)
            + WinINET refresh broadcast so changes apply immediately.
* Linux   : GNOME (gsettings) - best effort, falls back to printed instructions.
* macOS   : networksetup on the primary network service.

The previous values are stored in the state file before overriding, so
`system off` (or a clean Ctrl+C) always restores the original configuration.
"""
import os
import platform
import shutil

from .utils import C, err, info, ok, warn

IS_WINDOWS = os.name == "nt"
IS_MACOS = platform.system() == "Darwin"

WIN_REG_KEY = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
BYPASS_LIST = "<local>;localhost;127.*;10.*;172.16.*;172.17.*;172.18.*;172.19.*;" \
              "172.2?.*;172.30.*;172.31.*;192.168.*"


def _state_backup_key():
    return "system_proxy_backup"


# ------------------------------------------------------------------- windows
def _winreg_read(name):
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, WIN_REG_KEY, 0,
                            winreg.KEY_READ) as k:
            val, typ = winreg.QueryValueEx(k, name)
            return {"value": val, "type": typ}
    except OSError:
        return None


def _winreg_write(name, value, typ):
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, WIN_REG_KEY, 0,
                        winreg.KEY_SET_VALUE) as k:
        winreg.SetValueEx(k, name, 0, typ, value)


def _winreg_delete(name):
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, WIN_REG_KEY, 0,
                            winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, name)
    except OSError:
        pass


def _wininet_refresh():
    """Broadcast settings change so browsers pick it up immediately."""
    try:
        import ctypes
        wininet = ctypes.windll.wininet
        INTERNET_OPTION_SETTINGS_CHANGED = 39
        INTERNET_OPTION_REFRESH = 37
        wininet.InternetSetOptionW(None, INTERNET_OPTION_SETTINGS_CHANGED, None, 0)
        wininet.InternetSetOptionW(None, INTERNET_OPTION_REFRESH, None, 0)
    except Exception:
        pass


# -------------------------------------------------------------------- linux
def _mac_read_proxy(service: str, kind: str) -> dict:
    """Read one proxy slot of a service via networksetup (v1.3.0).

    kind is 'webproxy' or 'securewebproxy'.  Returns {enabled, host, port}
    parsed from output like:
        Enabled: Yes
        Server: 127.0.0.1
        Port: 6152
        Authenticated Proxy Enabled: 0
    Values are kept EXACTLY as reported so system_off() can restore them.
    """
    import subprocess
    out = {"enabled": "No", "host": "", "port": ""}
    try:
        r = subprocess.run(["networksetup", f"-get{kind}", service],
                           capture_output=True, text=True, timeout=15)
        for line in r.stdout.splitlines():
            low = line.strip().lower()
            if low.startswith("enabled:"):
                out["enabled"] = line.split(":", 1)[1].strip()
            elif low.startswith("server:"):
                out["host"] = line.split(":", 1)[1].strip()
            elif low.startswith("port:"):
                out["port"] = line.split(":", 1)[1].strip()
    except Exception:
        pass
    return out


def _mac_is_set(slot: dict) -> bool:
    """True when a backed-up proxy slot was actually pointing somewhere."""
    host = (slot or {}).get("host", "")
    port = str((slot or {}).get("port", ""))
    return bool(host) and host.lower() not in ("(null)", "-") and \
        port not in ("", "0")
def _gnome_available():
    # v1.2.4 fix: operator precedence - the old "A and B or C" returned a
    # truthy WAYLAND string even without gsettings, then every gsettings
    # call silently failed while system_on still claimed success.
    return shutil.which("gsettings") is not None and bool(
        os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def _gsettings(args):
    import subprocess
    try:
        subprocess.run(["gsettings"] + args, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        return True
    except Exception:
        return False


def _gnome_backup(state):
    import subprocess
    backup = {}
    for schema_key in [
        ("org.gnome.system.proxy", "mode"),
        ("org.gnome.system.proxy.http", "host"),
        ("org.gnome.system.proxy.http", "port"),
        ("org.gnome.system.proxy.https", "host"),
        ("org.gnome.system.proxy.https", "port"),
        ("org.gnome.system.proxy", "use-same-proxy"),
    ]:
        schema, key = schema_key
        try:
            r = subprocess.run(["gsettings", "get", schema, key],
                               capture_output=True, text=True, timeout=10)
            if r.returncode == 0:
                backup[f"{schema}:{key}"] = r.stdout.strip()
        except Exception:
            pass
    if backup:
        state.set(_state_backup_key(), backup)


def _gnome_restore(state):
    backup = state.get(_state_backup_key()) or {}
    if not backup:
        _gsettings(["set", "org.gnome.system.proxy", "mode", "none"])
        return True
    for spec, value in backup.items():
        schema, key = spec.split(":", 1)
        if value.startswith(("'", '"')):
            value = value.strip("'\"")
            args = ["set", schema, key, f"'{value}'"]
        else:
            args = ["set", schema, key, value]
        _gsettings(args)
    return True


# ------------------------------------------------------------------- public
def system_on(state, host="127.0.0.1", http_port=8080, socks_port=1080):
    """Point the OS at our local HTTP proxy (and mention SOCKS5 where possible)."""
    if IS_WINDOWS:
        import winreg
        prev = {
            "ProxyEnable": _winreg_read("ProxyEnable"),
            "ProxyServer": _winreg_read("ProxyServer"),
            "ProxyOverride": _winreg_read("ProxyOverride"),
        }
        state.set(_state_backup_key(), prev)
        _winreg_write("ProxyServer", f"{host}:{http_port}", winreg.REG_SZ)
        _winreg_write("ProxyOverride", BYPASS_LIST, winreg.REG_SZ)
        _winreg_write("ProxyEnable", 1, winreg.REG_DWORD)
        _wininet_refresh()
        ok(f"Windows system proxy -> {host}:{http_port} (HTTP & HTTPS via CONNECT)")
        info(f"SOCKS5 available separately at {host}:{socks_port}")
        return True

    if IS_MACOS:
        import subprocess
        try:
            svc = subprocess.run(
                ["networksetup", "-listallnetworkservices"],
                capture_output=True, text=True, timeout=10
            ).stdout.strip().splitlines()[1:]
            # v1.3.0: back up the FULL per-service proxy state (enabled,
            # host, port for web + secure-web) - the old backup stored only
            # the service NAMES, so system_off() blindly switched the HTTP
            # and HTTPS proxies off and a user's pre-existing custom proxy
            # configuration was silently destroyed instead of restored.
            detail = {s: {"web": _mac_read_proxy(s, "webproxy"),
                          "secure": _mac_read_proxy(s, "securewebproxy")}
                      for s in svc[:3]}
            state.set(_state_backup_key(), {"services": svc, "detail": detail})
            for s in svc[:3]:
                subprocess.run(["networksetup", "-setwebproxy", s, host, str(http_port)],
                               capture_output=True, timeout=15)
                subprocess.run(["networksetup", "-setsecurewebproxy", s, host, str(http_port)],
                               capture_output=True, timeout=15)
            ok(f"macOS system proxy -> {host}:{http_port}")
            info(f"SOCKS5 available separately at {host}:{socks_port}")
            return True
        except Exception as e:
            err(f"macOS networksetup failed: {e}")
            return False

    # Linux
    if _gnome_available():
        _gnome_backup(state)
        _gsettings(["set", "org.gnome.system.proxy", "mode", "manual"])
        _gsettings(["set", "org.gnome.system.proxy", "use-same-proxy", "true"])
        _gsettings(["set", "org.gnome.system.proxy.http", "host", host])
        _gsettings(["set", "org.gnome.system.proxy.http", "port", str(http_port)])
        _gsettings(["set", "org.gnome.system.proxy.https", "host", host])
        _gsettings(["set", "org.gnome.system.proxy.https", "port", str(http_port)])
        ok(f"GNOME system proxy -> {host}:{http_port}")
        info(f"SOCKS5 available separately at {host}:{socks_port}")
        return True

    warn("Automatic system proxy setup is not available on this desktop.")
    print(f"""
  {C.BOLD}Manual setup{C.RESET}
  ─────────────────────────────────────────────────────
  HTTP  proxy : {C.CYAN}{host}:{http_port}{C.RESET}
  SOCKS5 proxy: {C.CYAN}{host}:{socks_port}{C.RESET}
  • Browsers  -> set "Use system proxy" or point HTTP/HTTPS at the HTTP port
  • CLI tools -> export https_proxy=http://{host}:{http_port}
  • Anything supporting SOCKS5 -> socks5://{host}:{socks_port}
""")
    return False


def system_off(state):
    backup = state.get(_state_backup_key())
    if not backup:
        info("No system-proxy backup found - nothing to restore.")
        return True
    if IS_WINDOWS:
        import winreg
        if backup.get("ProxyServer", {}) and backup["ProxyServer"].get("value") is not None:
            _winreg_write("ProxyServer", backup["ProxyServer"]["value"],
                          backup["ProxyServer"]["type"])
        else:
            _winreg_delete("ProxyServer")
        if backup.get("ProxyOverride", {}) and backup["ProxyOverride"].get("value") is not None:
            _winreg_write("ProxyOverride", backup["ProxyOverride"]["value"],
                          backup["ProxyOverride"]["type"])
        if backup.get("ProxyEnable", {}) and backup["ProxyEnable"].get("value") is not None:
            _winreg_write("ProxyEnable", backup["ProxyEnable"]["value"],
                          backup["ProxyEnable"]["type"])
        else:
            _winreg_write("ProxyEnable", 0, winreg.REG_DWORD)
        _wininet_refresh()
        ok("Windows system proxy restored.")
        return True
    if IS_MACOS:
        import subprocess
        detail = backup.get("detail") if isinstance(backup.get("detail"), dict) else None
        if detail:
            # v1.3.0: restore each service EXACTLY as it was found - enabled
            # slots get their original host/port back, never-enabled slots
            # are merely switched off (no fabricated host:port).
            for s, snap in detail.items():
                try:
                    for kind, setter in (("web", "setwebproxy"),
                                         ("secure", "setsecurewebproxy")):
                        slot = snap.get(kind) or {}
                        state_on = str(slot.get("enabled", "")).lower() == "yes"
                        if state_on and _mac_is_set(slot):
                            subprocess.run(
                                ["networksetup", f"-{setter}", s,
                                 str(slot.get("host")), str(slot.get("port"))],
                                capture_output=True, timeout=15)
                            subprocess.run(
                                ["networksetup", f"-{setter}state", s, "on"],
                                capture_output=True, timeout=15)
                        else:
                            subprocess.run(
                                ["networksetup", f"-{setter}state", s, "off"],
                                capture_output=True, timeout=15)
                except Exception:
                    continue
            ok("macOS system proxy restored (original values).")
            return True
        # legacy backup (older version): only the service names were saved
        for s in backup.get("services", [])[:3]:
            subprocess.run(["networksetup", "-setwebproxystate", s, "off"],
                           capture_output=True, timeout=15)
            subprocess.run(["networksetup", "-setsecurewebproxystate", s, "off"],
                           capture_output=True, timeout=15)
        ok("macOS system proxy restored.")
        return True
    if shutil.which("gsettings") is not None:
        _gnome_restore(state)
        ok("GNOME system proxy restored.")
        return True
    return True
