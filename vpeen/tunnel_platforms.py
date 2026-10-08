"""
Platform-specific TUN setup / teardown for VPeeN Tunnel mode.

Implements the exact up/down sequences per OS (see scripts/research notes):

  Windows : wintun adapter "VPeeN" (created by tun2socks) + static IP
            198.18.0.1/24 + def1 hijack (0.0.0.0/1 + 128.0.0.0/1) +
            upstream-server host routes via the original gateway.
  Linux   : vpeen0 + same routes via `ip` + optional resolv.conf swap.
  macOS   : utun9 + 8 net-route chunks + networksetup DNS swap.

Every mutation is journaled to a session file (~/.vpeen/tunnel_session.json)
so a crashed session can be replayed backwards by the cleanup worker.
All routes are non-persistent (route.exe without /p, `ip route`, route(8));
the wintun adapter itself disappears when the tun2socks process dies.
"""
import json
import os
import platform
import re
import shutil
import subprocess
import time

TUN_NAME = {"win32": "VPeeN", "linux": "vpeen0", "darwin": "utun9"}
TUN_IP = "198.18.0.1"
TUN_MASK = "255.255.255.0"
ADAPTER_GUID = "{6A6B5C61-9B3D-4BCE-9E4E-3D0C2BA1F5F2}"

CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".vpeen")
SESSION_PATH = os.path.join(CONFIG_DIR, "tunnel_session.json")
RESOLV_BAK = os.path.join(CONFIG_DIR, "resolv.conf.bak")

sys_platform = platform.system().lower()          # 'windows' | 'linux' | 'darwin'


def _log(msg, lvl="info"):
    print(f"[{lvl}] {msg}", flush=True)


def run(cmd, timeout=25, check=False, input=None):
    """Run a command, return (rc, stdout, stderr). Never raises for rc!=0."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           input=input, creationflags=_creation_flags())
        out = (p.stdout or "").strip()
        err = (p.stderr or "").strip()
        if check and p.returncode != 0:
            raise OSError(f"{cmd[0]} rc={p.returncode}: {err or out}")
        return p.returncode, out, err
    except FileNotFoundError:
        return 127, "", f"{cmd[0]}: not found"
    except subprocess.TimeoutExpired:
        return 124, "", f"{cmd[0]}: timeout"


def _creation_flags():
    if os.name == "nt":
        import subprocess as _s
        return getattr(_s, "CREATE_NO_WINDOW", 0)
    return 0


def is_admin() -> bool:
    if os.name == "nt":
        try:
            import ctypes
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            return False
    return os.geteuid() == 0


# ------------------------------------------------------------------ session
def save_session(data: dict) -> None:
    os.makedirs(CONFIG_DIR, exist_ok=True)
    data["saved_at"] = time.time()
    with open(SESSION_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)


def load_session() -> dict | None:
    try:
        with open(SESSION_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def clear_session() -> None:
    try:
        os.remove(SESSION_PATH)
    except OSError:
        pass


# ================================================================ WINDOWS
def _parse_route_print(out: str):
    """Parse `route print -4` output: return default-route rows from the
    ACTIVE routes table only -> [(gw, iface_ip, metric)].
    NOTE: the Interface column is an IP ADDRESS (not an index!) and the
    gateway may be 'On-link' (PPPoE / some DHCP / hotspots)."""
    rows = []
    in_active = False
    for ln in out.splitlines():
        s = ln.strip()
        if s.startswith("Active Routes:"):
            in_active = True
            continue
        if not in_active:
            continue
        if s.startswith("Persistent Routes"):
            break
        if not s:
            if rows:
                break       # blank line after the table ends it
            continue
        if s.startswith("Network Destination") or set(s) <= {"="}:
            continue
        parts = s.split()
        if len(parts) >= 5 and parts[0] == "0.0.0.0" and parts[1] == "0.0.0.0":
            gw, iface_ip = parts[2], parts[3]
            metric = int(parts[4]) if parts[4].isdigit() else 9999
            rows.append((gw, iface_ip, metric))
    return rows


def _parse_netsh_addresses(out: str):
    """Parse `netsh interface ipv4 show addresses` -> blocks of
    (iface_name, body).  Used to map the interface IP from route print to
    the interface NAME (the route print table never shows ifindexes)."""
    blocks = []
    parts = re.split(r'Configuration for interface "([^"]+)"', out)
    # re.split keeps separators: ['', name1, body1, name2, body2, ...]
    for i in range(1, len(parts) - 1, 2):
        blocks.append((parts[i].strip(), parts[i + 1]))
    return blocks


def _parse_netsh_interfaces(out: str):
    """Parse `netsh interface ipv4 show interfaces` -> [(ifindex, name)].
    v4.2.0 fix: the old regexes lumped the State column into the Name
    ("connected     VPeeN"), so the adapter was never detected. Columns
    are separated by 2+ spaces; the Name is everything after State (and
    names themselves contain single spaces)."""
    rows = []
    for ln in out.splitlines():
        s = ln.strip()
        if not s or set(s) <= {"-"}:
            continue
        parts = re.split(r"\s{2,}", s)
        if len(parts) >= 5 and parts[0].isdigit():
            rows.append((int(parts[0]), " ".join(parts[4:]).strip()))
    return rows


def _win_iface_index_by_name(name: str):
    """ifindex for a netsh interface name, or None."""
    rc, out, _ = run(["netsh", "interface", "ipv4", "show", "interfaces"],
                     timeout=15)
    if rc == 0:
        for idx, nm in _parse_netsh_interfaces(out):
            if nm == name:
                return idx
    return None


def _win_iface_of_ip(ip: str):
    """(name, ifindex) of the interface owning `ip` via netsh show
    addresses, best effort. Returns (None, None) if not found."""
    rc, out, _ = run(["netsh", "interface", "ipv4", "show", "addresses"],
                     timeout=15)
    if rc == 0:
        for name, body in _parse_netsh_addresses(out):
            if re.search(rf"\b{re.escape(ip)}\b", body):
                idx = _win_iface_index_by_name(name)
                return name, idx
    return None, None


def _win_orig_route():
    """Default IPv4 route: (gw, iface_alias, ifindex) - or (gw, None, None)
    if the alias/index could not be mapped.  NEVER raises.
    v4.2.0 rewrite: the old code did int(parts[3]) on the Interface column,
    which is an IP ADDRESS -> ValueError on every Windows machine, so the
    tunnel always failed with 'invalid literal for int()'."""
    try:
        rc, out, _ = run(["route", "print", "-4"], timeout=15)
        if rc != 0:
            return None
        rows = _parse_route_print(out)
        if not rows:
            return None
        rows.sort(key=lambda r: r[2])       # lowest metric first
        gw, iface_ip, _metric = rows[0]
        onlink = gw.lower().replace("_", "-") in ("on-link", "onlink")
        name, idx = _win_iface_of_ip(iface_ip)
        if onlink:
            # on-link default: use the interface's own address as gateway
            gw = iface_ip
        if name is None and idx is None:
            return gw, None, None
        return gw, name, idx
    except Exception:
        return None


def _win_iface_alias(ifindex: int) -> str:
    rc, out, _ = run(["netsh", "interface", "ipv4", "show", "interfaces"],
                     timeout=15)
    if rc == 0:
        for idx, nm in _parse_netsh_interfaces(out):
            if idx == ifindex:
                return nm
    return f"if{ifindex}"


def _win_tun_index() -> int | None:
    rc, out, _ = run(["netsh", "interface", "ipv4", "show", "interfaces"],
                     timeout=15)
    if rc == 0:
        for idx, nm in _parse_netsh_interfaces(out):
            if nm == TUN_NAME["win32"]:
                return idx
    return None


def win_up(mtu, upstream_ips, orig=None):
    """Bring up Windows TUN routes. Returns (session_dict, ok, err)."""
    orig = orig or _win_orig_route()
    if not orig:
        return None, False, "could not detect the default gateway/interface"
    gw, alias, idx = orig
    tun = TUN_NAME["win32"]
    journal = {"routes": [], "iface": tun, "ip": TUN_IP}

    # 1. static address + adapter-scoped DNS on the TUN adapter only
    #    (one retry - a brand-new wintun adapter is occasionally not ready
    #    for netsh the very first moment)
    addr_cmd = ["netsh", "interface", "ipv4", "set", "address",
                f"name={tun}", "source=static", f"addr={TUN_IP}",
                f"mask={TUN_MASK}", "gateway=none"]
    rc, _, err = run(addr_cmd)
    if rc != 0:
        time.sleep(1.0)
        rc, _, err = run(addr_cmd, check=True)
    journal["routes"].append({"type": "addr", "iface": tun})
    rc, _, err = run(["netsh", "interface", "ipv4", "set", "dnsservers",
                      f"name={tun}", "static", f"address={TUN_IP}",
                      "register=none", "validate=no"], check=True)
    journal["routes"].append({"type": "dnsadapter", "iface": tun})

    tun_idx = _win_tun_index()
    if tun_idx is None:
        return journal, False, "VPeeN adapter did not appear (tun2socks failed?)"

    # 2. anti-loop host routes for the upstream proxy servers via ORIGINAL gw
    #    (idx may be None on exotic setups - route.exe then resolves the
    #    interface from the gateway itself)
    for ip in upstream_ips:
        cmd = ["route", "add", ip, "mask", "255.255.255.255", gw,
               "metric", "1"]
        if idx is not None:
            cmd += ["if", str(idx)]
        rc, _, err = run(cmd)
        if rc == 0:
            journal["routes"].append({"type": "route", "prefix": ip,
                                      "mask": "255.255.255.255", "gw": gw,
                                      "metric": "1", "if": idx})
        else:
            _log(f"host route add failed for {ip}: {err}", "warn")

    # 3. def1 hijack - two /1 routes via the TUN (never touches real default)
    for prefix in ("0.0.0.0", "128.0.0.0"):
        rc, _, err = run(["route", "add", prefix, "mask", "128.0.0.0",
                          TUN_IP, "metric", "1", "if", str(tun_idx)])
        if rc == 0:
            journal["routes"].append({"type": "route", "prefix": prefix,
                                      "mask": "128.0.0.0", "gw": TUN_IP,
                                      "metric": "1", "if": tun_idx})
        else:
            return journal, False, f"route add {prefix}/1 failed: {err}"

    run(["ipconfig", "/flushdns"])
    return journal, True, None


def win_down(journal):
    for r in reversed(journal.get("routes", [])):
        try:
            if r["type"] == "route":
                run(["route", "delete", r["prefix"], "mask", r["mask"], r["gw"]],
                    timeout=15)
            elif r["type"] == "dnsadapter":
                run(["netsh", "interface", "ipv4", "set", "dnsservers",
                     f"name={r['iface']}", "source=dhcp"], timeout=15)
            elif r["type"] == "addr":
                pass    # adapter dies with the tun2socks process
        except Exception as e:
            _log(f"cleanup step failed: {e}", "warn")
    run(["ipconfig", "/flushdns"])


# ================================================================== LINUX
def _linux_orig_route():
    rc, out, _ = run(["ip", "route", "show", "default"])
    if rc == 0 and out:
        m = re.search(r"default via (\S+) dev (\S+)", out)
        if m:
            return m.group(1), m.group(2)
    return None


def _linux_dns_swap():
    """Backup /etc/resolv.conf and point it at the TUN resolver."""
    path = "/etc/resolv.conf"
    real = os.path.realpath(path)
    try:
        with open(real, "r", encoding="utf-8") as f:
            old = f.read()
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(RESOLV_BAK, "w", encoding="utf-8") as f:
            f.write(old)
        with open(real, "w", encoding="utf-8") as f:
            f.write(f"nameserver {TUN_IP}\noptions timeout:2 attempts:2\n")
        return {"type": "resolvconf", "real": real}
    except OSError as e:
        _log(f"resolv.conf swap failed: {e}", "warn")
        return None


def _linux_dns_restore(step):
    real = step.get("real")
    try:
        if os.path.exists(RESOLV_BAK):
            with open(RESOLV_BAK, "r", encoding="utf-8") as f:
                old = f.read()
            with open(real, "w", encoding="utf-8") as f:
                f.write(old)
            os.remove(RESOLV_BAK)
    except OSError as e:
        _log(f"resolv.conf restore failed: {e}", "warn")


def linux_up(mtu, upstream_ips, orig=None):
    orig = orig or _linux_orig_route()
    if not orig:
        return None, False, "could not detect the default route"
    gw, dev = orig
    tun = TUN_NAME["linux"]
    journal = {"routes": [], "dns": []}

    rc, _, err = run(["ip", "addr", "add", f"{TUN_IP}/15", "dev", tun])
    if rc != 0:
        return journal, False, f"ip addr add failed: {err}"
    run(["ip", "link", "set", tun, "up"])
    journal["routes"].append({"type": "addr", "iface": tun})

    rc, _, err = run(["sysctl", "-w",
                      f"net.ipv4.conf.{dev}.rp_filter=0"])
    if rc == 0:
        journal["routes"].append({"type": "sysctl",
                                  "key": f"net.ipv4.conf.{dev}.rp_filter"})

    for ip in upstream_ips:
        rc, _, err = run(["ip", "route", "add", f"{ip}/32", "via", gw])
        if rc == 0:
            journal["routes"].append({"type": "route", "prefix": f"{ip}/32",
                                      "via": gw})
        else:
            _log(f"host route add failed for {ip}: {err}", "warn")

    ok_all = True
    for prefix in ("0.0.0.0/1", "128.0.0.0/1"):
        rc, _, err = run(["ip", "route", "add", prefix, "dev", tun, "metric", "1"])
        if rc == 0:
            journal["routes"].append({"type": "route", "prefix": prefix,
                                      "dev": tun})
        else:
            ok_all = False
            _log(f"route add {prefix} failed: {err}", "err")

    step = _linux_dns_swap()
    if step:
        journal["dns"].append(step)
    run(["resolvectl", "flush-caches"]) if shutil.which("resolvectl") else None
    return journal, ok_all, None if ok_all else "some hijack routes failed"


def linux_down(journal):
    for r in reversed(journal.get("dns", [])):
        if r.get("type") == "resolvconf":
            _linux_dns_restore(r)
    for r in reversed(journal.get("routes", [])):
        try:
            if r["type"] == "route":
                if "via" in r:
                    run(["ip", "route", "del", r["prefix"], "via", r["via"]])
                else:
                    run(["ip", "route", "del", r["prefix"], "dev", r["dev"]])
            elif r["type"] == "sysctl":
                run(["sysctl", "-w", f"{r['key']}={2 if 'rp_filter' in r['key'] else 1}"])
            elif r["type"] == "addr":
                run(["ip", "addr", "flush", "dev", r["iface"]])
        except Exception as e:
            _log(f"cleanup step failed: {e}", "warn")


# ================================================================== MACOS
def _mac_orig_route():
    rc, out, _ = run(["route", "-n", "get", "default"])
    if rc == 0 and out:
        gw = re.search(r"gateway:\s*(\S+)", out)
        dev = re.search(r"interface:\s*(\S+)", out)
        if gw and dev:
            return gw.group(1), dev.group(1)
    return None


def _mac_services():
    rc, out, _ = run(["networksetup", "-listallnetworkservices"])
    if rc != 0:
        return []
    return [ln.strip() for ln in out.splitlines()[1:]
            if ln.strip() and not ln.startswith("*")]


def _mac_dns_set(services):
    """Swap DNS on all services to the TUN resolver; journal old values."""
    journal = []
    for svc in services[:5]:
        rc, out, _ = run(["networksetup", "-getdnsservers", svc])
        old = out if rc == 0 and out and "There aren't" not in out else ""
        rc2, _, err = run(["networksetup", "-setdnsservers", svc, TUN_IP])
        if rc2 == 0:
            journal.append({"type": "macdns", "service": svc, "old": old})
    return journal


def _mac_dns_restore(journal):
    for step in reversed(journal.get("dns", [])):
        if step.get("type") != "macdns":
            continue
        svc, old = step["service"], step.get("old", "")
        if old:
            run(["networksetup", "-setdnsservers", svc, *old.split()])
        else:
            run(["networksetup", "-setdnsservers", svc, "Empty"])


def mac_up(mtu, upstream_ips, orig=None):
    orig = orig or _mac_orig_route()
    if not orig:
        return None, False, "could not detect the default route"
    gw, dev = orig
    tun = TUN_NAME["darwin"]
    journal = {"routes": [], "dns": []}

    rc, _, err = run(["ifconfig", tun, TUN_IP, TUN_IP, "up"])
    if rc != 0:
        return journal, False, f"ifconfig failed: {err}"
    journal["routes"].append({"type": "addr", "iface": tun})

    for ip in upstream_ips:
        rc, _, err = run(["route", "-n", "add", "-host", ip, gw])
        if rc == 0:
            journal["routes"].append({"type": "route", "kind": "host",
                                      "prefix": ip, "gw": gw})
        else:
            _log(f"host route add failed for {ip}: {err}", "warn")

    ok_all = True
    chunks = ["1.0.0.0/8", "2.0.0.0/7", "4.0.0.0/6", "8.0.0.0/5",
              "16.0.0.0/4", "32.0.0.0/3", "64.0.0.0/2", "128.0.0.0/1"]
    for prefix in chunks:
        rc, _, err = run(["route", "-n", "add", "-net", prefix,
                          "-gateway", TUN_IP, "-interface", tun])
        if rc == 0:
            journal["routes"].append({"type": "route", "kind": "net",
                                      "prefix": prefix, "gw": TUN_IP,
                                      "iface": tun})
        else:
            ok_all = False
            _log(f"route add {prefix} failed: {err}", "err")

    journal["dns"].extend(_mac_dns_set(_mac_services()))
    return journal, ok_all, None if ok_all else "some hijack routes failed"


def mac_down(journal):
    _mac_dns_restore(journal)
    for r in reversed(journal.get("routes", [])):
        try:
            if r["type"] == "route" and r["kind"] == "host":
                run(["route", "-n", "delete", "-host", r["prefix"], r["gw"]])
            elif r["type"] == "route" and r["kind"] == "net":
                run(["route", "-n", "delete", "-net", r["prefix"],
                     "-gateway", r["gw"], "-interface", r["iface"]])
            elif r["type"] == "addr":
                pass   # utun dies with the process
        except Exception as e:
            _log(f"cleanup step failed: {e}", "warn")


# ============================================================== dispatcher
def bring_up(mtu, upstream_ips):
    """Journalized UP for the current OS. Returns (session, ok, err)."""
    if sys_platform.startswith("win"):
        journal, ok, err = win_up(mtu, upstream_ips)
    elif sys_platform == "linux":
        journal, ok, err = linux_up(mtu, upstream_ips)
    elif sys_platform == "darwin":
        journal, ok, err = mac_up(mtu, upstream_ips)
    else:
        return None, False, f"unsupported platform {sys_platform}"
    if journal is not None:
        save_session({"os": sys_platform, "journal": journal})
    return journal, ok, err


def bring_down(journal):
    if sys_platform.startswith("win"):
        win_down(journal)
    elif sys_platform == "linux":
        linux_down(journal)
    elif sys_platform == "darwin":
        mac_down(journal)
    clear_session()


def cleanup_from_session(session) -> None:
    """Best-effort replay of a crashed session (called elevated)."""
    os_name = session.get("os", sys_platform)
    journal = session.get("journal", {})
    try:
        if os_name.startswith("win"):
            win_down(journal)
        elif os_name == "linux":
            linux_down(journal)
        elif os_name == "darwin":
            mac_down(journal)
        _log("orphaned tunnel state cleaned up.", "ok")
    finally:
        clear_session()
