"""Tests for the MoodleConfig loader — fail-fast behavior + URL normalization."""

from __future__ import annotations

import pytest

from moodle_mcp.config import (
    INVALID_SCHEME_MESSAGE,
    MISSING_AUTH_MESSAGE,
    MISSING_URL_MESSAGE,
    ConfigError,
    MoodleConfig,
)


def _load(**env: str) -> MoodleConfig:
    """Load config without touching any on-disk .env file."""
    return MoodleConfig.load(_env_file=None, **env)


def test_missing_url_raises_exact_spec_message(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ConfigError) as exc_info:
        MoodleConfig.load(_env_file=None)
    assert str(exc_info.value) == MISSING_URL_MESSAGE


def test_whitespace_only_url_treated_as_missing() -> None:
    with pytest.raises(ConfigError) as exc_info:
        MoodleConfig.load(_env_file=None, url="   ")
    assert str(exc_info.value) == MISSING_URL_MESSAGE


def test_url_set_but_no_auth_raises() -> None:
    with pytest.raises(ConfigError) as exc_info:
        _load(url="https://moodle.example.com")
    assert str(exc_info.value) == MISSING_AUTH_MESSAGE


def test_direct_token_is_sufficient() -> None:
    cfg = _load(url="https://moodle.example.com", token="abc123")
    assert cfg.token == "abc123"
    assert cfg.has_direct_token is True


def test_user_and_password_is_sufficient() -> None:
    cfg = _load(
        url="https://moodle.example.com",
        username="alice",
        password="s3cret",
    )
    assert cfg.username == "alice"
    assert cfg.password == "s3cret"
    assert cfg.has_password_auth is True


def test_username_without_password_not_sufficient() -> None:
    with pytest.raises(ConfigError) as exc_info:
        _load(url="https://moodle.example.com", username="alice")
    assert str(exc_info.value) == MISSING_AUTH_MESSAGE


def test_trailing_slash_is_stripped() -> None:
    cfg = _load(url="https://moodle.example.com/", token="abc")
    assert cfg.url == "https://moodle.example.com"


def test_multiple_trailing_slashes_are_stripped() -> None:
    # rstrip('/') handles arbitrary runs of '/'.
    cfg = _load(url="https://moodle.example.com///", token="abc")
    assert cfg.url == "https://moodle.example.com"


def test_default_token_cache_path_under_home(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    # recompute via fresh load (Path.home() is read lazily via factory)
    cfg = _load(url="https://moodle.example.com", token="abc")
    assert str(cfg.token_cache).endswith("moodle-mcp/token.json")


def test_url_without_scheme_raises() -> None:
    with pytest.raises(ConfigError) as exc_info:
        _load(url="lms.lernen.hamburg", token="abc")
    assert str(exc_info.value) == INVALID_SCHEME_MESSAGE


def test_http_scheme_is_accepted() -> None:
    cfg = _load(url="http://localhost:8080", token="abc")
    assert cfg.url == "http://localhost:8080"


def test_env_vars_are_picked_up(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MOODLE_URL", "https://moodle.example.com")
    monkeypatch.setenv("MOODLE_TOKEN", "from-env")
    cfg = MoodleConfig.load(_env_file=None)
    assert cfg.url == "https://moodle.example.com"
    assert cfg.token == "from-env"


# ---------------------------------------------------------------- speed/security/open-notebook fields
def test_new_fields_have_sensible_defaults() -> None:
    cfg = _load(url="https://moodle.example.com", token="abc")
    assert cfg.max_concurrency == 6
    assert cfg.retry_max_attempts == 4
    assert cfg.retry_backoff_base == 0.5
    assert cfg.read_only is False
    assert cfg.open_notebook_url is None
    assert cfg.open_notebook_password is None
    assert cfg.has_open_notebook is False


def test_open_notebook_url_enables_has_open_notebook() -> None:
    cfg = _load(
        url="https://moodle.example.com",
        token="abc",
        open_notebook_url="https://notebook-api.example.dev",
    )
    assert cfg.has_open_notebook is True


def test_read_only_and_tuning_fields_settable_via_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MOODLE_URL", "https://moodle.example.com")
    monkeypatch.setenv("MOODLE_TOKEN", "abc")
    monkeypatch.setenv("MOODLE_READ_ONLY", "true")
    monkeypatch.setenv("MOODLE_MAX_CONCURRENCY", "2")
    monkeypatch.setenv("MOODLE_RETRY_MAX_ATTEMPTS", "1")
    monkeypatch.setenv("MOODLE_RETRY_BACKOFF_BASE", "0.1")
    cfg = MoodleConfig.load(_env_file=None)
    assert cfg.read_only is True
    assert cfg.max_concurrency == 2
    assert cfg.retry_max_attempts == 1
    assert cfg.retry_backoff_base == 0.1
