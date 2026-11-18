"""Sprinto outbound pipeline: scrape -> extract -> qualify -> PoC -> pain points -> draft -> send.

See CLAUDE.md for the architecture and guardrails.
"""

import warnings
from pathlib import Path

# urllib3 warns that optional SOCKS proxy support isn't installed. We don't use proxies; hide the noise.
warnings.filterwarnings("ignore", message=".*SOCKS support.*")

# The project root folder (the one that contains CLAUDE.md). Every other path is built from this,
# so the code works no matter which folder you run it from.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
