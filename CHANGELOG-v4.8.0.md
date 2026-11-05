# VPeeN v4.8.0 — "Sentinel"

Two things this release is about: tunnel mode actually working every
time, and never shipping a broken build again.

v4.7.0 "Nitro" was fast — but on a fresh connect its tunnel mode could
come up "connected" with a dead network, and a crash could leave the
default route hijacked after exit. Every one of those paths is now
root-caused, fixed, and pinned by a dedicated verification suite
(`scripts/test_tunnel_fixes.py` + `scripts/test_selfupdate.py`, both of
which now run in CI **before** anything is packaged).

## Tunnel mode — the real bugs (all fixed)

* **The empty anti-loop IP set (the "connected but dead" blackhole).**
  On every *first* connect, the GUI raised the TUN layer from
  `self.listeners` — but the `listeners` event used to arrive *after*
  `phase=connected`, so the worker was handed an **empty**
  `upstream_ips` list. It installed zero anti-loop host routes, hijacked
  the default route anyway, and every dial to the upstream looped back
  into the tunnel. Fixed at three levels: the `listeners` event now
  precedes `phase=connected` (and the payload rides on the phase event
  itself); the bridge reads IPs from the event, not from stale state;
  and the worker **refuses to hijack at all** when the IP set that
  should carry host routes is empty (strict mode — see next).
* **Hostnames are not routes.** `upstream_ips` used to be the raw
  server address list — which contains *hostnames*. `ip route` rejects
  them outright (and `route.exe` resolved them inconsistently, after
  the resolver had already been hijacked). Every hostname is now
  resolved to concrete IPs **before** any route is touched, host routes
  are built only from valid IPv4 literals, and hostname/v6 entries are
  filtered out of every later route refresh.
* **Round-robin DNS re-poisoning after the hijack.** Once the default
  route points into the tunnel, re-resolving a proxy hostname goes
  through the tunnel's own relay and can return a *different* A record
  with no host route — instant blackhole mid-session. All proxy
  hostnames are now **pinned**: resolved once pre-hijack, answered from
  the pin map by both the process resolver (`upstream.resolve_host`)
  and the DNS relay (synthesized NOERROR replies for A, empty
  NOERROR for AAAA), and replaced wholesale on credential rotation.
* **`--interface` on tun2socks: removed.** Binding tun2socks' outbound
  socket to the physical interface while its destination is loopback
  was never guaranteed to work on every stack. Loopback beats the /1
  hijack routes by itself.
* **The crash-recovery sweep that never ran.** `tunnel_sweep` was a
  setting, `cleanup_orphans()` existed — and nothing ever called it.
  A crash while the TUN layer was up left the def1 hijack routes
  behind: your network stayed bricked until a manual `ip route del`
  or a reboot. The sweep now runs on every boot, silently when
  there is nothing to clean, and only prompts for elevation when a
  crashed session is actually found.
* **Journal checkpoints after every mutation.** The session journal
  (what teardown and the sweep replay backwards) used to be saved once
  at the *end* of bring-up. A crash mid-way left a journal that did
  not match the live routes. It is now checkpointed after every route
  mutation, so recovery always replays exactly what is on the system.
* **DNS order-of-operations.** The system DNS used to be swapped to the
  tunnel resolver *inside* bring-up — before the relay had even tried
  to bind. Port 53 taken → machine with dead DNS for the whole session.
  The relay now binds first; the swap happens only after it is live,
  is journaled like every other mutation, and a failed relay bind keeps
  the physical resolver (a leak warning, not a blackout).
* **`upd` race guard.** Two credential rotations in quick succession
  could interleave route deletes/adds. The refresh is now single-flight
  like `up`.

## Automatic updates (new)

* **Detect → download → verify → swap → restart, one button.**
  The app checks GitHub on start and every 6 h, pre-downloads the
  update in the background (through the local SOCKS5 when GitHub is
  unreachable directly — the network that needs a VPN most), verifies
  it, and then offers a single button that installs and restarts.
* **SHA256SUMS.txt is the contract.** CI now checksums every release
  asset in the same job that uploads them, and the updater **refuses**
  any release without a matching entry — a tampered or truncated
  download is discarded before it can reach disk-as-the-new-binary.
* **Never unverified, never half-installed.** The staged archive is
  unpacked with path-traversal and symlink defense; the swap renames
  the old build aside (a running image is never deleted), rolls back
  on any failure, and writes a pending marker that the *next* boot
  verifies ("you are on the new build" — or a rollback warning).
  The restart happens only after a clean disconnect: tunnel torn down,
  system proxy restored, core stopped.
* **Format sniffing.** The staged archive is unpacked by magic bytes,
  not by file-name extension. Source installations (git checkouts) are
  detected and told to `git pull` instead of self-swapping.
* **UI.** A version chip in the footer (dot when an update is waiting),
  an update sheet with progress, notes and the one-click button, and a
  settings switch to disable automatic checks.

## Release discipline

* **A test gate in CI.** The workflow now has a dedicated `test` job
  (full portable regression: tunnel fixes, self-update, speed pass,
  fix pack, integration, Web UI) that must pass before the release job
  runs at all — a failing regression anywhere means no release is
  published, on all three platforms.

## Numbers

* Verification: **31** new tunnel-fix checks + **36** self-update
  checks, all green; **42** speed-pass, **59** fix-pack, **11**
  integration and **23** Web UI regressions re-run green
  (**202/202** total).

Upgrade in place from 4.7.0 is not possible (this is the release that
*brings* the updater) — download it once from Releases; every future
release updates itself with one click.
