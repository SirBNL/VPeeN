#!/usr/bin/env python3
"""
VPeeN - GUI launcher.

  python main.py          -> open the desktop app
  python -m vpeen.cli     -> terminal mode (list / test / run / export)
"""
import sys


def main() -> int:
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
