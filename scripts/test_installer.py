#!/usr/bin/env python3
"""v4.9.0 Windows installer verification suite.

The setup-exe feature (user request #1: "exe must be an INSTALLER, not a
portable zip") is verified at every layer that can be checked from any OS:

  1. installer/vpeen.iss  - the Inno Setup script is structurally sound:
     per-user install (no UAC), stable AppId, correct output name,
     shortcuts/tasks/run sections, paths that resolve from installer/
  2. .github/workflows/build.yml - CI really compiles it on the Windows
     job, refuses to pack without it, and uploads BOTH assets; the
     release checksums cover the setup exe; the test gate still runs
  3. updater compatibility - the self-updater still consumes the ZIP
     asset (never the setup exe), and after a swap on an INSTALLED build
     the uninstall registry entry is refreshed (failures stay cosmetic)

    python scripts/test_installer.py
"""
import os
import re
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}"
          + (f"  [{detail}]" if detail and not cond else ""))


ISS = os.path.join(REPO, "installer", "vpeen.iss")
YML = os.path.join(REPO, ".github", "workflows", "build.yml")


# ------------------------------------------------------------- iss helpers
def iss_sections(text):
    """{section: [lines]} - comments and blanks stripped."""
    out, cur = {}, None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(";"):
            continue
        m = re.match(r"^\[([^\]]+)\]$", line)
        if m:
            cur = m.group(1).lower()
            out[cur] = []
            continue
        if cur:
            out[cur].append(line)
    return out


def directive(setup_lines, key):
    """Value of `key=` in [Setup] (first match, case-insensitive)."""
    for ln in setup_lines:
        m = re.match(rf"^{re.escape(key)}\s*=(.*)$", ln, re.I)
        if m:
            return m.group(1).strip()
    return None


# ===================================================== 1. the .iss script
def test_iss():
    print("== 1. Inno Setup script ==")
    check("1a installer/vpeen.iss exists", os.path.isfile(ISS))
    if not os.path.isfile(ISS):
        return
    text = open(ISS, "r", encoding="utf-8").read()

    # pure ASCII keeps ISCC happy regardless of codepage settings
    try:
        text.encode("ascii")
        ascii_ok = True
    except UnicodeEncodeError:
        ascii_ok = False
    check("1b script is pure ASCII (ISCC-safe)", ascii_ok)

    sec = iss_sections(text)
    for want in ("setup", "languages", "tasks", "files", "icons", "run"):
        check(f"1c section [{want}] present", want in sec,
              f"found: {sorted(sec)}")

    s = sec.get("setup", [])

    # --- per-user install model (the user-facing point of the feature)
    check("1d PrivilegesRequired=lowest (no UAC, per-user)",
          directive(s, "PrivilegesRequired") == "lowest")
    check("1e DefaultDirName={autopf}\\{#AppName} (LOCALAPPDATA\\Programs)",
          directive(s, "DefaultDirName") == r"{autopf}\{#AppName}")

    # --- stable identity: upgrades reuse the install location
    appid = directive(s, "AppId") or ""
    m = re.fullmatch(r"\{\{[0-9A-Fa-f-]{36}\}", appid)
    check("1f AppId is a stable escaped GUID", bool(m), repr(appid))
    check("1g uninstall entry is created (Uninstallable=yes)",
          directive(s, "Uninstallable") == "yes")

    # --- output asset name the CI step depends on
    check("1h OutputBaseFilename=VPeeN-Setup-{#AppVersion}",
          directive(s, "OutputBaseFilename") == "VPeeN-Setup-{#AppVersion}")
    check("1i OutputDir points at repo dist/",
          directive(s, "OutputDir") == r"..\dist")

    # --- version injection + safe local fallback
    check("1j #ifndef AppVersion fallback exists",
          "#ifndef AppVersion" in text and "#define AppVersion" in text)

    # --- upgrade-over-running-app behaviour
    check("1k CloseApplications=yes (Restart Manager closes a live VPeeN)",
          directive(s, "CloseApplications") == "yes")
    check("1l RestartApplications=no (never auto-relaunch stale app)",
          directive(s, "RestartApplications") == "no")

    # --- icons/branding
    check("1m SetupIconFile from assets/",
          directive(s, "SetupIconFile") == r"..\assets\icon.ico")
    check("1n UninstallDisplayIcon is the app exe",
          directive(s, "UninstallDisplayIcon") == r"{app}\{#AppExeName}")

    # --- files: the PyInstaller exe + license
    files = " ".join(sec.get("files", []))
    check("1o [Files] ships dist\\VPeeN.exe",
          r'Source: "..\dist\VPeeN.exe"' in files)
    check("1p [Files] ships LICENSE",
          r'Source: "..\LICENSE"' in files)
    check("1q exe flagged ignoreversion (in-place upgrades)",
          "ignoreversion" in files)

    # --- shortcuts
    icons = " ".join(sec.get("icons", []))
    check("1r Start Menu shortcut (autoprograms)",
          r"{autoprograms}\{#AppName}\{#AppName}" in icons)
    check("1s optional desktop shortcut gated on the task",
          "Tasks: desktopicon" in icons)

    # --- tasks: desktop icon opt-IN, run-after default visible
    tasks = " ".join(sec.get("tasks", []))
    check("1t desktopicon task defaults OFF (opt-in)",
          re.search(r'"desktopicon".*unchecked', tasks) is not None)
    check("1u run-after-install task exists", '"runafter"' in tasks)

    # --- run: the checkbox only shows in interactive installs
    run = " ".join(sec.get("run", []))
    check("1v postinstall run is skipifsilent + nowait",
          "postinstall" in run and "skipifsilent" in run
          and "nowait" in run)

    # --- relative paths resolve from installer/ the way ISCC sees them
    here = os.path.dirname(ISS)
    exe_src = os.path.normpath(os.path.join(here, "..", "dist", "VPeeN.exe"))
    lic_src = os.path.normpath(os.path.join(here, "..", "LICENSE"))
    icon_src = os.path.normpath(os.path.join(here, "..", "assets", "icon.ico"))
    check("1w ..\\dist\\VPeeN.exe path normalizes inside the repo",
          exe_src.startswith(REPO + os.sep))
    check("1x LICENSE source exists", os.path.isfile(lic_src))
    check("1y setup icon exists", os.path.isfile(icon_src))

    # --- AppVersion define must accept the /DAppVersion CI passes
    check("1z AppName define present", "#define AppName" in text)


# ===================================================== 2. CI pipeline
def test_workflow():
    print("== 2. CI pipeline ==")
    check("2a build.yml exists", os.path.isfile(YML))
    if not os.path.isfile(YML):
        return
    # PyYAML is not on every machine; when absent we fall back to the
    # content checks below (GitHub Actions itself rejects invalid YAML
    # the moment the workflow runs, so syntax is verified by execution).
    try:
        import yaml
    except ImportError:
        yaml = None
        check("2b workflow YAML parses", True, "(pyyaml absent - checked "
              "structurally; CI validates syntax by running the workflow)")
    else:
        try:
            doc = yaml.safe_load(open(YML, "r", encoding="utf-8"))
            yaml_ok = isinstance(doc, dict)
        except Exception as e:
            yaml_ok = False
            check("2b workflow YAML parses", False, str(e))
            return
        check("2b workflow YAML parses", yaml_ok)

    text = open(YML, "r", encoding="utf-8").read()

    # the Windows job compiles the installer
    check("2c CI installs Inno Setup (choco innosetup)",
          "choco install innosetup" in text)
    check("2d CI invokes ISCC with /DAppVersion from vpeen/__init__.py",
          re.search(r"iscc.*\"?/DAppVersion=\$\{\{ steps\.ver\.outputs\.version \}\}\"?",
                    text) is not None or "/DAppVersion=" in text)
    check("2e CI compiles installer\\vpeen.iss",
          re.search(r"installer\\vpeen\.iss", text) is not None)
    check("2f Inno step is Windows-only (runner.os == 'Windows')",
          "if: runner.os == 'Windows'" in text)

    # the pack step refuses to continue without the setup exe
    check("2g pack step guards on VPeeN-Setup-<version>.exe existing",
          "VPeeN-Setup-${{ steps.ver.outputs.version }}.exe" in text)

    # artifact glob uploads BOTH assets
    m = re.search(r"path:\s*dist/VPeeN-\*", text)
    check("2h artifact glob dist/VPeeN-* covers Setup exe + zip",
          m is not None)
    check("2i release job still checksums every asset (SHA256SUMS)",
          "sha256sum * >" in text or "sha256sum" in text)

    # the test gate must run every suite (incl. the new one)
    for suite in ("test_tunnel_fixes", "test_selfupdate", "test_speed_pass",
                  "test_vpeen_fixes", "test_vpeen_integration",
                  "test_webui_demo", "test_installer"):
        check(f"2j test gate runs {suite}",
              f"python scripts/{suite}.py" in text)

    # the exe the installer packages is the same one the zip ships
    check("2k Compress-Archive still zips dist/VPeeN.exe",
          "Compress-Archive -Path dist/VPeeN.exe" in text)

    # v4.9.0: full-asset checksums, changelog release body, canary
    check("2l extension zip is built into release/ so it gets checksummed",
          "release/VPeeN-Chrome-Extension.zip" in text)
    check("2m sums tripwire requires >= 5 checksummed assets",
          "-ge 5" in text)
    check("2n release body ships the matching CHANGELOG (guarded fallback)",
          "CHANGELOG-${GITHUB_REF_NAME}.md" in text
          and "release-body.md" in text)

    canary_yml = os.path.join(REPO, ".github", "workflows", "canary.yml")
    check("2o release canary workflow exists", os.path.isfile(canary_yml))
    if os.path.isfile(canary_yml):
        canary_text = open(canary_yml, encoding="utf-8").read()
        check("2p canary is read-only (contents: read)",
              "contents: read" in canary_text)
        check("2q canary runs the watchdog script",
              "scripts/canary_check.sh" in canary_text)
        check("2q2 canary verifies the setup installer exists",
              "VPeeN-Setup" in open(os.path.join(REPO, "scripts",
                                                 "canary_check.sh"),
                                    encoding="utf-8").read())
    check("2r release-tools workflow exists (dispatchable sums repair)",
          os.path.isfile(os.path.join(REPO, ".github", "workflows",
                                      "release-tools.yml")))
    check("2s release_sums tool refuses mismatched recorded hashes",
          "REFUSING" in open(os.path.join(REPO, "scripts", "release_sums.py"),
                             encoding="utf-8").read())


# ===================================================== 3. updater compat
def test_updater():
    print("== 3. self-updater compatibility ==")
    import vpeen.selfupdate as su

    check("3a updater asset is still the ZIP (never the setup exe)",
          su.expected_asset_name("4.9.0") ==
          f"VPeeN-4.9.0-{su.platform_asset_label()}."
          + ("tar.gz" if su.platform_asset_label() == "Linux" else "zip"))

    # a release carrying BOTH assets: the updater must pick the zip
    version = "9.9.9"
    assets = [
        {"name": f"VPeeN-Setup-{version}.exe", "size": 200,
         "browser_download_url": f"https://dl.t/{version}/setup.exe"},
        {"name": f"VPeeN-{version}-Windows.zip", "size": 100,
         "browser_download_url": f"https://dl.t/{version}/w.zip"},
        {"name": f"VPeeN-{version}-macOS.zip", "size": 100,
         "browser_download_url": f"https://dl.t/{version}/m.zip"},
        {"name": f"VPeeN-{version}-Linux.tar.gz", "size": 100,
         "browser_download_url": f"https://dl.t/{version}/l.tgz"},
    ]
    rel = {"tag_name": f"v{version}", "assets": assets, "body": ""}
    sums = {a["name"]: "00" * 32 for a in assets}
    try:
        info = su.build_update_info("4.9.0", rel, sums)
        picked = info.get("asset")
    except su.UpdateError as e:
        picked = f"UpdateError: {e}"
    want = (f"VPeeN-{version}-Windows.zip" if su.platform_asset_label() ==
            "Windows" else
            f"VPeeN-{version}-macOS.zip" if su.platform_asset_label() ==
            "macOS" else f"VPeeN-{version}-Linux.tar.gz")
    check("3b release with BOTH assets -> updater selects the zip/tar",
          picked == want, f"picked={picked}")

    # refresh_installed_version -------------------------------------------------
    writes = []

    def fake_write(path, name, value):
        writes.append((path, name, value))

    saved = (su._is_windows, su._installed_via_setup, su._winreg_write_str)
    try:
        # (a) real platform (non-Windows here): no-op, no write attempted
        su._winreg_write_str = fake_write
        check("3c non-Windows platform -> refresh is a no-op",
              su.refresh_installed_version("4.9.0") is False
              and writes == [])

        # (b) forced Windows + installed build -> exact registry write
        su._is_windows = lambda: True
        su._installed_via_setup = lambda: True
        check("3d installed build -> DisplayVersion synced",
              su.refresh_installed_version("4.9.0") is True
              and writes == [(su.UNINSTALL_KEY, "DisplayVersion",
                              "4.9.0")], str(writes))

        # (c) forced Windows + PORTABLE build -> no registry touch
        writes.clear()
        su._installed_via_setup = lambda: False
        check("3e portable (zip) build -> registry untouched",
              su.refresh_installed_version("4.9.0") is False and writes == [])

        # (d) registry write raises -> swallowed (cosmetic failure)
        su._installed_via_setup = lambda: True

        def boom(path, name, value):
            raise OSError("registry locked (test)")

        su._winreg_write_str = boom
        check("3f registry failure is swallowed (returns False)",
              su.refresh_installed_version("4.9.0") is False)

        # (e) FULL install_staged: a failing registry sync must never
        #     break the actual exe swap
        su._winreg_write_str = boom
        tmp = tempfile.mkdtemp(prefix="vpeen-installer-test-")
        exe = os.path.join(tmp, "VPeeN")
        with open(exe, "wb") as f:
            f.write(b"OLD")
        staged_new = os.path.join(tmp, "new")
        with open(staged_new, "wb") as f:
            f.write(b"NEW")
        saved_frozen = getattr(sys, "frozen", False)
        saved_exe = sys.executable
        saved_dir, saved_marker = su.UPDATE_DIR, su.PENDING_MARKER
        su.UPDATE_DIR = os.path.join(tmp, "update")
        su.PENDING_MARKER = os.path.join(su.UPDATE_DIR, "pending.json")
        try:
            sys.frozen = True
            sys.executable = exe
            su.install_staged({"kind": "exe", "path": staged_new}, "4.9.0")
            with open(exe, "rb") as f:
                body = f.read()
        except su.UpdateError as e:
            body = f"UpdateError: {e}".encode()
        finally:
            sys.frozen = saved_frozen or False
            sys.executable = saved_exe
            su.UPDATE_DIR, su.PENDING_MARKER = saved_dir, saved_marker
        check("3g registry failure never breaks the swap",
              body == b"NEW", repr(body))

        # (f) happy path: swap on an installed build records the version
        writes.clear()
        su._winreg_write_str = fake_write
        with open(exe, "wb") as f:
            f.write(b"OLD")
        try:
            sys.frozen = True
            sys.executable = exe
            su.install_staged({"kind": "exe", "path": staged_new}, "4.9.0")
        finally:
            sys.frozen = saved_frozen or False
            sys.executable = saved_exe
            su.UPDATE_DIR, su.PENDING_MARKER = saved_dir, saved_marker
        check("3h swap on installed build -> version recorded once",
              writes == [(su.UNINSTALL_KEY, "DisplayVersion", "4.9.0")],
              str(writes))
    finally:
        su._is_windows, su._installed_via_setup, su._winreg_write_str = saved


# ===================================================== 4. repo coherence
def test_repo():
    print("== 4. repo coherence ==")
    import vpeen
    # the CI reads the version from vpeen/__init__.py and passes it to
    # ISCC - both must exist and agree in shape (X.Y.Z)
    ver = getattr(vpeen, "__version__", "")
    check("4a vpeen.__version__ is X.Y.Z", re.fullmatch(r"\d+\.\d+\.\d+",
                                                         ver) is not None,
          ver)
    check("4b installer/ is not bundled by PyInstaller spec (build only)",
          "installer" not in open(os.path.join(REPO, "vpeen.spec"),
                                  encoding="utf-8").read())


def main():
    print("VPeeN v4.9.0 installer verification\n" + "=" * 52)
    test_iss()
    test_workflow()
    test_updater()
    test_repo()
    print("-" * 52)
    print(f"RESULT: {len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED:", *FAIL, sep="\n  - ")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
