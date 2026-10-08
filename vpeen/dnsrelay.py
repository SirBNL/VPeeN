"""
DNS relay for Tunnel (TUN) mode.

The tunnel's virtual adapter points its DNS at the TUN's own address
(198.18.0.1:53).  This module listens there and relays every query as
DNS-over-TCP through the local SOCKS5 proxy, so DNS packets travel the
same VeePN tunnel as everything else (no plaintext leak, no UDP needed,
user's real DNS settings are never touched).

Wire format notes (RFC 1035):
  * DNS-over-TCP prefixes every message with a 2-byte big-endian length.
  * UDP has no prefix.  If a reply is too large for a UDP datagram we
    set the TC (truncated) flag in its header so the stub resolver
    retries over TCP - a tiny, safe header edit.
"""
import asyncio
import collections
import struct
import time

DEFAULT_RESOLVERS = [
    ("1.1.1.1", 53),       # Cloudflare
    ("8.8.8.8", 53),       # Google
    ("9.9.9.9", 53),       # Quad9
]

UDP_REPLY_SAFE = 1200          # keep UDP replies below typical MTU
QUERY_TIMEOUT = 8.0
CACHE_TTL = 30.0
CACHE_MAX = 1024


def _set_tc(payload: bytes) -> bytes:
    """Set the TC flag in a DNS message header (byte 2, bit 0x0200)."""
    if len(payload) < 12:
        return payload
    b = bytearray(payload)
    b[2] |= 0x02
    return bytes(b)


def _servfail(query: bytes) -> bytes:
    """Minimal SERVFAIL reply echoing the question id."""
    if len(query) < 2:
        return b""
    return bytes([query[0], query[1], 0x81, 0x82, 0, 0, 0, 0, 0, 0, 0, 0])


class DNSRelay:
    """UDP+TCP :53 forwarder that tunnels DNS via `socks_dial`."""

    def __init__(self, socks_dial, bind_ip: str = "198.18.0.1", port: int = 53,
                 resolvers=None, log=None):
        """
        socks_dial: async (host, port) -> (reader, writer)  - opens a TCP
                    connection to the target through the tunnel (our local
                    SOCKS5 via the VeePN upstream).
        """
        self.socks_dial = socks_dial
        self.bind_ip = bind_ip
        self.port = port
        self.resolvers = list(resolvers or DEFAULT_RESOLVERS)
        self.log = log or (lambda msg, lvl="info": None)
        self._servers = []
        self._cache: "collections.OrderedDict[bytes, tuple[float, bytes]]" = \
            collections.OrderedDict()
        self._queries = 0

    # ------------------------------------------------------------------ api
    async def start(self):
        udp = await asyncio.get_running_loop().create_datagram_endpoint(
            lambda: _UDPProto(self), local_addr=(self.bind_ip, self.port))
        self._servers.append(udp)
        tcp = await asyncio.start_server(self._tcp_client,
                                         self.bind_ip, self.port)
        self._servers.append(tcp)
        self.log(f"DNS relay listening on {self.bind_ip}:{self.port} "
                 f"(TCP relay through tunnel)", "ok")

    async def stop(self):
        for s in self._servers:
            try:
                s.close()
            except Exception:
                pass
        self._servers.clear()

    # ---------------------------------------------------------------- cache
    def _cache_get(self, key):
        hit = self._cache.get(key)
        if not hit:
            return None
        ts, payload = hit
        if time.monotonic() - ts > CACHE_TTL:
            self._cache.pop(key, None)
            return None
        self._cache.move_to_end(key)
        return payload

    def _cache_put(self, key, payload):
        self._cache[key] = (time.monotonic(), payload)
        self._cache.move_to_end(key)
        while len(self._cache) > CACHE_MAX:
            self._cache.popitem(last=False)

    # -------------------------------------------------------------- upstream
    async def _relay_tcp_dns(self, query: bytes) -> bytes:
        """Send one DNS query over TCP through the tunnel, return response."""
        last_err = None
        for host, port in self.resolvers:
            try:
                reader, writer = await asyncio.wait_for(
                    self.socks_dial(host, port), timeout=QUERY_TIMEOUT)
                writer.write(struct.pack(">H", len(query)) + query)
                await writer.drain()
                hdr = await asyncio.wait_for(reader.readexactly(2), 8)
                (ln,) = struct.unpack(">H", hdr)
                if ln == 0 or ln > 65535:
                    raise ValueError("bad TCP-DNS length")
                body = await asyncio.wait_for(reader.readexactly(ln), 8)
                try:
                    writer.close()
                except Exception:
                    pass
                return body
            except Exception as e:
                last_err = e
        raise OSError(f"all resolvers failed ({last_err})")

    async def resolve(self, query: bytes) -> bytes:
        """Full query -> response (used by UDP and TCP paths)."""
        self._queries += 1
        cached = self._cache_get(query)
        if cached is not None:
            return cached
        resp = await self._relay_tcp_dns(query)
        # big responses can't travel back over UDP safely -> ask stub to retry TCP
        if len(resp) > UDP_REPLY_SAFE:
            resp = _set_tc(resp[:512])
        self._cache_put(query, resp)
        return resp

    # ------------------------------------------------------------------ tcp
    async def _tcp_client(self, reader, writer):
        """Direct DNS-over-TCP client (length-framed passthrough)."""
        try:
            while True:
                hdr = await asyncio.wait_for(reader.readexactly(2), 10)
                (ln,) = struct.unpack(">H", hdr)
                if ln == 0 or ln > 65535:
                    break
                query = await asyncio.wait_for(reader.readexactly(ln), 10)
                try:
                    resp = await self.resolve(query)
                except OSError:
                    resp = _servfail(query)
                writer.write(struct.pack(">H", len(resp)) + resp)
                await writer.drain()
        except (asyncio.IncompleteReadError, asyncio.TimeoutError,
                ConnectionError, OSError):
            pass
        finally:
            try:
                writer.close()
            except Exception:
                pass


class _UDPProto(asyncio.DatagramProtocol):
    def __init__(self, relay: DNSRelay):
        self.relay = relay
        self.transport = None
        self._tasks = set()    # anchor tasks - asyncio only holds weak refs

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        try:
            t = asyncio.get_running_loop().create_task(self._handle(data, addr))
        except RuntimeError:
            return
        self._tasks.add(t)

        def _reap(task, _s=None):
            self._tasks.discard(task)
            if not task.cancelled():
                exc = task.exception()
                if exc is not None:
                    try:
                        self.relay.log(f"dns udp handler error: {exc}", "warn")
                    except Exception:
                        pass

        t.add_done_callback(_reap)

    async def _handle(self, data, addr):
        if len(data) < 12 or len(data) > 9000:
            return
        try:
            resp = await self.relay.resolve(data)
        except OSError:
            resp = _servfail(data)
        try:
            self.transport.sendto(resp, addr)
        except Exception:
            pass
