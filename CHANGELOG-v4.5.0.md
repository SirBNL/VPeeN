# VPeeN v4.5.0 — “Velocity” speed & quality pass

One goal: make every proxied connection faster and make “Connect” feel
instant. Every change below is client-side — the server network is what
it is, but the round-trips we spend on it are now a third of what they
were, and the first bytes of a page load arrive far sooner.

## The big one: a pre-warmed TLS connection pool

Until now **every single proxied connection** paid the full setup toll:
TCP handshake + complete TLS handshake + CONNECT round-trip — roughly
**3 round-trips before the first byte of real data** (≈450 ms on a
150 ms path, per connection, and browsers open 20–40 of them at once).

`TunnelFactory` now keeps up to 4 **TLS-established (pre-CONNECT)
connections warm in the background** toward the current best server:

* a client CONNECT on a warm connection costs **one round-trip**
  (~3× lower per-connection latency — measured 2.3× even on loopback)
* the refiller runs through the same handshake semaphore as normal
  connects, so it never starves real traffic
* a warm connection the server silently reaped while idle is detected
  instantly and transparently retried fresh (counted as `stale`, never
  surfaced to the client)
* pool is dropped on credential rotation / server failure and rebuilt
  toward the next healthy target

This required splitting `connect_via_server()` into
`tunnel_connect` + `send_connect` (`vpeen/upstream.py`) — the composite
keeps the old signature and error taxonomy, so all existing callers and
tests behave identically.

## Happy-Eyeballs address racing (RFC 8305 style)

A server entry can list several addresses. The old code tried them
**sequentially**, so one blackholed address cost the full connect
timeout (8 s) before the next was even attempted — and some regions list
their least-reliable address first.

Addresses are now raced with a 250 ms stagger: the first one starts
immediately, each extra one 250 ms later, first established TLS wins,
losers are cancelled. Worst case per blackholed address: **250 ms
instead of 8 s**.

## Latency-ranked servers (sticky fastest-first)

Server selection inside a region used to be pure round-robin — a
300 ms server was as likely to be picked as a 30 ms one. At startup the
factory now probes every server concurrently (plain-TCP RTT, 1.5 s
budget) and reorders the list **fastest-first**; round-robin then
spends its life on the quickest healthy server. Warm-pool connections
target the same winner.

The GUI’s per-region ping was upgraded the same way: it now probes up
to 5 servers of each region concurrently and reports the **best** one,
so a region is no longer misjudged because its first entry is slow or
blackholed.

## Connect feels instant: async exit-IP verification

“Connect” used to sit on the exit-IP probe (up to 15 s × 2 servers)
**after the local proxies were already live and usable**. The connected
phase is now emitted the moment the listeners are up; the exit IP is
verified in the background and arrives as its own event a moment later.

## Everything else in the pass

* **Cached SSL contexts** — a fresh `ssl.create_default_context()` was
  built per connection, reloading the entire system CA bundle each time
  (tens of ms on Windows). One context per verify mode now.
* **256 KiB relay chunks + reader limits** (was 64 KiB) on both the
  upstream and the local listeners — fewer event-loop iterations and
  larger socket writes lift throughput on high-bandwidth×high-latency
  paths; matching `limit=` raised on every `open_connection`/
  `start_server`.
* **DNS cache for the SOCKS5 UDP-DNS path** — repeat lookups are
  answered locally (question-section key, ID-rewritten replies) instead
  of opening a fresh TLS tunnel per query. Same design as the tunnel
  DNS relay, now applied to SOCKS mode.
* **TCP tuning on every upstream socket** — explicit `TCP_NODELAY`
  (contractual now, not incidental) and 512 KiB buffers on Windows
  (Linux autotunes better untouched).
* **CLI parity** — the CLI factory gets the same latency probe, warm
  pool and reader limits.

## Housekeeping

* `scripts/test_speed_pass.py` — **17 new checks**: context cache,
  blackhole racing (with a real stalling first address), warm-pool
  refill/hit/stale-fallback, latency reordering, DNS-cache hit with ID
  rewrite, chunk constants.
* `scripts/test_webui_demo.py` — the 23-check live demo-server suite
  referenced by the v4.4.0 notes is actually in the tree now (static
  assets, state snapshot, settings validation, connect/switch/
  disconnect timelines, long-poll).

## Verification

```
scripts/test_speed_pass.py         17/17 PASS   (fake TLS upstream)
scripts/test_webui_demo.py         23/23 PASS   (live demo-mode server)
scripts/test_vpeen_integration.py  11/11 PASS   (fake TLS upstream)
scripts/test_vpeen_fixes.py        59/59 PASS
bench (loopback): cold connect 3.4 ms -> warm-pool 1.5 ms (2.3×)
pyflakes vpeen/*.py main.py        clean
node --check vpeen/web/app.js      clean
```
