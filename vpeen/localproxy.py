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
"""
import asyncio
import time

from .upstream import (SOCK_ERRORS, UpstreamError, connect_via_server,
                       enable_keepalive)

CHUNK = 65536
HANDSHAKE_TIMEOUT = 30        # per-phase client handshake budget
BODY_TIMEOUT = 600            # budget for streaming a request body upstream
MAX_UPSTREAM_ATTEMPTS = 3     # servers tried per connection (bounded latency)
CONNECT_TIMEOUT = 8           # per-server upstream connect budget
COOLDOWN_BASE = 20            # first failure cooldown (seconds)
COOLDOWN_MAX = 600            # max cooldown for a dead server
REFRESH_MIN_INTERVAL = 90     # min seconds between credential re-fetches


class Stats:
    def __init__(self):
        self.connections = 0
        self.active = 0
        self.failed = 0
        self.bytes_up = 0
        self.bytes_down = 0


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
                # clean EOF from one side -> half-close the other writer
                target = upstream_writer if t is up else client_writer
                try:
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
    """

    def __init__(self, api, servers, insecure_tls=False, on_refresh=None):
        self.api = api
        self.servers = list(servers)
        self.insecure_tls = insecure_tls
        self.on_refresh = on_refresh      # callback(new_servers) for tunnel mode
        self.rr = 0
        self._cooldown = {}          # server index -> monotonic deadline
        self._fail_streak = {}       # server index -> consecutive failures
        self._last_refresh = 0.0

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
        """Return the next server index to try, or None if exhausted."""
        n = len(self.servers)
        if n == 0:
            return None
        now = time.monotonic()
        # pass 1: healthy servers in round-robin order
        for i in range(n):
            idx = (self.rr + i) % n
            if self._cooldown.get(idx, 0.0) <= now:
                self.rr = (idx + 1) % n
                return idx
        # pass 2: everything is cooling down - try the one closest to expiry
        idx = min(self._cooldown, key=lambda k: self._cooldown.get(k, 0.0))
        return idx

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
        for attempt in range(MAX_UPSTREAM_ATTEMPTS):
            idx = self._pick_server()
            if idx is None:
                break
            server = self.servers[idx]
            try:
                rw = await connect_via_server(
                    server, target_host, target_port,
                    insecure_tls=self.insecure_tls,
                    connect_timeout=CONNECT_TIMEOUT,
                )
                self._mark_ok(idx)
                return rw
            except UpstreamError as e:
                last = e
                self._mark_fail(idx)
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
    def __init__(self, factory: TunnelFactory, stats: Stats):
        self.factory = factory
        self.stats = stats

    # ------------------------------------------------------------ helpers
    async def _readexactly(self, reader, n):
        return await asyncio.wait_for(reader.readexactly(n), HANDSHAKE_TIMEOUT)

    def _reply_fail(self, client_writer, code):
        try:
            client_writer.write(b"\x05" + bytes([code]) + b"\x00\x01" + b"\x00" * 6)
            client_writer.drain()
            client_writer.close()
        except Exception:
            pass

    # -------------------------------------------------------------- socks5
    async def handle_socks5(self, client_reader, client_writer):
        try:
            # --- greeting
            header = await self._readexactly(client_reader, 2)
            ver, nmethods = header[0], header[1]
            if ver != 5:
                client_writer.close()
                return
            await self._readexactly(client_reader, nmethods)
            client_writer.write(b"\x05\x00")  # no-auth-required
            await client_writer.drain()
            # --- request
            req = await self._readexactly(client_reader, 4)
            _ver, cmd, _rsv, atyp = req
            if cmd != 0x01:  # only CONNECT
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
                    self._reply_fail(client_writer, SOCK_ERRORS["general"])
                    return
                try:
                    host = (await self._readexactly(client_reader, ln)) \
                        .decode("ascii").strip().rstrip(".")
                except UnicodeDecodeError:
                    self._reply_fail(client_writer, SOCK_ERRORS["general"])
                    return
            elif atyp == 0x04:    # IPv6
                raw = await self._readexactly(client_reader, 16)
                import ipaddress
                host = str(ipaddress.IPv6Address(raw))
            else:
                client_writer.write(b"\x05\x08\x00\x01" + b"\x00" * 6)
                await client_writer.drain()
                client_writer.close()
                return
            port_hi, port_lo = await self._readexactly(client_reader, 2)
            port = (port_hi << 8) | port_lo
            if not host or not (0 < port < 65536):
                self._reply_fail(client_writer, SOCK_ERRORS["general"])
                return

            try:
                upstream_reader, upstream_writer = await self.factory.open(host, port)
            except UpstreamError as e:
                self.stats.failed += 1
                code = e.socks_code if e.socks_code != 0 else SOCK_ERRORS["general"]
                self._reply_fail(client_writer, code)
                return
            except Exception:
                self.stats.failed += 1
                self._reply_fail(client_writer, SOCK_ERRORS["general"])
                return

            enable_keepalive(client_writer)
            enable_keepalive(upstream_writer)
            # success reply (bnd.addr/port = 0)
            client_writer.write(b"\x05\x00\x00\x01" + b"\x00" * 6)
            await client_writer.drain()
            self.stats.connections += 1
            await _tunnel(client_writer, upstream_writer,
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

    # ------------------------------------------------------------------ http
    async def handle_http(self, client_reader, client_writer):
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
                except Exception:
                    self.stats.failed += 1
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
                await _tunnel(client_writer, upstream_writer,
                              client_reader, upstream_reader, self.stats)
                return

            # ---- plain HTTP (absolute-form) forwarding
            from urllib.parse import urlsplit
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
            except Exception:
                self.stats.failed += 1
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
                out.append(line)
            out.append("Connection: close")
            out.append("")
            upstream_writer.write(("\r\n".join(out) + "\r\n\r\n").encode("latin-1"))
            if content_length:
                body = await asyncio.wait_for(
                    client_reader.readexactly(content_length), BODY_TIMEOUT)
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
