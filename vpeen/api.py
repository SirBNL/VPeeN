"""
VeePN free-extension API client (reverse-engineered from the official
"Free VPN for Chrome - VPN Proxy VeePN" extension, v5.0.2).

Protocol summary
----------------
* API domains are rotated. The extension ships with two defaults and
  fetches reserve domain lists from two public JSON buckets:
      https://s3-oregon-1.s3-us-west-2.amazonaws.com/api.json
      https://proigor.com/payload.json
* POST /v3/launch/            {udid, appVersion, platform, platformVersion,
                               timeZone, deviceName}   -> {"access": token}
* GET  /v3/location/extension/                        -> {locations: [...], ...}
* GET  /v3/location/optimal/                          -> {id, region, ...}
* POST /v3/server/list/       {protocol:"https", region, type:0}
                              -> [{username, password, port, addresses, ...}]

The servers are HTTPS (CONNECT) proxies with basic auth.  The API rejects
any other protocol value ("Protocol is invalid." for socks5).
"""
import asyncio
import base64
import json
import random
import ssl
import urllib.request
import urllib.error
import uuid

from .utils import State, err, info, local_timezone_name, now_ms, ok, warn

APP_VERSION = "5.0.2"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)
DEVICE_NAME = "Chrome 130"

DEFAULT_FREE_DOMAINS = [
    "https://antpeak.com",
    "https://hibchr.com",
    "https://hisball.com",
    "https://bitphox.com",
    "https://freloop.com",
    "https://tronlit.com",
    "https://tronyza.com",
]

RESERVE_BUCKET_URLS = [
    "https://s3-oregon-1.s3-us-west-2.amazonaws.com/api.json",
    "https://proigor.com/payload.json",
]

SERVERS_CACHE_TTL_MS = 12 * 3600 * 1000     # 12 h
TOKEN_SOFT_TTL_MS = 7 * 24 * 3600 * 1000    # refresh token after 7 days

# Per-attempt domain-rotation backoff.  v1.2.4: the old schedule
# ([2,5,12,25,45] x 7 attempts) could stall a single API call for almost
# three minutes before surfacing an error - the single biggest "it just
# hangs" complaint.  The new budget surfaces a real failure in <= ~25 s
# while still rotating across every domain.
BACKOFF_SECONDS = [1, 2, 4, 8, 10]          # worst case ~25s per request

IP_ECHO_MIRRORS = [
    "https://api.ipify.org?format=json",
    "https://ifconfig.me/ip",
    "https://api.seeip.org/jsonip",
    "https://ipinfo.io/json",
]


def _http_request(url, method="GET", headers=None, body=None, timeout=20):
    """Blocking HTTP request returning (status, parsed_json_or_text)."""
    req_headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
    }
    if headers:
        req_headers.update(headers)
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            status = resp.status
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        status = e.code
    except Exception as e:
        raise OSError(f"{type(e).__name__}: {e}") from e
    try:
        parsed = json.loads(raw)
    except Exception:
        parsed = raw
    return status, parsed


async def _http_request_async(url, method="GET", headers=None, body=None, timeout=20):
    return await asyncio.to_thread(
        _http_request, url, method, headers, body, timeout
    )


class VeePNApi:
    """Client for the VeePN extension backend (free / anonymous mode)."""

    def __init__(self, state: State, insecure_tls: bool = False, log=True):
        self.state = state
        self.insecure_tls = insecure_tls
        self.log = log
        self._domains = None

    # ------------------------------------------------------------------ utils
    def _say(self, msg):
        if self.log:
            info(msg)

    @property
    def udid(self) -> str:
        """Stable per-installation device id (like the extension's UUID)."""
        udid = self.state.get("udid")
        if not udid:
            udid = str(uuid.uuid4())
            self.state.set("udid", udid)
        return udid

    def _ssl_context(self):
        ctx = ssl.create_default_context()
        if self.insecure_tls:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        return ctx

    async def _request(self, path, method="GET", body=None, auth=True,
                       attempts=None):
        """Request with domain rotation + exponential backoff on 429/5xx.

        v1.2.4: a cached token that the backend has since revoked used to
        401 forever (the soft TTL is 7 days and nothing invalidated it),
        killing server_list until the user deleted the state file by hand.
        Now a 401/403 on an authed request invalidates the cached token,
        fetches a fresh one and retries the request once."""
        attempts = attempts if attempts is not None else len(BACKOFF_SECONDS) + 1
        domains = await self.get_domains()
        last_error = "unknown"
        token_refreshed = False
        for attempt in range(attempts):
            headers = {}
            if auth:
                token = await self.ensure_token(force=token_refreshed)
                headers["Authorization"] = f"Bearer {token}"
            domain = domains[attempt % len(domains)]
            url = domain.rstrip("/") + path
            try:
                status, payload = await _http_request_async(
                    url, method=method, body=body, headers=headers
                )
            except OSError as e:
                last_error = f"{domain} unreachable ({e})"
                self._say(f"  {last_error}, trying next domain...")
                await asyncio.sleep(min(BACKOFF_SECONDS[attempt % len(BACKOFF_SECONDS)], 3))
                continue
            if 200 <= status < 300:
                return payload
            if auth and status in (401, 403) and not token_refreshed:
                # cached credential rejected -> force one re-launch and retry
                token_refreshed = True
                last_error = f"HTTP {status} from {domain} (token rejected)"
                self._say("  Access token rejected - requesting a fresh one...")
                continue
            if status == 429 or 500 <= status:
                wait = BACKOFF_SECONDS[min(attempt, len(BACKOFF_SECONDS) - 1)]
                last_error = f"HTTP {status} from {domain}"
                self._say(f"  {last_error} - backoff {wait}s, rotating domain...")
                await asyncio.sleep(wait)
                continue
            # 4xx (other than 429): surface the error to the caller
            raise ApiError(status, payload)
        raise ApiError(0, {"message": f"All API attempts failed ({last_error})"})

    # ------------------------------------------------------ domain discovery
    async def get_domains(self):
        """Default domains + reserve domains fetched from public buckets."""
        if self._domains:
            return self._domains
        domains = list(DEFAULT_FREE_DOMAINS)
        cached = self.state.get("reserve_domains")
        now = now_ms()
        if cached and now - cached.get("fetched_at", 0) < 24 * 3600 * 1000:
            extra = cached.get("free", [])
        else:
            extra = await self._fetch_reserve_domains()
            self.state.set("reserve_domains", {"free": extra, "fetched_at": now})
        for d in extra:
            if d not in domains:
                domains.append(d)
        self._domains = domains
        return domains

    async def _fetch_reserve_domains(self):
        found = []
        for bucket in RESERVE_BUCKET_URLS:
            try:
                status, payload = await _http_request_async(bucket, timeout=15)
                if status == 200 and isinstance(payload, dict):
                    d = payload.get("domains") or {}
                    for url in (d.get("free") or []):
                        if isinstance(url, str) and url.startswith("http"):
                            found.append(url)
                    # v1.2.4: only ever accept plain strings here.  The old
                    # code appended whatever `payload["free"]` was - a list
                    # or dict would later crash get_domains() consumers
                    # (domain.rstrip on a non-str).
                    extra = payload.get("free")
                    if isinstance(extra, str) and extra.startswith("http"):
                        found.append(extra)
                    elif isinstance(extra, list):
                        found.extend(u for u in extra
                                     if isinstance(u, str) and u.startswith("http"))
            except Exception:
                continue
        return found

    # ------------------------------------------------------------------ token
    async def ensure_token(self, force: bool = False) -> str:
        tok = self.state.get("token") or {}
        now = now_ms()
        if (not force) and tok.get("access") and \
                now - tok.get("fetched_at", 0) < TOKEN_SOFT_TTL_MS:
            return tok["access"]
        if self.log:
            self._say("Requesting anonymous access token (POST /v3/launch/) ...")
        body = {
            "udid": self.udid,
            "appVersion": APP_VERSION,
            "platform": "chrome",
            "platformVersion": USER_AGENT,
            "timeZone": local_timezone_name(),
            "deviceName": DEVICE_NAME,
        }
        # launch is not authenticated; do it manually to avoid recursion
        domains = await self.get_domains()
        last = None
        for attempt in range(len(BACKOFF_SECONDS) + 2):
            domain = domains[attempt % len(domains)]
            url = domain.rstrip("/") + "/v3/launch/"
            try:
                status, payload = await _http_request_async(
                    url, method="POST", body=body
                )
            except OSError as e:
                last = f"{domain}: {e}"
                await asyncio.sleep(min(BACKOFF_SECONDS[attempt % len(BACKOFF_SECONDS)], 5))
                continue
            if status == 200 and isinstance(payload, dict):
                access = payload.get("access")
                if access:
                    self.state.set("token", {"access": access, "fetched_at": now})
                    if self.log:
                        ok(f"Access token acquired via {domain}")
                    return access
                last = f"{domain}: bad launch payload"
            elif status == 429 or 500 <= status:
                wait = BACKOFF_SECONDS[min(attempt, len(BACKOFF_SECONDS) - 1)]
                last = f"{domain}: HTTP {status}"
                self._say(f"  {last} - backoff {wait}s ...")
                await asyncio.sleep(wait)
            else:
                raise ApiError(status, payload)
        raise ApiError(0, {"message": f"Could not obtain token ({last})"})

    # -------------------------------------------------------------- locations
    async def locations(self):
        """Return (all_locations, free_locations)."""
        payload = await self._request("/v3/location/extension/")
        locations = payload.get("locations", []) if isinstance(payload, dict) else []
        free = [l for l in locations if l.get("proxyType") == 0]
        return locations, free

    async def optimal_location(self):
        return await self._request("/v3/location/optimal/")

    # ------------------------------------------------------------- server list
    async def server_list(self, region: str, force_refresh=False):
        """List of upstream HTTPS-proxy servers (with credentials) for region."""
        now = now_ms()
        cache = (self.state.get("servers_cache") or {}).get(region)
        if cache and not force_refresh and now - cache.get("fetched_at", 0) < SERVERS_CACHE_TTL_MS:
            return cache["servers"]
        body = {"protocol": "https", "region": region, "type": 0}
        payload = await self._request("/v3/server/list/", method="POST", body=body)
        if not isinstance(payload, list) or not payload:
            raise ApiError(0, {"message": f"No servers returned for region '{region}'"})
        servers = [s for s in payload if s.get("addresses") and s.get("port")]
        # v1.2.4: a response of only malformed entries used to cache an
        # EMPTY list for 12h, so the factory had zero servers and every
        # connection failed with "no-server" until the cache expired.
        if not servers:
            raise ApiError(0, {"message":
                f"Region '{region}' returned {len(payload)} unusable "
                f"server entries"})
        prev = self.state.get("servers_cache") or {}
        prev[region] = {"servers": servers, "fetched_at": now}
        self.state.set("servers_cache", prev)
        return servers

    async def refresh_servers_if_expired(self, region: str):
        """Force re-fetch of the region's server list (credentials expired?)."""
        self._say(f"Refreshing server list for '{region}' ...")
        return await self.server_list(region, force_refresh=True)

    # -------------------------------------------------------------- ip checks
    async def check_ip_direct(self):
        """Public IP via the first reachable echo mirror.

        v1.2.4: only api.ipify.org was queried; when that host is slow or
        blocked (common on some national networks) the GUI/CLI reported
        'Could not determine your IP'.  Several mirrors are now tried in
        turn with a short timeout each."""
        for mirror in IP_ECHO_MIRRORS:
            try:
                status, payload = await _http_request_async(mirror, timeout=6)
                if status != 200:
                    continue
                if isinstance(payload, dict):
                    ip = payload.get("ip")
                    if ip:
                        return str(ip).strip()
                elif isinstance(payload, str) and payload.strip():
                    import re
                    m = re.search(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b",
                                  payload.strip())
                    if m:
                        return m.group(1)
            except Exception:
                continue
        return None


class ApiError(Exception):
    def __init__(self, status, payload):
        self.status = status
        self.payload = payload
        if isinstance(payload, dict):
            msg = payload.get("message") or json.dumps(payload, ensure_ascii=False)[:200]
        elif isinstance(payload, list) and payload and isinstance(payload[0], dict):
            msg = payload[0].get("message") or str(payload)[:200]
        else:
            msg = str(payload)[:200]
        super().__init__(f"HTTP {status}: {msg}")
