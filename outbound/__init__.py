"""Sprinto outbound pipeline: scrape -> extract -> qualify -> PoC -> pain points -> draft -> send.

See CLAUDE.md for the architecture and guardrails.
"""

from pathlib import Path

# The project root folder (the one that contains CLAUDE.md). Every other path is built from this,
# so the code works no matter which folder you run it from.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
