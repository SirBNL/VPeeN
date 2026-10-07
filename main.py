#!/usr/bin/env python3
"""
VPeeN - GUI launcher.

  python main.py          -> open the desktop app
  python -m vpeen.cli     -> terminal mode (list / test / run / export)
"""
import os
import sys
import time


def _install_run_log() -> None:
    """Always-on run/crash log: tee stdout+stderr into ~/.vpeen/last-run.log.

    The windowed frozen build has no console, so any traceback would be
    invisible to the user (and to us).  This guarantees that whatever
    happens - import errors, GUI crashes, thread exceptions - ends up in
    a file the user can send.  It must NEVER break the app, so every
    failure inside here is swallowed.
    """
    try:
        path = os.path.join(os.path.expanduser("~"), ".vpeen", "last-run.log")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        f = open(path, "w", encoding="utf-8", errors="replace", buffering=1)
    except Exception:
        return

    console_out = sys.stdout if sys.stdout is not None else None
    console_err = sys.stderr if sys.stderr is not None else None

    def _safe_write(console, s):
        if console is None:
            return len(s)
        try:
            return console.write(s)
        except Exception:
            return len(s)

    class _Tee:
        def __init__(self, console):
            self._c = console

        def write(self, s):
            try:
                f.write(s)
            except Exception:
                pass
            return _safe_write(self._c, s)

        def flush(self):
            try:
                f.flush()
            except Exception:
                pass
            if self._c is not None:
                try:
                    self._c.flush()
                except Exception:
                    pass

        def isatty(self):
            return False

        def fileno(self):
            if self._c is not None:
                return self._c.fileno()
            raise OSError("no underlying console")

        def reconfigure(self, *a, **k):  # utils.ensure_utf8_console calls this
            if self._c is not None:
                try:
                    self._c.reconfigure(*a, **k)
                except Exception:
                    pass

        def __getattr__(self, name):
            return getattr(self._c, name)

    def _crash(kind):
        def _hook(*args):
            try:
                import traceback
                f.write(f"\n--- {kind} {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
                if kind == "CRASH" and len(args) == 3:
                    traceback.print_exception(*args, file=f)
                elif kind == "THREAD-CRASH":
                    a = args[0]
                    traceback.print_exception(a.exc_type, a.exc_value,
                                              a.exc_traceback, file=f)
                f.write("--- end ---\n")
                f.flush()
            except Exception:
                pass
        return _hook

    try:
        import threading
        threading.excepthook = _crash("THREAD-CRASH")
        sys.excepthook = _crash("CRASH")
    except Exception:
        pass

    try:
        from vpeen import __version__
        v = __version__
    except Exception:
        v = "?"
    try:
        frozen = "frozen" if getattr(sys, "frozen", False) else "source"
        f.write(f"=== VPeeN v{v} ({frozen}, {sys.platform}) "
                f"{time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
        f.flush()
    except Exception:
        pass

    try:
        sys.stdout = _Tee(console_out)
        sys.stderr = _Tee(console_err)
    except Exception:
        pass


def main() -> int:
    _install_run_log()
    try:
        from vpeen.gui import run
    except ImportError as e:
        print(f"GUI dependencies missing: {e}")
        print("Install them with:  pip install -r requirements.txt")
        print("Or use the CLI:     python -m vpeen.cli --help")
        return 1
    try:
        return run()
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
