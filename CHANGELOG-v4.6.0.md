# VPeeN 4.6.0 — “Turbo”

The third speed-focused release. v4.5.0 “Velocity” removed the handshake
tax *in theory*; v4.6.0 makes it actually land on your page loads — and
adds the one thing no client-side proxy could honestly claim before: a
**measured, server-side quality signal** for picking the exit server.

Measured on the real free network (region `nl`, 6 sequential connections
— the shape of one browser page load):

| Metric | v4.5-like cold path | v4.6.0 Turbo | Gain |
|---|---|---|---|
| Connection setup (avg) | 1309 ms | 386 ms | **3.4×** |
| Whole 6-connection burst | 36.8 s | 13.0 s | **2.8×** |
| Effective throughput | 42 KiB/s | 119 KiB/s | **2.8×** |

Reproduce: `python3 scripts/test_real_speed.py nl`

---

## 1. Pool-aware scheduling (the 1/N bug)

v4.5.0 kept pre-warmed TLS connections to the best server — but `open()`
only tried the pool when the **round-robin cursor happened to pick that
one server**. With N upstream servers, at most every Nth connection could
ever hit the pool; the rest paid the full TCP + TLS + CONNECT handshake
while perfectly good warm connections sat idle. With the typical 3
servers per region, **two thirds of every browsing burst was needlessly
cold**.

`open()` now takes the newest live warm connection across *all* servers
(`_pool_take_any`), and `_pick_server` prefers servers that still have
warm connections. The pool is no longer luck — it is the default path.

## 2. The pool grew up: 12 slots, filled in parallel waves

* `POOL_SIZE` 4 → **12** — a single page load opens 6–30 sockets; a pool
  of 4 covered a fraction of the burst.
* Refill is now **parallel waves of 4** (was: one connection per loop
  iteration — a pool of 12 took 12 × handshake-time to top up).
* Refill stands down (`Semaphore.locked()` courtesy check) while
  client-facing connects hold the handshake budget, so the background
  warmer never makes a browsing burst wait.
* A server whose refills fail 3× in a row goes on the regular breaker
  cooldown — a dead “best” server stops being warmed.
* **Pre-warm starts with the connect**, not with the first client
  connection: v4.5.0 only scheduled the refiller after the first
  successful `open()`, so the *first* page load (the moment users judge
  “is it fast?”) always ran fully cold. `_probe_servers` now schedules
  the refill right after the latency ranking.

## 3. Throughput ranking — the server-side signal (issue #1 request)

RTT says nothing about server load: two servers with identical ping can
differ 10× in delivered bandwidth. At connect time VPeeN now downloads
128 KiB through each of the top-3 candidates from Cloudflare’s public
speed endpoint (`speed.cloudflare.com`, plain HTTP — deliberately, so
the measurement is the *proxy path*, not another certificate
handshake), ranks servers by measured KiB/s, and warms the pool against
the winner:

* `upstream.probe_throughput()` — bounded (top-3, ≤4 s each, concurrent,
  under the shared handshake semaphore), 32 KiB floor so a redirect
  page can never masquerade as a measurement, silent no-op on failure.
* `_server_order()` — the single ranking used by picking, warming and
  rotation: **measured throughput first, then latency, then round-robin
  fairness** (which it degrades to when nothing is measured — identical
  to v4.5.0 on unprobed lists).

## 4. Honest rankings mid-session

* Latency re-probe every 60 s updates the ranking *metadata only* —
  `self.servers` is never mutated, so the index-keyed breaker maps stay
  valid. A server that congests mid-session loses the warm pool to a
  better one without dropping anyone’s connection.
* **Fixed a v4.5.0 latent bug:** after the startup reorder the latency
  map kept the OLD indices, so every later latency lookup answered for
  the wrong server. The map is now remapped to the new order.

## 5. Everything else that moved

* `MAX_PARALLEL_UPSTREAM` 8 → **12** — 4 serialized waves of handshakes
  for a 30-socket burst was a visible chunk of “site loading feels
  slow”; 12 stays under the free network’s 429 reflex (bounded waves,
  staggered 60 ms, semaphore-fair).
* **macOS socket buffers** (512 KiB SO_RCVBUF/SO_SNDBUF) — macOS
  `maxsockbuf` is notoriously small for long-fat pipes; it now gets the
  same explicit tuning Windows already had (Linux keeps autotuning).
* Happy-Eyeballs stagger 250 ms → **150 ms** — a blackholed address
  costs even less before the race is decided.
* DNS cache TTL 30 s → **60 s** (every miss was a full tunnel
  round-trip).
* **Warm-pool telemetry**: the stats line shows `N warm`, and a
  `warm pool: X live · Y hits · Z stale` log line lands every 30 s.
* `TunnelFactory.stop()` now also swallows `CancelledError` from
  background tasks cancelled before their coroutine ever started (a
  BaseException that `except Exception` silently missed — it could
  crash the disconnect path in a narrow race).

## Issues closed

* **#1 — “Big issue in Mac Tahoe 26”**: the app was unsigned, so
  Gatekeeper shows “Apple could not verify …” with no Open-Anyway
  button on macOS Sequoia/Tahoe 26. v4.6.0 release builds are
  **ad-hoc code-signed** (`codesign --force --deep --sign -`) which
  removes the “damaged copy” failure mode, and the README now documents
  the two-click **System Settings → Privacy & Security → Open Anyway**
  path plus the one-line `xattr -cr` fallback. (Full notarization needs
  a $99/yr Apple Developer ID — not possible for this project today.)
* **#2 — “Please make a new open Chrome Extension”**: meet
  **VPeeN Bridge** (`extension/`, also attached to every release as
  `VPeeN-Chrome-Extension.zip`). A 2-permission MV3 extension
  (`proxy`, `storage`) with an on/off switch that routes all of Chrome
  through the local VPeeN SOCKS5 tunnel with remote DNS and the same
  localhost/LAN bypass list as the desktop app.

## Verification

* `scripts/test_speed_pass.py` — **25/25 PASS** (was 20; +5 new checks:
  pool-hit-regardless-of-RR-cursor, parallel refill timing, throughput
  ranking, latency-map remap, pre-warm without client connections).
* `scripts/test_vpeen_fixes.py` — **59/59 PASS**.
* `scripts/test_vpeen_integration.py` — **11/11 PASS**.
* `scripts/test_webui_demo.py` — **23/23 PASS**.
* `scripts/test_real_speed.py` — real-network A/B: cold vs Turbo
  (numbers in the table above).
