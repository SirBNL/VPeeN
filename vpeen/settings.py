"""VPeeN settings persistence (JSON under ~/.vpeen/)."""
import json
import os

CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".vpeen")
CONFIG_PATH = os.path.join(CONFIG_DIR, "settings.json")

DEFAULTS = {
    "bind": "127.0.0.1",
    "socks_port": 1080,
    "http_port": 8080,
    "auto_system_proxy": False,
    "theme": "Dark",
    "last_region": "",
    "auto_connect": False,
    "save_logs": True,
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
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        data = {k: cfg.get(k, DEFAULTS[k]) for k in DEFAULTS}
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except OSError:
        pass
