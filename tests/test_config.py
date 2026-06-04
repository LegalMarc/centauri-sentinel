"""Tests for sentinel/config.py."""

from __future__ import annotations

import pytest

from sentinel.config import Settings


def test_printer_ip_default() -> None:
    s = Settings()
    assert s.printer_ip == "192.168.1.10"


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


def test_telegram_disabled_when_empty_string() -> None:
    assert not Settings(telegram_bot_token="").telegram_enabled


def test_telegram_enabled_all_fields() -> None:
    s = Settings(
        telegram_bot_token="tok",
        telegram_chat_id="cid",
        telegram_user_ids="uid",
    )
    assert s.telegram_enabled


def test_telegram_enabled_token_only() -> None:
    with pytest.raises(ValueError):
        Settings(telegram_bot_token="tok")


def test_ntfy_disabled_by_default() -> None:
    assert not Settings().ntfy_enabled


def test_ntfy_disabled_when_empty_string() -> None:
    assert not Settings(ntfy_url="").ntfy_enabled


def test_ntfy_enabled() -> None:
    s = Settings(ntfy_url="https://ntfy.sh/topic")
    assert s.ntfy_enabled


def test_auth_disabled_by_default() -> None:
    assert not Settings().auth_enabled


def test_auth_enabled() -> None:
    s = Settings(auth_username="admin", auth_password="pw")
    assert s.auth_enabled


def test_auth_disabled_when_username_empty_string() -> None:
    s = Settings(auth_username="", auth_password="")
    assert not s.auth_enabled


def test_auth_plain_password_is_hashed() -> None:
    import bcrypt

    s = Settings(auth_username="admin", auth_password="secret")
    assert s.auth_password is None  # cleared after hashing
    assert s.auth_password_bcrypt is not None
    assert s.auth_password_bcrypt.startswith("$2b$")
    assert bcrypt.checkpw(b"secret", s.auth_password_bcrypt.encode())


def test_auth_password_cleared_from_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTH_PASSWORD", "should-be-cleared")
    import os

    Settings(auth_username="admin", auth_password="should-be-cleared")
    assert os.environ.get("AUTH_PASSWORD") is None


def test_auth_plain_password_does_not_override_existing_bcrypt() -> None:
    import bcrypt

    existing_hash = bcrypt.hashpw(b"original", bcrypt.gensalt()).decode()
    s = Settings(
        auth_username="admin",
        auth_password="ignored",
        auth_password_bcrypt=existing_hash,
    )
    assert s.auth_password_bcrypt == existing_hash


def test_external_bind_allowed_default() -> None:
    assert not Settings().external_bind_allowed


def test_ml_defaults() -> None:
    s = Settings()
    assert s.ml_api_url == "http://obico-ml:3333"
    assert s.ml_api_token_file == "/shared/token"
    assert s.ml_confirm_count == 3
    assert s.ml_score_threshold == 0.4


def test_detection_warmup_default() -> None:
    s = Settings()
    assert s.detection_warmup_seconds == 300


def test_db_path_default() -> None:
    s = Settings()
    assert s.db_path == "/data/sentinel.db"


# ---------------------------------------------------------------------------
# Validation tests
# ---------------------------------------------------------------------------


def test_telegram_validation_missing_chat_id() -> None:
    with pytest.raises(ValueError, match="TELEGRAM_CHAT_ID"):
        Settings(telegram_bot_token="tok", telegram_user_ids="123")


def test_telegram_validation_missing_user_ids() -> None:
    with pytest.raises(ValueError, match="TELEGRAM_USER_IDS"):
        Settings(telegram_bot_token="tok", telegram_chat_id="123")


def test_auth_validation_missing_password() -> None:
    with pytest.raises(ValueError, match="AUTH_PASSWORD"):
        Settings(auth_username="admin")


def test_auth_validation_invalid_bcrypt_hash() -> None:
    with pytest.raises(ValueError, match="AUTH_PASSWORD_BCRYPT"):
        Settings(auth_username="admin", auth_password_bcrypt="not_bcrypt_hash")


def test_printer_ip_validation_valid_ip() -> None:
    s = Settings(printer_ip="192.168.1.10")
    assert s.printer_ip == "192.168.1.10"


def test_printer_ip_validation_valid_hostname(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sentinel.network.socket.gethostbyname", lambda x: "192.168.1.50")
    s = Settings(printer_ip="printer.local")
    assert s.printer_ip == "192.168.1.50"


def test_printer_ip_validation_ssrf_disallowed() -> None:
    with pytest.raises(ValueError, match="SSRF Protection"):
        Settings(printer_ip="127.0.0.1")
    with pytest.raises(ValueError, match="SSRF Protection"):
        Settings(printer_ip="::1")
    with pytest.raises(ValueError, match="SSRF Protection"):
        Settings(printer_ip="localhost")
    with pytest.raises(ValueError, match="SSRF Protection"):
        Settings(printer_ip="localhost.localdomain")
    with pytest.raises(ValueError, match="SSRF Protection"):
        Settings(printer_ip="8.8.8.8")
    with pytest.raises(ValueError, match="SSRF Protection"):
        Settings(printer_ip="169.254.169.254")
    with pytest.raises(ValueError, match="SSRF Protection"):
        Settings(printer_ip="0.0.0.0")
    with pytest.raises(ValueError, match="SSRF Protection"):
        Settings(printer_ip="::")
    with pytest.raises(ValueError, match="SSRF Protection"):
        Settings(printer_ip="::ffff:127.0.0.1")
    with pytest.raises(ValueError, match="SSRF Protection"):
        Settings(printer_ip="::ffff:8.8.8.8")


def test_printer_ip_validation_invalid() -> None:
    with pytest.raises(ValueError, match="printer_ip must be a valid IP"):
        Settings(printer_ip="invalid_ip_or_hostname_!!")

def test_printer_ip_validation_hostname_resolves_to_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sentinel.network.socket.gethostbyname", lambda x: "127.0.0.1")
    with pytest.raises(ValueError, match="SSRF Protection"):
        Settings(printer_ip="malicious.com")

def test_printer_ip_validation_hostname_resolution_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    import socket
    def mock_gethostbyname(x: str) -> str:
        raise socket.gaierror("not found")
    monkeypatch.setattr("sentinel.network.socket.gethostbyname", mock_gethostbyname)
    with pytest.raises(ValueError, match="SSRF Protection: Cannot resolve hostname"):
        Settings(printer_ip="notfound.local")


# ---------------------------------------------------------------------------
# ML parameter validation tests
# ---------------------------------------------------------------------------


def test_ml_score_threshold_valid_range() -> None:
    s = Settings(ml_score_threshold=0.0)
    assert s.ml_score_threshold == 0.0

    s = Settings(ml_score_threshold=1.0)
    assert s.ml_score_threshold == 1.0

    s = Settings(ml_score_threshold=0.5)
    assert s.ml_score_threshold == 0.5


def test_ml_score_threshold_too_high() -> None:
    with pytest.raises(ValueError, match="ML_SCORE_THRESHOLD must be between"):
        Settings(ml_score_threshold=1.1)


def test_ml_score_threshold_negative() -> None:
    with pytest.raises(ValueError, match="ML_SCORE_THRESHOLD must be between"):
        Settings(ml_score_threshold=-0.1)


def test_ml_confirm_count_valid() -> None:
    s = Settings(ml_confirm_count=1)
    assert s.ml_confirm_count == 1

    s = Settings(ml_confirm_count=10)
    assert s.ml_confirm_count == 10


def test_ml_confirm_count_zero() -> None:
    with pytest.raises(ValueError, match="ML_CONFIRM_COUNT must be at least 1"):
        Settings(ml_confirm_count=0)


def test_ml_confirm_count_negative() -> None:
    with pytest.raises(ValueError, match="ML_CONFIRM_COUNT must be at least 1"):
        Settings(ml_confirm_count=-1)


def test_ml_poll_interval_valid() -> None:
    s = Settings(ml_poll_interval_seconds=1)
    assert s.ml_poll_interval_seconds == 1


def test_ml_poll_interval_zero() -> None:
    with pytest.raises(ValueError, match="ML_POLL_INTERVAL_SECONDS must be at least 1"):
        Settings(ml_poll_interval_seconds=0)


def test_resume_cooldown_seconds_valid() -> None:
    s = Settings(resume_cooldown_seconds=0)
    assert s.resume_cooldown_seconds == 0
    s = Settings(resume_cooldown_seconds=10)
    assert s.resume_cooldown_seconds == 10


def test_resume_cooldown_seconds_invalid() -> None:
    with pytest.raises(ValueError, match="RESUME_COOLDOWN_SECONDS must be at least 0"):
        Settings(resume_cooldown_seconds=-1)


