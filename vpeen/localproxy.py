"""
Local proxy servers (pure asyncio):

* SOCKS5  (RFC 1928, CONNECT only, no-auth)  - default 127.0.0.1:1080
* HTTP    (CONNECT + absolute-form requests) - default 127.0.0.1:8080

Both forward every connection through the VeePN HTTPS upstream proxy.
If the upstream fails (dead server / expired credentials) the next server
from the region's server list is tried; when the list is exhausted it is
re-fetched once from the API before giving up.
"""
import asyncio

CHUNK = 65536

from .upstream import SOCK_ERRORS, UpstreamError, connect_via_server




class Stats:
    def __init__(self):
        self.connections = 0
        self.active = 0
        self.failed = 0
        self.bytes_up = 0
        self.bytes_down = 0


async def _pipe(reader, writer, stats: Stats, direction: str):
    try:
        while True:
            data = await reader.read(CHUNK)
            if not data:
                break
            if direction == "up":
                stats.bytes_up += len(data)
            else:
                stats.bytes_down += len(data)
            writer.write(data)
            await writer.drain()
    except (ConnectionError, asyncio.IncompleteReadError, TimeoutError, OSError):
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def _tunnel(client_reader, client_writer, upstream_reader, upstream_writer,
                  stats: Stats):
    stats.active += 1
    try:
        up_task = asyncio.ensure_future(
            _pipe(client_reader, upstream_writer, stats, "up"))
        down_task = asyncio.ensure_future(
            _pipe(upstream_reader, client_writer, stats, "down"))
        done, pending = await asyncio.wait(
            [up_task, down_task], return_when=asyncio.FIRST_COMPLETED
        )
        for t in pending:
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
    finally:
        stats.active -= 1
        for w in (client_writer, upstream_writer):
            try:
                w.close()
            except Exception:
                pass


class TunnelFactory:
    """Builds upstream tunnels with server rotation + one auto-refresh."""

    def __init__(self, api, servers, insecure_tls=False):
        self.api = api
        self.servers = list(servers)
        self.insecure_tls = insecure_tls
        self.refreshed = False
        self.rr = 0  # round-robin cursor

    async def open(self, target_host, target_port):
        if not self.servers:
            raise UpstreamError("No upstream servers available")
        errors = []
        for _ in range(len(self.servers)):
            server = self.servers[self.rr % len(self.servers)]
            self.rr += 1
            try:
                return await connect_via_server(
                    server, target_host, target_port,
                    insecure_tls=self.insecure_tls,
                )
            except UpstreamError as e:
                errors.append(str(e))
                # 401/403 => credentials expired -> refresh list once
                if e.socks_code == SOCK_ERRORS["not_allowed"] and not self.refreshed:
                    self.refreshed = True
                    try:
                        region = self.servers[0].get("region")
                        self.servers = await self.api.refresh_servers_if_expired(region)
                    except Exception:
                        pass
        raise UpstreamError("; ".join(errors[-2:]))


class LocalProxyServer:
    def __init__(self, factory: TunnelFactory, stats: Stats):
        self.factory = factory
        self.stats = stats

    # ---------------------------------------------------------------- socks5
    async def handle_socks5(self, client_reader, client_writer):
        peer = client_writer.get_extra_info("peername")
        # --- greeting
        header = await client_reader.readexactly(2)
        ver, nmethods = header[0], header[1]
        if ver != 5:
            client_writer.close()
            return
        methods = await client_reader.readexactly(nmethods)
        client_writer.write(b"\x05\x00")  # no-auth-required
        await client_writer.drain()
        # --- request
        req = await client_reader.readexactly(4)
        ver, cmd, rsv, atyp = req
        if cmd != 0x01:  # only CONNECT
            client_writer.write(b"\x05\x07\x00\x01" + b"\x00" * 6)
            await client_writer.drain()
            client_writer.close()
            return
        if atyp == 0x01:      # IPv4
            raw = await client_reader.readexactly(4)
            host = ".".join(str(b) for b in raw)
        elif atyp == 0x03:    # domain
            ln = (await client_reader.readexactly(1))[0]
            host = (await client_reader.readexactly(ln)).decode("idna")
        elif atyp == 0x04:    # IPv6
            raw = await client_reader.readexactly(16)
            import ipaddress
            host = str(ipaddress.IPv6Address(raw))
        else:
            client_writer.write(b"\x05\x08\x00\x01" + b"\x00" * 6)
            await client_writer.drain()
            client_writer.close()
            return
        port_hi, port_lo = await client_reader.readexactly(2)
        port = (port_hi << 8) | port_lo

        try:
            upstream_reader, upstream_writer = await self.factory.open(host, port)
        except UpstreamError as e:
            self.stats.failed += 1
            code = e.socks_code if e.socks_code != 0 else SOCK_ERRORS["general"]
            try:
                client_writer.write(b"\x05" + bytes([code]) + b"\x00\x01" + b"\x00" * 6)
                await client_writer.drain()
                client_writer.close()
            except Exception:
                pass
            return
        except Exception:
            self.stats.failed += 1
            try:
                client_writer.write(b"\x05\x01\x00\x01" + b"\x00" * 6)
                await client_writer.drain()
                client_writer.close()
            except Exception:
                pass
            return

        # success reply (bnd.addr/port = 0)
        client_writer.write(b"\x05\x00\x00\x01" + b"\x00" * 6)
        await client_writer.drain()
        self.stats.connections += 1
        await _tunnel(client_reader, client_writer, upstream_reader,
                      upstream_writer, self.stats)

    # ------------------------------------------------------------------ http
    async def handle_http(self, client_reader, client_writer):
        try:
            head = await asyncio.wait_for(client_reader.readuntil(b"\r\n\r\n"), 30)
        except (asyncio.IncompleteReadError, asyncio.TimeoutError):
            client_writer.close()
            return
        try:
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
                port = int(p)
                try:
                    upstream_reader, upstream_writer = await self.factory.open(host, port)
                except UpstreamError:
                    self.stats.failed += 1
                    client_writer.write(
                        b"HTTP/1.1 502 Bad Gateway\r\n"
                        b"Proxy-Status: vpeen-upstream-failed\r\n\r\n"
                    )
                    await client_writer.drain()
                    client_writer.close()
                    return
                client_writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
                await client_writer.drain()
                self.stats.connections += 1
                await _tunnel(client_reader, client_writer, upstream_reader,
                              upstream_writer, self.stats)
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
            except UpstreamError:
                self.stats.failed += 1
                client_writer.write(
                    b"HTTP/1.1 502 Bad Gateway\r\n\r\n"
                )
                await client_writer.drain()
                client_writer.close()
                return

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
                body = await client_reader.readexactly(content_length)
                upstream_writer.write(body)
            await upstream_writer.drain()
            self.stats.connections += 1

            # stream response back until EOF (Connection: close)
            try:
                while True:
                    data = await upstream_reader.read(CHUNK)
                    if not data:
                        break
                    self.stats.bytes_down += len(data)
                    client_writer.write(data)
                    await client_writer.drain()
            except (ConnectionError, OSError):
                pass
            client_writer.close()
        except (ConnectionError, OSError):
            pass
        finally:
            try:
                client_writer.close()
            except Exception:
                pass
