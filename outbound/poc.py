"""Step 4: find the right point of contact (PoC) with Hunter.io.

Phase 0 only contains the free quota check (`hunter_remaining_searches`), used by
scripts/check_setup.py. The Domain Search logic is added in Phase 3.
"""

from __future__ import annotations

import httpx

from outbound.config import Settings

HUNTER_BASE = "https://api.hunter.io/v2"


def _hunter_headers(settings: Settings) -> dict[str, str]:
    # The key goes in a header, not the URL, so it can never show up in a logged URL.
    return {"X-API-KEY": settings.hunter_api_key.get_secret_value(), "User-Agent": settings.user_agent}


def hunter_remaining_searches(settings: Settings) -> int:
    """Ask Hunter how many Domain Searches are left this month. This call is free."""
    settings.require("hunter")
    resp = httpx.get(f"{HUNTER_BASE}/account", headers=_hunter_headers(settings), timeout=settings.request_timeout_seconds)
    resp.raise_for_status()
    data = resp.json().get("data", {})
    searches = data.get("requests", {}).get("searches") or data.get("calls") or {}
    available = int(searches.get("available", 0))
    used = int(searches.get("used", 0))
    return max(available - used, 0)
