# VPeeN v4.9.1 — "Sangar" patch

A one-fix patch for v4.9.0, plus release-integrity hardening that
landed on main right after the v4.9.0 tag.

## 1. Fixed: the Doctor modal was invisible

The v4.9.0 headline feature had a self-inflicted wound: the modal
open-state CSS listed only the *settings* and *update* dialogs, so
`body[data-modal="doctor"]` never matched — the protection self-check
ran silently behind an `opacity: 0` overlay and the shield button in
the top bar appeared to do **nothing**.

* Both open-state rules now include the doctor dialog (visible +
  card transform), and a regression check
  (`test_doctor` 3e2) pins the selector so it cannot regress silently.
* Everything else about the Doctor worked — which made it worse: the
  checks ran, the log line appeared, only the results were unreachable.

## 2. Improved: Persian glyph fallback

`--font` now appends **Vazirmatn** and **Tahoma** after the Latin
stack: Latin UI text is untouched (earlier fonts still win per glyph),
and Persian text falls back to a properly designed Persian UI font
instead of whatever the OS guesses. Users with Vazirmatn installed get
the best rendering; Windows gets Tahoma.

## 3. Release integrity (repo-side, landed post-v4.9.0)

* **Every release asset is checksummed now.** The Chrome-extension zip
  used to ship without a SHA256SUMS entry — users could not verify it.
  It is built into the checksummed release directory and a `-ge 5`
  tripwire fails any future release that drops the contract. The live
  v4.9.0 sums were repaired to cover it (verified by the canary).
* **Daily release canary** (read-only): verifies the latest release
  downloads, every hash matches, the Windows Setup installer exists,
  no asset ships unverified, magic bytes are sane, and no `v*` tag
  exists without its release.
* **Changelog-driven release bodies:** each tag's CHANGELOG-vX.Y.Z.md
  is published as the release body automatically (with a guard, so a
  missing changelog can never block a release).
* **Dispatchable sums repair** (`Release tools` workflow) for
  maintainers: surgical append-only rebuild of SHA256SUMS.txt.

## 4. Docs

* README refreshed for v4.9: stale badges fixed, version history
  table, the v4.8 "Sentinel" section restored, project structure
  updated, and **new screenshots** — including the first Persian (RTL)
  screenshot and the Doctor panel.
* Repo topics fixed (dropped `customtkinter`, which was removed in
  v4.4.0; added `kill-switch`, `tun`, `tun2socks`, `pywebview`).

## Tests (the gate)

444 checks / 11 suites, all green before tagging:

| Suite | Checks | Covers |
|---|---|---|
| test_tunnel_fixes | 32 | tunnel worker regression |
| test_selfupdate | 36 | updater + registry sync |
| test_speed_pass | 42 | speed/quality regression |
| test_vpeen_fixes | 59 | core fixes regression |
| test_vpeen_integration | 11 | end-to-end proxy chain |
| test_webui_demo | 23 | web UI regression |
| test_installer | 67 | installer, CI pipeline, updater compat, canary/tools |
| test_killswitch | 51 | containment engine, repair, lockdown, sweeps |
| test_i18n | 36 | dictionary parity, RTL wiring, settings flow |
| test_bypass | 30 | parser + 3 desktops + plumbing |
| test_doctor | 57 | check engine + endpoint + frontend (+ modal visibility) |

## Upgrading

In-app self-update (v4.8.0+) picks this up automatically; the Windows
installer and portable zip both update in place. No settings or
protocol changes.
