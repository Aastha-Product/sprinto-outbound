"""The only module that reads or writes stored results.

Three interchangeable stores with the same methods:
- `NeonStore`   when `DATABASE_URL` is set (used on Vercel; also locally if you copy the URL into .env)
- `FileStore`   otherwise: `runs/<run_id>/<domain>/01_scrape.json ...` and `.cache/<kind>/<key>.json`
- `MemoryStore` for tests (no disk, no network)

What gets stored:
- artifacts : one JSON per (run, domain, step), the "defense trail"
- run files : summary.csv, decision_report.md, <domain>/06_email.txt
- cache     : Hunter results (forever) and scrape results (a few days), shared by everyone
- events    : sends, runs started, web requests, used for the daily caps and rate limits
"""

from __future__ import annotations

import json
import os
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from outbound import PROJECT_ROOT
from outbound.config import Settings, get_settings


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _start_of_day() -> datetime:
    return _now().replace(hour=0, minute=0, second=0, microsecond=0)


class Store(ABC):
    """The interface every store implements."""

    # artifacts
    @abstractmethod
    def save_artifact(self, run_id: str, domain: str, step: str, data: dict) -> None: ...
    @abstractmethod
    def load_artifact(self, run_id: str, domain: str, step: str) -> dict | None: ...
    @abstractmethod
    def load_run(self, run_id: str) -> dict[str, dict[str, dict]]:
        """{domain: {step: artifact}} for a whole run."""
    @abstractmethod
    def list_runs(self, limit: int = 20) -> list[str]:
        """Most recent run ids first."""

    # run files
    @abstractmethod
    def save_run_file(self, run_id: str, name: str, text: str) -> None: ...
    @abstractmethod
    def load_run_file(self, run_id: str, name: str) -> str | None: ...

    # cache
    @abstractmethod
    def get_cache(self, kind: str, key: str, max_age_days: float | None = None) -> dict | None: ...
    @abstractmethod
    def set_cache(self, kind: str, key: str, data: dict) -> None: ...

    # events (caps, rate limits, send log)
    @abstractmethod
    def log_event(self, kind: str, run_id: str = "", domain: str = "", ip: str = "", status: str = "") -> None: ...
    @abstractmethod
    def events(self, kind: str, since: datetime) -> list[dict]: ...

    # ---------- shared helpers built on the methods above
    def count_events(self, kind: str, since: datetime | None = None, ip: str | None = None, status: str | None = None) -> int:
        rows = self.events(kind, since or _start_of_day())
        return sum(1 for r in rows if (ip is None or r["ip"] == ip) and (status is None or r["status"] == status))

    def sent_today(self) -> int:
        return self.count_events("send", status="sent")

    def sent_in_run(self, run_id: str) -> list[str]:
        """Domains already emailed in this run (to never send twice)."""
        rows = self.events("send", _now() - timedelta(days=30))
        return [r["domain"] for r in rows if r["run_id"] == run_id and r["status"] == "sent"]


# ---------------------------------------------------------------- memory (tests)
class MemoryStore(Store):
    def __init__(self) -> None:
        self.artifacts: dict[tuple[str, str, str], dict] = {}
        self.files: dict[tuple[str, str], str] = {}
        self.cache: dict[tuple[str, str], tuple[datetime, dict]] = {}
        self.event_rows: list[dict] = []
        self.run_order: list[str] = []

    def save_artifact(self, run_id, domain, step, data):
        if run_id not in self.run_order:
            self.run_order.append(run_id)
        self.artifacts[(run_id, domain, step)] = json.loads(json.dumps(data, default=str))

    def load_artifact(self, run_id, domain, step):
        return self.artifacts.get((run_id, domain, step))

    def load_run(self, run_id):
        out: dict[str, dict[str, dict]] = {}
        for (r, d, s), data in self.artifacts.items():
            if r == run_id:
                out.setdefault(d, {})[s] = data
        return out

    def list_runs(self, limit=20):
        return list(reversed(self.run_order))[:limit]

    def save_run_file(self, run_id, name, text):
        self.files[(run_id, name)] = text

    def load_run_file(self, run_id, name):
        return self.files.get((run_id, name))

    def get_cache(self, kind, key, max_age_days=None):
        hit = self.cache.get((kind, key))
        if not hit or (max_age_days is not None and _now() - hit[0] > timedelta(days=max_age_days)):
            return None
        return hit[1]

    def set_cache(self, kind, key, data):
        self.cache[(kind, key)] = (_now(), json.loads(json.dumps(data, default=str)))

    def log_event(self, kind, run_id="", domain="", ip="", status=""):
        self.event_rows.append({"kind": kind, "run_id": run_id, "domain": domain, "ip": ip, "status": status, "at": _now()})

    def events(self, kind, since):
        return [r for r in self.event_rows if r["kind"] == kind and r["at"] >= since]


# ---------------------------------------------------------------- files (local default)
def _safe(name: str) -> str:
    """Make a string safe to use as a file/folder name."""
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in name)


class FileStore(Store):
    def __init__(self, runs_dir: Path | None = None, cache_dir: Path | None = None) -> None:
        self.runs_dir = Path(runs_dir or PROJECT_ROOT / "runs")
        self.cache_dir = Path(cache_dir or PROJECT_ROOT / ".cache")

    def _artifact_path(self, run_id, domain, step) -> Path:
        return self.runs_dir / _safe(run_id) / _safe(domain) / f"{_safe(step)}.json"

    def save_artifact(self, run_id, domain, step, data):
        path = self._artifact_path(run_id, domain, step)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str), encoding="utf-8")

    def load_artifact(self, run_id, domain, step):
        path = self._artifact_path(run_id, domain, step)
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

    def load_run(self, run_id):
        out: dict[str, dict[str, dict]] = {}
        run_dir = self.runs_dir / _safe(run_id)
        if not run_dir.exists():
            return out
        for domain_dir in sorted(p for p in run_dir.iterdir() if p.is_dir()):
            for f in sorted(domain_dir.glob("*.json")):
                out.setdefault(domain_dir.name, {})[f.stem] = json.loads(f.read_text(encoding="utf-8"))
        return out

    def list_runs(self, limit=20):
        if not self.runs_dir.exists():
            return []
        return sorted((p.name for p in self.runs_dir.iterdir() if p.is_dir()), reverse=True)[:limit]

    def save_run_file(self, run_id, name, text):
        path = self.runs_dir / _safe(run_id) / Path(*[_safe(p) for p in name.split("/")])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def load_run_file(self, run_id, name):
        path = self.runs_dir / _safe(run_id) / Path(*[_safe(p) for p in name.split("/")])
        return path.read_text(encoding="utf-8") if path.exists() else None

    def _cache_path(self, kind, key) -> Path:
        return self.cache_dir / _safe(kind) / f"{_safe(key)}.json"

    def get_cache(self, kind, key, max_age_days=None):
        path = self._cache_path(kind, key)
        if not path.exists():
            return None
        if max_age_days is not None:
            age = _now() - datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
            if age > timedelta(days=max_age_days):
                return None
        return json.loads(path.read_text(encoding="utf-8"))

    def set_cache(self, kind, key, data):
        path = self._cache_path(kind, key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str), encoding="utf-8")

    def log_event(self, kind, run_id="", domain="", ip="", status=""):
        path = self.cache_dir / "events.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        row = {"kind": kind, "run_id": run_id, "domain": domain, "ip": ip, "status": status, "at": _now().isoformat()}
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")

    def events(self, kind, since):
        path = self.cache_dir / "events.jsonl"
        if not path.exists():
            return []
        rows = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            row["at"] = datetime.fromisoformat(row["at"])
            if row["kind"] == kind and row["at"] >= since:
                rows.append(row)
        return rows


# ---------------------------------------------------------------- Neon Postgres
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS artifacts (
    run_id TEXT NOT NULL, domain TEXT NOT NULL, step TEXT NOT NULL,
    data JSONB NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, domain, step));
CREATE TABLE IF NOT EXISTS run_files (
    run_id TEXT NOT NULL, name TEXT NOT NULL, content TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(), PRIMARY KEY (run_id, name));
CREATE TABLE IF NOT EXISTS cache (
    kind TEXT NOT NULL, key TEXT NOT NULL, data JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(), PRIMARY KEY (kind, key));
CREATE TABLE IF NOT EXISTS events (
    id BIGSERIAL PRIMARY KEY, kind TEXT NOT NULL, run_id TEXT NOT NULL DEFAULT '',
    domain TEXT NOT NULL DEFAULT '', ip TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT '',
    at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE INDEX IF NOT EXISTS events_kind_at ON events (kind, at);
"""


class NeonStore(Store):
    """Postgres on Neon. Opens a short connection per call (best for serverless)."""

    _schema_ready = False

    def __init__(self, url: str) -> None:
        self.url = url

    def _conn(self):
        import psycopg

        conn = psycopg.connect(self.url, connect_timeout=10, autocommit=True)
        if not NeonStore._schema_ready:
            conn.execute(SCHEMA_SQL)
            NeonStore._schema_ready = True
        return conn

    @staticmethod
    def _json(data: Any):
        from psycopg.types.json import Jsonb

        return Jsonb(json.loads(json.dumps(data, default=str)))

    def save_artifact(self, run_id, domain, step, data):
        with self._conn() as c:
            c.execute(
                "INSERT INTO artifacts (run_id, domain, step, data) VALUES (%s,%s,%s,%s) "
                "ON CONFLICT (run_id, domain, step) DO UPDATE SET data = EXCLUDED.data, created_at = now()",
                (run_id, domain, step, self._json(data)),
            )

    def load_artifact(self, run_id, domain, step):
        with self._conn() as c:
            row = c.execute("SELECT data FROM artifacts WHERE run_id=%s AND domain=%s AND step=%s", (run_id, domain, step)).fetchone()
        return row[0] if row else None

    def load_run(self, run_id):
        with self._conn() as c:
            rows = c.execute("SELECT domain, step, data FROM artifacts WHERE run_id=%s ORDER BY created_at", (run_id,)).fetchall()
        out: dict[str, dict[str, dict]] = {}
        for domain, step, data in rows:
            out.setdefault(domain, {})[step] = data
        return out

    def list_runs(self, limit=20):
        with self._conn() as c:
            rows = c.execute(
                "SELECT run_id FROM artifacts GROUP BY run_id ORDER BY max(created_at) DESC LIMIT %s", (limit,)
            ).fetchall()
        return [r[0] for r in rows]

    def save_run_file(self, run_id, name, text):
        with self._conn() as c:
            c.execute(
                "INSERT INTO run_files (run_id, name, content) VALUES (%s,%s,%s) "
                "ON CONFLICT (run_id, name) DO UPDATE SET content = EXCLUDED.content, created_at = now()",
                (run_id, name, text),
            )

    def load_run_file(self, run_id, name):
        with self._conn() as c:
            row = c.execute("SELECT content FROM run_files WHERE run_id=%s AND name=%s", (run_id, name)).fetchone()
        return row[0] if row else None

    def get_cache(self, kind, key, max_age_days=None):
        with self._conn() as c:
            row = c.execute("SELECT data, created_at FROM cache WHERE kind=%s AND key=%s", (kind, key)).fetchone()
        if not row:
            return None
        if max_age_days is not None and _now() - row[1] > timedelta(days=max_age_days):
            return None
        return row[0]

    def set_cache(self, kind, key, data):
        with self._conn() as c:
            c.execute(
                "INSERT INTO cache (kind, key, data) VALUES (%s,%s,%s) "
                "ON CONFLICT (kind, key) DO UPDATE SET data = EXCLUDED.data, created_at = now()",
                (kind, key, self._json(data)),
            )

    def log_event(self, kind, run_id="", domain="", ip="", status=""):
        with self._conn() as c:
            c.execute(
                "INSERT INTO events (kind, run_id, domain, ip, status) VALUES (%s,%s,%s,%s,%s)",
                (kind, run_id, domain, ip, status),
            )

    def events(self, kind, since):
        with self._conn() as c:
            rows = c.execute(
                "SELECT kind, run_id, domain, ip, status, at FROM events WHERE kind=%s AND at >= %s", (kind, since)
            ).fetchall()
        return [dict(zip(("kind", "run_id", "domain", "ip", "status", "at"), r)) for r in rows]


# ---------------------------------------------------------------- choose a store
def get_store(settings: Settings | None = None) -> Store:
    """Neon if DATABASE_URL is set; otherwise files (in /tmp on Vercel, where the project folder is read-only)."""
    settings = settings or get_settings()
    url = settings.database_url.get_secret_value()
    if url:
        return NeonStore(url)
    if os.environ.get("VERCEL"):
        return FileStore(Path("/tmp/runs"), Path("/tmp/cache"))
    return FileStore()


def ping_database(settings: Settings) -> str:
    """Return 'ok', 'not configured', or a short error type. Never returns the connection string."""
    url = settings.database_url.get_secret_value()
    if not url:
        return "not configured"
    try:
        import psycopg

        with psycopg.connect(url, connect_timeout=5) as conn:
            conn.execute("SELECT 1")
        return "ok"
    except Exception as exc:  # report the kind of error only; the message could contain the host
        return f"error ({type(exc).__name__})"
