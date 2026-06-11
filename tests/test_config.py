"""Tests for sentinel/config.py."""

from __future__ import annotations

import asyncio
from pathlib import Path

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
    assert s.printer_access_code.get_secret_value() == "123456"


def test_printer_mqtt_port_default() -> None:
    s = Settings()
    assert s.printer_mqtt_port == 1883


def test_bind_port_default() -> None:
    s = Settings()
    assert s.bind_port == 8000


def test_bind_host_default() -> None:
    s = Settings()
    assert s.bind_host == "127.0.0.1"


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
    s = Settings(auth_username="admin", auth_password_bcrypt="$2b$12$dummyhash")
    assert s.auth_enabled


def test_auth_disabled_when_username_empty_string() -> None:
    s = Settings(auth_username="", auth_password="")
    assert not s.auth_enabled


def test_auth_plain_password_raises_error() -> None:
    with pytest.raises(ValueError, match="Plain-text AUTH_PASSWORD is no longer supported"):
        Settings(auth_username="admin", auth_password="secret")


def test_external_bind_allowed_default() -> None:
    assert not Settings().external_bind_allowed


def test_ml_defaults() -> None:
    s = Settings()
    assert s.ml_api_url == "http://obico-ml:3333"
    assert s.ml_api_token_file == "shared/token"
    assert s.ml_confirm_count == 3
    assert s.ml_score_threshold == 0.4


def test_detection_warmup_default() -> None:
    s = Settings()
    assert s.detection_warmup_seconds == 300


def test_auto_stop_timeout_default_is_zero() -> None:
    """Auto-stop must default to 0 (disabled) — opt-in only."""
    s = Settings(printer_access_code="x")
    assert s.auto_stop_timeout_seconds == 0


def test_db_path_default() -> None:
    s = Settings()
    assert s.db_path == "data/sentinel.db"


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
    from sentinel.network import resolve_and_validate_printer_ip

    monkeypatch.setattr("sentinel.network.socket.gethostbyname", lambda x: "192.168.1.50")
    s = Settings(printer_ip="printer.local")
    assert s.printer_ip == "printer.local"
    assert asyncio.run(resolve_and_validate_printer_ip(s.printer_ip)) == "192.168.1.50"


def test_printer_ip_validation_ssrf_disallowed() -> None:
    from sentinel.network import resolve_and_validate_printer_ip

    # Pure syntax is allowed by config
    s1 = Settings(printer_ip="127.0.0.1")
    assert s1.printer_ip == "127.0.0.1"

    # Resolver should block SSRF
    with pytest.raises(ValueError, match="SSRF Protection"):
        asyncio.run(resolve_and_validate_printer_ip("127.0.0.1"))
    with pytest.raises(ValueError, match="SSRF Protection"):
        asyncio.run(resolve_and_validate_printer_ip("::1"))
    with pytest.raises(ValueError, match="SSRF Protection"):
        asyncio.run(resolve_and_validate_printer_ip("localhost"))
    with pytest.raises(ValueError, match="SSRF Protection"):
        asyncio.run(resolve_and_validate_printer_ip("localhost.localdomain"))
    with pytest.raises(ValueError, match="SSRF Protection"):
        asyncio.run(resolve_and_validate_printer_ip("8.8.8.8"))
    with pytest.raises(ValueError, match="SSRF Protection"):
        asyncio.run(resolve_and_validate_printer_ip("169.254.169.254"))
    with pytest.raises(ValueError, match="SSRF Protection"):
        asyncio.run(resolve_and_validate_printer_ip("0.0.0.0"))
    with pytest.raises(ValueError, match="SSRF Protection"):
        asyncio.run(resolve_and_validate_printer_ip("::"))
    with pytest.raises(ValueError, match="SSRF Protection"):
        asyncio.run(resolve_and_validate_printer_ip("::ffff:127.0.0.1"))
    with pytest.raises(ValueError, match="SSRF Protection"):
        asyncio.run(resolve_and_validate_printer_ip("::ffff:8.8.8.8"))


def test_printer_ip_validation_invalid() -> None:
    with pytest.raises(ValueError, match="printer_ip must be a valid IP"):
        Settings(printer_ip="invalid_ip_or_hostname_!!")


def test_printer_ip_validation_hostname_resolves_to_loopback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sentinel.network import resolve_and_validate_printer_ip

    monkeypatch.setattr("sentinel.network.socket.gethostbyname", lambda x: "127.0.0.1")
    s = Settings(printer_ip="malicious.com")
    assert s.printer_ip == "malicious.com"
    with pytest.raises(ValueError, match="SSRF Protection"):
        asyncio.run(resolve_and_validate_printer_ip("malicious.com"))


def test_printer_ip_validation_hostname_resolution_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    import socket

    from sentinel.network import resolve_and_validate_printer_ip

    def mock_gethostbyname(x: str) -> str:
        raise socket.gaierror("not found")

    monkeypatch.setattr("sentinel.network.socket.gethostbyname", mock_gethostbyname)
    s = Settings(printer_ip="notfound.local")
    assert s.printer_ip == "notfound.local"
    with pytest.raises(ValueError, match="SSRF Protection: Cannot resolve hostname"):
        asyncio.run(resolve_and_validate_printer_ip("notfound.local"))


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


def test_log_format_default() -> None:
    s = Settings()
    assert s.log_format == "text"


def test_log_format_valid() -> None:
    s = Settings(log_format="json")
    assert s.log_format == "json"
    s = Settings(log_format="TEXT")
    assert s.log_format == "text"


def test_log_format_invalid() -> None:
    with pytest.raises(ValueError, match="LOG_FORMAT must be one of"):
        Settings(log_format="yaml")


def test_camera_max_streams_default() -> None:
    s = Settings()
    assert s.camera_max_streams == 3


def test_camera_max_streams_valid() -> None:
    s = Settings(camera_max_streams=1)
    assert s.camera_max_streams == 1
    s = Settings(camera_max_streams=10)
    assert s.camera_max_streams == 10


def test_camera_max_streams_invalid() -> None:
    with pytest.raises(ValueError, match="CAMERA_MAX_STREAMS must be at least 1"):
        Settings(camera_max_streams=0)


# ---------------------------------------------------------------------------
# README doc-consistency tests
# Parses the README config table and asserts documented defaults match
# Settings() defaults for keys that have been historically mis-documented.
# ---------------------------------------------------------------------------


def _parse_readme_defaults() -> dict[str, str]:
    """Parse all README config-table rows into {VAR_NAME: default_string}."""
    readme_path = Path(__file__).parent.parent / "README.md"
    defaults: dict[str, str] = {}
    for line in readme_path.read_text().splitlines():
        # Match table rows of the form: | `VAR_NAME` | default | ... |
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [c.strip() for c in stripped.split("|")]
        # cells[0] is empty (before the first |), cells[1]=var, cells[2]=default, ...
        if len(cells) < 4:
            continue
        var_cell = cells[1]
        default_cell = cells[2]
        # var_cell must be a backtick-quoted identifier
        if not (var_cell.startswith("`") and var_cell.endswith("`")):
            continue
        var_name = var_cell[1:-1]
        # Strip surrounding backticks from the default value if present
        default_val = default_cell.strip("`")
        defaults[var_name] = default_val
    return defaults


def test_readme_telegram_send_snapshots_default_matches_code() -> None:
    """README must document TELEGRAM_SEND_SNAPSHOTS default as false (code default)."""
    defaults = _parse_readme_defaults()
    assert "TELEGRAM_SEND_SNAPSHOTS" in defaults, (
        "TELEGRAM_SEND_SNAPSHOTS not found in README config table"
    )
    readme_val = defaults["TELEGRAM_SEND_SNAPSHOTS"].lower()
    assert readme_val == "false", (
        f"README documents TELEGRAM_SEND_SNAPSHOTS default as {readme_val!r} but "
        f"Settings().telegram_send_snapshots is False"
    )
    # Also confirm code default
    assert Settings().telegram_send_snapshots is False


def test_readme_ml_api_token_file_default_matches_code() -> None:
    """README must document ML_API_TOKEN_FILE default matching config.py."""
    defaults = _parse_readme_defaults()
    assert "ML_API_TOKEN_FILE" in defaults, "ML_API_TOKEN_FILE not found in README config table"
    readme_val = defaults["ML_API_TOKEN_FILE"]
    code_val = Settings().ml_api_token_file
    assert readme_val == code_val, (
        f"README documents ML_API_TOKEN_FILE default as {readme_val!r} but "
        f"Settings().ml_api_token_file is {code_val!r}"
    )


def test_readme_no_plaintext_auth_password_row() -> None:
    """README must not present AUTH_PASSWORD as a supported option (it's hard-rejected)."""
    defaults = _parse_readme_defaults()
    assert "AUTH_PASSWORD" not in defaults, (
        "README config table still has an AUTH_PASSWORD row — remove it, "
        "plaintext passwords are rejected at startup"
    )


# ---------------------------------------------------------------------------
# Double-construction / env-scrub safety tests
# ---------------------------------------------------------------------------


def test_double_construction_with_scrub_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Settings() must be constructable twice even after the first call scrubs env secrets.

    The PYTEST_CURRENT_TEST guard is cleared so the scrub path executes.
    _SECRET_STASH is reset before the test so state from other test runs does not
    interfere, and restored afterward to keep the test suite idempotent.
    """
    import os

    import sentinel.config as cfg

    # Save and clear the stash so this test owns the stash lifecycle.
    original_stash = dict(cfg._SECRET_STASH)
    cfg._SECRET_STASH.clear()

    try:
        # Remove the pytest guard so model_post_init actually scrubs.
        monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)

        secret_value = "test-access-code-99"
        monkeypatch.setenv("PRINTER_ACCESS_CODE", secret_value)

        # First construction — should succeed and scrub the env var.
        s1 = cfg.Settings()
        assert s1.printer_access_code.get_secret_value() == secret_value

        # After first construction the env var must be gone.
        assert "PRINTER_ACCESS_CODE" not in os.environ, (
            "PRINTER_ACCESS_CODE should have been scrubbed from os.environ"
        )

        # Second construction — must not raise, must return same secret value.
        s2 = cfg.Settings()
        assert s2.printer_access_code.get_secret_value() == secret_value, (
            "Second Settings() construction returned a different printer_access_code"
        )

        # Env var must still be absent after the second construction.
        assert "PRINTER_ACCESS_CODE" not in os.environ
    finally:
        # Restore stash so subsequent tests are unaffected.
        cfg._SECRET_STASH.clear()
        cfg._SECRET_STASH.update(original_stash)
