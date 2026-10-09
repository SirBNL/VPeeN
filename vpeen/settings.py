"""VPeeN settings persistence (JSON under ~/.vpeen/)."""
import json
import os
import tempfile

CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".vpeen")
CONFIG_PATH = os.path.join(CONFIG_DIR, "settings.json")

DEFAULTS = {
    "bind": "127.0.0.1",
    "socks_port": 1080,
    "http_port": 8080,
    "auto_system_proxy": False,
    "tun_fallback": True,          # v4.3.0: on TUN failure, land in proxy
                                   # mode (loudly) instead of stopping; turn
                                   # OFF for kill-switch-style behaviour
    "theme": "Dark",
    "last_region": "",
    "auto_connect": False,
    "save_logs": True,
    # --- tunnel (VPN) mode
    "tunnel_enabled": False,       # use TUN mode on connect
    "tunnel_dns": True,            # relay DNS through the tunnel (no leaks)
    "tunnel_mtu": 1500,            # conservative; raise to 8500 if stable
    "tunnel_sweep": True,          # clean orphaned state from a crashed session
}


def load() -> dict:
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            user = json.load(f)
        if isinstance(user, dict):
            for k in DEFAULTS:
                if k in user:
                    cfg[k] = user[k]
    except (OSError, ValueError):
        pass
    return cfg


def save(cfg: dict) -> None:
    """v4.2.2: atomic write (temp file + os.replace) - a crash or two racing
    writers mid-dump used to be able to leave a truncated settings.json,
    silently resetting every setting to defaults on next launch."""
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        data = {k: cfg.get(k, DEFAULTS[k]) for k in DEFAULTS}
        fd, tmp = tempfile.mkstemp(prefix=".settings-", suffix=".tmp",
                                   dir=CONFIG_DIR)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp, CONFIG_PATH)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except OSError:
        pass
