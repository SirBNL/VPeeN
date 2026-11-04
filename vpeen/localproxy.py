"""
Local proxy servers (pure asyncio):

* SOCKS5  (RFC 1928, CONNECT only, no-auth)  - default 127.0.0.1:1080
* HTTP    (CONNECT + absolute-form requests) - default 127.0.0.1:8080

Both forward every connection through the VeePN HTTPS upstream proxy.

Stability design (v4.1):
* Full TCP half-close semantics - a tunnel stays alive until BOTH
  directions finish, so SSE / long-poll / keep-alive flows (IDEs, AI
  clients) are never cut prematurely.
* Per-server circuit breaker - a failing upstream is skipped with an
  exponential cooldown instead of being retried on every request.
* Credentials are re-fetched whenever the upstream says 401/403
  (rate-limited to once per cooldown, not once per process lifetime).
* Handshake timeouts everywhere so hung sockets never pile up.
* Parallel upstream handshakes are capped (browsers open 30+ sockets
  at once; hammering the free proxy with all of them triggers 429s).
* Every failed connection is counted WITH a reason (http429, timeout,
  tls, ...) so failure counts are never a mystery.

Security (v4.3.0):
* Optional proxy authentication - RFC 1929 username/password for SOCKS5,
  Basic auth for HTTP.  A non-loopback bind without credentials is refused
  by the GUI (see gui._validate_bind_cfg / cli.validate_bind_security): an
  open relay on the LAN would let any device spend your VeePN quota and
  observe your traffic.
* SOCKS5 UDP ASSOCIATE (RFC 1928 §7): DNS datagrams (:53) are relayed as
  DNS-over-TCP THROUGH the tunnel (the upstream itself is TCP-only, so
  general UDP relay is impossible by design); everything else is dropped,
  which is documented behaviour rather than a silent black hole.
"""
import asyncio
import base64
import collections
import hmac
import ipaddress
import struct
import time
from urllib.parse import urlsplit

from .upstream import (READ_LIMIT, SOCK_ERRORS, UpstreamError,
                       connect_via_server, enable_keepalive, probe_server_latency,
                       probe_throughput, send_connect, tls_connect)

CHUNK = 262144              # v4.5.0: 256 KiB relay chunks (was 64 KiB)
HANDSHAKE_TIMEOUT = 30        # per-phase client handshake budget
BODY_TIMEOUT = 600            # budget for streaming a request body upstream
MAX_REQUEST_BODY = 128 * 1024 * 1024   # plain-HTTP re-encode safety cap
MAX_UPSTREAM_ATTEMPTS = 3     # servers tried per connection (bounded latency)
CONNECT_TIMEOUT = 8           # per-server upstream connect budget
COOLDOWN_BASE = 20            # first failure cooldown (seconds)
COOLDOWN_MAX = 600            # max cooldown for a dead server
REFRESH_MIN_INTERVAL = 90     # min seconds between credential re-fetches
MAX_PARALLEL_UPSTREAM = 12    # cap concurrent upstream TLS+CONNECT handshakes
                             # (v4.6.0: 8 -> 12 - browsers open 30+ sockets
                             # per page load and 4 serialized waves of
                             # handshakes was a visible chunk of 'site
                             # loading feels slow'; 12 keeps the free
                             # proxy's 429 reflex out of the blast radius)

# v4.5.0 "Velocity" - pre-warmed TLS connection pool ------------------------
# Every proxied connection used to pay TCP + full TLS handshake + CONNECT
# (~3 round-trips) before the first byte of real data.  The factory now
# keeps a few TLS-established (pre-CONNECT) connections to the current
# best server warm in the background, so a client CONNECT costs a single
# round-trip.  A stale warm connection (server reaped it while idle) is
# detected instantly and transparently retried fresh.
#
# v4.6.0 "Turbo" - the pool became the main event:
# * POOL_SIZE 4 -> 12: a single page load opens 6-30 sockets; a pool of
#   4 meant most connections still paid the full handshake.
# * Pool-aware scheduling (open() now takes ANY live warm connection,
#   not just the round-robin cursor's server - v4.5.0 only matched the
#   chosen index, so with N servers at most 1/N of connects could EVER
#   hit the pool; with 3 servers two thirds of every burst still paid
#   full TLS handshakes for nothing).
# * Parallel refill waves (was: one connection per loop iteration).
POOL_SIZE = 12                # warm connections kept per factory
POOL_IDLE_TTL = 25.0          # seconds before a warm conn is replaced
PROBE_BUDGET = 1.5            # seconds for the startup latency ranking
REFILL_PARALLEL = 4           # concurrent connects per refill wave
REPROBE_INTERVAL = 60.0       # seconds between latency re-probes
THROUGHPUT_CANDIDATES = 3     # servers measured by the throughput probe
REFILL_FAIL_LIMIT = 3         # consecutive refill fails before cooldown


class Stats:
    RECENT_MAX = 30

    def __init__(self):
        self.connections = 0
        self.active = 0
        self.failed = 0
        self.bytes_up = 0
        self.bytes_down = 0
        self.udp_dns = 0                           # v4.3.0 UDP-ASSOCIATE DNS relays
        self.reasons = collections.Counter()      # reason -> count
        self.recent = collections.deque(          # (reason, host:port)
            maxlen=self.RECENT_MAX)

    def note_fail(self, reason: str, target: str = ""):
        """Record one failed connection with its cause (never raises)."""
        try:
            self.failed += 1
            self.reasons[reason] += 1
            self.recent.append((reason, target))
        except Exception:
            pass

    def reasons_summary(self, top=3) -> str:
        """Compact text for the stats line, e.g. ' (http429:20 timeout:4)'."""
        try:
            if not self.reasons:
                return ""
            parts = [f"{r}:{n}" for r, n in self.reasons.most_common(top)]
            more = len(self.reasons) - top
            if more > 0:
                parts.append(f"+{more} more")
            return "  (" + " ".join(parts) + ")"
        except Exception:
            return ""


def fail_reason(e: Exception) -> str:
    """Compact reason tag for a failed upstream open."""
    try:
        if isinstance(e, UpstreamError):
            if e.status:
                return f"http{e.status}"
            text = str(e).lower()
            if "timeout" in text or "timed out" in text:
                return "timeout"
            if "ssl" in text or "certificate" in text:
                return "tls"
            if "getaddrinfo" in text or "nodename" in text or \
                    "name or service" in text or "no such host" in text:
                return "dns"
            if "malformed" in text:
                return "bad-server"
            if "no upstream servers" in text:
                return "no-server"
            return "upstream"
        if isinstance(e, asyncio.TimeoutError):
            return "timeout"
        if isinstance(e, (ConnectionError, OSError)):
            return "net"
        return type(e).__name__
    except Exception:
        return "unknown"


def _socks_udp_reply(ip: str, port: int) -> bytes:
    """BND.ADDR/BND.PORT for a UDP ASSOCIATE reply (v4.3.0, v4/IPv6 aware)."""
    try:
        packed = ipaddress.ip_address(ip).packed
        atyp = 0x01 if len(packed) == 4 else 0x04
    except ValueError:
        packed, atyp = b"\x00\x00\x00\x00", 0x01
    return (b"\x05\x00\x00" + bytes([atyp]) + packed +
            bytes([(port >> 8) & 0xFF, port & 0xFF]))


# ---- v4.5.0: DNS cache helpers (question-section key + ID rewrite) ------
def _dns_question_key(query: bytes) -> bytes:
    """Cache key = the QUESTION section (name+type+class), not the raw
    packet: stub resolvers randomise the transaction ID, so keying on
    raw bytes would make the cache almost never hit."""
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


def _dns_rewrite_id(resp: bytes, qid: bytes) -> bytes:
    """Copy of `resp` with the transaction ID replaced by `qid`."""
    if len(resp) < 2:
        return resp
    return qid + resp[2:]


class _UdpRelay(asyncio.DatagramProtocol):
    """Per-association UDP endpoint (v4.3.0).

    * Only DNS (destination port 53) is relayed - as DNS-over-TCP THROUGH
      the tunnel, because the upstream is an HTTPS CONNECT proxy and can
      never carry UDP itself.
    * Fragmented datagrams (FRAG != 0) and oversized payloads are dropped.
    * Concurrent queries are capped so a burst cannot exhaust the upstream.
    * The endpoint exists only while the TCP control connection lives.
    """

    MAX_DGRAM = 9000             # sanity limit (matches the GUI dnsrelay)
    MAX_PARALLEL_DNS = 8
    DNS_TIMEOUT = 8.0
    # v4.5.0: repeat lookups are answered from a small local cache instead
    # of opening a fresh TLS tunnel for every single query (which used to
    # add ~3 RTTs per uncached lookup - very visible while browsing).
    # v4.6.0: TTL 30 s -> 60 s - a page rarely re-resolves within a minute
    # and every miss was a full tunnel round-trip.
    DNS_CACHE_TTL = 60.0
    DNS_CACHE_MAX = 512

    def __init__(self, proxy: "LocalProxyServer"):
        self.proxy = proxy
        self.transport = None
        self._tasks = set()
        self._sem = asyncio.Semaphore(self.MAX_PARALLEL_DNS)
        self._closed = False
        self._dns_cache = collections.OrderedDict()   # key -> (ts, answer)
        self.dns_cache_hits = 0

    def connection_made(self, transport):
        self.transport = transport

    def error_received(self, exc):
        pass      # ICMP port-unreachable noise from probes is normal

    def local_addr(self):
        try:
            return self.transport.get_extra_info("sockname")[:2]
        except Exception:
            return ("127.0.0.1", 0)

    def close(self):
        self._closed = True
        if self.transport is not None:
            try:
                self.transport.close()
            except Exception:
                pass
        for t in list(self._tasks):
            t.cancel()

    # ------------------------------------------------------- dns cache
    def _dns_cache_get(self, key):
        hit = self._dns_cache.get(key)
        if not hit:
            return None
        ts, payload = hit
        if time.monotonic() - ts > self.DNS_CACHE_TTL:
            self._dns_cache.pop(key, None)
            return None
        self._dns_cache.move_to_end(key)
        return payload

    def _dns_cache_put(self, key, payload):
        self._dns_cache[key] = (time.monotonic(), payload)
        self._dns_cache.move_to_end(key)
        while len(self._dns_cache) > self.DNS_CACHE_MAX:
            self._dns_cache.popitem(last=False)

    def datagram_received(self, data, addr):
        if self._closed or len(data) < 10 or len(data) > self.MAX_DGRAM:
            return
        try:
            t = asyncio.get_running_loop().create_task(self._handle(data, addr))
        except RuntimeError:
            return          # loop already gone - shutting down
        self._tasks.add(t)

        def _reap(task, _s=None):
            self._tasks.discard(task)
            if not task.cancelled():
                exc = task.exception()
                if exc is not None:
                    try:
                        self.proxy.stats.note_fail("udp-dns", repr(exc)[:80])
                    except Exception:
                        pass
        t.add_done_callback(_reap)

    async def _handle(self, data, addr):
        # wire format: RSV(2) FRAG(1) ATYP(1) DST.ADDR(var) DST.PORT(2) payload
        if data[2] != 0:            # FRAG != 0 - fragmentation unsupported
            return
        atyp = data[3]
        try:
            if atyp == 0x01:
                host = ".".join(str(b) for b in data[4:8]); pos = 8
            elif atyp == 0x03:
                ln = data[4]; pos = 5
                host = data[pos:pos + ln].decode("ascii", "replace")
                pos += ln
            elif atyp == 0x04:
                host = str(ipaddress.IPv6Address(data[4:20])); pos = 20
            else:
                return
        except Exception:
            return
        if pos + 2 > len(data):
            return
        port = (data[pos] << 8) | data[pos + 1]
        payload = data[pos + 2:]
        if port != 53 or not payload:
            return      # non-DNS UDP cannot traverse the upstream (documented)

        # v4.5.0: cache hit?  (question-section key, so the stub
        # resolver's randomised transaction ID does not defeat the cache;
        # the reply's ID is rewritten to the query's on the way out)
        key = _dns_question_key(payload)
        cached = self._dns_cache_get(key)
        if cached is not None:
            if self._closed or self.transport is None:
                return
            try:
                resp = _dns_rewrite_id(cached, payload[0:2])
                self.transport.sendto(b"\x00\x00\x00" + data[3:pos + 2] + resp,
                                      addr)
                self.proxy.stats.udp_dns += 1
                self.dns_cache_hits += 1
            except Exception:
                pass
            return

        try:
            async with self._sem:
                resp = await asyncio.wait_for(
                    self._dns_over_tcp(host, payload), self.DNS_TIMEOUT)
        except Exception:
            self.proxy.stats.note_fail("udp-dns", f"{host}:53")
            return
        if resp:
            self._dns_cache_put(key, resp)
        if self._closed or self.transport is None:
            return
        try:
            # reply header: RSV(2) FRAG(0) + the request's ATYP/ADDR/PORT
            # (10 bytes for IPv4 - the FRAG byte must NOT be dropped or
            # strict clients misparse the whole datagram by one byte)
            self.transport.sendto(b"\x00\x00\x00" + data[3:pos + 2] + resp,
                                  addr)
            self.proxy.stats.udp_dns += 1
        except Exception:
            pass

    async def _dns_over_tcp(self, host, query):
        """One DNS query over TCP (:53) through the tunnel."""
        reader, writer = await self.proxy.factory.open(host, 53)
        try:
            writer.write(struct.pack(">H", len(query)) + query)
            await writer.drain()
            hdr = await asyncio.wait_for(reader.readexactly(2),
                                         self.DNS_TIMEOUT)
            (ln,) = struct.unpack(">H", hdr)
            if not (12 <= ln <= 65535):
                raise ValueError("bad DNS-over-TCP length")
            return await asyncio.wait_for(reader.readexactly(ln),
                                          self.DNS_TIMEOUT)
        finally:
            try:
                writer.close()
            except Exception:
                pass


def install_noise_filter():
    """Windows asyncio: browsers hard-reset sockets constantly, which makes
    the transport's _call_connection_lost callback raise
    ConnectionResetError (WinError 10054). That is pure noise - the tunnel
    code already handles resets - so silence exactly that case (callback
    contexts only) and surface everything else to the default handler."""
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

async def _pipe(reader, writer, stats: Stats, direction: str) -> str:
    """Copy reader -> writer until EOF or error.

    Returns "eof" (clean end of stream) or "error" (broken/reset).
    Never raises: every failure mode is folded into the return value so
    the tunnel logic stays simple and tasks are always reaped.
    """
    try:
        while True:
            data = await reader.read(CHUNK)
            if not data:
                return "eof"
            if direction == "up":
                stats.bytes_up += len(data)
            else:
                stats.bytes_down += len(data)
            writer.write(data)
            await writer.drain()
    except asyncio.CancelledError:
        raise
    except Exception:
        return "error"


async def _tunnel(client_writer, upstream_writer,
                  client_reader, upstream_reader, stats: Stats):
    """Relay both directions with correct half-close semantics.

    * One side reaching clean EOF only half-closes the other writer;
      the opposite direction keeps draining (critical for SSE/HTTP/2).
    * A hard error on either side aborts the whole tunnel.
    * The tunnel ends only when both pipes are done.
    """
    stats.active += 1
    up = down = None
    try:
        up = asyncio.ensure_future(
            _pipe(client_reader, upstream_writer, stats, "up"))
        down = asyncio.ensure_future(
            _pipe(upstream_reader, client_writer, stats, "down"))

        pending = {up, down}
        aborted = False
        while pending:
            done, pending = await asyncio.wait(
                pending, return_when=asyncio.FIRST_COMPLETED)
            for t in done:
                outcome = "error"
                if not t.cancelled():
                    exc = t.exception()
                    if exc is None:
                        outcome = t.result()
                if aborted:
                    continue
                if outcome == "error":
                    aborted = True
                    continue
                # clean EOF from one side -> half-close the other writer.
                # v4.3.1: asyncio SSL transports CANNOT half-close
                # (write_eof() raises NotImplementedError, see
                # can_write_eof()).  The old code caught that exception
                # and hard-aborted the tunnel, which CANCELLED the
                # still-draining opposite direction - so any client that
                # half-closed its socket (shutdown(SHUT_WR): nc, some
                # HTTP clients, test harnesses) lost whatever the server
                # was still sending back.  The "full TCP half-close
                # semantics" advertised above never actually worked
                # through the TLS upstream.  Now we half-close only when
                # the transport supports it; otherwise that direction
                # simply stays open and the tunnel still ends when the
                # other side finishes or errors.
                target = upstream_writer if t is up else client_writer
                try:
                    if target.can_write_eof():
                        target.write_eof()
                        await target.drain()
                except Exception:
                    aborted = True

            if aborted and pending:
                for t in pending:
                    t.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                pending = set()
    finally:
        stats.active -= 1
        for t in (up, down):
            if t and not t.done():
                t.cancel()
        for w in (client_writer, upstream_writer):
            try:
                w.close()
            except Exception:
                pass
        for w in (client_writer, upstream_writer):
            try:
                await asyncio.wait_for(w.wait_closed(), timeout=5)
            except Exception:
                pass


class TunnelFactory:
    """Builds upstream tunnels with health-aware server rotation.

    * Round-robin start position, skipping servers in failure cooldown.
    * Bounded attempts per connection (MAX_UPSTREAM_ATTEMPTS) so a bad
      network never translates into minute-long stalls.
    * Re-fetches credentials on 401/403 (at most once per
      REFRESH_MIN_INTERVAL), resetting the breaker afterwards.
    * on_refresh: optional callback(new_servers) - used by tunnel mode
      to keep anti-loop routes in sync after credentials rotate.

    v4.5.0 "Velocity":
    * Pre-warmed TLS pool - see POOL_SIZE above.
    * Startup latency ranking - probe_server_latency() measures every
      server concurrently and reorders the list fastest-first (sticky),
      so round-robin prefers the lowest-RTT server instead of an
      arbitrary one.  Reordering only happens while no failure state
      exists yet (breaker maps are index-keyed; remapping under live
      failures would corrupt them).

    v4.6.0 "Turbo":
    * Pool-aware scheduling - open() takes ANY live warm connection
      (newest first) instead of only the round-robin cursor's server,
      so the pool's hit rate is no longer capped at 1/N by rotation.
    * Throughput ranking - the top candidates are measured with a real
      128 KiB download through each server (see upstream.probe_throughput)
      and the best-bandwidth server wins, not merely the lowest-RTT one.
      RTT says nothing about server load; this is the strongest
      "server-side" quality signal a client can obtain.
    * Periodic latency re-probe (every REPROBE_INTERVAL seconds) keeps
      the ranking honest mid-session WITHOUT ever mutating self.servers
      (breaker maps are index-keyed; only ordering metadata refreshes).
    * Parallel refill waves - see _refill_loop.
    """

    def __init__(self, api, servers, insecure_tls=False, on_refresh=None,
                 log=None):
        self.api = api
        self.servers = list(servers)
        self.insecure_tls = insecure_tls
        self.on_refresh = on_refresh      # callback(new_servers) for tunnel mode
        self.log = log or (lambda msg, lvl="info": None)
        self.rr = 0
        self._cooldown = {}          # server index -> monotonic deadline
        self._fail_streak = {}       # server index -> consecutive failures
        self._last_refresh = 0.0
        # Browsers fire 20-40 sockets at once; serialising upstream
        # handshakes (max 8 in flight) avoids tripping the free proxy's
        # per-credential rate limit, which used to cascade into
        # dozens of failed connections during browsing bursts.
        self._sem = asyncio.Semaphore(MAX_PARALLEL_UPSTREAM)
        # --- v4.5.0: warm pool + latency probe ---
        self._pool = []              # [(idx, reader, writer, created_mono)]
        self._refill_task = None
        self._probe_task = None
        self._closing = False
        self._pool_hits = 0
        self._pool_stale = 0
        self.server_latency = {}     # idx -> ms (from the startup probe)
        # --- v4.6.0: throughput ranking + refill health ---
        self.server_throughput = {}  # idx -> KB/s (128 KiB measured download)
        self._refill_fails = {}      # idx -> consecutive refill failures

    # ------------------------------------------------------ lifecycle
    def start_latency_probe(self):
        """Kick the background latency ranking (safe to call repeatedly)."""
        if self._closing:
            return
        if self._probe_task is None or self._probe_task.done():
            self._probe_task = asyncio.ensure_future(self._probe_servers())

    async def stop(self):
        """Drop warm connections and background tasks (called on core stop)."""
        self._closing = True
        for task in (self._refill_task, self._probe_task):
            if task is not None and not task.done():
                task.cancel()
        for task in (self._refill_task, self._probe_task):
            if task is not None:
                try:
                    await asyncio.wait_for(task, timeout=2)
                except (Exception, asyncio.CancelledError):
                    # v4.6.0: a task cancelled before its coroutine ever
                    # started raises CancelledError (a BaseException, so
                    # 'except Exception' missed it) - swallowing it here is
                    # exactly right: teardown must keep going.
                    pass
        self._pool_clear()

    # ------------------------------------------------------ warm pool
    def _pool_take(self, idx):
        """Pop a live warm connection for server `idx` (newest first).
        Expired entries are closed on the way."""
        now = time.monotonic()
        while self._pool:
            pos = None
            for i in range(len(self._pool) - 1, -1, -1):
                if self._pool[i][0] == idx:
                    pos = i
                    break
            if pos is None:
                return None
            entry = self._pool.pop(pos)
            _i, reader, writer, created = entry
            if now - created > POOL_IDLE_TTL:
                self._pool_stale += 1
                try:
                    writer.close()
                except Exception:
                    pass
                continue
            return reader, writer
        return None

    def _pool_take_any(self):
        """v4.6.0: pop the NEWEST live warm connection across ALL servers.

        Warm connections all target the current best server anyway; what
        changed is WHO gets to use them.  v4.5.0 matched the pool only
        against the round-robin cursor's chosen server, so with N servers
        at most every Nth open() could hit the pool - the rest paid a full
        TLS handshake while perfectly good warm connections sat idle.
        Returns (idx, reader, writer) or None; entries of servers that
        entered cooldown after being pooled are skipped (their
        credentials may have just been rejected)."""
        now = time.monotonic()
        while self._pool:
            pos = None
            for i in range(len(self._pool) - 1, -1, -1):
                if self._cooldown.get(self._pool[i][0], 0.0) <= now:
                    pos = i
                    break
            if pos is None:
                return None
            entry = self._pool.pop(pos)
            idx, reader, writer, created = entry
            if now - created > POOL_IDLE_TTL:
                self._pool_stale += 1
                try:
                    writer.close()
                except Exception:
                    pass
                continue
            return idx, reader, writer
        return None

    def _pool_clear(self, idx=None):
        """Close+drop warm connections (all, or one server's)."""
        keep = []
        for entry in self._pool:
            if idx is None or entry[0] == idx:
                try:
                    entry[2].close()
                except Exception:
                    pass
            else:
                keep.append(entry)
        self._pool = keep

    def _pool_warm_target(self):
        """The server warm connections should target: the current BEST
        healthy server (throughput first, then latency - v4.6.0), WITHOUT
        advancing the round-robin cursor."""
        order = self._server_order()
        return order[0] if order else None

    def _server_order(self):
        """v4.6.0: healthy server indices, best-first.

        Ranking key (per server):
          1. measured throughput (higher wins) - the strongest signal we
             have; unmeasured servers sort after measured ones;
          2. measured latency (lower wins);
          3. round-robin fairness offset - when nothing is measured this
             degrades to the classic round-robin, spreading load across
             servers exactly like v4.5.0 did.

        Only the ITERATION order changes - self.servers is never mutated
        here, so the index-keyed breaker maps stay valid."""
        n = len(self.servers)
        now = time.monotonic()
        healthy = [i for i in range(n) if self._cooldown.get(i, 0.0) <= now]
        if not healthy:
            return []
        tp = self.server_throughput
        lat = self.server_latency

        def _key(i):
            t = tp.get(i)
            return (0 if t is not None else 1,
                    -(t or 0.0),
                    lat.get(i, 999999.0),
                    (i - self.rr) % n)

        return sorted(healthy, key=_key)

    def _schedule_refill(self):
        if self._closing:
            return
        if self._refill_task is None or self._refill_task.done():
            self._refill_task = asyncio.ensure_future(self._refill_loop())

    async def _refill_loop(self):
        """Keep the pool topped up in the background (v4.6.0: in PARALLEL
        waves, bounded by the same handshake semaphore as fresh connects
        so it never steals more than its fair share).

        Courtesy rule: while client-facing connects hold the semaphore, a
        refill wave stands by instead of queueing behind them - the
        browser's 30 sockets must never wait for a background warmer.
        A server whose refills keep failing is put on the regular breaker
        cooldown after REFILL_FAIL_LIMIT strikes, so a dead 'best' server
        stops being warmed."""
        try:
            while not self._closing:
                try:
                    now = time.monotonic()
                    self._pool = [e for e in self._pool
                                  if now - e[3] <= POOL_IDLE_TTL]
                    need = POOL_SIZE - len(self._pool)
                    if need <= 0:
                        await asyncio.sleep(0.5)
                        continue
                    if self._sem.locked():
                        # a browsing burst owns the handshake budget right
                        # now - warming can wait a quarter beat
                        await asyncio.sleep(0.25)
                        continue
                    idx = self._pool_warm_target()
                    if idx is None:
                        await asyncio.sleep(1.0)
                        continue
                    wave = min(need, REFILL_PARALLEL)
                    results = await asyncio.gather(
                        *[self._warm_one(idx, k) for k in range(wave)],
                        return_exceptions=True)
                    added = sum(1 for r in results if r is True)
                    if added:
                        self._refill_fails.pop(idx, None)
                    else:
                        fails = self._refill_fails.get(idx, 0) + 1
                        self._refill_fails[idx] = fails
                        if fails >= REFILL_FAIL_LIMIT:
                            # stop warming a server that keeps refusing
                            self._mark_fail(idx)
                            self.log(f"warm-pool refill failed {fails}x for "
                                     f"server #{idx} - cooling it down",
                                     "warn")
                    await asyncio.sleep(0.2)   # tiny breather between waves
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # network hiccup - pause briefly instead of hot-looping
                    await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            pass

    async def _warm_one(self, idx: int, k: int = 0):
        """Open ONE warm TLS connection to server `idx` (refill wave
        member).  Staggered slightly so a wave does not look like a
        handshake flood to the free proxy's rate limiter."""
        if k:
            await asyncio.sleep(0.06 * k)
        async with self._sem:
            if self._closing:
                return None
            server = self.servers[idx]
            try:
                reader, writer = await tls_connect(
                    server, insecure_tls=self.insecure_tls,
                    connect_timeout=CONNECT_TIMEOUT,
                    limit=READ_LIMIT)
            except Exception:
                return None
        if self._closing:
            try:
                writer.close()
            except Exception:
                pass
            return None
        self._pool.append((idx, reader, writer, time.monotonic()))
        return True

    def pool_stats(self):
        return {"warm": len(self._pool), "hits": self._pool_hits,
                "stale": self._pool_stale}

    # ------------------------------------------------------ latency probe
    async def _probe_servers(self):
        """Rank servers by measured RTT (once, at startup), then by real
        throughput, then keep re-probing latency so a congested server is
        demoted mid-session (v4.6.0)."""
        try:
            measured = await probe_server_latency(self.servers,
                                                  budget=PROBE_BUDGET)
        except Exception:
            measured = {}
        if self._closing:
            return
        if measured:
            self.server_latency = dict(measured)
        # Only reorder while no breaker state exists: _cooldown/_fail_streak
        # are index-keyed and remapping under live failures is not worth it
        # (the probe finishes ~1.5 s after startup, long before first
        # failures can accumulate in practice).
        if measured and not (self._cooldown or self._fail_streak or self._pool):
            order = sorted(
                range(len(self.servers)),
                key=lambda i: (measured.get(i) is None,
                               measured.get(i, 0)),
            )
            if order != list(range(len(self.servers))):
                self.servers = [self.servers[i] for i in order]
                # v4.6.0 FIX: remap the latency map to the NEW indices.
                # v4.5.0 kept the OLD keys, so after any reorder every
                # later latency lookup answered for the wrong server.
                self.server_latency = {new: measured[old]
                                       for new, old in enumerate(order)}
                self.rr = 0
        if measured:
            fastest = min(measured.values())
            self.log(f"latency-ranked {len(measured)}/{len(self.servers)} "
                     f"servers - fastest: {fastest} ms", "ok")

        # v4.6.0: start warming the pool NOW - right after the ranking,
        # BEFORE the throughput probe and long before the user's browser
        # fires its first connection burst.  v4.5.0 only started filling
        # the pool after the FIRST client open(), so the very first page
        # load (the moment users judge 'is it fast?') always paid full
        # TLS handshakes for every socket.
        self._schedule_refill()

        # ---- v4.6.0 "Turbo": the server-side quality signal.  RTT says
        # nothing about load; a 128 KiB download through each of the top
        # candidates says plenty.  Bounded (top-3, ~4 s each, concurrent),
        # and a silent no-op on any failure.
        await self._probe_throughput()

        # ---- v4.6.0: periodic latency re-probe keeps the ranking honest
        # mid-session without ever mutating self.servers.
        while not self._closing:
            await asyncio.sleep(REPROBE_INTERVAL)
            try:
                m = await probe_server_latency(self.servers,
                                               budget=PROBE_BUDGET)
                if m and not self._closing:
                    self.server_latency.update(m)
            except Exception:
                pass

    async def _probe_throughput(self):
        """Measure real download speed through the top candidates and
        rank by it (v4.6.0).  Uses the shared handshake semaphore so the
        probes cannot crowd out client traffic."""
        if self._closing or len(self.servers) < 2:
            return
        top = self._server_order()[:THROUGHPUT_CANDIDATES]
        if not top:
            return
        results = {}

        async def one(idx: int):
            try:
                async with self._sem:
                    if self._closing:
                        return
                    kbps = await asyncio.wait_for(
                        probe_throughput(self.servers[idx],
                                         insecure_tls=self.insecure_tls,
                                         connect_timeout=6,
                                         budget=4.0),
                        timeout=6.5)
            except Exception:
                return
            if kbps:
                results[idx] = kbps

        await asyncio.gather(*[one(i) for i in top])
        if self._closing or not results:
            return
        self.server_throughput = dict(results)
        best_idx, best_kbps = max(results.items(), key=lambda kv: kv[1])
        self.log(f"throughput probe: best server #{best_idx} at "
                 f"{best_kbps:.0f} KB/s ({len(results)}/{len(top)} "
                 f"measured)", "ok")

    def upstream_ips(self):
        """All known upstream proxy addresses (for anti-loop host routes)."""
        ips = []
        for s in self.servers:
            for a in s.get("addresses") or []:
                if a and a not in ips:
                    ips.append(a)
        return ips

    # ----------------------------------------------------------- breaker
    def _pick_server(self):
        """Return the next server index to try, or None if exhausted.

        v4.6.0: pass 0 prefers a server that has live warm connections
        (they are pre-paid - one round-trip instead of three); pass 1
        walks the healthy servers in _server_order() (throughput, then
        latency, then fairness); pass 2 is the old all-cooling fallback
        (closest to expiry)."""
        n = len(self.servers)
        if n == 0:
            return None
        now = time.monotonic()
        if self._pool:
            warm_idx = {e[0] for e in self._pool}
            for i in self._server_order():
                if i in warm_idx:
                    self.rr = (i + 1) % n
                    return i
        order = self._server_order()
        if order:
            i = order[0]
            self.rr = (i + 1) % n
            return i
        # everything is cooling down - try the one closest to expiry
        if self._cooldown:
            return min(self._cooldown, key=lambda k: self._cooldown.get(k, 0.0))
        return None

    def _mark_ok(self, idx):
        self._fail_streak.pop(idx, None)
        self._cooldown.pop(idx, None)

    def _mark_fail(self, idx):
        streak = self._fail_streak.get(idx, 0) + 1
        self._fail_streak[idx] = streak
        cd = min(COOLDOWN_BASE * (2 ** (streak - 1)), COOLDOWN_MAX)
        self._cooldown[idx] = time.monotonic() + cd

    async def _maybe_refresh(self, server):
        if time.monotonic() - self._last_refresh < REFRESH_MIN_INTERVAL:
            return
        self._last_refresh = time.monotonic()
        region = server.get("region") or (
            self.servers[0].get("region") if self.servers else None)
        if not region:
            return
        try:
            new_servers = await asyncio.wait_for(
                self.api.refresh_servers_if_expired(region), timeout=45)
            if new_servers:
                # credentials rotated: warm conns are still TLS-valid but the
                # old entries may not even be in the new list - drop them.
                self._pool_clear()
                self.servers = list(new_servers)
                self._cooldown.clear()
                self._fail_streak.clear()
                self.rr = 0
                if self.on_refresh:
                    try:
                        self.on_refresh(self.servers)
                    except Exception:
                        pass
        except Exception:
            pass

    # -------------------------------------------------------------- open
    async def open(self, target_host, target_port):
        last = None

        # ---- v4.6.0 fast path: ANY live warm connection, newest first.
        # Sending CONNECT on an already-established TLS conn costs one
        # round-trip instead of TCP+TLS+CONNECT (~3 RTTs).  v4.5.0 tried
        # the pool only for the round-robin cursor's chosen server (with
        # N servers at most 1/N of opens could ever hit it); the pool now
        # serves ANY open().  A stale warm conn falls through to the
        # normal fresh-connect path below without burning a rotation slot.
        warm = self._pool_take_any()
        if warm is not None:
            idx, w_reader, w_writer = warm
            server = self.servers[idx]
            try:
                rw = await asyncio.wait_for(
                    send_connect(w_reader, w_writer, server,
                                 target_host, target_port,
                                 connect_timeout=CONNECT_TIMEOUT),
                    timeout=CONNECT_TIMEOUT)
                self._pool_hits += 1
                self._mark_ok(idx)
                self._schedule_refill()
                return rw
            except UpstreamError as e:
                last = e
                if e.status in (401, 403):
                    # real auth rejection: same treatment as fresh
                    self._mark_fail(idx)
                    self._pool_clear(idx)
                    await self._maybe_refresh(server)
                elif e.status == 429:
                    self._mark_fail(idx)
                    self._pool_clear(idx)
                    await asyncio.sleep(0.5)
                else:
                    # server closed the idle conn (stale) or odd reply:
                    # NOT the server's fault - drop every warm conn of
                    # this server; the fresh path below picks up at once.
                    self._pool_stale += 1
                    self._pool_clear(idx)
            except asyncio.CancelledError:
                try:
                    w_writer.close()
                except Exception:
                    pass
                raise
            except Exception:
                # network-level staleness (reset/broken pipe on write)
                self._pool_stale += 1
                self._pool_clear(idx)
                try:
                    w_writer.close()
                except Exception:
                    pass
                # fall through: fresh connect below

        for attempt in range(MAX_UPSTREAM_ATTEMPTS):
            idx = self._pick_server()
            if idx is None:
                break
            server = self.servers[idx]
            try:
                async with self._sem:
                    rw = await connect_via_server(
                        server, target_host, target_port,
                        insecure_tls=self.insecure_tls,
                        connect_timeout=CONNECT_TIMEOUT,
                        limit=READ_LIMIT,
                    )
                self._mark_ok(idx)
                self._schedule_refill()
                return rw
            except UpstreamError as e:
                last = e
                self._mark_fail(idx)
                self._pool_clear(idx)
                if e.status in (401, 403):
                    await self._maybe_refresh(server)
                elif e.status == 429:
                    await asyncio.sleep(0.5)  # brief breather, then rotate
            except Exception as e:  # safety net - never crash a handler
                last = UpstreamError(f"upstream error: {e}")
                self._mark_fail(idx)
        if last is None:
            last = UpstreamError("No upstream servers available")
        raise last


class LocalProxyServer:
    def __init__(self, factory: TunnelFactory, stats: Stats, auth=None):
        self.factory = factory
        self.stats = stats
        # v4.3.0: optional proxy authentication.  A (user, password) tuple
        # enables RFC 1929 username/password auth for SOCKS5 and Basic auth
        # for the HTTP proxy; None keeps the historical no-auth behaviour.
        if auth and auth[0] and auth[1]:
            self.auth = (str(auth[0]), str(auth[1]))
        else:
            self.auth = None

    # ------------------------------------------------------------ helpers
    async def _readexactly(self, reader, n):
        return await asyncio.wait_for(reader.readexactly(n), HANDSHAKE_TIMEOUT)

    async def _read_chunked(self, reader) -> bytes:
        """Decode a chunked request body (v4.2.1: chunked POSTs arriving at
        the plain-HTTP path used to be forwarded header-only, silently
        corrupting the request - the body was never read or re-sent)."""
        body = bytearray()
        while True:
            size_line = await asyncio.wait_for(reader.readline(), BODY_TIMEOUT)
            try:
                size = int(size_line.split(b";")[0].strip() or b"0", 16)
            except ValueError:
                raise ValueError("bad chunk size")
            if size == 0:
                while True:                       # consume trailers
                    t = await asyncio.wait_for(reader.readline(), BODY_TIMEOUT)
                    if t in (b"\r\n", b"\n", b""):
                        break
                return bytes(body)
            if len(body) + size > MAX_REQUEST_BODY:
                raise ValueError("request body too large")
            body += await asyncio.wait_for(reader.readexactly(size), BODY_TIMEOUT)
            await asyncio.wait_for(reader.readexactly(2), BODY_TIMEOUT)  # CRLF

    async def _reply_fail(self, client_writer, code):
        """Send a SOCKS5 failure reply and close. v4.1.3: drain is awaited
        (an unawaited drain coroutine leaked a RuntimeWarning per failure)."""
        try:
            client_writer.write(b"\x05" + bytes([code]) + b"\x00\x01" + b"\x00" * 6)
            await client_writer.drain()
            client_writer.close()
        except Exception:
            pass

    # ------------------------------------------------------------------ auth
    async def _socks5_auth(self, client_reader, client_writer) -> bool:
        """RFC 1929 username/password subnegotiation (v4.3.0).
        Returns True when the client may proceed; the reply is always sent."""
        try:
            head = await self._readexactly(client_reader, 2)
            ver, ulen = head[0], head[1]
            if ver != 1 or ulen == 0:
                raise ValueError("bad auth subnegotiation")
            uname = (await self._readexactly(client_reader, ulen)) \
                .decode("utf-8", "replace")
            plen = (await self._readexactly(client_reader, 1))[0]
            passwd = (await self._readexactly(client_reader, plen)) \
                .decode("utf-8", "replace") if plen else ""
            u_ok = hmac.compare_digest(uname.encode("utf-8"),
                                       self.auth[0].encode("utf-8"))
            p_ok = hmac.compare_digest(passwd.encode("utf-8"),
                                       self.auth[1].encode("utf-8"))
            if u_ok and p_ok:
                client_writer.write(b"\x01\x00")
                await client_writer.drain()
                return True
            raise ValueError("credentials rejected")
        except (asyncio.IncompleteReadError, asyncio.TimeoutError,
                ConnectionError, OSError, ValueError):
            try:
                client_writer.write(b"\x01\x01")
                await client_writer.drain()
            except Exception:
                pass
            try:
                client_writer.close()
            except Exception:
                pass
            return False

    def _http_auth_ok(self, header_lines) -> bool:
        """Proxy-Authorization: Basic check for the HTTP proxy (v4.3.0).
        Constant-time compare; False also when auth is simply missing."""
        expected = "Basic " + base64.b64encode(
            f"{self.auth[0]}:{self.auth[1]}".encode("utf-8")).decode("ascii")
        for line in header_lines:
            name, _, value = line.partition(":")
            if name.strip().lower() == "proxy-authorization":
                return hmac.compare_digest(value.strip().encode("latin-1"),
                                           expected.encode("ascii"))
        return False

    # ------------------------------------------------------- udp associate
    async def _handle_udp_associate(self, client_reader, client_writer):
        """SOCKS5 UDP ASSOCIATE (RFC 1928 section 7), v4.3.0.

        The upstream (VeePN) is an HTTPS CONNECT proxy - TCP only - so a
        general UDP relay is impossible by design.  What CAN be relayed
        safely is DNS: each client datagram for a :53 destination is re-sent
        as a DNS-over-TCP query THROUGH the tunnel and the answer goes back
        over UDP.  Other datagrams are dropped, which keeps tun2socks-based
        setups working (DNS resolves, QUIC apps fall back to TCP) instead of
        blackholing everything.  The association lives until the TCP control
        connection closes."""
        assoc = None
        try:
            sockname = client_writer.get_extra_info("sockname")
            bind_ip = (sockname[0] if isinstance(sockname, tuple)
                       and sockname and sockname[0] else "127.0.0.1")
            assoc = _UdpRelay(self)
            loop = asyncio.get_running_loop()
            await loop.create_datagram_endpoint(
                lambda: assoc, local_addr=(bind_ip, 0))
            bnd_ip, bnd_port = assoc.local_addr()
            client_writer.write(_socks_udp_reply(bnd_ip, bnd_port))
            await client_writer.drain()
            # hold the control connection open until the client goes away
            while True:
                if not await client_reader.read(256):
                    break
        except asyncio.CancelledError:
            raise
        except (asyncio.IncompleteReadError, asyncio.TimeoutError,
                ConnectionError, OSError):
            pass
        except Exception:
            pass
        finally:
            if assoc is not None:
                assoc.close()
            try:
                client_writer.close()
            except Exception:
                pass

    # -------------------------------------------------------------- socks5
    async def handle_socks5(self, client_reader, client_writer):
        # v4.2.2: upstream socket LEAK fix (mirrors the CLI fix).  Everything
        # between a successful factory.open() and the hand-off to _tunnel can
        # raise if the client resets at that exact moment - the old handler
        # closed only the CLIENT side and the upstream tunnel stayed open
        # until keepalive reaped it.  Ownership is explicit now: the finally
        # below closes upstream_writer unless it was handed to _tunnel
        # (which closes both sides; a second close is a harmless no-op).
        upstream_writer = None
        try:
            # --- greeting (v4.3.0: RFC-strict method selection + optional auth)
            header = await self._readexactly(client_reader, 2)
            ver, nmethods = header[0], header[1]
            if ver != 5:
                client_writer.close()
                return
            methods = await self._readexactly(client_reader, nmethods) \
                if nmethods else b""
            if self.auth:
                if 0x02 not in methods:
                    # credentials required but the client offers none
                    client_writer.write(b"\x05\xFF")
                    await client_writer.drain()
                    client_writer.close()
                    return
                client_writer.write(b"\x05\x02")   # username/password
                await client_writer.drain()
                if not await self._socks5_auth(client_reader, client_writer):
                    return
            else:
                if 0x00 not in methods:
                    client_writer.write(b"\x05\xFF")
                    await client_writer.drain()
                    client_writer.close()
                    return
                client_writer.write(b"\x05\x00")  # no-auth-required
                await client_writer.drain()
            # --- request
            req = await self._readexactly(client_reader, 4)
            _ver, cmd, _rsv, atyp = req
            # v4.3.0: CONNECT (0x01) and UDP ASSOCIATE (0x03) are supported;
            # BIND (0x02) and everything else stays RFC-unsupported (0x07)
            if cmd not in (0x01, 0x03):
                client_writer.write(b"\x05\x07\x00\x01" + b"\x00" * 6)
                await client_writer.drain()
                client_writer.close()
                return
            if atyp == 0x01:      # IPv4
                raw = await self._readexactly(client_reader, 4)
                host = ".".join(str(b) for b in raw)
            elif atyp == 0x03:    # domain (already ASCII/punycode on wire)
                ln = (await self._readexactly(client_reader, 1))[0]
                if ln == 0:
                    await self._reply_fail(client_writer, SOCK_ERRORS["general"])
                    return
                try:
                    host = (await self._readexactly(client_reader, ln)) \
                        .decode("ascii").strip().rstrip(".")
                except UnicodeDecodeError:
                    await self._reply_fail(client_writer, SOCK_ERRORS["general"])
                    return
            elif atyp == 0x04:    # IPv6
                raw = await self._readexactly(client_reader, 16)
                host = str(ipaddress.IPv6Address(raw))
            else:
                client_writer.write(b"\x05\x08\x00\x01" + b"\x00" * 6)
                await client_writer.drain()
                client_writer.close()
                return
            port_hi, port_lo = await self._readexactly(client_reader, 2)
            port = (port_hi << 8) | port_lo
            if cmd == 0x03:
                # UDP ASSOCIATE: DST.ADDR/DST.PORT is the client's advertised
                # socket and may legitimately be 0.0.0.0:0 - no validation
                await self._handle_udp_associate(client_reader, client_writer)
                return
            if not host or not (0 < port < 65536):
                await self._reply_fail(client_writer, SOCK_ERRORS["general"])
                return

            try:
                upstream_reader, upstream_writer = await self.factory.open(host, port)
            except UpstreamError as e:
                upstream_writer = None
                self.stats.note_fail(fail_reason(e), f"{host}:{port}")
                code = e.socks_code if e.socks_code != 0 else SOCK_ERRORS["general"]
                await self._reply_fail(client_writer, code)
                return
            except Exception as e:
                upstream_writer = None
                self.stats.note_fail(fail_reason(e), f"{host}:{port}")
                await self._reply_fail(client_writer, SOCK_ERRORS["general"])
                return

            enable_keepalive(client_writer)
            enable_keepalive(upstream_writer)
            # success reply (bnd.addr/port = 0)
            client_writer.write(b"\x05\x00\x00\x01" + b"\x00" * 6)
            await client_writer.drain()
            self.stats.connections += 1
            uw, upstream_writer = upstream_writer, None   # hand-off to _tunnel
            await _tunnel(client_writer, uw,
                          client_reader, upstream_reader, self.stats)
        except asyncio.CancelledError:
            # never swallow cancellation (shutdown depends on it)
            try:
                client_writer.close()
            except Exception:
                pass
            raise
        except (asyncio.IncompleteReadError, asyncio.TimeoutError,
                ConnectionError, OSError):
            # client hung up or stalled mid-handshake - quiet close
            try:
                client_writer.close()
            except Exception:
                pass
        except Exception:
            # safety net - a handler bug must never kill the server
            try:
                client_writer.close()
            except Exception:
                pass
        finally:
            if upstream_writer is not None:
                try:
                    upstream_writer.close()
                except Exception:
                    pass

    # ------------------------------------------------------------------ http
    async def handle_http(self, client_reader, client_writer):
        upstream_writer = None      # leak guard - see handle_socks5 (v4.2.2)
        try:
            try:
                head = await asyncio.wait_for(
                    client_reader.readuntil(b"\r\n\r\n"), HANDSHAKE_TIMEOUT)
            except (asyncio.IncompleteReadError, asyncio.TimeoutError,
                    asyncio.LimitOverrunError):
                client_writer.close()
                return
            lines = head.decode("latin-1").split("\r\n")
            request_line = lines[0]
            parts = request_line.split(" ")
            if len(parts) < 3:
                client_writer.close()
                return
            method, target, httpver = parts[0], parts[1], parts[2]

            # v4.3.0: optional Basic proxy auth for BOTH CONNECT and
            # absolute-form requests; 407 with the right challenge otherwise
            if self.auth and not self._http_auth_ok(lines[1:]):
                client_writer.write(
                    b"HTTP/1.1 407 Proxy Authentication Required\r\n"
                    b"Proxy-Authenticate: Basic realm=\"vpeen\"\r\n"
                    b"Connection: close\r\n\r\n")
                await client_writer.drain()
                client_writer.close()
                return

            if method.upper() == "CONNECT":
                if ":" not in target:
                    client_writer.close()
                    return
                host, p = target.rsplit(":", 1)
                try:
                    port = int(p)
                except ValueError:
                    client_writer.close()
                    return
                try:
                    upstream_reader, upstream_writer = await self.factory.open(host, port)
                except Exception as e:
                    upstream_writer = None
                    self.stats.note_fail(fail_reason(e), f"{host}:{port}")
                    client_writer.write(
                        b"HTTP/1.1 502 Bad Gateway\r\n"
                        b"Proxy-Status: vpeen-upstream-failed\r\n\r\n"
                    )
                    await client_writer.drain()
                    client_writer.close()
                    return
                enable_keepalive(client_writer)
                enable_keepalive(upstream_writer)
                client_writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
                await client_writer.drain()
                self.stats.connections += 1
                uw, upstream_writer = upstream_writer, None   # hand-off
                await _tunnel(client_writer, uw,
                              client_reader, upstream_reader, self.stats)
                return

            # ---- plain HTTP (absolute-form) forwarding
            sp = urlsplit(target)
            host = sp.hostname or ""
            port = sp.port or 80
            if not host:
                client_writer.write(b"HTTP/1.1 400 Bad Request\r\n\r\n")
                await client_writer.drain()
                client_writer.close()
                return
            path = (sp.path or "/") + (("?" + sp.query) if sp.query else "")
            try:
                upstream_reader, upstream_writer = await self.factory.open(host, port)
            except Exception as e:
                upstream_writer = None
                self.stats.note_fail(fail_reason(e), f"{host}:{port}")
                client_writer.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
                await client_writer.drain()
                client_writer.close()
                return

            enable_keepalive(client_writer)
            enable_keepalive(upstream_writer)
            hop = {"proxy-connection", "proxy-authorization", "proxy-authenticate",
                   "keep-alive", "connection", "te", "trailers", "upgrade"}
            out = [f"{method} {path} {httpver}"]
            content_length = None
            chunked = False
            for line in lines[1:]:
                if not line:
                    continue
                name, _, value = line.partition(":")
                lname = name.strip().lower()
                if lname in hop:
                    continue
                if lname == "content-length":
                    try:
                        content_length = int(value.strip())
                    except ValueError:
                        pass
                    # v4.2.3: NEVER forward the client's original
                    # Content-Length - an exact one is re-encoded below,
                    # and a duplicate pair makes strict servers (nginx)
                    # reject the request with 400 Bad Request.
                    continue
                if lname == "transfer-encoding" and "chunked" in value.lower():
                    chunked = True
                    continue      # re-encoded with Content-Length below
                out.append(line)
            # v4.2.1: read the body BEFORE sending headers upstream so the
            # request can always be re-encoded with an exact Content-Length
            # (a header-only request with a detached body used to hang or
            # corrupt POSTs on the plain-HTTP path).
            if chunked:
                body = await self._read_chunked(client_reader)
            elif content_length and content_length > 0:
                body = await asyncio.wait_for(
                    client_reader.readexactly(content_length), BODY_TIMEOUT)
            else:
                body = b""
            out.append("Connection: close")
            out.append(f"Content-Length: {len(body)}")
            # v1.2.4 fix: the old trailing out.append("") made join() emit a
            # THIRD CRLF (double blank line) - tolerated by body-less GETs
            # but it shifted every request body by two bytes.
            upstream_writer.write(("\r\n".join(out) + "\r\n\r\n").encode("latin-1"))
            if body:
                upstream_writer.write(body)
            await upstream_writer.drain()
            self.stats.connections += 1

            # stream response back until EOF (Connection: close)
            while True:
                data = await upstream_reader.read(CHUNK)
                if not data:
                    break
                self.stats.bytes_down += len(data)
                client_writer.write(data)
                await client_writer.drain()
            client_writer.close()
        except asyncio.CancelledError:
            raise
        except (ConnectionError, OSError, asyncio.IncompleteReadError,
                asyncio.TimeoutError, asyncio.LimitOverrunError, ValueError):
            pass
        except Exception:
            pass
        finally:
            try:
                client_writer.close()
            except Exception:
                pass
            if upstream_writer is not None:
                try:
                    upstream_writer.close()
                except Exception:
                    pass
