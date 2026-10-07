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


def _backup_and_store(state, values: dict):
    old = {}
    for k in values:
        old[k] = _winreg_read(k)
    state.set(_state_backup_key(), old)


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
def _gnome_available():
    return shutil.which("gsettings") is not None and os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")


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
            state.set(_state_backup_key(), {"services": svc})
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
