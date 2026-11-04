#!/usr/bin/env python3
"""v4.5.0 "Velocity" speed-pass tests.

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
   fastest-first.
5. SOCKS5 UDP DNS cache: a repeated query (different transaction ID) is
   answered from cache - the upstream sees it exactly once.
6. Relay chunk size / read limit constants are wired as intended.

Run:  python3 scripts/test_speed_pass.py
"""
import asyncio
import os
import shutil
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
    await asyncio.sleep(0.8)          # pool warms, then server reaps it
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
    """Monkeypatched probe results reorder servers fastest-first."""
    up_srv, up_port = await make_tls_upstream(connect_echo_upstream)
    servers = [
        {"addresses": ["127.0.0.1"], "port": up_port, "username": "u",
         "password": "p", "region": "test", "name": "slow"},
        {"addresses": ["127.0.0.1"], "port": up_port, "username": "u",
         "password": "p", "region": "test", "name": "fast"},
    ]
    factory = TunnelFactory(FakeApi(), servers, insecure_tls=True)

    real_probe = localproxy.probe_server_latency

    async def fake_probe(sv, budget=1.5, max_per_server=1):
        return {0: 200, 1: 5}        # idx1 (fast) is 40x faster

    localproxy.probe_server_latency = fake_probe
    try:
        factory.start_latency_probe()
        await asyncio.wait_for(factory._probe_task, 3)
    finally:
        localproxy.probe_server_latency = real_probe
    check("latency probe reorders fastest-first",
          factory.servers[0]["name"] == "fast")
    check("probe latency recorded", factory.server_latency.get(1) == 5)
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
