"""Tests for sentinel/ml/client.py and sentinel/ml/nonce.py."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from sentinel.config import Settings
from sentinel.ml.client import MlClient
from sentinel.ml.nonce import NonceStore
from sentinel.ml.types import MlResult

if TYPE_CHECKING:
    from pathlib import Path

_SETTINGS = Settings(printer_ip="10.0.0.1", printer_access_code="test")
_JPEG = b"\xff\xd8\xff\xd9"  # minimal JPEG


# ---------------------------------------------------------------------------
# NonceStore
# ---------------------------------------------------------------------------


def test_nonce_store_put_and_pop() -> None:
    store = NonceStore()
    nonce = store.put(_JPEG)
    assert store.pop(nonce) == _JPEG


def test_nonce_store_single_use() -> None:
    store = NonceStore()
    nonce = store.put(_JPEG)
    store.pop(nonce)
    assert store.pop(nonce) is None


def test_nonce_store_remove() -> None:
    store = NonceStore()
    nonce = store.put(_JPEG)
    store.remove(nonce)
    assert store.pop(nonce) is None


def test_nonce_store_missing_returns_none() -> None:
    store = NonceStore()
    assert store.pop("no-such-nonce") is None


def test_nonce_store_unique_nonces() -> None:
    store = NonceStore()
    n1 = store.put(_JPEG)
    n2 = store.put(_JPEG)
    assert n1 != n2


def test_nonce_store_edge_cases() -> None:
    # 1. Oldest eviction when max size (20) is reached
    store = NonceStore()
    nonces = []
    for i in range(21):
        nonces.append(store.put(f"jpeg-{i}".encode()))

    # The first one should have been evicted (nonces[0])
    assert store.get(nonces[0]) is None
    # The rest should be present
    assert store.get(nonces[1]) == b"jpeg-1"
    assert store.get(nonces[20]) == b"jpeg-20"

    # 2. TTL expiration for get and pop
    short_store = NonceStore(ttl=-1.0)  # expired immediately
    n_expired = short_store.put(b"expired")

    assert short_store.get(n_expired) is None
    assert short_store.pop(n_expired) is None

    # 3. Sweep deletes expired entries on next put
    sweep_store = NonceStore(ttl=-1.0)
    n1 = sweep_store.put(b"e1")
    assert n1 in sweep_store._store
    sweep_store.put(b"e2")
    # n1 should be swept away now
    assert n1 not in sweep_store._store


# ---------------------------------------------------------------------------
# MlClient — URL-fetch success (results list)
# ---------------------------------------------------------------------------


def _make_http_client(response_data: object, status_code: int = 200) -> MagicMock:
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    if status_code >= 400:
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "error", request=MagicMock(), response=MagicMock()
        )
    resp.json = MagicMock(return_value=response_data)

    client_mock = MagicMock()
    client_mock.get = AsyncMock(return_value=resp)
    client_mock.__aenter__ = AsyncMock(return_value=client_mock)
    client_mock.__aexit__ = AsyncMock(return_value=False)
    return client_mock


async def test_detect_results_list_format() -> None:
    client_mock = _make_http_client({"results": [{"score": 0.85}]})
    store = NonceStore()

    with patch("sentinel.ml.client.httpx.AsyncClient", return_value=client_mock):
        ml = MlClient(_SETTINGS, nonce_store=store)
        result = await ml.detect(_JPEG)

    assert result.score == pytest.approx(0.85)


async def test_detect_flat_score_format() -> None:
    client_mock = _make_http_client({"score": 0.42})
    store = NonceStore()

    with patch("sentinel.ml.client.httpx.AsyncClient", return_value=client_mock):
        ml = MlClient(_SETTINGS, nonce_store=store)
        result = await ml.detect(_JPEG)

    assert result.score == pytest.approx(0.42)


async def test_detect_empty_results_returns_fail_open() -> None:
    client_mock = _make_http_client({"results": []})
    store = NonceStore()

    with patch("sentinel.ml.client.httpx.AsyncClient", return_value=client_mock):
        ml = MlClient(_SETTINGS, nonce_store=store)
        result = await ml.detect(_JPEG)

    assert result.score == 0.0


# ---------------------------------------------------------------------------
# MlClient — fail-open scenarios
# ---------------------------------------------------------------------------


async def test_detect_http_5xx_fail_open() -> None:
    client_mock = _make_http_client({}, status_code=500)
    store = NonceStore()

    with patch("sentinel.ml.client.httpx.AsyncClient", return_value=client_mock):
        ml = MlClient(_SETTINGS, nonce_store=store)
        result = await ml.detect(_JPEG)

    assert result.score == 0.0


async def test_detect_connect_error_fail_open() -> None:
    client_mock = MagicMock()
    client_mock.__aenter__ = AsyncMock(side_effect=httpx.ConnectError("refused"))
    client_mock.__aexit__ = AsyncMock(return_value=False)
    store = NonceStore()

    with patch("sentinel.ml.client.httpx.AsyncClient", return_value=client_mock):
        ml = MlClient(_SETTINGS, nonce_store=store)
        result = await ml.detect(_JPEG)

    assert result.score == 0.0


async def test_detect_malformed_json_fail_open() -> None:
    client_mock = _make_http_client("not a dict")
    store = NonceStore()

    with patch("sentinel.ml.client.httpx.AsyncClient", return_value=client_mock):
        ml = MlClient(_SETTINGS, nonce_store=store)
        result = await ml.detect(_JPEG)

    assert result.score == 0.0


async def test_detect_timeout_fail_open() -> None:
    client_mock = MagicMock()
    client_mock.__aenter__ = AsyncMock(side_effect=httpx.TimeoutException("timeout"))
    client_mock.__aexit__ = AsyncMock(return_value=False)
    store = NonceStore()

    with patch("sentinel.ml.client.httpx.AsyncClient", return_value=client_mock):
        ml = MlClient(_SETTINGS, nonce_store=store)
        result = await ml.detect(_JPEG)

    assert result.score == 0.0


# ---------------------------------------------------------------------------
# Nonce cleanup — always removed even on error
# ---------------------------------------------------------------------------


async def test_nonce_cleaned_up_on_success() -> None:
    client_mock = _make_http_client({"score": 0.5})
    store = NonceStore()

    with patch("sentinel.ml.client.httpx.AsyncClient", return_value=client_mock):
        ml = MlClient(_SETTINGS, nonce_store=store)
        await ml.detect(_JPEG)

    assert len(store._store) == 0


async def test_nonce_cleaned_up_on_error() -> None:
    client_mock = MagicMock()
    client_mock.__aenter__ = AsyncMock(side_effect=httpx.ConnectError("refused"))
    client_mock.__aexit__ = AsyncMock(return_value=False)
    store = NonceStore()

    with patch("sentinel.ml.client.httpx.AsyncClient", return_value=client_mock):
        ml = MlClient(_SETTINGS, nonce_store=store)
        await ml.detect(_JPEG)

    assert len(store._store) == 0


# ---------------------------------------------------------------------------
# Token file reload
# ---------------------------------------------------------------------------


async def test_token_loaded_from_file(tmp_path: Path) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("my-secret-token")

    settings = Settings(printer_ip="10.0.0.1", printer_access_code="test", ml_api_token_file=str(token_file))
    ml = MlClient(settings)
    token = ml._load_token()
    assert token == "my-secret-token"


async def test_token_reloads_on_mtime_change(tmp_path: Path) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("token-v1")

    settings = Settings(printer_ip="10.0.0.1", printer_access_code="test", ml_api_token_file=str(token_file))
    ml = MlClient(settings)
    ml._load_token()
    assert ml._token == "token-v1"

    # Simulate mtime change by bumping it
    new_mtime = os.path.getmtime(token_file) + 1
    os.utime(token_file, (new_mtime, new_mtime))
    token_file.write_text("token-v2")
    # Force mtime update
    os.utime(token_file, (new_mtime, new_mtime))

    ml._load_token()
    assert ml._token == "token-v2"


def test_token_missing_file_returns_none() -> None:
    settings = Settings(printer_ip="10.0.0.1", printer_access_code="test", ml_api_token_file="/nonexistent/token")
    ml = MlClient(settings)
    assert ml._load_token() is None


def test_token_os_error_returns_none(tmp_path: Path) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("tok")
    settings = Settings(printer_ip="10.0.0.1", printer_access_code="test", ml_api_token_file=str(token_file))
    ml = MlClient(settings)
    with patch("sentinel.ml.client.os.path.getmtime", side_effect=OSError("perm")):
        result = ml._load_token()
    assert result is None


async def test_detect_with_token_sends_auth_header(
    tmp_path: Path,
) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("my-token")
    settings = Settings(
        printer_ip="10.0.0.1", printer_access_code="test",
        ml_api_token_file=str(token_file),
    )

    client_mock = _make_http_client({"score": 0.7})
    store = NonceStore()

    with patch("sentinel.ml.client.httpx.AsyncClient", return_value=client_mock):
        ml = MlClient(settings, nonce_store=store)
        await ml.detect(_JPEG)

    call_kwargs = client_mock.get.call_args
    headers_sent = call_kwargs.kwargs.get("headers", {})
    assert headers_sent.get("Authorization") == "Bearer my-token"


def test_parse_type_error_returns_fail_open() -> None:
    from sentinel.ml.client import MlClient as _C

    result = _C._parse({"results": [{"score": None}]})
    assert result.score == 0.0


# ---------------------------------------------------------------------------
# MlResult
# ---------------------------------------------------------------------------


def test_ml_result_score_zero() -> None:
    assert MlResult(score=0.0).score == 0.0


def test_ml_result_score_above_zero() -> None:
    assert MlResult(score=0.1).score > 0.0


async def test_ml_client_connection_reuse() -> None:
    client_mock = _make_http_client({"score": 0.5})
    store = NonceStore()

    with patch(
        "sentinel.ml.client.httpx.AsyncClient", return_value=client_mock
    ) as mock_async_client:
        ml = MlClient(_SETTINGS, nonce_store=store)

        # Verify the client constructor was called once
        mock_async_client.assert_called_once()

        # Make multiple detect calls
        res1 = await ml.detect(_JPEG)
        res2 = await ml.detect(_JPEG)

        assert res1.score == 0.5
        assert res2.score == 0.5

        # Verify get was called twice on the same client instance
        assert client_mock.get.call_count == 2

        # Verify close closes the persistent client
        client_mock.aclose = AsyncMock()
        await ml.close()
        client_mock.aclose.assert_called_once()


async def test_ml_callback_host_parameter() -> None:
    # 1. Test when ml_callback_host is set to a hostname
    settings = Settings(printer_ip="10.0.0.1", printer_access_code="test", ml_callback_host="custom-sentinel-host")
    client_mock = _make_http_client({"score": 0.1})
    store = NonceStore()
    with patch("sentinel.ml.client.httpx.AsyncClient", return_value=client_mock):
        ml = MlClient(settings, nonce_store=store)
        await ml.detect(_JPEG)

    call_kwargs = client_mock.get.call_args
    params_sent = call_kwargs.kwargs.get("params", {})
    img_url = params_sent.get("img", "")
    assert img_url.startswith("http://custom-sentinel-host:8000/__internal_snapshot/")

    # 2. Test when ml_callback_host is set to a full URL
    settings_url = Settings(
        printer_ip="10.0.0.1", printer_access_code="test", ml_callback_host="https://sentinel.example.com/subdir"
    )
    client_mock_url = _make_http_client({"score": 0.1})
    with patch("sentinel.ml.client.httpx.AsyncClient", return_value=client_mock_url):
        ml = MlClient(settings_url, nonce_store=store)
        await ml.detect(_JPEG)

    call_kwargs_url = client_mock_url.get.call_args
    params_sent_url = call_kwargs_url.kwargs.get("params", {})
    img_url_url = params_sent_url.get("img", "")
    assert img_url_url.startswith("https://sentinel.example.com/subdir/__internal_snapshot/")


def test_parse_obico_detections_format() -> None:
    from sentinel.ml.client import MlClient

    # 1. Empty detections list
    assert MlClient._parse({"detections": []}).score == 0.0

    # 2. Valid and invalid detections mixed
    data = {
        "detections": [
            ["spaghetti", "0.85", [10, 20, 30, 40]],
            ["other", 0.42, [5, 5, 5, 5]],
            ["malformed", "not-a-float"],
            ["short"],
            "not-a-list",
        ]
    }
    assert MlClient._parse(data).score == pytest.approx(0.85)
