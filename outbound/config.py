"""Loads settings from `.env` and checks that the keys each feature needs are present.

Plain-English summary:
- `get_settings()` reads `.env` once and returns a `Settings` object.
- Secrets are stored as `SecretStr`, so printing the settings shows `**********` instead of the key.
- Keys are checked *lazily*: `require("gemini")` only complains about Gemini keys, so you can
  run the scraping steps before you have a Hunter or Gmail key.
"""

from __future__ import annotations

import re
from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from outbound import PROJECT_ROOT

SETUP_GUIDE = "docs/SETUP_WINDOWS.md"
PLACEHOLDER_EMAIL = "you@gmail.com"
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class ConfigError(RuntimeError):
    """Raised when a required setting is missing. The message tells the user how to fix it."""


class Settings(BaseSettings):
    """All configuration for the pipeline. Field names match the keys in `.env` (case-insensitive)."""

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        env_ignore_empty=True,  # "KEY=" with nothing after it counts as "not set"
    )

    # Owner / sender
    owner_email: str = ""
    sender_name: str = "Anshull Mishra"
    sender_title: str = "Growth at Sprinto"

    # Gemini
    gemini_api_key: SecretStr = SecretStr("")
    gemini_model: str = ""
    use_search_grounding: bool = True

    # Hunter
    hunter_api_key: SecretStr = SecretStr("")
    hunter_min_remaining: int = 3

    # Gmail
    gmail_credentials_file: str = "credentials.json"
    gmail_token_file: str = "token.json"
    send_enabled: bool = False
    max_emails_per_run: int = 3
    gmail_client_id: str = ""  # Vercel only (no token.json there)
    gmail_client_secret: SecretStr = SecretStr("")
    gmail_refresh_token: SecretStr = SecretStr("")

    # Neon Postgres (empty = store results as files in runs/ and .cache/)
    database_url: SecretStr = SecretStr("")

    # Public website limits (all visitors together, counted in Neon)
    max_emails_per_day: int = 5
    max_runs_per_day: int = 20

    # Pipeline limits
    max_pages_per_domain: int = 6
    max_chars_per_page: int = 8000
    max_agent_iterations: int = 3
    max_llm_calls_per_domain: int = 12
    request_timeout_seconds: float = 15
    user_agent: str = "MESA-Student-Research-Bot/0.1 (educational project)"

    # ---------- helpers ----------
    def owner_email_ok(self) -> bool:
        """True if OWNER_EMAIL looks like a real address (not empty, not the placeholder)."""
        e = self.owner_email.strip()
        return bool(e) and e.lower() != PLACEHOLDER_EMAIL and bool(EMAIL_RE.match(e))

    def missing_for(self, feature: str) -> list[str]:
        """Return the `.env` keys that are missing for one feature (empty list = all good)."""
        if feature not in FEATURE_KEYS:
            raise ValueError(f"Unknown feature '{feature}'. Known: {sorted(FEATURE_KEYS)}")
        missing = []
        for key in FEATURE_KEYS[feature]:
            if key == "owner_email":
                if not self.owner_email_ok():
                    missing.append("OWNER_EMAIL")
                continue
            value = getattr(self, key)
            if isinstance(value, SecretStr):
                value = value.get_secret_value()
            if not str(value).strip():
                missing.append(key.upper())
        return missing

    def require(self, feature: str) -> None:
        """Raise a friendly `ConfigError` if any key needed by `feature` is missing."""
        missing = self.missing_for(feature)
        if missing:
            raise ConfigError(
                f"Missing setting(s) for {feature}: {', '.join(missing)}.\n"
                f"Open the .env file in the project folder and fill them in. "
                f"Step-by-step help: {SETUP_GUIDE}"
            )


# Which keys each feature needs. Checked only when that feature is used ("lazy validation").
FEATURE_KEYS: dict[str, list[str]] = {
    "gemini_key": ["gemini_api_key"],  # enough to list models
    "gemini": ["gemini_api_key", "gemini_model"],  # enough to call a model
    "hunter": ["hunter_api_key"],
    "gmail": ["owner_email", "sender_name", "sender_title"],
    "database": ["database_url"],
}


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Read `.env` once and reuse the result everywhere."""
    return Settings()
