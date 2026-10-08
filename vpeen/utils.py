"""Shared helpers: colored console output, state persistence, misc utils."""
import json
import os
import sys
import tempfile
import threading
import time

IS_WINDOWS = os.name == "nt"


def _enable_vt():
    """Enable ANSI colors on legacy Windows consoles (best effort)."""
    if not IS_WINDOWS:
        return
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
    except Exception:
        pass


_enable_vt()


class C:
    """ANSI color codes."""
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RED = "\033[91m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"
    MAGENTA = "\033[95m"
    CYAN = "\033[96m"
    GREY = "\033[90m"


def info(msg):
    print(f"{C.CYAN}[i]{C.RESET} {msg}")


def ok(msg):
    print(f"{C.GREEN}[+]{C.RESET} {msg}")


def warn(msg):
    print(f"{C.YELLOW}[!]{C.RESET} {msg}")


def err(msg):
    print(f"{C.RED}[x]{C.RESET} {msg}")


def dim(msg):
    print(f"{C.GREY}{msg}{C.RESET}")


def banner():
    from . import __version__
    v = ".".join(__version__.split(".")[:2])
    print(f"""{C.BOLD}{C.MAGENTA}
  __   ___  _  _  ___  ___  ___ ___  _  _  ___  ___
  \\ \\ / / \\| \\| ||   \\| _ \\| __| _ \\| \\| ||_ _|| _ |
   \\ V /| . | .  || |) |  _/| _|| v /| .  | | | |  _/
    \\_/ |___|_|\\_||___/|_|  |___|_|_\\|_|\\_||___||_|   v{v}
{C.RESET}{C.GREY}  VeePN (free extension proxy) -> local SOCKS5 / HTTP proxy{C.RESET}
""")


def ensure_utf8_console():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


class State:
    """Tiny JSON state file (token / servers cache / system-proxy backup).

    v1.2.4: writes are now ATOMIC (temp file + os.replace) and the parent
    directory is created on demand.  The GUI writes this file from several
    threads (core + quick-task workers); two concurrent plain open(...,"w")
    calls could interleave and truncate the JSON, silently wiping the cached
    token/udid and forcing a full re-registration.

    v4.2.2: the whole State object is now guarded by a re-entrant lock.
    The atomic write alone protected the FILE, not the dump: json.dump
    iterating self.data while another thread's set() inserted a key raised
    "dictionary changed size during iteration" and that write was lost."""

    def __init__(self, path):
        self.path = path
        self.data = {}
        self._lock = threading.RLock()
        self._load()

    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                self.data = json.load(f)
        except Exception:
            self.data = {}

    def save(self):
        with self._lock:
            try:
                d = os.path.dirname(self.path)
                if d:
                    os.makedirs(d, exist_ok=True)
                fd, tmp = tempfile.mkstemp(prefix=".state-", suffix=".tmp",
                                           dir=d or ".")
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as f:
                        json.dump(self.data, f, ensure_ascii=False, indent=2)
                    os.replace(tmp, self.path)
                except Exception:
                    try:
                        os.unlink(tmp)
                    except Exception:
                        pass
                    raise
            except Exception as e:
                err(f"Could not save state file: {e}")

    def get(self, key, default=None):
        with self._lock:
            return self.data.get(key, default)

    def set(self, key, value):
        with self._lock:
            self.data[key] = value
            self.save()


def local_timezone_name() -> str:
    """Best-effort IANA timezone name (what the extension sends)."""
    tz = os.environ.get("TZ")
    if tz:
        return tz
    try:
        with open("/etc/timezone", "r", encoding="utf-8") as f:
            tz = f.read().strip()
            if tz:
                return tz
    except Exception:
        pass
    if IS_WINDOWS:
        try:
            import winreg
            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SYSTEM\CurrentControlSet\Control\TimeZoneInformation",
            ) as k:
                name, _ = winreg.QueryValueEx(k, "TimeZoneKeyName")
                # "Iran Standard Time" -> "Iran" (the trailing slash the old
                # replace() left behind looked like "Iran/" on the wire)
                return name.replace(" Standard Time", "").strip(" /") or "UTC"
        except Exception:
            pass
    try:
        import datetime
        return datetime.datetime.now().astimezone().tzname() or "UTC"
    except Exception:
        return "UTC"


def now_ms() -> int:
    return int(time.time() * 1000)
