"""Configuration loader for the Moodle MCP server.

Loads credentials from environment variables (optionally via `.env`) and
fails fast with human-readable messages when required fields are missing —
as specified in the deep-interview spec.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


MISSING_URL_MESSAGE = "Setze die URL für deine Moodle Platform"
MISSING_AUTH_MESSAGE = (
    "Moodle-Authentifizierung fehlt. Setze entweder MOODLE_TOKEN "
    "oder MOODLE_USERNAME + MOODLE_PASSWORD in deiner .env."
)
INVALID_SCHEME_MESSAGE = (
    "MOODLE_URL muss mit http:// oder https:// beginnen "
    "(Beispiel: https://lms.lernen.hamburg)."
)


class ConfigError(RuntimeError):
    """Raised when the Moodle MCP server cannot start due to bad/missing config."""


def _default_cache_path() -> Path:
    return Path.home() / ".cache" / "moodle-mcp" / "token.json"


def _default_download_root() -> Path:
    return Path.home() / "Documents"


def _default_submissions_log() -> Path:
    return Path.home() / ".moodle-mcp" / "submissions.log"


class MoodleConfig(BaseSettings):
    """Runtime configuration.

    Prefer :meth:`load` over calling the constructor directly — it normalizes
    the URL and raises :class:`ConfigError` with spec-mandated messages when
    required fields are missing.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="MOODLE_",
        extra="ignore",
        case_sensitive=False,
    )

    url: Optional[str] = Field(default=None)
    username: Optional[str] = Field(default=None)
    password: Optional[str] = Field(default=None)
    token: Optional[str] = Field(default=None)
    token_cache: Path = Field(default_factory=_default_cache_path)
    timeout: float = Field(default=30.0)
    download_root: Path = Field(default_factory=_default_download_root)
    submissions_log: Path = Field(default_factory=_default_submissions_log)

    @property
    def has_direct_token(self) -> bool:
        return bool(self.token)

    @property
    def has_password_auth(self) -> bool:
        return bool(self.username) and bool(self.password)

    @classmethod
    def load(cls, **overrides) -> "MoodleConfig":
        """Load config from env / .env, normalize, and validate.

        Raises:
            ConfigError: If MOODLE_URL or auth credentials are missing.
        """
        cfg = cls(**overrides)

        if not cfg.url or not cfg.url.strip():
            raise ConfigError(MISSING_URL_MESSAGE)

        # Normalize: strip trailing slash, whitespace.
        cfg.url = cfg.url.strip().rstrip("/")

        if not (cfg.url.startswith("http://") or cfg.url.startswith("https://")):
            raise ConfigError(INVALID_SCHEME_MESSAGE)

        if not cfg.has_direct_token and not cfg.has_password_auth:
            raise ConfigError(MISSING_AUTH_MESSAGE)

        return cfg
