"""
Upstream connector: tunnels a client connection through the VeePN
HTTPS (CONNECT-over-TLS) proxy with basic auth.

    client -> [TLS] -> VeePN proxy (CONNECT host:port) -> target

v1.2 hardening:
* the whole handshake (TCP+TLS+CONNECT+status line) is bounded by
  connect_timeout, and the status line read itself has a deadline, so a
  half-dead server can never stall a client connection for minutes.
* UpstreamError carries the HTTP status (401/403/429/...) so the tunnel
  factory can decide between "refresh credentials" and "just rotate".
* TCP keepalive on the upstream socket to spot dead peers sooner.
"""
import asyncio
import base64
import ipaddress
import json
import os
import re
import socket
import ssl
import sys
import time

from .api import USER_AGENT

# v4.3.1: IPv6 exit IPs are recognised too (validated with ipaddress so
# stray time-like or MAC-like tokens can never be mistaken for an IP).
_IPV4_RE = re.compile(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b")
_IPV6_TOKEN_RE = re.compile(
    r"\b([0-9A-Fa-f]{1,4}(?::[0-9A-Fa-f]{0,4}){2,7})\b")

CHUNK = 65536

# v4.5.0 "Velocity" perf pass -------------------------------------------------
# 256 KiB StreamReader limit for upstream/client connections: bigger reads
# mean fewer event-loop iterations and larger socket writes, which
# measurably lifts throughput on high-bandwidth x high-latency paths
# (the 64 KiB default caps in-flight data at window size, not link speed).
READ_LIMIT = 262144

# Happy-Eyeballs stagger (RFC 8305 style) between a server's addresses:
# the first address connects immediately, each extra one 150 ms later -
# a blackholed first address costs 150 ms instead of the full timeout
# (v4.6.0: 250 ms was needlessly patient when the winner is usually the
# first address that answers at all).
RACE_STAGGER = 0.15

# v4.7.0 "Nitro" ---------------------------------------------------------
# Widest race across one server's resolved addresses.  Server entries
# carry hostnames; one hostname can resolve to several A/AAAA records,
# and only the FIRST used to be raced against the other entries.  Now a
# hostname is expanded to its concrete IPs and all of them race staggered,
# so a blackholed A record costs RACE_STAGGER instead of the full timeout.
MAX_RACE = 4

# Proxy-hostname resolution cache.  Every fresh upstream connect used to
# pay a blocking getaddrinfo (thread-pool hop; on Windows the resolver
# also tries LLMNR/mDNS before DNS, easily 50-200 ms).  Resolved IPs are
# now cached for RESOLVE_TTL and shared by every connect, latency probe
# and refill wave; concurrent resolves of the same host share one task
# (singleflight) so a browser burst produces ONE lookup, not 30.
RESOLVE_TTL = 60.0
RESOLVE_MAX = 64
RESOLVE_TIMEOUT = 5.0

_resolve_cache: "dict[str, tuple[float, list[str]]]" = {}
_resolve_inflight: "dict[str, asyncio.Task]" = {}

# Tests replace this to fake the resolver; production uses the loop's
# getaddrinfo (which runs in the default executor).
_getaddrinfo_hook = None

# Cached SSL contexts (one per verify mode).  Building a context reloads
# the whole system CA bundle - tens of ms on Windows per connection - and
# was previously done for EVERY upstream connect.
_SSL_CTX_CACHE: "dict[bool, ssl.SSLContext]" = {}

# Socket buffers for Windows and macOS (v4.6.0: macOS joined in - its
# default maxsockbuf is notoriously small for long-fat pipes, which caps
# throughput on high-bandwidth x high-latency paths; Linux autotunes
# better WITHOUT an explicit SO_RCVBUF/SO_SNDBUF, which would cap it).
_WIN_BUF = 512 * 1024
_MAC_BUF = 512 * 1024


def _is_ip_literal(host: str) -> bool:
    """True when `host` is already a bare IPv4/IPv6 literal."""
    try:
        ipaddress.ip_address((host or "").strip("[]"))
        return True
    except ValueError:
        return False


async def _resolve_one(host: str) -> list[str]:
    loop = asyncio.get_running_loop()
    stale = _resolve_cache.get(host)
    try:
        getter = _getaddrinfo_hook or loop.getaddrinfo
        infos = await asyncio.wait_for(getter(host, None), RESOLVE_TIMEOUT)
    except Exception:
        # resolver failed: a stale answer beats no answer, and falling
        # back to the hostname itself preserves the pre-4.7 behaviour
        # (asyncio will resolve it internally, sequential per address).
        if stale is not None:
            return stale[1]
        return [host]
    ips: list[str] = []
    for info in infos:
        try:
            ip = info[4][0]
        except (IndexError, TypeError):
            continue
        if ip and ip not in ips:
            ips.append(ip)
    if not ips:
        return [host]
    _resolve_cache[host] = (time.monotonic() + RESOLVE_TTL, ips)
    while len(_resolve_cache) > RESOLVE_MAX:
        _resolve_cache.pop(next(iter(_resolve_cache)))
    return ips


async def resolve_host(host: str) -> list[str]:
    """IPs for a proxy hostname, from the TTL cache (v4.7.0 "Nitro").

    IP literals return immediately (no resolver, no thread hop, no cache).
    Singleflight: while one resolve is in flight every other caller for
    the same host awaits the SAME task instead of piling 30 getaddrinfo
    calls onto the thread pool during a browsing burst."""
    if _is_ip_literal(host):
        return [host]
    hit = _resolve_cache.get(host)
    if hit is not None and time.monotonic() < hit[0]:
        return hit[1]
    task = _resolve_inflight.get(host)
    if task is not None and not task.done():
        return await asyncio.shield(task)
    task = asyncio.ensure_future(_resolve_one(host))
    _resolve_inflight[host] = task
    try:
        return await asyncio.shield(task)
    finally:
        if _resolve_inflight.get(host) is task:
            _resolve_inflight.pop(host, None)


async def _expand_addresses(addresses) -> "list[tuple[str, str]]":
    """Server addresses -> [(connect_ip, tls_server_hostname), ...].

    Hostnames are resolved through the cache and expanded to concrete
    IPs (capped at MAX_RACE); IP literals pass through untouched.  The
    ORIGINAL hostname is kept as server_hostname so TLS SNI and certificate
    validation behave exactly as before resolution was introduced."""
    out: "list[tuple[str, str]]" = []
    for a in addresses:
        if not a:
            continue
        if _is_ip_literal(a):
            out.append((a, a))
            if len(out) >= MAX_RACE:
                break
            continue
        try:
            ips = await resolve_host(a)
        except Exception:
            ips = [a]
        for ip in ips:
            out.append((ip, a))
            if len(out) >= MAX_RACE:
                break
        if len(out) >= MAX_RACE:
            break
    if not out:
        first = addresses[0] if addresses else ""
        out = [(first, first)]
    return out


def _ssl_context(insecure: bool = False) -> ssl.SSLContext:
    ctx = _SSL_CTX_CACHE.get(insecure)
    if ctx is None:
        ctx = ssl.create_default_context()
        if insecure:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        _SSL_CTX_CACHE[insecure] = ctx
    return ctx


def _tune_transport(writer) -> None:
    """Best-effort socket tuning on a freshly established transport:
    explicit TCP_NODELAY (asyncio normally sets it, but make it
    contractual) and bigger buffers on Windows, whose defaults are small
    for long-fat pipes."""
    if writer is None:
        return
    try:
        sock = writer.get_extra_info("socket")
        if sock is None:
            return
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except (OSError, AttributeError):
            pass
        # v4.6.0: macOS gets explicit buffers too (Windows keeps them).
        buf = (_MAC_BUF if sys.platform == "darwin"
               else _WIN_BUF if os.name == "nt" else 0)
        if buf:
            for opt in (socket.SO_RCVBUF, socket.SO_SNDBUF):
                try:
                    if sock.getsockopt(socket.SOL_SOCKET, opt) < buf:
                        sock.setsockopt(socket.SOL_SOCKET, opt, buf)
                except (OSError, AttributeError):
                    pass
    except Exception:
        pass


# Hosts probed for the exit IP through a freshly opened tunnel.
# v4.2.2: only api.ipify.org was queried; when it is slow or blocked the
# whole "is the tunnel alive" check fails even though the tunnel is fine.
EXIT_IP_PROBES = (
    ("api.ipify.org", "/?format=json"),
    ("ifconfig.me", "/"),
)

# v4.6.0 "Turbo": throughput-probe endpoint - Cloudflare's public speed
# service (reliable, global, anycast).  Plain HTTP on :80 on purpose: no
# inner TLS, so the probe measures the PROXY path, not another handshake.
THROUGHPUT_HOST = "speed.cloudflare.com"
THROUGHPUT_PATH = "/__down?bytes=131072"

SOCK_ERRORS = {
    "general": 0x01,
    "not_allowed": 0x02,
    "net_unreachable": 0x03,
    "host_unreachable": 0x04,
    "refused": 0x05,
    "ttl": 0x06,
    "not_supported": 0x07,
}


class UpstreamError(Exception):
    def __init__(self, message, socks_code=SOCK_ERRORS["general"], status=None):
        self.socks_code = socks_code
        self.status = status          # HTTP status code of the CONNECT reply
        super().__init__(message)


def _classify_connect_failure(status_line: str):
    """Return (socks_code, http_status or None) for a failed CONNECT."""
    s = status_line.lower()
    try:
        code = int(status_line.split()[1]) if len(status_line.split()) > 1 else None
    except (ValueError, IndexError):
        code = None
    if " 403" in s or " 401" in s:
        return SOCK_ERRORS["not_allowed"], code       # bad/expired credentials
    if " 404" in s or " 410" in s:
        return SOCK_ERRORS["host_unreachable"], code
    if " 429" in s:
        return SOCK_ERRORS["not_allowed"], code       # rate limited -> rotate
    if "refused" in s or " 500" in s or " 502" in s or " 503" in s or " 504" in s:
        return SOCK_ERRORS["refused"], code
    return SOCK_ERRORS["general"], code


def _fmt_target(host: str) -> str:
    """Format a host for the CONNECT request line.

    v4.2.2: bare IPv6 literals produced 'CONNECT 2001:db8::1:443', which is
    ambiguous and rejected by RFC 7231-aware proxies - they must be
    bracketed ('CONNECT [2001:db8::1]:443').  Hostnames and IPv4 pass
    through untouched; already-bracketed hosts are not double-bracketed."""
    host = (host or "").strip()
    if host.startswith("[") and host.endswith("]"):
        return host                       # already bracketed
    if ":" in host:
        return f"[{host}]"
    return host


def enable_keepalive(writer, idle_hint=45):
    """Best-effort TCP keepalive so dead sockets are noticed instead of
    silently half-open piling up (especially useful through NAT)."""
    if writer is None:
        return
    try:
        sock = writer.get_extra_info("socket")
        if sock is None:
            return
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        # fine-tune where the platform supports it (Linux); harmless elsewhere
        TCP_KEEPIDLE = getattr(socket, "TCP_KEEPIDLE", None)
        TCP_KEEPINTVL = getattr(socket, "TCP_KEEPINTVL", None)
        TCP_KEEPCNT = getattr(socket, "TCP_KEEPCNT", None)
        if TCP_KEEPIDLE is not None:
            sock.setsockopt(socket.IPPROTO_TCP, TCP_KEEPIDLE, idle_hint)
        if TCP_KEEPINTVL is not None:
            sock.setsockopt(socket.IPPROTO_TCP, TCP_KEEPINTVL, 10)
        if TCP_KEEPCNT is not None:
            sock.setsockopt(socket.IPPROTO_TCP, TCP_KEEPCNT, 3)
    except Exception:
        pass


async def _single_tls(host: str, port: int, ctx: ssl.SSLContext,
                      timeout: float, limit: int, sni: str = ""):
    """One TCP+TLS connect attempt to a single proxy address.

    `sni` is the ORIGINAL server hostname when `host` is a resolved IP
    (v4.7.0): certificate validation and SNI keep referring to the name
    the API gave us, never to the resolved literal."""
    return await asyncio.wait_for(
        asyncio.open_connection(host, port, ssl=ctx,
                                server_hostname=sni or host, limit=limit),
        timeout=timeout,
    )


async def _race_tls(candidates, port: int, ctx: ssl.SSLContext,
                    timeout: float, limit: int):
    """Happy-Eyeballs (RFC 8305 style) across a server's addresses.

    `candidates` is a list of (connect_ip, tls_server_hostname) pairs
    (v4.7.0: resolved IPs race alongside entry addresses).  Attempt #1
    starts immediately; each further candidate starts RACE_STAGGER
    seconds after the previous one.  The FIRST fully established TLS
    connection wins and the stragglers are cancelled, so one blackholed
    address costs RACE_STAGGER instead of the whole connect timeout
    (the old sequential loop paid up to N x timeout in the worst case).

    Returns (reader, writer, winning_connect_ip) of the winner.
    Raises the last observed error if every attempt fails.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout

    async def _staggered(i: int, host: str, sni: str):
        if i:
            await asyncio.sleep(RACE_STAGGER * i)
            if loop.time() >= deadline:
                raise asyncio.TimeoutError()
        left = max(0.05, deadline - loop.time())
        return await _single_tls(host, port, ctx, left, limit, sni)

    tasks = [asyncio.ensure_future(_staggered(i, h, s))
             for i, (h, s) in enumerate(candidates)]
    last_exc: Exception | None = None
    try:
        pending = set(tasks)
        while pending:
            left = deadline - loop.time()
            if left <= 0:
                for t in pending:
                    t.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                raise last_exc or asyncio.TimeoutError()
            done, pending = await asyncio.wait(
                pending, return_when=asyncio.FIRST_COMPLETED, timeout=left)
            for t in done:
                if t.cancelled():
                    continue
                exc = t.exception()
                if exc is None:
                    winner = t
                    # first success wins -> cancel the rest
                    for other in pending:
                        other.cancel()
                    await asyncio.gather(*pending, return_exceptions=True)
                    reader, writer = winner.result()
                    return reader, writer, candidates[tasks.index(winner)][0]
                last_exc = exc
        raise last_exc or UpstreamError("all addresses failed")
    except UpstreamError:
        raise
    except asyncio.CancelledError:
        for t in tasks:
            if not t.done():
                t.cancel()
        raise
    finally:
        # belt and suspenders: never leave a straggler behind
        for t in tasks:
            if not t.done():
                t.cancel()


async def tls_connect(server: dict, insecure_tls: bool = False,
                      connect_timeout: float = 10.0,
                      limit: int = READ_LIMIT):
    """
    Establish TCP + TLS to the VeePN proxy server (NO CONNECT yet).

    Split out of connect_via_server in v4.5.0 so the local proxy can keep
    a pool of pre-warmed TLS connections: with a warm connection a client
    CONNECT costs a single round-trip instead of TCP + full TLS handshake
    + CONNECT (~3 RTTs) on every single proxied connection.

    Uses the cached SSL context (no CA-bundle reload per connection) and
    races the server's addresses Happy-Eyeballs style (v4.7.0: hostnames
    are resolved through the TTL cache first, so the race covers every
    A/AAAA record AND skips the per-connect getaddrinfo thread hop).

    Returns (reader, writer).
    """
    addresses = server.get("addresses") or []
    port = int(server.get("port") or 0)
    if not addresses or not port:
        raise UpstreamError("Malformed server entry")

    ctx = _ssl_context(insecure_tls)
    candidates = await _expand_addresses(addresses)
    if len(candidates) == 1:
        addr, sni = candidates[0]
        try:
            reader, writer = await _single_tls(addr, port, ctx,
                                               connect_timeout, limit, sni)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            kind = ("handshake timeout" if isinstance(e, asyncio.TimeoutError)
                    else e)
            raise UpstreamError(
                f"Cannot reach upstream proxy {addresses}:{port}: {kind}",
                SOCK_ERRORS["host_unreachable"],
            )
    else:
        try:
            reader, writer, _addr = await _race_tls(
                candidates, port, ctx, connect_timeout, limit)
        except UpstreamError:
            raise
        except asyncio.CancelledError:
            raise
        except Exception as e:
            kind = ("handshake timeout" if isinstance(e, asyncio.TimeoutError)
                    else e)
            raise UpstreamError(
                f"Cannot reach upstream proxy {addresses}:{port}: {kind}",
                SOCK_ERRORS["host_unreachable"],
            )
    _tune_transport(writer)
    enable_keepalive(writer)
    return reader, writer


async def send_connect(reader, writer, server: dict,
                       target_host: str, target_port: int,
                       connect_timeout: float = 10.0):
    """
    Issue `CONNECT host:port` with the server's credentials on an
    established TLS connection and return (reader, writer) of the tunnel
    once the proxy answers 200.

    Raises UpstreamError (with .status where parseable) otherwise; the
    caller owns closing the connection on failure.
    """
    username = server.get("username") or ""
    password = server.get("password") or ""
    target = _fmt_target(target_host)

    auth = base64.b64encode(f"{username}:{password}".encode()).decode()
    req = (
        f"CONNECT {target}:{target_port} HTTP/1.1\r\n"
        f"Host: {target}:{target_port}\r\n"
        f"Proxy-Authorization: Basic {auth}\r\n"
        f"User-Agent: {USER_AGENT}\r\n"
        f"Proxy-Connection: keep-alive\r\n\r\n"
    )
    writer.write(req.encode("latin-1"))
    await asyncio.wait_for(writer.drain(), timeout=connect_timeout)

    # status line with its own deadline - a silent proxy must not
    # hold the client connection hostage
    status_line = (await asyncio.wait_for(
        reader.readline(), timeout=connect_timeout)
    ).decode("latin-1", errors="replace")
    # consume response headers
    while True:
        line = await asyncio.wait_for(reader.readline(),
                                      timeout=connect_timeout)
        if line in (b"\r\n", b"\n", b""):
            break
    if " 200" in status_line:
        return reader, writer
    code, status = _classify_connect_failure(status_line)
    raise UpstreamError(
        f"Upstream CONNECT failed: {status_line.strip()}",
        code, status,
    )


async def connect_via_server(server: dict, target_host: str, target_port: int,
                             insecure_tls: bool = False,
                             connect_timeout: float = 10.0,
                             limit: int = READ_LIMIT):
    """
    Open a TLS connection to the VeePN proxy server, issue CONNECT and return
    (reader, writer) of the tunnel once the proxy answers 200.

    `server` is one entry from the /v3/server/list/ response:
        {addresses: [..], port: int, username: str, password: str, ...}

    v4.5.0: composed of tls_connect() + send_connect() so pre-warmed
    connections can skip straight to the CONNECT step; behaviour and
    error taxonomy are unchanged for existing callers.
    """
    reader, writer = await tls_connect(server, insecure_tls=insecure_tls,
                                       connect_timeout=connect_timeout,
                                       limit=limit)
    try:
        return await send_connect(reader, writer, server, target_host,
                                  target_port,
                                  connect_timeout=connect_timeout)
    except Exception:
        try:
            writer.close()
        except Exception:
            pass
        raise


async def probe_server_latency(servers, budget: float = 1.5,
                               max_per_server: int = 1):
    """
    Concurrent plain-TCP RTT probe of the servers (v4.5.0).

    Returns {index: milliseconds} for every server that answered within
    `budget`; unresponsive/failed servers are simply absent.  Used to
    latency-rank the server list (sticky fastest-first) and to give the
    GUI an honest per-region number measured against the region's BEST
    server, not just its first entry.
    """
    async def one(idx: int, srv: dict):
        addresses = srv.get("addresses") or []
        port = int(srv.get("port") or 0)
        hosts: list[str] = []
        for a in addresses[:max_per_server]:
            if not a:
                continue
            if _is_ip_literal(a):
                hosts.append(a)
            else:
                # v4.7.0: resolved through the shared cache - the probe no
                # longer pays a getaddrinfo per server per re-probe cycle
                # (60 re-probes/hour x N servers used to be N x 60 lookups).
                try:
                    hosts.extend(await resolve_host(a))
                except Exception:
                    hosts.append(a)
        hosts = hosts[:2]
        if not hosts or not port:
            return idx, None
        best = None
        for host in hosts:
            t0 = time.monotonic()
            writer = None
            try:
                _r, writer = await asyncio.wait_for(
                    asyncio.open_connection(host, port), budget)
                ms = (time.monotonic() - t0) * 1000.0
                best = ms if best is None else min(best, ms)
            except Exception:
                pass
            finally:
                if writer is not None:
                    try:
                        writer.close()
                    except Exception:
                        pass
            if best is not None:
                break          # first reachable address is enough to rank
        return idx, best

    tasks = [asyncio.ensure_future(one(i, s)) for i, s in enumerate(servers)]
    if not tasks:
        return {}
    await asyncio.wait(tasks, timeout=budget + 0.5)
    out = {}
    for t in tasks:
        if t.cancelled():
            continue
        exc = t.exception()
        if exc is not None:
            continue
        idx, ms = t.result()
        if ms is not None:
            out[idx] = int(ms)
    return out


async def probe_throughput(server: dict, insecure_tls: bool = False,
                           connect_timeout: float = 6.0,
                           budget: float = 4.0,
                           max_bytes: int = 131072):
    """
    Rough KB/s through ONE candidate server (v4.6.0 "Turbo").

    Downloads 128 KiB from Cloudflare's public speed endpoint THROUGH the
    candidate's tunnel.  This is the "server-side" quality signal a
    client can actually obtain: two servers with identical RTT can
    differ 10x in delivered bandwidth, and only a measured download
    tells them apart.

    Returns KB/s (elapsed measured from tunnel open, so it integrates
    handshake latency + bandwidth), or None on ANY failure - callers
    treat an unmeasured server conservatively instead of guessing.
    """
    t0 = time.monotonic()
    reader, writer = await connect_via_server(
        server, THROUGHPUT_HOST, 80, insecure_tls=insecure_tls,
        connect_timeout=connect_timeout,
    )
    try:
        req = (f"GET {THROUGHPUT_PATH} HTTP/1.1\r\n"
               f"Host: {THROUGHPUT_HOST}\r\n"
               f"User-Agent: {USER_AGENT}\r\n"
               f"Accept: */*\r\n"
               f"Connection: close\r\n\r\n")
        writer.write(req.encode("latin-1"))
        await asyncio.wait_for(writer.drain(), timeout=budget)
        total = 0
        while total < max_bytes:
            try:
                data = await asyncio.wait_for(reader.read(65536),
                                              timeout=budget)
            except asyncio.TimeoutError:
                break
            if not data:
                break
            total += len(data)
        elapsed = time.monotonic() - t0
        # 32 KiB floor: a redirect page or an error body must not be
        # mistaken for a (tiny) throughput measurement.
        if total >= 32768 and elapsed >= 0.05:
            return total / 1024.0 / elapsed
        return None
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def check_exit_ip(server: dict, insecure_tls: bool = False,
                        connect_timeout: float = 10.0) -> str:
    """
    One-shot test: fetch the public IP through the upstream proxy.

    Inside a CONNECT tunnel the CLIENT must do TLS with the target itself
    (the proxy only relays bytes), so after CONNECT we upgrade to TLS like a
    browser would (StreamWriter.start_tls, Python 3.11+).  On older Pythons
    we fall back to the plain-HTTP (port 80) variant of the IP service.

    v4.2.2: two echo hosts are tried instead of one, so a blocked/slow
    ipify no longer makes every healthy tunnel look dead.
    """
    last = None
    for echo_host, path in EXIT_IP_PROBES:
        try:
            ip = await _exit_ip_via(server, echo_host, path,
                                    insecure_tls=insecure_tls,
                                    connect_timeout=connect_timeout)
            if ip:
                return ip
            # tunnel opened but no IP in the answer -> try the next mirror
        except UpstreamError:
            raise            # tunnel itself failed - mirrors will not help
        except Exception as e:
            last = e         # inner TLS/HTTP hiccup -> try the next mirror
    if last is not None:
        raise last
    return None


async def _exit_ip_via(server: dict, echo_host: str, path: str,
                       insecure_tls: bool = False,
                       connect_timeout: float = 10.0) -> str:
    use_inner_tls = hasattr(asyncio.StreamWriter, "start_tls")
    tls_ctx = ssl.create_default_context()
    if insecure_tls:
        tls_ctx.check_hostname = False
        tls_ctx.verify_mode = ssl.CERT_NONE
    port = 443 if use_inner_tls else 80
    reader, writer = await connect_via_server(
        server, echo_host, port, insecure_tls=insecure_tls,
        connect_timeout=connect_timeout,
    )
    try:
        if use_inner_tls:
            # StreamWriter.start_tls upgrades in place and returns None.
            await writer.start_tls(tls_ctx, server_hostname=echo_host)
        req = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {echo_host}\r\n"
            f"User-Agent: {USER_AGENT}\r\n"
            f"Accept: */*\r\n"
            f"Connection: close\r\n\r\n"
        )
        writer.write(req.encode("latin-1"))
        await writer.drain()

        chunks = []
        total = 0
        while total < 1048576:
            try:
                data = await asyncio.wait_for(reader.read(CHUNK), timeout=10)
            except asyncio.TimeoutError:
                break
            if not data:
                break
            chunks.append(data)
            total += len(data)
        text = b"".join(chunks).decode("utf-8", errors="replace")
        body = text.split("\r\n\r\n", 1)[1] if "\r\n\r\n" in text else text
        # v4.3.1: JSON answers first (both families), then regex fallbacks
        # for plain-text mirrors - IPv6 included this time.
        try:
            parsed = json.loads(body.strip())
            if isinstance(parsed, dict) and parsed.get("ip"):
                return str(parsed["ip"]).strip()
        except Exception:
            pass
        m = _IPV4_RE.search(body)
        if m:
            return m.group(1)
        m = _IPV6_TOKEN_RE.search(body)
        if m:
            try:
                return str(ipaddress.ip_address(m.group(1)))
            except ValueError:
                pass
        return None
    finally:
        try:
            writer.close()
        except Exception:
            pass
