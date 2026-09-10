"""Local test path setup for the example package."""

from __future__ import annotations

import sys
from pathlib import Path

PACKAGE_ROOT = Path(__file__).parents[1] / "src"
PROJECT_ROOT = Path(__file__).parents[3]
for path in (PACKAGE_ROOT, PROJECT_ROOT / "src"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
