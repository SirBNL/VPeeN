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
import socket
import struct
import time

DEFAULT_RESOLVERS = [
    ("1.1.1.1", 53),       # Cloudflare
    ("8.8.8.8", 53),       # Google
    ("9.9.9.9", 53),       # Quad9
]

UDP_REPLY_SAFE = 1200          # keep UDP replies below typical MTU
QUERY_TIMEOUT = 8.0
# v4.7.0: 30 s -> 60 s, aligned with the SOCKS-UDP relay's cache.  A page
# rarely re-resolves within a minute, and every miss was a full tunnel
# round-trip (see localproxy._UdpRelay.DNS_CACHE_TTL).
CACHE_TTL = 60.0
CACHE_MAX = 1024
PIN_TTL = 60                  # v4.8.0: TTL written into pinned A replies


def _qname(query: bytes) -> "tuple[str, int] | None":
    """(name, qtype) of the FIRST question in a DNS message, or None when
    the packet is too malformed to read (v4.8.0 pin support)."""
    try:
        if len(query) < 12:
            return None
        (qdcount,) = struct.unpack("!H", query[4:6])
        if qdcount < 1:
            return None
        pos = 12
        labels = []
        while True:
            ln = query[pos]
            pos += 1
            if ln == 0:
                break
            labels.append(query[pos:pos + ln].decode("ascii", "replace"))
            pos += ln
            if pos >= len(query):
                return None
        (qtype,) = struct.unpack("!H", query[pos:pos + 2])
        return (".".join(labels).lower(), qtype)
    except Exception:
        return None


def _pin_reply(query: bytes, ips: list[str]) -> bytes:
    """Synthesize an answer for a PINNED hostname (v4.8.0).

    While the TUN layer is up, the only IP set the system resolver may
    see for the upstream proxy hostnames is the set that carries anti-loop
    host routes - a live round-robin answer from the relayed resolver
    could hand back an unrouted record and blackhole the machine.  The
    question section is echoed verbatim, answers use compression
    pointers, TTL is short so a post-tunnel stub cache heals fast.
    AAAA questions get an empty NOERROR (pins are IPv4; a fake v6 record
    would just feed unrouted addresses to a v6-capable dialer)."""
    qname, qtype = _qname(query) or ("", 1)
    # question section end = header(12) + qname + qtype(2) + qclass(2)
    try:
        pos = 12
        while query[pos] != 0:
            pos += 1 + query[pos]
        pos += 1 + 4
        question = query[12:pos]
    except Exception:
        return _servfail(query)
    if qtype == 28:                    # AAAA -> "no v6 record", not SERVFAIL
        counts = struct.pack("!HHHH", 1, 0, 0, 0)
        return query[:2] + b"\x81\x80" + counts + question
    v4 = []
    try:
        for ip in ips:
            packed = socket.inet_aton(ip)
            if packed:
                v4.append(packed)
    except OSError:
        v4 = []
    if not v4:
        return _servfail(query)
    counts = struct.pack("!HHHH", 1, len(v4), 0, 0)
    answer = b""
    for packed in v4:
        answer += struct.pack("!HHIH", 0xC00C, 1, 1, PIN_TTL) + \
            struct.pack("!H", 4) + packed
    return query[:2] + b"\x81\x80" + counts + question + answer


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
                 resolvers=None, log=None, pins=None):
        """
        socks_dial: async (host, port) -> (reader, writer)  - opens a TCP
                    connection to the target through the tunnel (our local
                    SOCKS5 via the VeePN upstream).
        pins:       {hostname -> [ip, ...]} - v4.8.0: queries for these
                    hostnames are answered from this snapshot (never
                    relayed), so the OS only ever resolves the proxy
                    servers to IPs that carry anti-loop host routes.
        """
        self.socks_dial = socks_dial
        self.bind_ip = bind_ip
        self.port = port
        self.resolvers = list(resolvers or DEFAULT_RESOLVERS)
        self.log = log or (lambda msg, lvl="info": None)
        self.pins = {str(k).lower(): list(v) for k, v in (pins or {}).items()
                     if v}
        self._servers = []
        self._cache: "collections.OrderedDict[bytes, tuple[float, bytes]]" = \
            collections.OrderedDict()
        self._queries = 0
        self._pin_hits = 0

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
    @staticmethod
    def _question_key(query: bytes) -> bytes:
        """Cache key = the QUESTION section (name+type+class), not the raw
        packet.  Keying on raw bytes meant the cache almost never hit - stub
        resolvers randomise the transaction ID on every query, so two
        lookups of the same name produced different keys.  The reply's ID is
        rewritten to the current query's ID on cache hits (see resolve())."""
        try:
            if len(query) < 12:
                return query
            (qdcount,) = struct.unpack("!H", query[4:6])
            pos = 12
            for _ in range(qdcount):
                while True:
                    ln = query[pos]
                    pos += 1
                    if ln == 0:
                        break
                    pos += ln
                pos += 4                      # qtype + qclass
            return query[12:pos]
        except Exception:
            return query

    async def _relay_tcp_dns(self, query: bytes) -> bytes:
        """Send one DNS query over TCP through the tunnel, return response."""
        last_err = None
        for host, port in self.resolvers:
            writer = None
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
                return body
            except Exception as e:
                last_err = e
            finally:
                # v4.2.1: the socket used to LEAK when the reply read failed
                # (each failed query left an open tunnel + localproxy handler
                # behind - a DNS outage multiplied into a socket famine).
                if writer is not None:
                    try:
                        writer.close()
                    except Exception:
                        pass
        raise OSError(f"all resolvers failed ({last_err})")

    async def resolve(self, query: bytes, udp: bool = False) -> bytes:
        """Full query -> response (used by UDP and TCP paths).

        v4.3.1: `udp=True` (the datagram path) truncates oversized
        responses to a TC-flagged 512-byte stub so the client retries over
        TCP, as RFC 1035 intends.  The TCP path must return the response
        WHOLE - the old code truncated for TCP clients too, so any answer
        over ~1200 bytes (DNSSEC, large record sets) was corrupted for
        DNS-over-TCP clients even though the full response was in cache.

        v4.8.0: pinned hostnames (the upstream proxy servers) are answered
        from the pre-hijack snapshot - see __init__(pins=).
        """
        self._queries += 1
        if self.pins:
            q = _qname(query)
            if q is not None and q[0] in self.pins and q[1] in (1, 28):
                self._pin_hits += 1
                return _pin_reply(query, self.pins[q[0]])
        key = self._question_key(query)
        cached = self._cache_get(key)
        if cached is not None:
            # rewrite the transaction ID so the stub accepts the reply
            resp = query[:2] + cached[2:]
            if udp and len(resp) > UDP_REPLY_SAFE:
                resp = _set_tc(resp[:512])
            return resp
        resp = await self._relay_tcp_dns(query)
        self._cache_put(key, resp)
        # big responses can't travel back over UDP safely -> ask stub to
        # retry over TCP (cache keeps the FULL response; TCP clients get
        # it whole, UDP clients get the TC stub)
        if udp and len(resp) > UDP_REPLY_SAFE:
            resp = _set_tc(resp[:512])
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
            resp = await self.relay.resolve(data, udp=True)
        except OSError:
            resp = _servfail(data)
        try:
            self.transport.sendto(resp, addr)
        except Exception:
            pass
