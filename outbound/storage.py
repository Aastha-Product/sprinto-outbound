"""The only module that reads or writes stored results.

- `DATABASE_URL` set   -> Neon Postgres (used on Vercel, and locally if you copy the URL into .env)
- `DATABASE_URL` empty -> plain files in runs/ and .cache/
- tests                -> an in-memory store (no disk, no network)

Phase 0b only has `ping_database()` for the health check. The stores are built in Phase 1.
"""

from __future__ import annotations

import psycopg

from outbound.config import Settings


def ping_database(settings: Settings) -> str:
    """Return 'ok', 'not configured', or a short error type. Never returns the connection string."""
    url = settings.database_url.get_secret_value()
    if not url:
        return "not configured"
    try:
        with psycopg.connect(url, connect_timeout=5) as conn:
            conn.execute("SELECT 1")
        return "ok"
    except Exception as exc:  # report the kind of error only; the message could contain the host
        return f"error ({type(exc).__name__})"
