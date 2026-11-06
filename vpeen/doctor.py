"""
VPeeN Doctor (v4.9.0) - the one-click protection self-check.

Answers, honestly and offline, the questions a VPN user actually asks
after connecting:

  * are the local listeners really up?
  * does the exit IP differ from my real IP (leak detection)?
  * is ALL my traffic routed, or only what honors a proxy?
  * where do my DNS queries go?
  * is the kill switch armed?  did it have to engage?
  * is the system proxy applied?  what about WebRTC / QUIC side channels?

NO third-party "what is my IP" services are contacted: the doctor runs
on the bridge's own live snapshot (the direct IP and exit IP already
come from the app's own API probes).  run_checks() is a pure function -
the web UI layer merely feeds it the snapshot and renders the list.

    python -c "from vpeen.doctor import run_checks"
"""

_OK, _WARN, _FAIL = "ok", "warn", "fail"


def _check(cid, status, title, detail):
    return {"id": cid, "status": status, "title": title, "detail": detail}


def run_checks(snapshot: dict) -> list:
    """Bridge snapshot -> ordered list of check dicts.

    Each check is {id, status: ok|warn|fail, title, detail}.  The ids are
    stable - the web UI localizes titles by id and keeps the (technical)
    detail text as-is.
    """
    phase = snapshot.get("phase") or "disconnected"
    mode = snapshot.get("mode") or "proxy"
    connected = phase == "connected"
    listeners = snapshot.get("listeners") or {}
    cfg = snapshot.get("settings") or {}
    socks = listeners.get("socks_port")
    http = listeners.get("http_port")
    checks = []

    # ---------------------------------------------------------- 1. listeners
    if connected and (socks or http):
        ports = [p for p in (f"SOCKS5 :{socks}" if socks else None,
                             f"HTTP :{http}" if http else None) if p]
        checks.append(_check(
            "listeners", _OK, "Local proxy listeners",
            "live and accepting connections (" + " | ".join(ports) + ")"))
    elif connected:
        checks.append(_check(
            "listeners", _FAIL, "Local proxy listeners",
            "connected but no listener ports were reported - check the logs"))
    else:
        checks.append(_check(
            "listeners", _WARN, "Local proxy listeners",
            "not connected - connect first for a meaningful check"))

    # ------------------------------------------------- 2. exit vs direct IP
    exit_ip = snapshot.get("exit_ip")
    direct_ip = snapshot.get("direct_ip")
    if connected:
        if exit_ip and direct_ip:
            if str(exit_ip).strip() == str(direct_ip).strip():
                checks.append(_check(
                    "ip_split", _FAIL, "Exit IP differs from your real IP",
                    f"LEAK: both are {exit_ip} - your traffic is NOT going "
                    "through the VPN; disconnect and reconnect"))
            else:
                checks.append(_check(
                    "ip_split", _OK, "Exit IP differs from your real IP",
                    f"real {direct_ip} -> exit {exit_ip}"))
        elif exit_ip:
            checks.append(_check(
                "ip_split", _OK, "Exit IP differs from your real IP",
                f"exit {exit_ip} (real IP not probed yet)"))
        else:
            checks.append(_check(
                "ip_split", _WARN, "Exit IP differs from your real IP",
                "exit IP not verified yet - it is probed right after "
                "connecting, retry in a moment"))
    else:
        checks.append(_check(
            "ip_split", _WARN, "Exit IP differs from your real IP",
            f"real IP {direct_ip or 'unknown'} - nothing is tunneled "
            "while disconnected"))

    # ------------------------------------------------------- 3. routing mode
    if connected and mode == "tunnel":
        checks.append(_check(
            "routing", _OK, "Traffic routing",
            "TUN mode: every app, every protocol (TCP+UDP) is routed "
            "through the tunnel"))
    elif connected:
        checks.append(_check(
            "routing", _WARN, "Traffic routing",
            "proxy mode: only apps that honor the system/browser proxy "
            "are protected - use the VPN Tunnel switch for full-device "
            "coverage"))
    else:
        want = "tunnel" if cfg.get("tunnel_enabled") else "proxy"
        checks.append(_check(
            "routing", _WARN, "Traffic routing",
            f"not connected (next connect will use {want} mode)"))

    # --------------------------------------------------------------- 4. DNS
    if connected and mode == "tunnel" and cfg.get("tunnel_dns", True):
        checks.append(_check(
            "dns", _OK, "DNS privacy",
            "queries resolve through the tunnel relay - no ISP visibility"))
    elif connected and mode == "tunnel":
        checks.append(_check(
            "dns", _WARN, "DNS privacy",
            "tunnel DNS relay is OFF - queries leak to your physical "
            "resolver (enable 'Route DNS through the tunnel')"))
    elif connected:
        checks.append(_check(
            "dns", _WARN, "DNS privacy",
            "proxy mode: DNS goes to your system resolver; only DoH/HTTPS "
            "inside the browser is encrypted"))
    else:
        checks.append(_check(
            "dns", _WARN, "DNS privacy",
            "not connected - your resolver sees every hostname you visit"))

    # ------------------------------------------------------- 5. kill switch
    ks = snapshot.get("ks")
    if ks == "lockdown":
        checks.append(_check(
            "killswitch", _FAIL, "Kill switch",
            "LOCKDOWN: traffic is blocked because the tunnel could not be "
            "repaired - disconnect to restore your network, then retry"))
    elif ks == "healing":
        checks.append(_check(
            "killswitch", _WARN, "Kill switch",
            "engaged right now - traffic is blocked while the tunnel is "
            "being repaired automatically"))
    elif cfg.get("kill_switch", True):
        if mode == "tunnel" or (connected and mode == "tunnel"):
            checks.append(_check(
                "killswitch", _OK, "Kill switch",
                "armed: if the tunnel breaks, traffic is blocked instead "
                "of leaking (tunnel mode)"))
        else:
            checks.append(_check(
                "killswitch", _WARN, "Kill switch",
                "armed, but it only protects TUNNEL mode - in proxy mode "
                "a dropped connection simply means unprotected traffic"))
    else:
        checks.append(_check(
            "killswitch", _WARN, "Kill switch",
            "disabled in Settings - a broken tunnel falls back to "
            "unprotected traffic"))

    # ----------------------------------------------------- 6. system proxy
    if snapshot.get("system_proxy"):
        bypass = cfg.get("bypass_domains") or ""
        extra = ""
        if bypass:
            n = len([d for d in bypass.split(",") if d.strip()])
            extra = f" ({n} domain(s) bypass DIRECT by your choice)"
        checks.append(_check(
            "system", _OK, "System proxy applied",
            "browsers and system apps are pointed at VPeeN" + extra))
    elif connected and mode == "proxy":
        checks.append(_check(
            "system", _WARN, "System proxy applied",
            "off - apps must be configured manually (Settings: 'Auto "
            "system proxy')"))
    else:
        checks.append(_check(
            "system", _WARN, "System proxy applied",
            "not applied (tunnel mode does not need it)"))

    # ------------------------------------------------------------ 7. QUIC
    if connected and mode == "tunnel":
        checks.append(_check(
            "quic", _OK, "QUIC / UDP side channel",
            "UDP (incl. QUIC port 443) flows into the tunnel - no bypass "
            "path exists"))
    else:
        checks.append(_check(
            "quic", _WARN, "QUIC / UDP side channel",
            "browsers can send QUIC (UDP/443) DIRECTLY around an HTTP "
            "proxy, exposing your real IP - tunnel mode or disabling QUIC "
            "in the browser closes this"))

    # --------------------------------------------------------- 8. WebRTC
    checks.append(_check(
        "webrtc", _WARN, "WebRTC advisory",
        "browser WebRTC may expose local/real IPs regardless of the VPN; "
        "disable it (or use a hardened browser profile) for maximum "
        "privacy"))

    return checks


def worst_status(report: list) -> str:
    """'fail' > 'warn' > 'ok' - the headline for the toast/badge."""
    ranks = {_OK: 0, _WARN: 1, _FAIL: 2}
    worst = _OK
    for c in report or []:
        if ranks.get(c.get("status"), 0) > ranks.get(worst, 0):
            worst = c.get("status")
    return worst


def summarize(report: list) -> str:
    """One-line summary for logs: '8 checks: 4 ok, 4 warnings, 0 problems'."""
    n = {"ok": 0, "warn": 0, "fail": 0}
    for c in report or []:
        n[c.get("status", "ok")] = n.get(c.get("status", "ok"), 0) + 1
    return (f"{len(report or [])} checks: {n['ok']} ok, "
            f"{n['warn']} warnings, {n['fail']} problems")
