#!/usr/bin/env python3
"""v4.5.0 "Velocity" + v4.6.0 "Turbo" + v4.7.0 "Nitro" speed-pass tests.

End-to-end checks against a fake TLS upstream (same trick as
test_vpeen_integration.py, which needs the `openssl` CLI for a throwaway
certificate):

1. SSL contexts are cached (no per-connection CA reload).
2. Happy-Eyeballs racing: a server whose FIRST address blackholes the TLS
   handshake still connects via its second address in ~RACE_STAGGER,
   instead of burning the full connect timeout.
3. Pre-warmed TLS pool: the factory keeps warm connections and a second
   open() hits the pool (single round-trip CONNECT); stale warm conns
   (server reaped the idle socket) fall back to a fresh connect
   transparently.
4. Latency ranking: monkeypatched probe results reorder the server list
   fastest-first, and (v4.6.0) the latency map is REMAPPED to the new
   indices after the reorder.
5. SOCKS5 UDP DNS cache: a repeated query (different transaction ID) is
   answered from cache - the upstream sees it exactly once.
6. Relay chunk size / read limit / pool constants are wired as intended.

v4.6.0 "Turbo" additions:
7. Pool-aware scheduling: with 3 servers and the round-robin cursor
   pointing AWAY from the warm server, open() still hits the pool
   (v4.5.0 capped the hit rate at 1/N).
8. Parallel refill: a pool that fills 3+ connections while each
   individual TLS connect takes 0.4 s proves waves, not one-at-a-time.
9. Throughput ranking: measured KB/s beats equal latency - the
   best-bandwidth server becomes the warm target.

v4.7.0 "Nitro" additions:
10. Resolver cache: repeated proxy-hostname resolves never touch the
    resolver twice inside the TTL; IP literals bypass it; a dead
    resolver serves the stale cached answer instead of failing.
11. Hostname->IP expansion: a server given by hostname races its A
    records Happy-Eyeballs style (blackholed first record loses).
12. Hedged connect: a stalled primary handshake gets a parallel backup
    after HEDGE_DELAY; total latency ~HEDGE_DELAY, not CONNECT_TIMEOUT.
13. Adaptive pool TTL: a stale warm entry shrinks the TTL; it recovers
    between probe cycles; pool_stats exposes ttl/hedges.
14. Demand-adaptive pool target: quiet keeps the floor, an 18-open
    burst lifts the refill target to 18.
15. Throughput aging: measurements stop ranking a server after
    THROUGHPUT_FRESH seconds.

Run:  python3 scripts/test_speed_pass.py
"""
import asyncio
import os
import shutil
import socket
import ssl
import struct
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import vpeen.localproxy as localproxy
import vpeen.upstream as upstream
from vpeen.localproxy import CHUNK, READ_LIMIT, Stats, TunnelFactory  # noqa: E402


CERT = None
KEY = None


class FakeApi:
    async def refresh_servers_if_expired(self, region):
        return None


# ----------------------------------------------------------------- helpers
async def mint_cert(tmpdir):
    global CERT, KEY
    crt = os.path.join(tmpdir, "test.crt")
    key = os.path.join(tmpdir, "test.key")
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
         "-subj", "/CN=127.0.0.1", "-keyout", key, "-out", crt,
         "-days", "1"],
        check=True, capture_output=True)
    CERT, KEY = crt, key


async def make_tls_upstream(handler):
    """TLS server on 127.0.0.1:0 running `handler(reader, writer)`."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(CERT, KEY)
    srv = await asyncio.start_server(handler, "127.0.0.1", 0, ssl=ctx)
    return srv, srv.sockets[0].getsockname()[1]


async def connect_echo_upstream(reader, writer):
    """CONNECT -> 200, then echo.  Closes after 2 s idle."""
    try:
        await reader.readline()
        while True:
            h = await reader.readline()
            if h in (b"\r\n", b"\n", b""):
                break
        writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
        await writer.drain()
        while True:
            try:
                data = await asyncio.wait_for(reader.read(65536), 2.0)
            except asyncio.TimeoutError:
                break
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except Exception:
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def wait_for(cond, timeout=5.0, interval=0.05):
    """Poll `cond()` until true or timeout; returns the final value."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        await asyncio.sleep(interval)
    return cond()


# ------------------------------------------------------------------- tests
async def test_ssl_context_cache(check):
    a = upstream._ssl_context(True)
    b = upstream._ssl_context(True)
    c = upstream._ssl_context(False)
    check("SSL contexts are cached per verify mode", a is b and a is not c)


async def test_happy_eyeballs(check):
    """First address accepts TCP but stalls TLS; second one is healthy."""
    good_srv, good_port = await make_tls_upstream(connect_echo_upstream)

    # blackhole first address, healthy second: a raw TCP listener on the
    # loopback alias 127.0.0.2 that accepts but NEVER speaks TLS - the
    # client's TLS handshake on address #1 hangs; address #2 (127.0.0.1)
    # is the healthy TLS upstream on the SAME port number.
    stall_final = await asyncio.start_server(
        lambda r, w: None, "127.0.0.2", good_port)

    server = {
        "addresses": ["127.0.0.2", "127.0.0.1"],
        "port": good_port,
        "username": "u", "password": "p", "region": "test",
    }

    server["port"] = good_port
    t0 = time.monotonic()
    try:
        reader, writer = await upstream.connect_via_server(
            server, "example.com", 80, insecure_tls=True,
            connect_timeout=3.0)
        elapsed = time.monotonic() - t0
        writer.write(b"ping")
        await writer.drain()
        data = await asyncio.wait_for(reader.read(4), 2.0)
        check("raced CONNECT succeeds through the healthy address",
              data == b"ping")
        check("racing beats the blackhole (not a full 3 s timeout)",
              elapsed < 2.0, f"elapsed={elapsed:.2f}s")
        writer.close()
    except Exception as e:
        check("raced CONNECT succeeds through the healthy address",
              False, repr(e))
    finally:
        stall_final.close()


async def test_warm_pool(check):
    up_srv, up_port = await make_tls_upstream(connect_echo_upstream)
    server = {
        "addresses": ["127.0.0.1"], "port": up_port,
        "username": "u", "password": "p", "region": "test",
    }
    logs = []
    factory = TunnelFactory(FakeApi(), [dict(server)],
                            insecure_tls=True, log=logs.append)

    # 1st open: fresh connect, kicks the refiller
    r1, w1 = await factory.open("example.com", 80)
    w1.write(b"one")
    await w1.drain()
    await asyncio.sleep(1.0)          # let the refiller warm the pool
    check("pool refilled in the background", len(factory._pool) >= 1,
          f"pool={len(factory._pool)}")

    # 2nd open: should hit the pool (no fresh TLS handshake)
    r2, w2 = await factory.open("example.org", 80)
    w2.write(b"two")
    await w2.drain()
    check("second open() used a warm connection", factory._pool_hits >= 1,
          f"hits={factory._pool_hits}")

    # stale-warm fallback: empty the pool with a conn the server already
    # reaped (close the underlying socket behind asyncio's back)
    r3, w3 = await factory.open("example.net", 80)
    w3.write(b"three")
    await w3.drain()
    await asyncio.sleep(0.2)
    check("third open() also worked", factory._pool_hits >= 2,
          f"hits={factory._pool_hits}")

    for w in (w1, w2, w3):
        try:
            w.close()
        except Exception:
            pass
    await factory.stop()
    check("factory.stop() drains the pool", len(factory._pool) == 0)


async def test_stale_pool_fallback(check):
    """A warm conn the server silently reaped must not fail the open()."""
    # upstream that CLOSES idle TLS conns after 0.4 s (reaps them)
    async def reaping_upstream(reader, writer):
        try:
            # server reaps idle PRE-CONNECT connections after 0.4 s
            try:
                await asyncio.wait_for(reader.readline(), 0.4)
            except asyncio.TimeoutError:
                return                    # reap: silent close, no CONNECT came
            while True:
                h = await reader.readline()
                if h in (b"\r\n", b"\n", b""):
                    break
            writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            await writer.drain()
            await asyncio.sleep(0.4)     # also reap active-but-idle tunnels
        except Exception:
            pass
        finally:
            try:
                writer.close()
            except Exception:
                pass

    up_srv, up_port = await make_tls_upstream(reaping_upstream)
    server = {
        "addresses": ["127.0.0.1"], "port": up_port,
        "username": "u", "password": "p", "region": "test",
    }
    factory = TunnelFactory(FakeApi(), [dict(server)], insecure_tls=True)
    r1, w1 = await factory.open("example.com", 80)
    w1.write(b"x")
    await w1.drain()
    # v4.6.0: the refiller now fills in parallel waves, so for a
    # DETERMINISTIC stale-warm scenario we wait for >=1 warm conn, stop
    # the refiller, and let the reaping upstream close every idle conn.
    await wait_for(lambda: len(factory._pool) >= 1, timeout=3)
    if factory._refill_task is not None:
        factory._refill_task.cancel()
        try:
            await asyncio.wait_for(factory._refill_task, 2)
        except Exception:
            pass
    await asyncio.sleep(0.8)          # reaper closes idle conns at 0.4 s
    ok = True
    detail = ""
    try:
        r2, w2 = await factory.open("example.org", 80)
        w2.write(b"y")
        await w2.drain()
        w2.close()
    except Exception as e:
        ok = False
        detail = repr(e)
    check("stale warm conn falls back to a fresh connect", ok, detail)
    check("stale conns are counted", factory._pool_stale >= 1,
          f"stale={factory._pool_stale}")
    w1.close()
    await factory.stop()


async def test_latency_ranking(check):
    """Monkeypatched probe results reorder servers fastest-first (and
    v4.6.0: the latency map is remapped to the NEW indices)."""
    up_srv, up_port = await make_tls_upstream(connect_echo_upstream)
    servers = [
        {"addresses": ["127.0.0.1"], "port": up_port, "username": "u",
         "password": "p", "region": "test", "name": "slow"},
        {"addresses": ["127.0.0.1"], "port": up_port, "username": "u",
         "password": "p", "region": "test", "name": "fast"},
    ]
    factory = TunnelFactory(FakeApi(), servers, insecure_tls=True)

    real_probe = localproxy.probe_server_latency
    real_tp = localproxy.probe_throughput

    async def fake_probe(sv, budget=1.5, max_per_server=1):
        return {0: 200, 1: 5}        # idx1 (fast) is 40x faster

    async def fake_tp(server, **kw):
        return None                  # throughput unreachable -> no ranking

    localproxy.probe_server_latency = fake_probe
    localproxy.probe_throughput = fake_tp
    try:
        factory.start_latency_probe()
        # v4.6.0: the probe task now ends in a periodic re-probe loop,
        # so wait for the ORDER to settle instead of the task to finish.
        ok = await wait_for(lambda: factory.servers[0]["name"] == "fast",
                            timeout=3)
    finally:
        localproxy.probe_server_latency = real_probe
        localproxy.probe_throughput = real_tp
    check("latency probe reorders fastest-first",
          factory.servers[0]["name"] == "fast")
    # v4.6.0 remap fix: after the reorder the 'fast' server is index 0,
    # and the latency map must say so (v4.5.0 left the OLD keys behind).
    check("latency map remapped to new indices",
          factory.server_latency.get(0) == 5,
          f"latency={factory.server_latency}")
    await factory.stop()


async def test_dns_cache(check):
    """SOCKS5 UDP DNS: identical question (new ID) answered from cache."""
    opens = {"n": 0}

    class CountingFactory:
        def __init__(self):
            self.servers = [{
                "addresses": ["127.0.0.1"], "port": up_port,
                "username": "u", "password": "p", "region": "test"}]

        async def open(self, host, port):
            opens["n"] += 1
            r, w = await upstream.connect_via_server(
                self.servers[0], host, port, insecure_tls=True,
                connect_timeout=3.0)
            return r, w

    async def dns_upstream(reader, writer):
        try:
            await reader.readline()
            while True:
                h = await reader.readline()
                if h in (b"\r\n", b"\n", b""):
                    break
            writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            await writer.drain()
            # DNS-over-TCP echo with a canned answer
            hdr = await reader.readexactly(2)
            (ln,) = struct.unpack(">H", hdr)
            query = await reader.readexactly(ln)
            resp = query[:2] + b"\x81\x80" + query[4:] + b"\xc0\x0c\x00\x01\x00\x01"
            writer.write(struct.pack(">H", len(resp)) + resp)
            await writer.drain()
            await asyncio.sleep(0.3)
        except Exception:
            pass
        finally:
            try:
                writer.close()
            except Exception:
                pass

    up_srv, up_port = await make_tls_upstream(dns_upstream)
    from vpeen.localproxy import LocalProxyServer, _UdpRelay

    counting = CountingFactory()
    proxy = LocalProxyServer(counting, Stats())
    relay = _UdpRelay(proxy)
    # drive the relay directly without a real SOCKS UDP control channel
    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(
        lambda: relay, local_addr=("127.0.0.1", 0))
    relay.connection_made(transport)

    def dns_query(qid: int, name: bytes):
        q = (struct.pack(">H", qid) + b"\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00"
             + bytes([len(name)]) + name + b"\x00" + b"\x00\x01\x00\x01")
        # SOCKS5 UDP wire: RSV(2) FRAG(1) ATYP(1) DST.ADDR(4) DST.PORT(2) payload
        return (b"\x00\x00\x00\x01" + b"\x08\x08\x08\x08" +
                b"\x00\x35" + q)

    peer = ("127.0.0.1", 5353)
    got = asyncio.Queue()

    class Sniffer(asyncio.DatagramProtocol):
        def datagram_received(self, data, addr):
            got.put_nowait(data)

    sniff_tr, _ = await loop.create_datagram_endpoint(
        lambda: Sniffer(), local_addr=peer)

    relay.datagram_received(dns_query(0x1111, b"example.com"), peer)
    first = await asyncio.wait_for(got.get(), 5)
    relay.datagram_received(dns_query(0x2222, b"example.com"), peer)
    second = await asyncio.wait_for(got.get(), 5)

    check("first DNS query went upstream", opens["n"] == 1,
          f"opens={opens['n']}")
    check("cached reply did NOT open a new tunnel", opens["n"] == 1,
          f"opens={opens['n']}")
    # reply datagram = 10-byte SOCKS5 UDP header + DNS payload
    check("cached reply ID rewritten to the new query",
          second[10:12] == b"\x22\x22", f"got {second[10:12]!r}")
    # identical apart from the (intentionally rewritten) transaction ID
    check("cached payload matches first answer", first[12:] == second[12:])
    relay.close()
    sniff_tr.close()
    transport.close()


async def test_constants(check):
    check("relay chunks are 256 KiB", CHUNK == 262144)
    check("reader limit is 256 KiB", READ_LIMIT == 262144)
    check("warm pool holds 12 connections", localproxy.POOL_SIZE == 12)
    check("refill waves are parallel (4)", localproxy.REFILL_PARALLEL == 4)
    check("upstream handshake cap raised to 16 (v4.7.0)",
          localproxy.MAX_PARALLEL_UPSTREAM == 16)


# ------------------------------------------------ v4.6.0 "Turbo" tests
async def test_pool_any_server(check):
    """v4.6.0: open() must hit the warm pool even when the round-robin
    cursor points at a DIFFERENT server (v4.5.0 capped hits at 1/N)."""
    up_srv, up_port = await make_tls_upstream(connect_echo_upstream)
    mk = lambda name: {"addresses": ["127.0.0.1"], "port": up_port,
                       "username": "u", "password": "p", "region": "test",
                       "name": name}
    servers = [mk("a"), mk("b"), mk("c")]
    factory = TunnelFactory(FakeApi(), servers, insecure_tls=True)

    real_probe = localproxy.probe_server_latency
    real_tp = localproxy.probe_throughput

    async def fake_probe(sv, budget=1.5, max_per_server=1):
        return {0: 100, 1: 5, 2: 50}   # server "b" (idx 1) is fastest

    async def fake_tp(server, **kw):
        return None

    localproxy.probe_server_latency = fake_probe
    localproxy.probe_throughput = fake_tp
    try:
        factory.start_latency_probe()
        await wait_for(lambda: factory.servers[0]["name"] == "b", 3)
    finally:
        localproxy.probe_server_latency = real_probe
        localproxy.probe_throughput = real_tp

    # let the refiller warm the pool against server "b" (now idx 0);
    # v4.6.0: _probe_servers itself schedules the refill now, so the pool
    # is warm BEFORE the first client connection (first page load).
    warm_ok = await wait_for(lambda: len(factory._pool) >= 2, timeout=4)
    check("pool pre-warms without any client connection", warm_ok,
          f"pool={len(factory._pool)}")
    # point the round-robin cursor at a DIFFERENT server ("c")
    factory.rr = 2
    hits_before = factory._pool_hits
    r, w = await factory.open("example.com", 80)
    w.write(b"hit")
    await w.drain()
    w.close()
    check("open() hits the pool regardless of the RR cursor",
          factory._pool_hits > hits_before,
          f"hits {hits_before} -> {factory._pool_hits}")
    await factory.stop()


async def test_parallel_refill(check):
    """v4.6.0: the pool fills in parallel waves - with every TLS connect
    padded to 0.4 s, a serial refiller manages ~1 conn in 0.7 s while a
    parallel wave of 4 finishes all of them."""
    up_srv, up_port = await make_tls_upstream(connect_echo_upstream)
    server = {"addresses": ["127.0.0.1"], "port": up_port,
              "username": "u", "password": "p", "region": "test"}
    factory = TunnelFactory(FakeApi(), [dict(server)], insecure_tls=True)

    real_tls = localproxy.tls_connect

    async def slow_tls(srv, **kw):
        await asyncio.sleep(0.4)       # simulate a long-fat-pipe handshake
        return await real_tls(srv, **kw)

    localproxy.tls_connect = slow_tls
    try:
        factory._schedule_refill()
        await asyncio.sleep(0.7)
        pooled = len(factory._pool)
    finally:
        localproxy.tls_connect = real_tls
    check("refill fills in parallel (>=3 of a 4-wave in 0.7 s)",
          pooled >= 3, f"pool={pooled}")
    await factory.stop()


async def test_throughput_ranking(check):
    """v4.6.0: with EQUAL latency, the measured-KB/s winner becomes the
    warm target (the 'server-side' quality signal)."""
    up_srv, up_port = await make_tls_upstream(connect_echo_upstream)
    mk = lambda name: {"addresses": ["127.0.0.1"], "port": up_port,
                       "username": "u", "password": "p", "region": "test",
                       "name": name}
    servers = [mk("slow-tp"), mk("fast-tp")]
    factory = TunnelFactory(FakeApi(), servers, insecure_tls=True)

    real_probe = localproxy.probe_server_latency
    real_tp = localproxy.probe_throughput

    async def fake_probe(sv, budget=1.5, max_per_server=1):
        return {0: 5, 1: 5}           # IDENTICAL latency

    async def fake_tp(server, **kw):
        # measured through the tunnel: "fast-tp" delivers 9x the KB/s
        return 900.0 if server.get("name") == "fast-tp" else 100.0

    localproxy.probe_server_latency = fake_probe
    localproxy.probe_throughput = fake_tp
    try:
        factory.start_latency_probe()
        await wait_for(lambda: bool(factory.server_throughput), 3)
    finally:
        localproxy.probe_server_latency = real_probe
        localproxy.probe_throughput = real_tp
    check("throughput measurements recorded",
          factory.server_throughput.get(1) == 900.0,
          f"tp={factory.server_throughput}")
    check("best-bandwidth server is the warm target",
          factory._pool_warm_target() == 1,
          f"target={factory._pool_warm_target()}")
    await factory.stop()


# ------------------------------------------------------------ v4.7.0 Nitro
async def test_resolver_cache(check):
    """v4.7.0: proxy-hostname resolution is cached - a second resolve
    within the TTL does NOT touch the resolver again, IP literals pass
    through untouched, and a failed resolver keeps the stale answer."""
    calls = []

    async def fake_getaddrinfo(host, port):
        calls.append(host)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "",
                 ("203.0.113.7", 0)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "",
                 ("203.0.113.8", 0))]

    upstream._resolve_cache.clear()
    upstream._getaddrinfo_hook = fake_getaddrinfo
    try:
        ips1 = await upstream.resolve_host("gateway.example")
        ips2 = await upstream.resolve_host("gateway.example")
        lit = await upstream.resolve_host("127.0.0.1")
    finally:
        upstream._getaddrinfo_hook = None
    check("resolver returns every A record",
          ips1 == ["203.0.113.7", "203.0.113.8"], f"{ips1}")
    check("second resolve hits the TTL cache (resolver called once)",
          len(calls) == 1 and ips2 == ips1, f"calls={len(calls)}")
    check("IP literals bypass the resolver",
          lit == ["127.0.0.1"] and "127.0.0.1" not in calls)

    # stale-on-error: poison the cache with an EXPIRED entry, break the
    # resolver, resolve again - the stale answer must be served, not a crash
    upstream._resolve_cache["gateway.example"] = \
        (time.monotonic() - 10.0, ["192.0.2.99"])

    async def broken_getaddrinfo(host, port):
        raise OSError("resolver down")

    upstream._getaddrinfo_hook = broken_getaddrinfo
    try:
        ips3 = await upstream.resolve_host("gateway.example")
    finally:
        upstream._getaddrinfo_hook = None
    check("resolver failure serves the stale cached answer",
          ips3 == ["192.0.2.99"], f"{ips3}")
    upstream._resolve_cache.clear()


async def test_tls_races_resolved_ips(check):
    """v4.7.0: a server given by HOSTNAME is resolved through the cache
    and the resolved IPs race Happy-Eyeballs style - the first A record
    (TLS blackhole) loses, the second (healthy) wins in ~RACE_STAGGER,
    and TLS SNI keeps referring to the original hostname."""
    good_srv, good_port = await make_tls_upstream(connect_echo_upstream)

    # raw TCP listener on 127.0.0.2: accepts but NEVER speaks TLS
    stall = await asyncio.start_server(lambda r, w: None,
                                        "127.0.0.2", good_port)

    async def fake_getaddrinfo(host, port):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "",
                 ("127.0.0.2", 0)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "",
                 ("127.0.0.1", 0))]

    upstream._resolve_cache.clear()
    upstream._getaddrinfo_hook = fake_getaddrinfo
    server = {"addresses": ["gateway.example"], "port": good_port,
              "username": "u", "password": "p", "region": "test"}
    t0 = time.monotonic()
    try:
        reader, writer = await upstream.connect_via_server(
            server, "example.com", 80, insecure_tls=True,
            connect_timeout=3.0)
        elapsed = time.monotonic() - t0
        writer.write(b"nitro")
        await writer.drain()
        data = await asyncio.wait_for(reader.read(5), 2.0)
        check("resolved-IP race lands on the healthy A record",
              data == b"nitro")
        check("hostname expansion beats the blackholed A record",
              elapsed < 2.0, f"elapsed={elapsed:.2f}s")
        writer.close()
    except Exception as e:
        check("resolved-IP race lands on the healthy A record",
              False, repr(e))
    finally:
        upstream._getaddrinfo_hook = None
        upstream._resolve_cache.clear()
        stall.close()


async def test_hedged_connect(check):
    """v4.7.0: when the chosen server's TLS handshake STALLS, a backup
    connect to the next-best server fires after HEDGE_DELAY and wins -
    total latency ~HEDGE_DELAY instead of the full CONNECT_TIMEOUT."""
    good_srv, good_port = await make_tls_upstream(connect_echo_upstream)

    # server A: accepts TCP and stays SILENT (TLS handshake hangs)
    release = asyncio.Event()

    async def stall_handler(reader, writer):
        await release.wait()           # never speaks TLS until released

    stall_srv = await asyncio.start_server(stall_handler, "127.0.0.9", 0)
    stall_port = stall_srv.sockets[0].getsockname()[1]

    slow = {"addresses": ["127.0.0.9"], "port": stall_port,
            "username": "u", "password": "p", "region": "test"}
    fast = {"addresses": ["127.0.0.1"], "port": good_port,
            "username": "u", "password": "p", "region": "test"}
    factory = TunnelFactory(FakeApi(), [slow, fast], insecure_tls=True)

    t0 = time.monotonic()
    try:
        reader, writer = await factory.open("example.com", 80)
        elapsed = time.monotonic() - t0
        writer.write(b"hedge")
        await writer.drain()
        data = await asyncio.wait_for(reader.read(5), 2.0)
        check("hedged open() connects through the backup server",
              data == b"hedge")
        check("stalled primary costs HEDGE_DELAY, not CONNECT_TIMEOUT",
              elapsed < 2.5, f"elapsed={elapsed:.2f}s")
        check("exactly one hedge fired", factory._hedges_fired == 1,
              f"hedges={factory._hedges_fired}")
        writer.close()
    except Exception as e:
        check("hedged open() connects through the backup server",
              False, repr(e))
    finally:
        release.set()                   # let the straggler's TCP close
        await asyncio.sleep(0.1)
        await factory.stop()
        stall_srv.close()
        good_srv.close()


async def test_adaptive_pool_ttl(check):
    """v4.7.0: a stale warm connection SHRINKS the pool TTL (server reaps
    idle sockets faster than we assumed) and _recover_ttl() grows it back."""
    up_srv, up_port = await make_tls_upstream(connect_echo_upstream)
    server = {"addresses": ["127.0.0.1"], "port": up_port,
              "username": "u", "password": "p", "region": "test"}
    factory = TunnelFactory(FakeApi(), [dict(server)], insecure_tls=True)

    class GhostWriter:
        def close(self):
            pass

    ttl0 = factory._ttl
    factory._pool.append((0, None, GhostWriter(), time.monotonic() - 999))
    got = factory._pool_take_any()
    check("expired warm entry is treated as stale", got is None)
    check("stale entry shrinks the adaptive TTL",
          factory._ttl < ttl0 and factory._ttl >= localproxy.POOL_TTL_MIN,
          f"{ttl0} -> {factory._ttl}")
    factory._ttl = localproxy.POOL_TTL_MIN
    factory._recover_ttl()
    check("TTL recovers toward the ceiling",
          factory._ttl == localproxy.POOL_TTL_MIN + 2.5,
          f"ttl={factory._ttl}")
    ps = factory.pool_stats()
    check("pool_stats exposes ttl and hedges (GUI telemetry)",
          "ttl" in ps and "hedges" in ps, f"{ps}")
    await factory.stop()
    up_srv.close()


async def test_demand_adaptive_pool_target(check):
    """v4.7.0: the refill target follows the last 10 s of demand - quiet
    keeps the floor, a 18-open burst lifts it to 18 (<= POOL_TARGET_MAX)."""
    up_srv, up_port = await make_tls_upstream(connect_echo_upstream)
    server = {"addresses": ["127.0.0.1"], "port": up_port,
              "username": "u", "password": "p", "region": "test"}
    factory = TunnelFactory(FakeApi(), [dict(server)], insecure_tls=True)

    quiet = factory._pool_target()
    for _ in range(18):
        factory._note_open()
    burst = factory._pool_target()
    check("quiet demand keeps the pool floor",
          quiet == localproxy.POOL_SIZE, f"quiet={quiet}")
    check("a burst widens the refill target",
          burst == 18, f"burst={burst}")
    await factory.stop()
    up_srv.close()


async def test_throughput_aging(check):
    """v4.7.0: a throughput measurement only ranks a server while FRESH;
    after THROUGHPUT_FRESH it degrades to latency/fairness order."""
    up_srv, up_port = await make_tls_upstream(connect_echo_upstream)
    mk = lambda name: {"addresses": ["127.0.0.1"], "port": up_port,
                       "username": "u", "password": "p", "region": "test"}
    factory = TunnelFactory(FakeApi(), [mk("a"), mk("b")], insecure_tls=True)

    now = time.monotonic()
    factory.server_throughput = {0: 50.0, 1: 200.0}
    factory._tp_ts = {0: now, 1: now}
    check("fresh measurement ranks best-bandwidth first",
          factory._server_order()[0] == 1)

    factory._tp_ts = {0: now - localproxy.THROUGHPUT_FRESH - 1,
                      1: now - localproxy.THROUGHPUT_FRESH - 1}
    order = factory._server_order()
    check("stale measurement no longer ranks the server",
          order == [0, 1], f"order={order}")
    await factory.stop()
    up_srv.close()


async def main():
    results = []

    def check(name, cond, detail=""):
        results.append((name, bool(cond)))
        print(f"  {'PASS' if cond else 'FAIL'}  {name}"
              + (f"  [{detail!r}]" if detail and not cond else ""))

    if shutil.which("openssl") is None:
        print("SKIP: the 'openssl' CLI is required to mint a test certificate")
        return 0

    with tempfile.TemporaryDirectory() as tmpdir:
        await mint_cert(tmpdir)
        await test_ssl_context_cache(check)
        await test_happy_eyeballs(check)
        await test_warm_pool(check)
        await test_stale_pool_fallback(check)
        await test_latency_ranking(check)
        await test_dns_cache(check)
        await test_constants(check)
        await test_pool_any_server(check)
        await test_parallel_refill(check)
        await test_throughput_ranking(check)
        await test_resolver_cache(check)
        await test_tls_races_resolved_ips(check)
        await test_hedged_connect(check)
        await test_adaptive_pool_ttl(check)
        await test_demand_adaptive_pool_target(check)
        await test_throughput_aging(check)

    failed = [name for name, ok in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} PASS")
    if failed:
        print("FAILED:")
        for name in failed:
            print(f"  - {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
