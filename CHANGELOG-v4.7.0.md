# VPeeN v4.7.0 — "Nitro"

The third speed pass. v4.5.0 paid every handshake's TLS round-trips up
front ("Velocity"), v4.6.0 ranked servers by measured bandwidth and
filled the warm pool before your first click ("Turbo"). v4.7.0 goes
after what was left: the resolver, the tail, and the pool's honesty.

## Highlights

* **Hedged upstream connects** — when the chosen server's TLS handshake
  has not landed within 350 ms, a parallel backup connect to the
  next-best server starts; the first success wins. One stalled "best"
  server used to cost every connection of a burst its full 8 s connect
  budget; it now costs 0.35 s. The straggler is reaped in the background
  (never cancelled mid-handshake — that is a socket-leak path) and the
  losing server gets no breaker penalty. Hedges only fire when a
  handshake permit is free, so browsing bursts never double their load.
* **Proxy-hostname resolver cache (singleflight)** — every fresh
  upstream connect used to pay a blocking `getaddrinfo` (on Windows the
  resolver also tries LLMNR/mDNS first — routinely 50–200 ms). Resolved
  IPs are now cached for 60 s and shared by every connect, refill wave
  and latency probe; concurrent resolves of the same host share ONE
  task, so a 30-socket burst produces one lookup instead of 30. A dead
  resolver serves the stale cached answer instead of failing.
* **Full Happy-Eyeballs across DNS records** — a server given by
  hostname is expanded to its concrete A/AAAA records and ALL of them
  race staggered (before, only the first record was ever used). A
  blackholed first A record now costs 150 ms instead of the full
  timeout. TLS SNI and certificate validation still use the original
  hostname, so security is byte-for-byte unchanged.
* **Adaptive warm-pool TTL** — when the upstream reaps idle sockets
  faster than our 25 s TTL assumed, every warm hit became a wasted
  CONNECT. Each stale entry now shrinks the TTL (down to 8 s floor);
  it recovers slowly between probe cycles. The pool stops donating
  failed connects to aggressive servers.
* **Demand-adaptive pool size** — the refill target tracks the last
  10 s of connection demand (12 → up to 24). Browsing is bursty; the
  burst after a burst now starts mostly pre-paid.
* **Throughput ranking that ages** — a measured-KB/s number only ranks
  a server for 10 minutes, and the top candidates are re-measured every
  ~3 minutes, so a "best" server that collapses under load is demoted
  mid-session instead of clinging to a stale number.
* **Wider handshake window** — the parallel-handshake cap rose 12 → 16;
  with the resolver cache each handshake starts without the getaddrinfo
  hop, so the wider window is no longer a flood for the free proxy.
* **DNS relay caches 60 s** (was 30) — aligned with the SOCKS-UDP relay;
  a page rarely re-resolves within a minute and every miss was a full
  tunnel round-trip.
* **UI telemetry** — the status line now shows "N rescued" (hedged
  connects that saved a stalled handshake) and the 30 s log line
  reports hedges and the adaptive TTL next to hits/stale.

## Why it feels faster

| Scenario | v4.6.0 | v4.7.0 |
|---|---|---|
| First connect of a burst (fresh path) | getaddrinfo + TCP + TLS + CONNECT | (cached resolve) + TCP + TLS + CONNECT |
| Best server stalls mid-session | every connection waits 8 s | 0.35 s hedge, then the backup carries it |
| Server with a blackholed first DNS record | up to 8 s | 150 ms |
| Server reaps idle conns at 10 s | warm hits become failed CONNECTs | TTL shrinks to fit, hits stay real |
| Second burst within 10 s | pool back to floor 12 | pool sized to the burst (≤ 24) |
| "Best" server collapses under load | ranked by a 10-min-old number | re-measured every ~3 min |

## Compatibility

No settings, commands or file formats changed. The SOCKS5/HTTP surface
is untouched; the only observable differences are speed, the extra
status fields, and one log line.

**Full test pass:** 42/42 speed tests, 11/11 integration, 59/59 fixes,
23/23 web UI.
