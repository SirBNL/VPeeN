"""
VPeeN Kill Switch (v4.9.0).

WHY THIS EXISTS
---------------
When tun2socks dies mid-session the TUN adapter dies with it, and the
kernel drops every route that pointed at the adapter - including the
def1 hijack.  From that instant the machine routes DIRECTLY through the
physical default gateway while the UI still says "Protected": a silent,
unbounded IP leak.  A dead adapter is not a firewall.

THE MODEL
---------
    1. DETECT    - the elevated tunnel worker polls tun2socks every
                   second; worst-case exposure before containment is
                   that one poll interval (~1 s).
    2. CONTAIN   - platform firewall rules drop every non-loopback
                   outbound packet ("VPeeN-KS" tagged, journaled):
                     * Windows: netsh advfirewall block rule
                       (loopback is exempt by design in WFAS, so the
                       app <-> tun2socks <-> local-SOCKS plumbing stays
                       alive for the repair below)
                     * Linux:   iptables/ip6tables OUTPUT DROP for
                       non-loopback traffic (comment-tagged)
                   macOS has no safe stdlib firewall primitive - there
                   the kill switch is detection + fast repair only
                   (documented limitation).
    3. REPAIR    - respawn tun2socks (loopback only - needs no
                   internet), re-bind the hijack routes to the new
                   adapter, verify, and only THEN lift the containment:
                   in the successful path there is no leak window after
                   detection.  The local SOCKS core reconnects by
                   itself once containment lifts.
    4. LOCKDOWN  - if repair fails 3x, containment STAYS: nothing in,
                   nothing out (that is what a kill switch is for).
                   The user sees a red state and can disconnect (which
                   removes the rules) or retry.

NEVER-BRICK GUARANTEES
----------------------
* Rules carry a fixed tag and are journaled into the session file, so
  the existing crash-recovery sweep replays their removal.
* The startup sweep ALSO removes stale "VPeeN-KS" rules even when no
  session file exists (crash inside the engage window).
* engage() is delete-then-add: duplicate rules cannot pile up.
* release() is idempotent and verifies absence where possible.
* No rule survives "start the app once" - the only user-visible worst
  case of a hard crash during lockdown.

Standard library only, like the rest of the core.
"""
import os
import shutil
import subprocess
import sys

RULE_TAG = "VPeeN-KS"           # the netsh rule name / iptables comment
_DELETE_TRIES = 4               # idempotent-release loop bound

# Tests inject these; production hits real subprocess/which.
_run_hook = None                # (cmd: list[str]) -> (rc, out, err)
_which_hook = None              # (name: str) -> str | None


# ------------------------------------------------------------------ plumbing
def _run(cmd, timeout=25):
    if _run_hook is not None:
        return _run_hook(cmd)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout)
        return r.returncode, r.stdout or "", r.stderr or ""
    except Exception as e:                       # missing binary, timeout
        return 127, "", str(e)


def _which(name):
    if _which_hook is not None:
        return _which_hook(name)
    return shutil.which(name)


def _is_windows() -> bool:
    return os.name == "nt"


def _is_linux() -> bool:
    return sys.platform.startswith("linux")


# -------------------------------------------------------------- availability
def available() -> "tuple[bool, str]":
    """(supported, why-not) for THIS platform."""
    if _is_windows():
        return True, ""
    if _is_linux():
        if _which("iptables"):
            return True, ""
        return False, "iptables not found"
    return False, "macOS firewall containment is not supported yet " \
                  "(detection + repair still are)"


# ------------------------------------------------------------------- windows
_WIN_DELETE = ["netsh", "advfirewall", "firewall", "delete", "rule",
               f"name={RULE_TAG}"]
_WIN_ADD = ["netsh", "advfirewall", "firewall", "add", "rule",
            f"name={RULE_TAG}", "dir=out", "action=block", "enable=yes",
            "profile=any"]
_WIN_SHOW = ["netsh", "advfirewall", "firewall", "show", "rule",
             f"name={RULE_TAG}"]


def _win_engage() -> bool:
    _run(_WIN_DELETE)                    # stale rules must not stack up
    rc, _, _err = _run(_WIN_ADD)
    return rc == 0


def _win_release() -> bool:
    gone = True
    for _ in range(_DELETE_TRIES):
        rc, out, _ = _run(_WIN_SHOW)
        if rc != 0 or RULE_TAG not in out:
            return gone                   # rule absent -> released
        rc, _, _ = _run(_WIN_DELETE)
        gone = rc == 0
    return gone


# -------------------------------------------------------------------- linux
def _iptables_bin(family: str):
    name = "iptables" if family == "v4" else "ip6tables"
    return _which(name)


def _linux_rule(family: str) -> "list[str] | None":
    binp = _iptables_bin(family)
    if not binp:
        return None
    return [binp, "-I", "OUTPUT", "1", "!", "-o", "lo", "-m", "comment",
            "--comment", RULE_TAG, "-j", "DROP"]


def _linux_delete(family: str) -> "list[str] | None":
    binp = _iptables_bin(family)
    if not binp:
        return None
    return [binp, "-D", "OUTPUT", "!", "-o", "lo", "-m", "comment",
            "--comment", RULE_TAG, "-j", "DROP"]


def _linux_list(family: str):
    binp = _iptables_bin(family)
    if not binp:
        return None
    return [binp, "-S", "OUTPUT"]


def _linux_engage() -> "list[str] | None":
    """Insert the DROP rule for v4 (+v6 when present); returns the
    families actually covered, or None when even v4 failed."""
    families = []
    for family in ("v4", "v6"):
        rule = _linux_rule(family)
        if rule is None:
            continue
        # delete-then-insert keeps the engage idempotent (no stacking)
        dele = _linux_delete(family)
        if dele is not None:
            for _ in range(_DELETE_TRIES):
                rc, out, _ = _run(_linux_list(family))
                if rc != 0 or RULE_TAG not in (out or ""):
                    break
                rc, _, _ = _run(dele)
                if rc != 0:
                    break
        rc, _, _err = _run(rule)
        if family == "v4" and rc != 0:
            return None                  # v4 is the hard requirement
        if rc == 0:
            families.append(family)
    return families or None


def _linux_release(families: "list[str]") -> bool:
    gone = True
    for family in families or ["v4", "v6"]:
        dele = _linux_delete(family)
        if dele is None:
            continue
        lst = _linux_list(family)
        for _ in range(_DELETE_TRIES):
            rc, out, _ = _run(lst) if lst else (0, "", "")
            if rc != 0 or RULE_TAG not in (out or ""):
                break
            rc, _, _ = _run(dele)
            gone = gone and rc == 0
    return gone


# ------------------------------------------------------------------- public
def engage() -> "dict | None":
    """Raise the containment.  Returns a JOURNAL STEP on success (store
    it in the session journal so crash recovery releases the rules), or
    None when the platform cannot contain (caller falls back to
    repair-only and logs it)."""
    if _is_windows():
        if _win_engage():
            return {"type": "ks", "engine": "netsh", "families": ["v4"]}
        return None
    if _is_linux():
        families = _linux_engage()
        if families:
            return {"type": "ks", "engine": "iptables",
                    "families": families}
        return None
    return None


def release(step: "dict | None") -> bool:
    """Lift the containment described by the journal step.  Idempotent;
    safe to call with None or an unknown engine."""
    if not step or step.get("type") != "ks":
        return True
    engine = step.get("engine")
    if engine == "netsh":
        return _win_release()
    if engine == "iptables":
        return _linux_release(step.get("families") or ["v4"])
    return True


def release_from_journal(journal: dict) -> int:
    """Release every KS step inside a session journal (teardown +
    crash-recovery path).  Returns how many steps were handled."""
    n = 0
    for step in (journal or {}).get("ks", []):
        if isinstance(step, dict):
            release(step)
            n += 1
    return n


def sweep_stale() -> int:
    """Remove ANY leftover VPeeN-KS rules, journal or not.

    The startup sweep calls this elevated: a hard crash inside the
    engage window can leave rules on disk with no session file pointing
    at them - this is the guarantee that no rule survives "start the
    app once"."""
    n = 0
    if _is_windows():
        rc, out, _ = _run(_WIN_SHOW)
        if rc == 0 and RULE_TAG in (out or ""):
            _win_release()
            n += 1
    elif _is_linux():
        for family in ("v4", "v6"):
            lst = _linux_list(family)
            if lst is None:
                continue
            rc, out, _ = _run(lst)
            if rc == 0 and RULE_TAG in (out or ""):
                _linux_release([family])
                n += 1
    return n
