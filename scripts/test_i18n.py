#!/usr/bin/env python3
"""v4.9.0 Persian UI (i18n + RTL) verification suite.

  1. the I18N dictionary in app.js is valid JSON (extracted with node)
     with IDENTICAL key sets for en + fa and matching placeholders
  2. every data-i18n / data-i18n-ph / data-i18n-title key used by
     index.html exists in BOTH dictionaries
  3. every t("key") call in app.js exists in BOTH dictionaries
  4. the fa side is really Persian and RTL is wired: dir flipping,
     rtl CSS rules, the language select, the persisted setting and its
     validation through /api/settings

    python scripts/test_i18n.py
"""
import json
import os
import re
import shutil
import subprocess
import sys
import threading

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}"
          + (f"  [{detail}]" if detail and not cond else ""))


APP_JS = os.path.join(REPO, "vpeen", "web", "app.js")
INDEX_HTML = os.path.join(REPO, "vpeen", "web", "index.html")
STYLE_CSS = os.path.join(REPO, "vpeen", "web", "style.css")


def extract_i18n():
    """Run the I18N object out of app.js with node (the sandbox AND the
    CI runners ship Node; without node the suite degrades to structural
    checks so it never silently skips)."""
    if shutil.which("node") is None:
        return None
    src = open(APP_JS, encoding="utf-8").read()
    m = re.search(r"const I18N = (\{.*?\n\});\n", src, re.S)
    if not m:
        return None
    script = ("const I18N = " + m.group(1) + ";\n"
              "process.stdout.write(JSON.stringify(I18N));\n")
    r = subprocess.run(["node", "-e", script], capture_output=True,
                       text=True, timeout=30)
    if r.returncode != 0:
        return None
    return json.loads(r.stdout)


def test_dictionary():
    print("== 1. the I18N dictionary ==")
    check("1a I18N block exists in app.js",
          "const I18N = {" in open(APP_JS, encoding="utf-8").read())
    data = extract_i18n()
    if data is None:
        check("1b dictionary extracts as valid JSON (node)", False,
              "node extraction failed")
        return None
    check("1b dictionary extracts as valid JSON (node)",
          isinstance(data, dict) and set(data) >= {"en", "fa"})
    en, fa = data["en"], data["fa"]
    check("1c en and fa carry IDENTICAL key sets",
          set(en) == set(fa),
          f"only-en={sorted(set(en) - set(fa))[:6]} "
          f"only-fa={sorted(set(fa) - set(en))[:6]}")
    check("1d both dictionaries are substantial (>=90 keys)",
          len(en) >= 90 and len(fa) >= 90, f"{len(en)}/{len(fa)}")

    # every value is a non-empty string
    bad = [k for k, v in en.items() if not isinstance(v, str) or not v.strip()]
    bad += [k for k, v in fa.items() if not isinstance(v, str) or not v.strip()]
    check("1e all values are non-empty strings", not bad, str(bad[:6]))

    # placeholders must match across languages ({label} in en -> {label} in fa)
    def ph(s):
        return sorted(re.findall(r"\{[a-z]+\}", s))
    mismatch = [k for k in en if ph(en[k]) != ph(fa[k])]
    check("1f placeholders match across en/fa", not mismatch,
          str(mismatch[:6]))

    # fa must actually be Persian (Arabic-script codepoints)
    fa_script = sum(1 for v in fa.values()
                    if any("\u0600" <= c <= "\u06FF" for c in v))
    check("1g fa side is written in Persian script",
          fa_script >= len(fa) * 0.9, f"{fa_script}/{len(fa)}")

    # a few load-bearing translations spot-checked
    spots = {
        "status_not_protected": "بدون محافظت",
        "ks_lockdown_title": "کیل‌سوئیچ فعال",
        "set_killswitch": "کیل‌سوئیچ",
        "settings_title": "تنظیمات",
        "btn_save": "ذخیرهٔ تغییرات",
    }
    for k, v in spots.items():
        check(f"1h fa[{k}] == {v}", fa.get(k) == v, repr(fa.get(k)))
    return data


def test_html_keys(data):
    print("== 2. index.html <-> dictionary ==")
    html = open(INDEX_HTML, encoding="utf-8").read()
    used = set(re.findall(r'data-i18n(?:-ph|-title)?="([a-z_0-9]+)"', html))
    check("2a html carries data-i18n attributes", len(used) >= 30,
          str(len(used)))
    if data:
        en, fa = data["en"], data["fa"]
        missing = sorted(k for k in used if k not in en or k not in fa)
        check("2b every html i18n key exists in BOTH dictionaries",
              not missing, str(missing[:8]))
    # the three attribute flavors are used
    check("2c placeholder + title + text flavors all used",
          'data-i18n-ph="search_ph"' in html
          and 'data-i18n-title="title_btn_logs"' in html
          and 'data-i18n="stat_down"' in html)
    # language select present with exactly the two options
    check("2d settings modal carries the language select",
          'id="set-lang"' in html and '<option value="en">English</option>'
          in html and 'value="fa"' in html)
    # the root element supports direction flipping
    check("2e <html> starts as lang=en dir=ltr (flipped at runtime)",
          '<html lang="en" dir="ltr">' in html)


def test_js_keys(data):
    print("== 3. app.js t() usage <-> dictionary ==")
    js = open(APP_JS, encoding="utf-8").read()
    # literal t("key") calls only - dynamic prefixes (t("dr_" + c.id))
    # are covered by the doctor suite's key checks
    used = set(re.findall(r'\bt\("([a-z_0-9]+)"\s*(?:,|\))', js))
    # CONNECTING_MSG_KEYS are indirect - they must exist too
    used |= {"connecting_msg_1", "connecting_msg_2",
             "connecting_msg_3", "connecting_msg_4"}
    check("3a app.js uses t() broadly", len(used) >= 60, str(len(used)))
    if data:
        en, fa = data["en"], data["fa"]
        missing = sorted(k for k in used if k not in en or k not in fa)
        check("3b every t() key exists in BOTH dictionaries",
              not missing, str(missing[:8]))
    # no hardcoded English left in the dynamic hot paths (search OUTSIDE
    # the I18N dictionary itself - its en values obviously contain these)
    js_body = re.sub(r"const I18N = \{.*?\n\};\n", "", js, flags=re.S)
    stale = [pat for pat in (
        '"Not Protected"', '"Kill Switch ON"', '"Repairing…"',
        '"Optimal Location"', '"Fastest free server"',
        '`Update available: v${', '"Settings saved."',
        '"Disconnecting…"') if pat in js_body]
    check("3c no hardcoded strings remain in the hot paths",
          not stale, str(stale))
    # RTL wiring
    check("3d setLang flips document dir + lang + body.rtl",
          'document.documentElement.dir = langDir(LANG)' in js
          and 'document.documentElement.lang = LANG' in js
          and 'classList.toggle("rtl"' in js)
    check("3e boot + settings events apply the persisted language",
          js.count('ui_lang === "fa"') >= 2)


def test_rtl_css():
    print("== 4. RTL stylesheet ==")
    css = open(STYLE_CSS, encoding="utf-8").read()
    need = [
        'html[dir="rtl"] body { direction: rtl; }',
        'html[dir="rtl"] .sheet {',
        'html[dir="rtl"] .sheet-head {',
        'html[dir="rtl"] .bottombar { flex-direction: row-reverse; }',
        'html[dir="rtl"] .toasts {',
        'html[dir="rtl"] .modal-foot { flex-direction: row-reverse; }',
    ]
    for pat in need:
        check(f"4a rule present: {pat[:46]}…", pat in css)
    check("4b log lines stay LTR under RTL (timestamps/hostnames)",
          'html[dir="rtl"] .log-lines { direction: ltr; }' in css)
    check("4c numeric stats stay LTR under RTL",
          'unicode-bidi: embed' in css)


def test_settings_flow():
    print("== 5. persisted language setting ==")
    import vpeen.settings as cfgmod
    import vpeen.webui as webui_mod
    check("5a DEFAULTS carry ui_lang=en",
          cfgmod.DEFAULTS.get("ui_lang") == "en")

    saved_save = cfgmod.save
    cfgmod.save = lambda cfg: None          # never touch ~/.vpeen
    try:
        class FakeBridge:
            lock = threading.RLock()
            update = None
            cfg = dict(cfgmod.DEFAULTS)

            def log(self, *a, **k):
                pass

            def emit(self, *a, **k):
                pass

            def public_settings(self):
                return dict(self.cfg)

        fb = FakeBridge()
        errs = webui_mod.Bridge.update_settings(fb, {"ui_lang": "fa"})
        check("5b ui_lang=fa accepted", errs == []
              and fb.cfg["ui_lang"] == "fa", str(errs))
        errs = webui_mod.Bridge.update_settings(fb, {"ui_lang": "en"})
        check("5c ui_lang=en accepted", errs == []
              and fb.cfg["ui_lang"] == "en")
        errs = webui_mod.Bridge.update_settings(fb, {"ui_lang": "klingon"})
        check("5d unknown language falls back to en (never saved raw)",
              errs == [] and fb.cfg["ui_lang"] == "en")
        errs = webui_mod.Bridge.update_settings(
            fb, {"ui_lang": "FA"})
        check("5e language is case-insensitive (FA -> fa)",
              errs == [] and fb.cfg["ui_lang"] == "fa")
        snap = webui_mod.Bridge.public_settings(fb)
        check("5f snapshot exposes ui_lang to the UI", "ui_lang" in snap)
    finally:
        cfgmod.save = saved_save

    # the demo webui serves the same page and the language rides along
    # in /api/state (smoke-checked through the settings dict above)


def main():
    print("VPeeN v4.9.0 Persian UI verification\n" + "=" * 52)
    data = test_dictionary()
    test_html_keys(data)
    test_js_keys(data)
    test_rtl_css()
    test_settings_flow()
    print("-" * 52)
    print(f"RESULT: {len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED:", *FAIL, sep="\n  - ")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
