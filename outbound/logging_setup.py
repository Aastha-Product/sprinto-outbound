"""One function that sets up pretty, safe console logging (with `rich`)."""

from __future__ import annotations

import logging

from rich.logging import RichHandler


def setup_logging(level: int = logging.INFO) -> None:
    """Configure logging once. Also silences libraries that would log full request URLs."""
    logging.basicConfig(
        level=level,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(rich_tracebacks=True, show_path=False)],
        force=True,
    )
    # httpx logs every request URL at INFO level. Keep it quiet so nothing sensitive is ever printed.
    for noisy in ("httpx", "httpcore", "google_genai", "urllib3", "googleapiclient"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
