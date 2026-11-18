"""Shared start-up for the scripts in this folder.

Lets `python scripts\\something.py` find the `outbound` package, and makes the Windows
console print ✅/❌ correctly.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
