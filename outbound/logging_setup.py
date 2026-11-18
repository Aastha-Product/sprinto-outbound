"""One function that sets up pretty, safe console logging (with `rich`)."""

from __future__ import annotations

import logging
import sys

from rich.logging import RichHandler


def setup_logging(level: int = logging.INFO) -> None:
    """Configure logging once. Also silences libraries that would log full request URLs."""
    for stream in (sys.stdout, sys.stderr):  # Windows consoles: print ✅ / → without crashing
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    handler = RichHandler(rich_tracebacks=True, show_path=False)
    handler.addFilter(_QuietLibraries())
    logging.basicConfig(level=level, format="%(message)s", datefmt="[%X]", handlers=[handler], force=True)
    for noisy in NOISY_LOGGERS:
        logging.getLogger(noisy).setLevel(logging.WARNING)


# httpx logs every request URL at INFO level. Some libraries turn that back on when they load, so the
# handler itself also drops their below-WARNING messages: request URLs are never printed.
NOISY_LOGGERS = ("httpx", "httpx2", "httpcore", "httpcore2", "google_genai", "google.genai", "urllib3", "googleapiclient")


class _QuietLibraries(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        noisy = any(record.name == n or record.name.startswith(n + ".") for n in NOISY_LOGGERS)
        return not noisy or record.levelno >= logging.WARNING
