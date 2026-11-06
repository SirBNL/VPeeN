# VPeeN v4.9.0 — "Sangar" (سنگر)

**Theme of this release: hold the line.** A real Windows installer, a firewall-grade
kill switch, a Persian UI, split tunneling, and a one-click protection self-check.
Everything shipped only after the full CI test gate passed — 434 checks across
11 suites.

---

## 1. Windows: a real installer (the #1 user request)

`VPeeN-Setup-x.y.z.exe` replaces the zip as the way to install VPeeN on Windows:

* **Inno Setup** builds a proper setup exe in CI (`installer/vpeen.iss`).
* **Per-user install** (`PrivilegesRequired=lowest`) into
  `%LOCALAPPDATA%\Programs\VPeeN` — **no UAC prompt, no admin rights** to install
  or upgrade (the VS Code "user setup" model).
* Start Menu shortcuts, an optional desktop icon, a run-after-install task, and a
  real **uninstaller** in *Apps & Features*.
* Stable `AppId` → in-place upgrades keep your install location.
* `CloseApplications=yes` → upgrading over a running VPeeN closes it cleanly
  instead of failing on a locked exe.
* The portable zip stays as a secondary asset (USB sticks, restricted machines).
* The in-app self-updater still consumes the zip asset for its atomic swap
  (the per-user install dir stays user-writable), and now **syncs the
  "Apps & Features" DisplayVersion** after every self-update
  (`selfupdate.refresh_installed_version`, cosmetic failures are swallowed).

## 2. Kill switch — traffic is blocked, never leaked (tunnel mode)

The uncomfortable truth first: when tun2socks dies mid-session, the TUN adapter
dies with it and **the kernel drops the hijack routes** — every packet then
silently leaves through your real gateway while the UI still says "Protected".
v4.8's "crash sweep" only cleaned up afterwards; it did not contain.

v4.9's kill switch (`vpeen/killswitch.py`, default **ON**):

1. **DETECT** — the elevated worker polls tun2socks every second.
2. **CONTAIN** — tagged firewall rules drop all non-loopback outbound traffic:
   * Windows: `netsh advfirewall` block rule `VPeeN-KS`
     (loopback is exempt in WFAS, so the repair plumbing stays alive)
   * Linux: `iptables`/`ip6tables` OUTPUT DROP (`! -o lo`), comment-tagged
   * macOS: no safe stdlib primitive — detection + repair only (documented)
3. **REPAIR** — respawn tun2socks (loopback only), re-bind the address +
   hijack routes to the reborn adapter (`tunnel_platforms.rebind_tun_routes`,
   incl. the new Windows interface index), restart the DNS relay, verify it
   stays alive — and only **then** lift the containment. The successful path
   has **no leak window after detection**; the local SOCKS core reconnects by
   itself once rules lift.
4. **LOCKDOWN** — after 3 failed repairs the containment *stays*: nothing in,
   nothing out, a red "Kill Switch ON" state, one click to disconnect/retry.

Never-brick guarantees: rules are journaled into the session file (crash
recovery replays their removal), engage is delete-then-add (no stacking),
release is idempotent, and the boot/cleanup sweeps remove stale `VPeeN-KS`
rules even with no session file. Heartbeat loss with the kill switch ON now
**keeps the blackhole** instead of restoring routes under a dead GUI.

## 3. Persian UI (فارسی) with full RTL

* Settings → Interface → **Language: English / فارسی** — persisted, applied
  instantly, no reload.
* ~120 translated strings; `dir="rtl"` flips the whole layout (sheets slide
  from the left, row directions, text alignment) while logs/figures stay LTR.
* The dictionary is pure JSON inside `app.js`; the CI test gate verifies
  key parity, placeholder parity and real Persian script.

## 4. Split tunneling (proxy mode)

Settings → Local proxy → **Bypass list** (`*.ir`, `bank.example.com`, …):
those destinations connect **directly** while the system proxy is on — bank
sites that block foreign IPs keep working.

* Windows: merged into `ProxyOverride` (restored byte-for-byte on disconnect).
* macOS: `networksetup -setproxybypassdomains` per service, original lists
  backed up and restored (empty originals restored as `Empty`).
* GNOME: `ignore-hosts`, backed up and restored verbatim.
* CLI: `--bypass "*.ir,example.com"` with `--set-system`.
* One parser everywhere (`systemproxy.parse_bypass`): wildcards kept, junk
  dropped, de-duplicated, capped at 200 entries; loopback safety floor always
  merged first. Tunnel mode deliberately ignores the list (full tunnel).

## 5. Doctor — one-click protection self-check

The shield button in the top bar runs 8 honest checks on the live state
(no third-party "what is my IP" sites — it uses the app's own probes):

| Check | Catches |
|---|---|
| Local proxy listeners | listeners actually up |
| Exit IP ≠ real IP | **the leak itself** (red FAIL if they match) |
| Traffic routing | proxy-mode partial coverage |
| DNS privacy | DNS going to your ISP |
| Kill switch | armed / engaged / lockdown / disabled |
| System proxy applied | + bypass-list summary |
| QUIC / UDP side channel | QUIC bypassing an HTTP proxy |
| WebRTC advisory | browser-level IP exposure |

Results are sorted problems-first, localized (EN/FA), and summarized in the
log (`Self-check: 8 checks: 4 ok, 4 warnings, 0 problems`).

---

## Upgrading & compatibility

* Settings file gains `kill_switch` (default `true`), `ui_lang` (`"en"`),
  `bypass_domains` (`""`) — older settings.json files upgrade silently.
* No protocol changes; existing sessions and locations work as before.
* macOS containment is repair-only for now (pf integration is future work);
  everything else behaves identically cross-platform.

## Tests (the gate)

434 checks / 11 suites, all green before tagging:

| Suite | Checks | Covers |
|---|---|---|
| test_tunnel_fixes | 32 | tunnel worker regression |
| test_selfupdate | 36 | updater + new registry sync |
| test_speed_pass | 42 | speed/quality regression |
| test_vpeen_fixes | 59 | core fixes regression |
| test_vpeen_integration | 11 | end-to-end proxy chain |
| test_webui_demo | 23 | web UI regression |
| **test_installer** | **58** | **.iss script, CI pipeline, updater compat** |
| **test_killswitch** | **51** | **containment engine, repair, lockdown, sweeps** |
| **test_i18n** | **36** | **dictionary parity, RTL wiring, settings flow** |
| **test_bypass** | **30** | **parser + 3 desktops + plumbing** |
| **test_doctor** | **56** | **check engine + endpoint + frontend** |
