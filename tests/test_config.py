"""Tests for sentinel/config.py."""

from __future__ import annotations

import pytest

from sentinel.config import Settings


def test_printer_ip_default() -> None:
    s = Settings()
    assert s.printer_ip == "127.0.0.1"


def test_printer_ip_custom() -> None:
    s = Settings(printer_ip="10.0.0.1")
    assert s.printer_ip == "10.0.0.1"


def test_printer_access_code_default() -> None:
    s = Settings()
    assert s.printer_access_code == "123456"


def test_printer_mqtt_port_default() -> None:
    s = Settings()
    assert s.printer_mqtt_port == 1883


def test_bind_port_default() -> None:
    s = Settings()
    assert s.bind_port == 8000


def test_bind_host_default() -> None:
    s = Settings()
    assert s.bind_host == "0.0.0.0"


def test_log_level_default() -> None:
    s = Settings()
    assert s.log_level == "INFO"


def test_log_level_normalised_to_upper() -> None:
    s = Settings(log_level="debug")
    assert s.log_level == "DEBUG"


def test_log_level_invalid() -> None:
    with pytest.raises(ValueError):
        Settings(log_level="VERBOSE")


def test_telegram_disabled_by_default() -> None:
    assert not Settings().telegram_enabled


def test_telegram_enabled_all_fields() -> None:
    s = Settings(
        telegram_bot_token="tok",
        telegram_chat_id="cid",
        telegram_user_ids="uid",
    )
    assert s.telegram_enabled


def test_telegram_enabled_token_only() -> None:
    s = Settings(telegram_bot_token="tok")
    assert s.telegram_enabled


def test_ntfy_disabled_by_default() -> None:
    assert not Settings().ntfy_enabled


def test_ntfy_enabled() -> None:
    s = Settings(ntfy_url="https://ntfy.sh/topic")
    assert s.ntfy_enabled


def test_auth_disabled_by_default() -> None:
    assert not Settings().auth_enabled


def test_auth_enabled() -> None:
    s = Settings(auth_username="admin")
    assert s.auth_enabled


def test_external_bind_allowed_default() -> None:
    assert not Settings().external_bind_allowed


def test_ml_defaults() -> None:
    s = Settings()
    assert s.ml_api_url == "http://obico-ml:3333"
    assert s.ml_confirm_count == 3
    assert s.ml_score_threshold == 0.4


def test_detection_warmup_default() -> None:
    s = Settings()
    assert s.detection_warmup_seconds == 300


def test_db_path_default() -> None:
    s = Settings()
    assert s.db_path == "/data/sentinel.db"
