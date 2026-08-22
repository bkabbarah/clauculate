"""Entry point for both `python run.py` and the PyInstaller build."""

import os
import sys

# When frozen, the package is bundled; in source form it lives under src/.
_SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "src")
if os.path.isdir(_SRC) and _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from claude_usage.app import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
