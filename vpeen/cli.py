"""
VPeeN command-line interface (interactive + subcommands).
All user-facing strings are Persian to match the tool's audience.
"""
import argparse
import asyncio
import ipaddress
import os
import platform
import signal
import sys

from . import __version__
from .api import ApiError, VeePNApi
from .localproxy import LocalProxyServer, Stats, TunnelFactory
from .systemproxy import system_off, system_on
from .upstream import check_exit_ip
from .utils import (C, State, banner, dim, ensure_utf8_console, err, info, ok,
                    warn)

DEFAULT_STATE_PATH = os.path.join(os.path.expanduser("~"), ".vpeen", "state.json")
DEFAULT_SOCKS_PORT = 1080
DEFAULT_HTTP_PORT = 8080



def _state_path(args):
    p = getattr(args, "state", None) or DEFAULT_STATE_PATH
    return os.path.abspath(p)


def _make_api(args) -> VeePNApi:
    return VeePNApi(State(_state_path(args)), insecure_tls=getattr(args, "insecure", False))


def validate_bind_security(bind: str, user, pwd) -> str | None:
    """v4.3.0: refuse to expose an unauthenticated proxy to the network.

    Returns None when the bind is safe, otherwise a human-readable error.
    Loopback binds (127.0.0.1 / ::1 / localhost) are always fine.  Anything
    else (0.0.0.0, a LAN IP, a hostname) makes the proxy reachable from
    other devices - that is only allowed with proxy credentials set, so the
    neighbours cannot spend your VeePN quota or ride your traffic."""
    bind = (bind or "127.0.0.1").strip()
    try:
        if ipaddress.ip_address(bind).is_loopback:
            return None
    except ValueError:
        if bind.lower() in ("localhost", "::1"):
            return None
    if not (user and pwd):
        return (f"refusing to bind to '{bind}': the local proxy would be "
                f"reachable from your network WITHOUT a password.  "
                f"Set the proxy auth fields in Settings, or use the "
                f"default 127.0.0.1.")
    return None


def _interactive_args() -> argparse.Namespace:
    """Complete default namespace so every cmd_* works from the menu
    (subcommand flags like --bind / --count do not exist there)."""
    return argparse.Namespace(
        state=None, insecure=False,
        all=False, region=None, count=2,
        best=False, bind="127.0.0.1",
        socks_port=DEFAULT_SOCKS_PORT, http_port=DEFAULT_HTTP_PORT,
        set_system=False,
        proxy_user=None, proxy_pass=None,
        show_secrets=False,
    )


# ------------------------------------------------------------------- helpers
async def cmd_list(args):
    api = _make_api(args)
    locations, free = await api.locations()
    print(f"\n{C.BOLD}Free locations (proxyType=0): {len(free)}{C.RESET}")
    print(f"{C.GREY}{'region':<16} {'country':<10} {'city':<22} {'id':<8}{C.RESET}")
    for l in sorted(free, key=lambda x: x.get("region", "")):
        cc = l.get("countryCode", "")
        print(f"{C.GREEN}{l.get('region',''):<16}{C.RESET} {cc:<10} "
              f"{l.get('name',''):<22} {l.get('id',''):<8}")
    if getattr(args, "all", False):
        premium = [l for l in locations if l.get("proxyType") != 0]
        print(f"\n{C.BOLD}Premium locations: {len(premium)} {C.GREY}(need paid account - shown for reference){C.RESET}")
        for l in sorted(premium, key=lambda x: x.get("region", ""))[:25]:
            print(f"{C.GREY}  {l.get('region',''):<16} {l.get('countryCode',''):<6} {l.get('name','')}{C.RESET}")
        if len(premium) > 25:
            print(f"{C.GREY}  ... and {len(premium) - 25} more{C.RESET}")
    print()


async def cmd_best(args):
    api = _make_api(args)
    loc = await api.optimal_location()
    free = loc.get("proxyType") == 0
    tag = f"{C.GREEN}FREE{C.RESET}" if free else f"{C.YELLOW}PREMIUM{C.RESET}"
    ok(f"Optimal location: {C.BOLD}{loc.get('name')}{C.RESET} "
       f"(region={loc.get('region')}, {tag})")
    return loc


async def cmd_ip(args):
    api = _make_api(args)
    ip = await api.check_ip_direct()
    if ip:
        ok(f"Your current (direct) IP: {C.BOLD}{ip}{C.RESET}")
    else:
        err("Could not determine your IP.")


async def cmd_test(args):
    """Full chain test: token -> server list -> connect -> exit IP."""
    region = args.region
    api = _make_api(args)
    print(f"\n{C.BOLD}=== VPeeN end-to-end test ==={C.RESET}")
    try:
        if not region:
            locations, free = await api.locations()
            free_regions = sorted({l["region"] for l in free})
            if not free_regions:
                err("No free locations available right now - try again later.")
                return
            region = "nl" if "nl" in free_regions else free_regions[0]
            info(f"No region given - using '{region}'")
        ip_direct = await api.check_ip_direct()
        info(f"Direct IP        : {ip_direct or 'unknown'}")
        servers = await api.server_list(region)
        info(f"Servers in region: {len(servers)}")
        tested = 0
        for s in servers:
            if tested >= args.count:
                break
            host = s["addresses"][0]
            dim(f"  testing {host}:{s['port']} ...")
            try:
                ip = await check_exit_ip(s, insecure_tls=getattr(args, "insecure", False))
                if ip:
                    ok(f"  {C.GREEN}OK{C.RESET}  exit IP = {C.BOLD}{ip}{C.RESET} "
                       f"({s.get('regionName', region)})")
                    tested += 1
                else:
                    warn(f"  {C.YELLOW}FAIL{C.RESET} {host} - no IP in response")
            except Exception as e:
                warn(f"  {C.YELLOW}FAIL{C.RESET} {host} - {e}")
        if tested:
            print(f"\n{C.GREEN}Result: chain works. Start the proxy with:{C.RESET}")
            print(f"  python -m vpeen.cli run --region {region}\n")
        else:
            err("No working server found in this region right now.")
    except ApiError as e:
        err(f"API error: {e}")
        if "invalid" in str(e).lower() and region:
            dim("Tip: run `python -m vpeen.cli list` to see valid free regions.")


def _print_running(bind, socks_port, http_port, region, auth=None):
    auth_line = (f"  Auth     : {C.CYAN}username/password required"
                 f"{C.RESET}\n") if auth else ""
    print(f"""
{C.BOLD}{C.GREEN}  VPN is UP - exit region: {region}{C.RESET}

  Local endpoints
  ──────────────────────────────────────────────────
  SOCKS5 : {C.CYAN}socks5://{bind}:{socks_port}{C.RESET}
  HTTP   : {C.CYAN}http://{bind}:{http_port}{C.RESET}
{auth_line}
  Quick set (system-wide if not auto-set)
  ──────────────────────────────────────────────────
  Windows      -> Settings > Proxy > {bind}:{http_port}
  Windows cmd  -> set https_proxy=http://{bind}:{http_port}
  Linux/macOS  -> export https_proxy=http://{bind}:{http_port}
  Browsers/soft-> use SOCKS5 {bind}:{socks_port}

  Test in another terminal:
    curl -x socks5h://{bind}:{socks_port} https://api.ipify.org
    curl -x http://{bind}:{http_port} https://api.ipify.org

  {C.GREY}Press Ctrl+C to stop and restore the system proxy.{C.RESET}
""")


def _install_noise_filter():
    """Windows Proactor: browsers hard-reset sockets constantly, which makes
    _ProactorBasePipeTransport._call_connection_lost raise
    ConnectionResetError (WinError 10054) inside a call_soon callback.
    That is pure noise - the tunnel code already handles resets - so we
    silence exactly that case (callback-raised Connection* errors) and
    surface everything else through the default handler."""
    try:
        loop = asyncio.get_running_loop()

        def _handler(l, context):
            exc = context.get("exception")
            if "handle" in context and isinstance(
                    exc, (ConnectionResetError, ConnectionAbortedError,
                          BrokenPipeError)):
                return  # peer went away mid-teardown - harmless
            l.default_exception_handler(context)

        loop.set_exception_handler(_handler)
    except Exception:
        pass


async def cmd_run(args):
    _install_noise_filter()
    api = _make_api(args)
    region = args.region
    if not region or args.best:
        loc = await api.optimal_location()
        region = loc.get("region")
        if loc.get("proxyType") != 0:
            warn("Optimal location is premium-only - falling back to 'nl'.")
            region = "nl"
        else:
            info(f"Auto-selected optimal region: {region} ({loc.get('name')})")
    else:
        locations, free = await api.locations()
        if not any(l.get("region") == region and l.get("proxyType") == 0 for l in free):
            warn(f"'{region}' is not in the free list - the API may reject it. "
                 f"Free regions: " + ", ".join(sorted(l['region'] for l in free)))

    info(f"Fetching upstream servers for '{region}' ...")
    servers = await api.server_list(region)
    ok(f"{len(servers)} upstream server(s) ready.")

    # sanity check the first server before opening the doors (bounded so a
    # dead region cannot stall startup for minutes)
    first_ip = None
    for s in servers[:2]:
        try:
            first_ip = await asyncio.wait_for(
                check_exit_ip(s, insecure_tls=getattr(args, "insecure", False),
                              connect_timeout=6), timeout=15)
            if first_ip:
                ok(f"Upstream check OK - exit IP: {C.BOLD}{first_ip}{C.RESET}")
                break
        except Exception as e:
            warn(f"First upstream check failed ({e}) - trying next server...")
    if not first_ip:
        warn("Could not verify upstream right now - starting anyway "
             "(tunnels retry other servers automatically).")

    bind = args.bind
    socks_port, http_port = args.socks_port, args.http_port
    # v4.3.0: an unauthenticated non-loopback bind is refused outright
    user = getattr(args, "proxy_user", None)
    pwd = getattr(args, "proxy_pass", None)
    problem = validate_bind_security(bind, user, pwd)
    if problem:
        err(problem)
        return 1
    auth = (user, pwd) if (user and pwd) else None
    stats = Stats()
    factory = TunnelFactory(api, servers, insecure_tls=getattr(args, "insecure", False),
                            on_refresh=None)
    proxy = LocalProxyServer(factory, stats, auth=auth)

    # --- explicit client-task registry: every handler task is tracked so we
    # can cancel and REAP it on shutdown (prevents "Task was destroyed but
    # it is pending!" spam and unretrieved-exception noise).
    client_tasks = set()

    def _spawn(coro):
        t = asyncio.ensure_future(coro)
        client_tasks.add(t)

        def _done(task, _set=client_tasks):
            _set.discard(task)
            if not task.cancelled():
                exc = task.exception()  # retrieve so asyncio stays quiet
                if exc is not None:
                    dim(f"  [conn] handler error: {type(exc).__name__}: {exc}")
        t.add_done_callback(_done)
        return t

    def socks_client_cb(r, w):
        _spawn(proxy.handle_socks5(r, w))

    def http_client_cb(r, w):
        _spawn(proxy.handle_http(r, w))

    socks_srv = await asyncio.start_server(socks_client_cb, bind, socks_port)
    http_srv = await asyncio.start_server(http_client_cb, bind, http_port)
    _print_running(bind, socks_port, http_port, region, auth=auth)
    if auth:
        info("Proxy authentication is ON - clients must supply the "
             "configured user/password.")

    if args.set_system:
        system_on(api.state, bind, http_port, socks_port)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _sigint(*_):
        stop.set()

    if platform.system() != "Windows":
        for s in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(s, _sigint)
    else:
        # Windows: KeyboardInterrupt propagates from asyncio.run
        pass

    try:
        async def reporter():
            while True:
                await asyncio.sleep(30)
                extra = (f" dns_udp={stats.udp_dns}"
                         if stats.udp_dns else "")
                dim(f"  [stats] conns={stats.connections} active={stats.active} "
                    f"failed={stats.failed} up={stats.bytes_up // 1024} KiB "
                    f"down={stats.bytes_down // 1024} KiB{extra}"
                    f"{stats.reasons_summary()}")

        rep = asyncio.ensure_future(reporter())
        try:
            await stop.wait()
        except asyncio.CancelledError:
            raise
        except KeyboardInterrupt:
            pass
    except KeyboardInterrupt:
        pass
    except asyncio.CancelledError:
        pass
    finally:
        # ---- graceful, warning-free shutdown ----
        # NOTE: on Python 3.12.1+ Server.wait_closed() waits for ALL client
        # handlers to finish, so we must cancel+reap handlers BEFORE awaiting
        # it, otherwise active/idle connections deadlock the shutdown.
        socks_srv.close()
        http_srv.close()
        if rep is not None:
            try:
                rep.cancel()
                await rep
            except (asyncio.CancelledError, Exception):
                pass
        for t in list(client_tasks):
            t.cancel()
        if client_tasks:
            await asyncio.gather(*client_tasks, return_exceptions=True)
        # v1.2.5: bound the wait - a Python 3.12.x Server.wait_closed() quirk
        # can hang forever when handlers completed just before close(); the
        # GUI core already bounds this with wait_for(10).
        for srv in (socks_srv, http_srv):
            try:
                await asyncio.wait_for(srv.wait_closed(), timeout=10)
            except Exception:
                pass
        if args.set_system:
            system_off(api.state)
        # dump the last failures with targets so the log file can be
        # pasted for support (console output is tee'd to last-run.log)
        if stats.recent:
            dim("  [conn] last upstream failures:")
            for reason, target in list(stats.recent)[-10:]:
                dim(f"    {reason:<12} {target}")
        info("Shut down cleanly.")


async def cmd_export(args):
    """Print ready-to-use proxy configs for the chosen region.

    v4.3.0: the upstream password is MASKED by default - the old output
    printed live credentials in cleartext, which is dangerously easy to
    paste into a chat or a public issue.  Pass --show-secrets to reveal."""
    api = _make_api(args)
    region = args.region or "nl"
    servers = await api.server_list(region)
    if not servers:
        err(f"No usable servers for region '{region}' right now.")
        return
    s = servers[0]
    host, port = s["addresses"][0], s["port"]
    user, pwd = s["username"], s["password"]
    if getattr(args, "show_secrets", False):
        warn("Output below contains LIVE proxy credentials - "
             "do not paste it into public places.")
        pwd_show = pwd
    else:
        pwd_show = "*" * max(8, min(len(pwd), 12)) + \
            "   (masked - pass --show-secrets to reveal)"
    print(f"\n{C.BOLD}Upstream (VeePN HTTPS proxy) - region {region}{C.RESET}")
    print(f"  host={host}  port={port}")
    print(f"  user={user}")
    print(f"  pass={pwd_show}")
    print(f"\n{C.BOLD}curl examples{C.RESET}")
    print(f'  curl -x "https://{user}:{pwd_show}@{host}:{port}" '
          f"https://api.ipify.org")
    print(f"\n{C.BOLD}Programmatic{C.RESET}")
    print(f'  requests: proxies={{"https": "https://{user}:{pwd_show}@{host}:{port}", '
          f'"http": "https://{user}:{pwd_show}@{host}:{port}"}}')
    print()


# --------------------------------------------------------------- interactive
async def interactive(args):
    banner()
    while True:
        print(f"{C.BOLD}1){C.RESET} Show free locations")
        print(f"{C.BOLD}2){C.RESET} Show my IP")
        print(f"{C.BOLD}3){C.RESET} Full test (token -> server -> exit IP)")
        print(f"{C.BOLD}4){C.RESET} Start proxy (SOCKS5 + HTTP) for a region")
        print(f"{C.BOLD}5){C.RESET} Export upstream config")
        print(f"{C.BOLD}0){C.RESET} Exit")
        try:
            choice = input(f"\n{C.BOLD}Choice> {C.RESET}").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        try:
            if choice == "1":
                await cmd_list(args)
            elif choice == "2":
                await cmd_ip(args)
            elif choice == "3":
                args.region = input("Region [nl]: ").strip() or "nl"
                await cmd_test(args)
            elif choice == "4":
                args.region = input("Region [nl for Amsterdam, or press Enter]: ").strip() or "nl"
                args.best = False
                args.set_system = (input("Set system proxy automatically? [y/N]: ").strip().lower() == "y")
                await cmd_run(args)
            elif choice == "5":
                args.region = input("Region [nl]: ").strip() or "nl"
                await cmd_export(args)
            else:
                print("Bye!")
                break
        except ApiError as e:
            err(f"API error: {e}")
        except KeyboardInterrupt:
            print()
            info("Interrupted - back to menu.")
        print()
    return 0


# -------------------------------------------------------------------- parser
def build_parser():
    p = argparse.ArgumentParser(
        prog="vpeen",
        description="VeePN free extension proxy -> local SOCKS5/HTTP proxy")
    p.add_argument("--state", help="state file path (default: project state.json)")
    p.add_argument("--insecure", action="store_true",
                   help="disable TLS certificate verification (not recommended)")
    sub = p.add_subparsers(dest="cmd")

    sp = sub.add_parser("list", help="list available locations")
    sp.add_argument("--all", action="store_true", help="also show premium ones")

    sub.add_parser("best", help="show optimal location")
    sub.add_parser("ip", help="show your direct IP")

    tp = sub.add_parser("test", help="end-to-end chain test")
    tp.add_argument("region", nargs="?", default=None)
    tp.add_argument("--count", type=int, default=2, help="how many servers to test")

    rp = sub.add_parser("run", help="start local SOCKS5 + HTTP proxies")
    rp.add_argument("--region", default=None, help="free region code (e.g. nl, us-va)")
    rp.add_argument("--best", action="store_true", help="use API optimal location")
    rp.add_argument("--bind", default="127.0.0.1")
    rp.add_argument("--socks-port", type=int, default=DEFAULT_SOCKS_PORT)
    rp.add_argument("--http-port", type=int, default=DEFAULT_HTTP_PORT)
    rp.add_argument("--proxy-user", default=None,
                    help="require user/pass auth on the local proxy "
                         "(mandatory when binding to a non-loopback address)")
    rp.add_argument("--proxy-pass", default=None,
                    help="password for --proxy-user")
    rp.add_argument("--set-system", action="store_true",
                    help="auto-set system proxy (restored on exit)")

    ep = sub.add_parser("export", help="print upstream proxy config")
    ep.add_argument("region", nargs="?", default="nl")
    ep.add_argument("--show-secrets", action="store_true",
                    help="print the upstream password in cleartext "
                         "(masked by default)")

    return p


def main(argv=None):
    ensure_utf8_console()
    argv = argv if argv is not None else sys.argv[1:]
    if not argv:
        return asyncio.run(interactive(_interactive_args()))
    args = build_parser().parse_args(argv)
    try:
        if args.cmd == "list":
            return asyncio.run(cmd_list(args))
        if args.cmd == "best":
            return asyncio.run(cmd_best(args))
        if args.cmd == "ip":
            return asyncio.run(cmd_ip(args))
        if args.cmd == "test":
            return asyncio.run(cmd_test(args))
        if args.cmd == "run":
            return asyncio.run(cmd_run(args))
        if args.cmd == "export":
            return asyncio.run(cmd_export(args))
    except ApiError as e:
        err(f"API error: {e}")
        return 1
    except KeyboardInterrupt:
        print()
        info("Interrupted.")
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
