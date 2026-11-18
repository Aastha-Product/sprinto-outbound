"""Reads a list of domains. Shared by the CLI and the Streamlit app so both behave the same.

Rules:
- blank lines are ignored
- lines starting with `#` are ignored
- anything after ` #` on a line is an inline comment and is ignored
- `https://www.Example.com/about` becomes `example.com`
- duplicates are removed (first one wins, order kept)
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse


def normalise_domain(raw: str) -> str:
    """Turn whatever the user typed into a bare lowercase domain like `rocketlane.com`."""
    text = raw.strip().lower()
    if not text:
        return ""
    if "://" not in text:
        text = "https://" + text
    host = urlparse(text).hostname or ""
    if host.startswith("www."):
        host = host[4:]
    return host.strip(".")


def parse_domains(text: str) -> list[str]:
    """Parse the contents of a domains file (or a Streamlit textarea) into a clean list."""
    domains: list[str] = []
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        domain = normalise_domain(line)
        if domain and "." in domain and domain not in domains:
            domains.append(domain)
    return domains


def read_domains(path: str | Path) -> list[str]:
    """Read and parse a domains file such as `data/domains.txt`."""
    return parse_domains(Path(path).read_text(encoding="utf-8"))
