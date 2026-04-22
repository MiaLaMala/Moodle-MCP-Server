"""Shared pytest fixtures.

Ensure tests never accidentally pick up the developer's real MOODLE_* env
vars or the repo's .env file.
"""

from __future__ import annotations

import pytest


_MOODLE_ENV_KEYS = (
    "MOODLE_URL",
    "MOODLE_USERNAME",
    "MOODLE_PASSWORD",
    "MOODLE_TOKEN",
    "MOODLE_TOKEN_CACHE",
    "MOODLE_TIMEOUT",
)


@pytest.fixture(autouse=True)
def _isolate_moodle_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in _MOODLE_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
