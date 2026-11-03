"""
VPeeN desktop GUI launcher (v4.4.0 "Aurora" web-tech UI).

The GUI is a local web application served from vpeen/web/ on a random
loopback port (vpeen.webui).  This launcher opens it in a native desktop
window via pywebview when that package is available and falls back to
the system browser otherwise (the UI is fully functional either way).

    python main.py            -> desktop app
    VPeeN_DEMO_DATA=1 ...     -> deterministic offline feed (preview)

Why the rewrite: the previous CustomTkinter shell hit the ceiling of
what Tk can render (no gradients on text, no CSS-quality shadows, no
SVG, chunky animations).  The web stack gives the app a real
product-grade look while the proxy core stays pure-stdlib Python.
"""
import sys
import time
import webbrowser

from .webui import WebUI

WINDOW_W, WINDOW_H = 1180, 780
WINDOW_MIN = (960, 640)


def _open_webview(url: str, ui: WebUI) -> int:
    """Native window via pywebview (optional dependency)."""
    import webview  # noqa: F401 - optional, imported lazily on purpose

    win = webview.create_window(
        "VPeeN",
        url,
        width=WINDOW_W,
        height=WINDOW_H,
        min_size=WINDOW_MIN,
        background_color="#05070d",
        text_select=False,
    )

    def _on_closing():
        try:
            ui.bridge.log("Window closed - shutting down.", "info")
        except Exception:
            pass
        ui.stop()

    win.events.closing += _on_closing
    webview.start()
    return 0


def _open_browser(url: str, ui: WebUI) -> int:
    """Fallback: run the UI in the default browser.

    The process stays alive (Ctrl+C to stop, or the "Shut down VPeeN"
    button inside Settings).  A frozen windowed build has no console,
    so the in-UI button is the primary exit there."""
    print()
    print("=" * 62)
    print("  VPeeN is running in browser mode.")
    print(f"  Open:  {url}")
    print("  Stop:  Ctrl+C here, or 'Shut down VPeeN' in Settings.")
    print("=" * 62)
    print()
    try:
        webbrowser.open(url)
    except Exception:
        pass
    try:
        while not ui.bridge.done.is_set():
            time.sleep(0.4)
    except KeyboardInterrupt:
        pass
    return 0


def run() -> int:
    native = _webview_available()   # probe once, not twice
    ui = WebUI(native_window=native)
    ui.start()
    url = ui.url
    try:
        if native:
            return _open_webview(url, ui)
        return _open_browser(url, ui)
    finally:
        ui.stop()
        # give the core a beat to restore system-proxy state cleanly
        deadline = time.time() + 6
        while ui.bridge.core.is_busy() and time.time() < deadline:
            time.sleep(0.1)


def _webview_available() -> bool:
    """True when pywebview imports AND has a usable backend for this OS.

    On Linux without GTK/QT web bindings `import webview` raises, so the
    app degrades to browser mode instead of crashing."""
    try:
        import importlib
        importlib.import_module("webview")
        return True
    except Exception:
        return False

if __name__ == "__main__":
    sys.exit(run())
