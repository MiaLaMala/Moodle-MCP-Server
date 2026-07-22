"""Tests for :mod:`moodle_mcp.moodle_client` retry/backoff behavior.

Uses ``httpx.MockTransport`` to simulate transient failures (network errors,
429/5xx) without any real network access — safe to run while the real
Moodle instance is offline for maintenance.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from moodle_mcp.config import MoodleConfig
from moodle_mcp.moodle_client import MoodleAPIError, MoodleClient


def _config(**overrides) -> MoodleConfig:
    return MoodleConfig.load(
        _env_file=None,
        url="https://moodle.example.com",
        token="test-token",
        retry_max_attempts=overrides.pop("retry_max_attempts", 3),
        retry_backoff_base=overrides.pop("retry_backoff_base", 0.001),
        **overrides,
    )


def _client_with_transport(config: MoodleConfig, transport: httpx.MockTransport) -> MoodleClient:
    client = MoodleClient(config)
    client._http = httpx.AsyncClient(transport=transport, headers={"User-Agent": "test"})
    return client


@pytest.mark.asyncio
async def test_ws_call_succeeds_immediately_on_200() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    client = _client_with_transport(_config(), httpx.MockTransport(handler))
    result = await client._ws_call("some_function")
    assert result == {"ok": True}


@pytest.mark.asyncio
async def test_ws_call_retries_transient_5xx_then_succeeds() -> None:
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        if calls["count"] < 3:
            return httpx.Response(503, text="Service Unavailable")
        return httpx.Response(200, json={"ok": True})

    client = _client_with_transport(
        _config(retry_max_attempts=4), httpx.MockTransport(handler)
    )
    result = await client._ws_call("some_function")
    assert result == {"ok": True}
    assert calls["count"] == 3


@pytest.mark.asyncio
async def test_ws_call_retries_429_then_succeeds() -> None:
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        if calls["count"] < 2:
            return httpx.Response(429, text="Too Many Requests")
        return httpx.Response(200, json={"ok": True})

    client = _client_with_transport(
        _config(retry_max_attempts=4), httpx.MockTransport(handler)
    )
    result = await client._ws_call("some_function")
    assert result == {"ok": True}
    assert calls["count"] == 2


@pytest.mark.asyncio
async def test_ws_call_exhausts_retries_and_raises() -> None:
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return httpx.Response(500, text="Internal Server Error")

    client = _client_with_transport(
        _config(retry_max_attempts=3), httpx.MockTransport(handler)
    )
    # Exhausting transient-status retries returns the last (still 5xx)
    # response rather than raising from _post_with_retry -- the 5xx body
    # is not valid JSON, so _ws_call itself raises MoodleAPIError.
    with pytest.raises(MoodleAPIError):
        await client._ws_call("some_function")
    assert calls["count"] == 3


@pytest.mark.asyncio
async def test_ws_call_network_error_retries_then_raises_moodle_api_error() -> None:
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        raise httpx.ConnectError("boom", request=request)

    client = _client_with_transport(
        _config(retry_max_attempts=3), httpx.MockTransport(handler)
    )
    with pytest.raises(MoodleAPIError):
        await client._ws_call("some_function")
    assert calls["count"] == 3


@pytest.mark.asyncio
async def test_ws_call_network_error_recovers_within_retry_budget() -> None:
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        if calls["count"] < 2:
            raise httpx.ConnectError("boom", request=request)
        return httpx.Response(200, json={"ok": True})

    client = _client_with_transport(
        _config(retry_max_attempts=3), httpx.MockTransport(handler)
    )
    result = await client._ws_call("some_function")
    assert result == {"ok": True}
    assert calls["count"] == 2


@pytest.mark.asyncio
async def test_ws_call_non_transient_404_is_not_retried() -> None:
    """404 is not in the transient-status set — no retry, immediate result."""
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return httpx.Response(404, text="not found")

    client = _client_with_transport(
        _config(retry_max_attempts=5), httpx.MockTransport(handler)
    )
    with pytest.raises(MoodleAPIError):
        await client._ws_call("some_function")
    assert calls["count"] == 1


@pytest.mark.asyncio
async def test_ws_call_401_triggers_reauth_not_transient_retry(tmp_path: Path) -> None:
    """401/403 handling is a separate single-shot reauth path, independent
    of the transient-retry loop -- must not consume retry attempts."""
    ws_calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/login/token.php"):
            return httpx.Response(200, json={"token": "fresh-token"})
        ws_calls["count"] += 1
        if ws_calls["count"] == 1:
            return httpx.Response(401, text="unauthorized")
        return httpx.Response(200, json={"ok": True})

    config = MoodleConfig.load(
        _env_file=None,
        url="https://moodle.example.com",
        username="alice",
        password="s3cret",
        token_cache=tmp_path / "token.json",
        retry_max_attempts=2,
        retry_backoff_base=0.001,
    )
    client = _client_with_transport(config, httpx.MockTransport(handler))
    result = await client._ws_call("some_function")
    assert result == {"ok": True}
    assert ws_calls["count"] == 2


@pytest.mark.asyncio
async def test_download_file_retries_transient_failure_then_succeeds(tmp_path: Path) -> None:
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        if calls["count"] < 2:
            return httpx.Response(503, text="unavailable")
        return httpx.Response(200, content=b"filedata")

    client = _client_with_transport(
        _config(retry_max_attempts=3), httpx.MockTransport(handler)
    )
    dest = tmp_path / "out.bin"
    written = await client.download_file("https://moodle.example.com/pluginfile.php/1/x", dest)
    assert written == len(b"filedata")
    assert dest.read_bytes() == b"filedata"
    assert calls["count"] == 2


@pytest.mark.asyncio
async def test_download_file_exhausts_retries_and_raises(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="fail")

    client = _client_with_transport(
        _config(retry_max_attempts=2), httpx.MockTransport(handler)
    )
    dest = tmp_path / "out.bin"
    with pytest.raises(MoodleAPIError):
        await client.download_file("https://moodle.example.com/pluginfile.php/1/x", dest)


@pytest.mark.asyncio
async def test_download_file_non_transient_404_raises_immediately(tmp_path: Path) -> None:
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return httpx.Response(404, text="not found")

    client = _client_with_transport(
        _config(retry_max_attempts=5), httpx.MockTransport(handler)
    )
    dest = tmp_path / "out.bin"
    with pytest.raises(MoodleAPIError):
        await client.download_file("https://moodle.example.com/pluginfile.php/1/x", dest)
    assert calls["count"] == 1
