# VPeeN v4.3.1 — Full audit & bugfix pass

A complete code review of the whole tree (≈6,500 LOC) found **13 real
defects**, including one that made Tunnel (VPN) mode crash on **every**
bring-up. All of them are fixed and covered by two test suites
(`scripts/test_vpeen_fixes.py`, 59 checks; `scripts/test_vpeen_integration.py`,
11 live checks against a fake TLS upstream).

## Critical

1. **Tunnel mode was dead on arrival — `tunnel_platforms.py`**
   `save_session()` and `_linux_dns_swap()` called `ensure_private_dir()`
   but the module **never imported it**. Every `bring_up()` on every OS
   raised `NameError` *after* the routes had been applied, the crash-
   recovery journal was never written, and the GUI could only ever
   "fall back to proxy mode" with a cryptic error. (Found by pyflakes:
   `undefined name 'ensure_private_dir'`.)
   *Fix:* `from .utils import ensure_private_dir`.

2. **DNS-over-TCP responses were corrupted — `dnsrelay.py`**
   `resolve()` truncated any answer larger than ~1200 bytes to a
   512-byte TC-flagged stub **for TCP clients too** (the comment even
   said "TCP clients must get it whole"). Large answers (DNSSEC, big
   record sets) silently broke. Cache hits were truncated inconsistently.
   *Fix:* `resolve(query, udp=...)` — truncation only on the datagram
   path, full answers on the TCP path, cache hits handled consistently.

3. **Half-close killed live reply streams — `localproxy.py`**
   asyncio SSL transports cannot half-close (`write_eof()` raises
   `NotImplementedError`). `_tunnel()` treated that as a hard abort and
   **cancelled the still-draining opposite direction** — any client that
   called `shutdown(SHUT_WR)` lost whatever the server was still sending.
   The "full TCP half-close semantics" advertised since v4.1 never
   actually worked through the TLS upstream.
   *Fix:* half-close only when `can_write_eof()`; otherwise the
   direction stays open until the tunnel ends naturally.

## High

4. **`--insecure` never reached the API layer — `api.py`**
   `VeePNApi._ssl_context()` existed and was referenced by docstrings,
   but **no call site used it**; `urllib.request.urlopen` always verified.
   The flag only affected upstream CONNECT tunnels, silently doing
   nothing for token/server-list/IP requests.
   *Fix:* `insecure` parameter threaded through `_http_request` /
   `_http_request_async` and every call site.

5. **47 lines defined twice — `api.py`**
   The whole v4.3.0 domain-rotation block (comment, `MAX_DOMAIN_ATTEMPTS`,
   `MAX_RESERVE_DOMAINS`, `REQUEST_BUDGET_SECONDS`, `API_TIMEOUT`,
   `_API_DOMAIN_RE`, `valid_api_domain`) was **pasted twice** (a bad
   merge). *Fix:* deduplicated.

6. **Elevated worker truncated the GUI crash log — `main.py`**
   The worker/cleanup helper opened the GUI's `last-run.log` with mode
   `"w"`, wiping it every time tunnel mode started, then both processes
   interleaved writes at clashing offsets.
   *Fix:* worker writes `last-run-worker.log`.

7. **Stale hard-coded macOS bundle version — `vpeen.spec`**
   `CFBundleShortVersionString`/`CFBundleVersion` were pinned to `"4.2.2"`
   while the app shipped 4.3.0. *Fix:* the spec now parses
   `vpeen/__init__.py` (same as CI).

## Medium

8. **Cross-thread state lost updates — `utils.py`**
   The GUI core thread and quick-task threads each own a `State` instance
   over the same file; a whole-dump save from a stale instance wiped keys
   written by the other (token/udid/reserve-domains).
   *Fix:* merge-on-save (adopt on-disk keys this instance lacks).

9. **Pre-existing loose `~/.vpeen` stayed loose — `utils.py`**
   `ensure_private_dir()` only chmod-ed freshly created directories, so
   upgrades from v4.2.x kept a 0775 dir holding the token file forever.
   *Fix:* tighten whenever group/other bits are present (POSIX).

10. **IPv6 exit/direct IPs never detected — `api.py`, `upstream.py`**
    The IP-extraction fallback only matched dotted quads; v6-only
    networks reported "could not determine your IP" forever.
    *Fix:* exact-token `ipaddress` parse + an IPv6 token regex
    (validated, so time-like/MAC-like tokens can't false-positive).

11. **Control-protocol robustness — `tunnel.py`**
    `json.loads(first)` raised uncaught `ValueError`/`AttributeError`
    inside the connection callback for garbage first lines;
    `argv.index(flag) + 1` raised `IndexError` for dangling flags.
    *Fix:* guarded parse + dict check; `_arg_value()` helper.

12. **macOS: disabled network services — `systemproxy.py`**
    `-listallnetworkservices` lines with a leading `*` (disabled) were
    passed to networksetup verbatim, which rejects starred names, so a
    machine with a disabled adapter first in the list got no proxy at
    all. Service cap raised 3→8 (VPN/VM adapters).
    *Fix:* filter starred services; also in tunnel_platforms (5→8).

13. **GUI/CLI polish — `gui.py`, `cli.py`, `api.py`**
    • SOCKS and HTTP ports could be configured equal → the second
      listener failed deep inside the core at connect time; now rejected
      at save **and** connect time with a readable message.
    • `pending_tunnel` was never consumed → any later `phase: connected`
      re-issued a second tunnel connect (double worker spawn).
    • `python -m vpeen.cli --state x` (flags, no subcommand) exited
      silently with code 0; now prints help, returns 2.
    • Dead imports removed (`base64`, `random`, `err`, `warn`,
      `__version__`, `PHASE_CONNECTING`), dead local `free_n` removed,
      `select() if cfg else None` expression-statement cleaned up.

## Housekeeping

* Version bumped to **4.3.1** (`vpeen/__init__.py`).
* `pyflakes` is now clean across the whole tree (was 10 findings,
  including the critical undefined name).

## Verification

```
scripts/test_vpeen_fixes.py         59/59 PASS
scripts/test_vpeen_integration.py   11/11 PASS   (fake TLS upstream)
pyflakes vpeen/*.py main.py         clean
py_compile all files                OK
```

The integration suite exercises the real SOCKS5/HTTP listeners against a
self-signed TLS upstream: greeting/method negotiation, CONNECT relay in
both directions, IPv6 targets, client half-close survival, absolute-form
POST re-encoding (exactly one `Content-Length`), and the UDP-ASSOCIATE
reply shape.
