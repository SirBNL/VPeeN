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
import socket
import ssl

CHUNK = 65536

from .api import USER_AGENT

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


async def connect_via_server(server: dict, target_host: str, target_port: int,
                             insecure_tls: bool = False,
                             connect_timeout: float = 10.0):
    """
    Open a TLS connection to the VeePN proxy server, issue CONNECT and return
    (reader, writer) of the tunnel once the proxy answers 200.

    `server` is one entry from the /v3/server/list/ response:
        {addresses: [..], port: int, username: str, password: str, ...}
    """
    addresses = server.get("addresses") or []
    port = int(server.get("port") or 0)
    username = server.get("username") or ""
    password = server.get("password") or ""
    if not addresses or not port:
        raise UpstreamError("Malformed server entry")

    last_exc = None
    for host in addresses:
        writer = None
        try:
            ssl_ctx = ssl.create_default_context()
            if insecure_tls:
                ssl_ctx.check_hostname = False
                ssl_ctx.verify_mode = ssl.CERT_NONE
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port, ssl=ssl_ctx,
                                        server_hostname=host),
                timeout=connect_timeout,
            )
            auth = base64.b64encode(f"{username}:{password}".encode()).decode()
            req = (
                f"CONNECT {target_host}:{target_port} HTTP/1.1\r\n"
                f"Host: {target_host}:{target_port}\r\n"
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
                enable_keepalive(writer)
                return reader, writer
            try:
                writer.close()
            except Exception:
                pass
            code, status = _classify_connect_failure(status_line)
            raise UpstreamError(
                f"Upstream CONNECT failed: {status_line.strip()}",
                code, status,
            )
        except UpstreamError:
            raise
        except asyncio.CancelledError:
            raise
        except Exception as e:  # TLS / DNS / timeout -> try next address
            last_exc = e
            if writer is not None:
                try:
                    writer.close()
                except Exception:
                    pass
            continue
    # str(TimeoutError()) is empty on Python 3.11+, so tag it explicitly
    kind = ("handshake timeout" if isinstance(last_exc, asyncio.TimeoutError)
            else last_exc)
    raise UpstreamError(
        f"Cannot reach upstream proxy {addresses}:{port}: {kind}",
        SOCK_ERRORS["host_unreachable"],
    )


async def check_exit_ip(server: dict, insecure_tls: bool = False) -> str:
    """
    One-shot test: fetch api.ipify.org through the upstream proxy.

    Inside a CONNECT tunnel the CLIENT must do TLS with the target itself
    (the proxy only relays bytes), so after CONNECT we upgrade to TLS like a
    browser would (StreamWriter.start_tls, Python 3.11+).  On older Pythons
    we fall back to the plain-HTTP (port 80) variant of the IP service.
    """
    use_inner_tls = hasattr(asyncio.StreamWriter, "start_tls")
    tls_ctx = ssl.create_default_context()
    if insecure_tls:
        tls_ctx.check_hostname = False
        tls_ctx.verify_mode = ssl.CERT_NONE
    port = 443 if use_inner_tls else 80
    reader, writer = await connect_via_server(
        server, "api.ipify.org", port, insecure_tls=insecure_tls
    )
    try:
        if use_inner_tls:
            # StreamWriter.start_tls upgrades in place and returns None.
            await writer.start_tls(tls_ctx, server_hostname="api.ipify.org")
        req = (
            "GET /?format=json HTTP/1.1\r\n"
            "Host: api.ipify.org\r\n"
            "User-Agent: " + USER_AGENT + "\r\n"
            "Accept: application/json\r\n"
            "Connection: close\r\n\r\n"
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
        import json
        try:
            return json.loads(body.strip()).get("ip")
        except Exception:
            import re
            m = re.search(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b", body)
            return m.group(1) if m else None
    finally:
        try:
            writer.close()
        except Exception:
            pass
