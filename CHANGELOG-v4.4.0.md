# VPeeN v4.4.0 — “Aurora” GUI rewrite + web-UI audit

The desktop GUI is now a **local web application**: a loopback-only HTTP
bridge (`vpeen/webui.py`) runs the proxy core + tunnel controller and
serves a zero-dependency UI (`vpeen/web/` — vanilla HTML/CSS/JS) that is
shown in a native window via **pywebview** (Windows/macOS) or the default
browser (Linux/any). CustomTkinter / Pillow / the whole Tk runtime are
gone from the bundle — the core stays pure-stdlib Python.

The rewrite was followed by a full audit of the new bridge + UI: **12
real defects** found and fixed, all covered by a new end-to-end test
(`scripts/test_webui_demo.py`, 27 checks against a live demo-mode server,
plus a real-network connect check).

## The Aurora UI

- Deep-space dark theme: drifting aurora gradients, dot grid, glass
  surfaces, gradient ring “power orb” with phase-reactive states
  (connecting shimmer / connected glow / error arc)
- Location sheet with circular flags, live search, per-region latency
  bars (real TCP-probe ms, not fake numbers), “Optimal” card
- Live stat cards: down/up totals **with live rates**, session timer,
  connections; IP card flips between real IP (offline) and exit IP
  (protected), click-to-copy
- Settings modal with full server-side validation; toasts; color-coded,
  autoscrolling, copyable log sheet
- Long-poll event stream (`/api/poll`) — sub-second UI updates with zero
  third-party JS
- Accessibility: full keyboard operation (Tab/Enter/Space/Escape), focus
  rings, `prefers-reduced-motion` support, ARIA labels
- `VPeeN_DEMO_DATA=1` — deterministic offline feed for screenshots/preview

## Critical

1. **Demo/data connect crashed the request — `webui.py`**
   `DEMO_LOCATIONS` rows lacked the `country` field, so
   `_region_label()` raised `KeyError` inside `POST /api/connect`. The
   handler thread died mid-response: the browser got a dropped
   connection instead of JSON, and the UI stayed stuck on
   “Connecting…” forever. Demo rows now carry `country`, and
   `_region_label()` uses `.get()` so no location row shape can ever
   crash a connect again.

2. **Bridge exceptions killed HTTP responses — `webui.py`**
   Any exception inside a bridge method called from `do_POST` aborted
   the handler before a response was written (this is exactly how the
   demo `KeyError` surfaced to users). Every endpoint is now wrapped:
   errors are logged, traced, and answered with a JSON 500.

3. **Tunnel mode never showed the exit country — `webui.py`**
   On `phase: connected` with a pending tunnel, the bridge returned
   *before* recording `self.region`, so the tunnel “up” event, the
   fallback messages, and the connected UI all reported region `None`
   (rendered as “Optimal Location”). The region is captured before the
   TUN layer is raised.

## High

4. **Tunnel disconnect hung for 8 s — `webui.py`**
   When the tunnel reported down while a disconnect was in flight, the
   bridge just returned and waited for the 8-second watchdog to stop
   the proxy core. The core is now stopped the moment the tunnel
   confirms down (same as the v4.3 Tk logic) — the watchdog remains
   only as a fallback for a silent tunnel helper.

5. **Region switch while connected did nothing in demo mode — `webui.py`**
   The demo branch of `request_connect()` ran *before* the
   busy/switch check, so `_pending_reconnect` was never set and
   `_demo_connect()` no-op'd while connected. The switch check now runs
   first (demo and real mode share it).

6. **Demo: disconnect during “connecting” self-reconnected — `webui.py`**
   The simulated handshake timeline kept running after a disconnect:
   ~4 s later it flipped the UI back to “connected” all by itself.
   Timeline threads now carry a generation counter that any new
   connect/disconnect invalidates.

7. **Out-of-range ports/MTU were saved silently — `webui.py`**
   `update_settings()` accepted `socks_port: 99999` or
   `tunnel_mtu: 100` and only exploded later at connect time with a
   vague “check Settings”. Ports are validated to 1–65535 and MTU to
   576–65535 at save time, with readable per-field messages.

## Medium

8. **Switch reconnect could be lost — `webui.py`**
   `request_disconnect()` with nothing busy (core already exited)
   ignored a pending reconnect, leaving `_pending_reconnect` set
   forever — the connect would silently never happen. The reconnect
   kick is now fired immediately, mirroring the v4.3 Tk GUI.

9. **Stale tunnel styling after disconnect — `web/app.js`**
   `S.mode` was never reset on the “disconnected” event, so
   `body[data-mode="tunnel"]` styling lingered after a tunnel session
   ended.

10. **Footer showed stale ports after saving settings — `web/app.js`**
    `#listeners-meta` was written once at boot; changing ports in
    Settings left the old values on screen until restart.

11. **Tunnel switch while connected didn’t apply — `web/app.js`**
    The v4.3 Tk GUI reconnected when the tunnel switch flipped while
    connected; the web UI only announced “next connect”. Parity
    restored: flipping the switch live triggers the same
    disconnect→reconnect switch path as changing location.

12. **Misleading System-Proxy tooltip — `web/index.html`**
    The toggle configures *automatic* system-proxy setup on connect,
    not an instant OS switch; the tooltip now says what it does. The
    pywebview availability probe is also computed once per launch
    instead of twice (`gui.py`).

## Housekeeping

* `vpeen.spec` bundles `vpeen/web/` (html/css/js) and drops the Tk /
  CustomTkinter / Pillow excludes’ GUI payload; Linux builds skip
  pywebview and open the browser instead.
* `requirements.txt` now contains only the optional `pywebview`
  (non-Linux) — the core needs nothing.
* Demo mode data includes countries; fresh `docs/` screenshots and a
  re-recorded cinematic `docs/demo.gif` (~25 s, 20 fps, <9 MB): a
  bespoke "aurora dart" cursor (white→mint gradient, teal rim glow,
  comet trail) glides through connect → location switch → copy-IP →
  logs with bezier arcs, live hover states and layered click ripples.
  Recorded frame-by-frame under a virtual clock — every CSS transition
  is stepped exactly one frame at a time and the /api feed is replayed
  from a deterministic timeline, so the motion is perfectly smooth
  (no dropped-frame stutter like a live screencast). The GIF itself is
  written by a custom delta-encoder (`scripts/write_gif.py`) with
  inter-frame transparency and motion-gated temporal stabilization —
  roughly half the size of an ffmpeg palette encode at equal quality.
* `pyflakes` stays clean; `python -m py_compile` across the tree.

## Verification

```
scripts/test_webui_demo.py         27/27 PASS   (live demo-mode server)
scripts/test_vpeen_fixes.py        59/59 PASS
scripts/test_vpeen_integration.py  11/11 PASS   (fake TLS upstream)
manual real-network check          connect → SOCKS5 fetch → exit-IP match
pyflakes vpeen/*.py main.py        clean
node --check vpeen/web/app.js      clean
```
