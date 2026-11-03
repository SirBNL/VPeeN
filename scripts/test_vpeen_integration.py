#!/usr/bin/env python3
"""Integration smoke test: real SOCKS5 + HTTP listeners over a fake
TLS upstream (self-signed cert generated on the fly, factory in
insecure mode).

Verifies the full localproxy stack behaves correctly after the v4.3.1
fixes: handshake, CONNECT relay in both directions, IPv6 targets,
client half-close survival, absolute-form re-encoding, UDP-ASSOCIATE
reply shape.

    python scripts/test_vpeen_integration.py

Requires the `openssl` CLI (only to mint a throwaway test certificate).
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

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from vpeen.localproxy import LocalProxyServer, Stats, TunnelFactory  # noqa: E402

CERT = None
KEY = None

FAKE_SERVERS = [{
    "addresses": ["127.0.0.1"], "port": None,
    "username": "u", "password": "p", "region": "test",
}]


class FakeApi:
    async def refresh_servers_if_expired(self, region):
        return FAKE_SERVERS


async def fake_upstream_tls(reader, writer):
    """Fake VeePN upstream: CONNECT -> 200, then echoes bytes.

    Closes after a short idle period - a real server closes once it has
    finished responding (the proxy cannot forward a client half-close
    through asyncio's SSL transport, by design).
    """
    try:
        await reader.readline()                 # CONNECT host:port ...
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
                break                            # idle -> done responding
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


async def main():
    results = []

    def check(name, cond, detail=""):
        results.append((name, cond))
        print(f"  {'PASS' if cond else 'FAIL'}  {name}"
              + (f"  [{detail!r}]" if detail and not cond else ""))

    # TLS-capable fake upstream
    tls_srv_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls_srv_ctx.load_cert_chain(CERT, KEY)
    up_srv = await asyncio.start_server(
        fake_upstream_tls, "127.0.0.1", 0, ssl=tls_srv_ctx)
    FAKE_SERVERS[0]["port"] = up_srv.sockets[0].getsockname()[1]

    factory = TunnelFactory(FakeApi(), FAKE_SERVERS, insecure_tls=True)
    stats = Stats()
    proxy = LocalProxyServer(factory, stats)

    socks_srv = await asyncio.start_server(proxy.handle_socks5, "127.0.0.1", 0)
    http_srv = await asyncio.start_server(proxy.handle_http, "127.0.0.1", 0)
    socks_port = socks_srv.sockets[0].getsockname()[1]
    http_port = http_srv.sockets[0].getsockname()[1]
    host = b"example.com"

    async def safe_eof(w):
        try:
            w.write_eof()
        except (OSError, RuntimeError):
            pass

    # --- SOCKS5 CONNECT + echo relay ---
    r, w = await asyncio.open_connection("127.0.0.1", socks_port)
    w.write(b"\x05\x01\x00")
    await w.drain()
    greet = await r.readexactly(2)
    check("socks5 greeting ok", greet == b"\x05\x00")
    w.write(b"\x05\x01\x00\x03" + bytes([len(host)]) + host +
            (443).to_bytes(2, "big"))
    await w.drain()
    rep = await r.readexactly(4)
    if rep[3] == 0x01:
        await r.readexactly(6)
    check("socks5 connect reply ok", rep[:2] == b"\x05\x00")
    w.write(b"hello-through-tunnel")
    await w.drain()
    await safe_eof(w)
    echoed = await asyncio.wait_for(r.read(), 10)
    check("socks5 tunnel relays both directions",
          echoed == b"hello-through-tunnel", repr(echoed))
    w.close()

    # --- SOCKS5 IPv6 literal target ---
    r, w = await asyncio.open_connection("127.0.0.1", socks_port)
    w.write(b"\x05\x01\x00")
    await w.drain()
    await r.readexactly(2)
    w.write(b"\x05\x01\x00\x04" + socket.inet_pton(socket.AF_INET6,
                                                   "2001:db8::1") +
            (443).to_bytes(2, "big"))
    await w.drain()
    rep = await r.readexactly(4)
    if rep[3] == 0x01:
        await r.readexactly(6)
    check("socks5 IPv6 target accepted", rep[:2] == b"\x05\x00")
    w.close()

    # --- HTTP CONNECT ---
    r, w = await asyncio.open_connection("127.0.0.1", http_port)
    w.write(b"CONNECT example.com:443 HTTP/1.1\r\nHost: example.com:443\r\n\r\n")
    await w.drain()
    head = await r.readuntil(b"\r\n\r\n")          # full reply head
    check("http CONNECT 200", b"200" in head.split(b"\r\n")[0])
    w.write(b"http-through-tunnel")
    await w.drain()
    await safe_eof(w)
    echoed = await asyncio.wait_for(r.read(), 10)
    check("http CONNECT relays bytes", echoed == b"http-through-tunnel",
          repr(echoed))
    w.close()

    # --- SOCKS5 echo again + half-close semantics (before port rebind) ---
    r, w = await asyncio.open_connection("127.0.0.1", socks_port)
    w.write(b"\x05\x01\x00")
    await w.drain()
    await r.readexactly(2)
    w.write(b"\x05\x01\x00\x03" + bytes([len(host)]) + host +
            (443).to_bytes(2, "big"))
    await w.drain()
    rep = await r.readexactly(4)
    if rep[3] == 0x01:
        await r.readexactly(6)
    check("socks5 reconnect reply ok", rep[:2] == b"\x05\x00")
    w.write(b"half-close-test")
    await w.drain()
    await safe_eof(w)                    # SHUT_WR while reply is pending
    echoed = await asyncio.wait_for(r.read(), 10)
    check("client half-close keeps reply stream alive (v4.3.1 fix)",
          b"half-close-test" in echoed, repr(echoed))
    w.close()

    # --- HTTP absolute-form POST (re-encoded with exact Content-Length) ---
    captured = []

    async def capture_get(reader, writer):
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            if head.upper().startswith(b"CONNECT"):
                # phase 1: upstream CONNECT handshake - accept it, then
                # capture the actual re-encoded request that follows
                writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
                await writer.drain()
                head = await reader.readuntil(b"\r\n\r\n")
            body_len = 0
            for line in head.decode("latin-1").split("\r\n")[1:]:
                if line.lower().startswith("content-length:"):
                    body_len = int(line.split(":")[1])
            body = await reader.readexactly(body_len) if body_len else b""
            captured.append(head + body)
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nhi")
            await writer.drain()
        except Exception:
            pass
        finally:
            try:
                writer.close()
            except Exception:
                pass

    up2 = await asyncio.start_server(
        capture_get, "127.0.0.1", 0, ssl=tls_srv_ctx)
    FAKE_SERVERS[0]["port"] = up2.sockets[0].getsockname()[1]
    factory2 = TunnelFactory(FakeApi(), FAKE_SERVERS, insecure_tls=True)
    proxy2 = LocalProxyServer(factory2, Stats())
    http2 = await asyncio.start_server(proxy2.handle_http, "127.0.0.1", 0)
    p2 = http2.sockets[0].getsockname()[1]
    r, w = await asyncio.open_connection("127.0.0.1", p2)
    w.write(b"POST http://example.com/path?a=1 HTTP/1.1\r\n"
            b"Host: example.com\r\nContent-Length: 5\r\n"
            b"Connection: keep-alive\r\n\r\nhello")
    await w.drain()
    await safe_eof(w)
    resp = await r.read()
    check("absolute-form POST answered", resp.endswith(b"hi"))
    cap = captured[0] if captured else b""
    check("request re-encoded with exactly one Content-Length",
          b"POST /path?a=1 HTTP/1.1\r\n" in cap
          and b"Content-Length: 5" in cap
          and cap.endswith(b"hello")
          and cap.count(b"Content-Length:") == 1, repr(cap))
    w.close()

    # --- SOCKS5 UDP ASSOCIATE reply shape ---
    r, w = await asyncio.open_connection("127.0.0.1", socks_port)
    w.write(b"\x05\x01\x00")
    await w.drain()
    await r.readexactly(2)
    w.write(b"\x05\x03\x00\x01" + b"\x00" * 4 + (0).to_bytes(2, "big"))
    await w.drain()
    rep = await r.readexactly(4)
    n = 4 if rep[3] == 0x01 else 16
    tail = await r.readexactly(n + 2)
    bnd_port = struct.unpack(">H", tail[-2:])[0]
    check("udp associate reply ok", rep[:2] == b"\x05\x00" and bnd_port > 0)
    w.close()
    await asyncio.sleep(0.3)

    for srv in (socks_srv, http_srv, http2, up_srv, up2):
        srv.close()
    await asyncio.sleep(0.1)

    fails = [n for n, c in results if not c]
    print(f"\nRESULT: {len(results) - len(fails)} passed, {len(fails)} failed")
    if fails:
        print("FAILED:", *fails, sep="\n  - ")
        sys.exit(1)


def _mint_test_cert():
    """Create a throwaway self-signed certificate for the fake upstream."""
    global CERT, KEY
    tmpd = tempfile.mkdtemp(prefix="vpeen-test-")
    CERT = os.path.join(tmpd, "cert.pem")
    KEY = os.path.join(tmpd, "key.pem")
    if shutil.which("openssl") is None:
        print("SKIP: the 'openssl' CLI is required to mint a test certificate")
        sys.exit(0)
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048",
         "-keyout", KEY, "-out", CERT, "-days", "1", "-nodes",
         "-subj", "/CN=127.0.0.1"],
        check=True, capture_output=True)


if __name__ == "__main__":
    _mint_test_cert()
    asyncio.run(main())
