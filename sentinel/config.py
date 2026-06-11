"""Application configuration — loaded from environment variables.

Full implementation in ticket #2. This stub satisfies the __main__.py
import so `python -m sentinel --help` works from ticket #1 onward.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Printer
    printer_ip: str = "192.168.1.10"
    printer_access_code: SecretStr
    printer_mqtt_port: int = 1883
    printer_mjpeg_port: int = 8080
    printer_mjpeg_path: str = "/mjpeg"

    # ML
    # WARNING: When ml_api_token_file is used, transmitting bearer tokens
    # over cleartext HTTP exposes them to network interception. Always
    # use HTTPS for external ML endpoints.
    ml_api_url: str = "http://obico-ml:3333"
    ml_api_token_file: str = "shared/token"
    ml_confirm_count: int = 3
    ml_consecutive_failure_threshold: int = 10
    ml_poll_interval_seconds: int = 10
    ml_score_threshold: float = 0.4
    ml_callback_host: str | None = None

    # Detection
    detection_warmup_seconds: int = 300
    detection_enabled_default: bool = True
    watcher_stall_seconds: int = 60
    auto_stop_timeout_seconds: int = 1800
    snapshot_cleanup_interval_seconds: int = 3600
    snapshot_retention_limit: int = 50
    event_retention_days: int = 0  # 0 = unlimited; otherwise prune rows older than this
    resume_cooldown_seconds: int = 5

    # Notifications
    notify_on_print_start: bool = False
    notify_on_print_completed: bool = True
    notify_on_print_paused: bool = True

    # Telegram
    telegram_bot_token: SecretStr | None = None
    telegram_chat_id: str | None = None
    telegram_user_ids: str | None = None
    telegram_send_snapshots: bool = False

    # ntfy
    ntfy_url: str | None = None
    ntfy_token: SecretStr | None = None
    ntfy_send_snapshots: bool = False

    # Auth
    # Set either AUTH_PASSWORD_BCRYPT (a bcrypt hash starting with $2b$) or the
    # plain-text AUTH_PASSWORD.
    # WARNING: Plain-text AUTH_PASSWORD in environment variables is visible via
    # `docker inspect` and `/proc/<pid>/environ`. For production, always use
    # AUTH_PASSWORD_BCRYPT.
    auth_username: str | None = None
    auth_password_bcrypt: str | None = None
    auth_password: SecretStr | None = None
    auth_cookie_secure: str = "auto"

    # Web server
    bind_host: str = "127.0.0.1"
    bind_port: int = 8000
    external_bind_allowed: bool = False
    trust_proxies: bool = False

    # Misc
    log_level: str = "INFO"
    log_format: str = "text"
    camera_max_streams: int = 3
    db_path: str = "data/sentinel.db"

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_bot_token)

    @property
    def ntfy_enabled(self) -> bool:
        return bool(self.ntfy_url)

    @property
    def auth_enabled(self) -> bool:
        return bool(self.auth_username)

    @model_validator(mode="after")
    def _validate_settings(self) -> Settings:
        """Validate config variables and hash plain AUTH_PASSWORD if provided."""

        # 1. Telegram checks
        if self.telegram_bot_token:
            if not self.telegram_chat_id or not self.telegram_chat_id.strip():
                raise ValueError("TELEGRAM_CHAT_ID is required when TELEGRAM_BOT_TOKEN is set")
            if not self.telegram_user_ids or not self.telegram_user_ids.strip():
                raise ValueError("TELEGRAM_USER_IDS is required when TELEGRAM_BOT_TOKEN is set")

        # 2. Auth checks
        if self.auth_username:
            if not self.auth_password_bcrypt and not self.auth_password:
                raise ValueError(
                    "AUTH_PASSWORD or AUTH_PASSWORD_BCRYPT is required when AUTH_USERNAME is set"
                )
            if self.auth_password_bcrypt and not (
                self.auth_password_bcrypt.startswith("$2a$")
                or self.auth_password_bcrypt.startswith("$2b$")
                or self.auth_password_bcrypt.startswith("$2y$")
            ):
                msg = (
                    "AUTH_PASSWORD_BCRYPT must be a valid bcrypt hash starting with "
                    "$2a$, $2b$, or $2y$"
                )
                raise ValueError(msg)

        # 3. Hash plain password
        if self.auth_password and not self.auth_password_bcrypt:
            raise ValueError(
                "Plain-text AUTH_PASSWORD is no longer supported for security reasons. "
                "Please generate a bcrypt hash and use AUTH_PASSWORD_BCRYPT instead."
            )
        self.auth_password = None
        return self

    @field_validator("log_level")
    @classmethod
    def _validate_log_level(cls, v: str) -> str:
        valid = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        if v.upper() not in valid:
            msg = f"LOG_LEVEL must be one of {valid}"
            raise ValueError(msg)
        return v.upper()

    @field_validator("log_format")
    @classmethod
    def _validate_log_format(cls, v: str) -> str:
        valid = {"text", "json"}
        if v.lower() not in valid:
            msg = f"LOG_FORMAT must be one of {valid}"
            raise ValueError(msg)
        return v.lower()

    @field_validator("camera_max_streams")
    @classmethod
    def _validate_camera_max_streams(cls, v: int) -> int:
        if v < 1:
            msg = "CAMERA_MAX_STREAMS must be at least 1"
            raise ValueError(msg)
        return v

    @field_validator("ml_score_threshold")
    @classmethod
    def _validate_ml_score_threshold(cls, v: float) -> float:
        if not 0.0 <= v <= 1.0:
            msg = "ML_SCORE_THRESHOLD must be between 0.0 and 1.0"
            raise ValueError(msg)
        return v

    @field_validator("ml_confirm_count")
    @classmethod
    def _validate_ml_confirm_count(cls, v: int) -> int:
        if v < 1:
            msg = "ML_CONFIRM_COUNT must be at least 1"
            raise ValueError(msg)
        return v

    @field_validator("ml_consecutive_failure_threshold")
    @classmethod
    def _validate_ml_consecutive_failure_threshold(cls, v: int) -> int:
        if v < 1:
            msg = "ML_CONSECUTIVE_FAILURE_THRESHOLD must be at least 1"
            raise ValueError(msg)
        return v

    @field_validator("ml_poll_interval_seconds")
    @classmethod
    def _validate_ml_poll_interval(cls, v: int) -> int:
        if v < 1:
            msg = "ML_POLL_INTERVAL_SECONDS must be at least 1 second"
            raise ValueError(msg)
        return v

    @field_validator("snapshot_cleanup_interval_seconds")
    @classmethod
    def _validate_snapshot_cleanup_interval(cls, v: int) -> int:
        if v < 1:
            msg = "SNAPSHOT_CLEANUP_INTERVAL_SECONDS must be at least 1 second"
            raise ValueError(msg)
        return v

    @field_validator("snapshot_retention_limit")
    @classmethod
    def _validate_snapshot_retention_limit(cls, v: int) -> int:
        if v < 1:
            msg = "SNAPSHOT_RETENTION_LIMIT must be at least 1"
            raise ValueError(msg)
        return v

    @field_validator("resume_cooldown_seconds")
    @classmethod
    def _validate_resume_cooldown_seconds(cls, v: int) -> int:
        if v < 0:
            msg = "RESUME_COOLDOWN_SECONDS must be at least 0"
            raise ValueError(msg)
        return v

    @field_validator("auto_stop_timeout_seconds")
    @classmethod
    def _validate_auto_stop_timeout(cls, v: int) -> int:
        if v != 0 and v < 60:
            msg = "AUTO_STOP_TIMEOUT_SECONDS must be 0 (disabled) or at least 60 seconds"
            raise ValueError(msg)
        return v

    @field_validator("event_retention_days")
    @classmethod
    def _validate_event_retention_days(cls, v: int) -> int:
        if v < 0:
            msg = "EVENT_RETENTION_DAYS must be 0 (unlimited) or a positive number of days"
            raise ValueError(msg)
        return v

    @field_validator("printer_ip")
    @classmethod
    def _validate_printer_ip(cls, v: str) -> str:
        from sentinel.network import validate_printer_ip

        return validate_printer_ip(v)

    @field_validator("auth_cookie_secure")
    @classmethod
    def _validate_auth_cookie_secure(cls, v: str) -> str:
        valid = {"auto", "always", "never"}
        if v.lower() not in valid:
            msg = f"AUTH_COOKIE_SECURE must be one of {valid}"
            raise ValueError(msg)
        return v.lower()

    def model_post_init(self, __context: Any) -> None:
        import os

        if "PYTEST_CURRENT_TEST" in os.environ:
            return

        secrets = [
            "PRINTER_ACCESS_CODE",
            "TELEGRAM_BOT_TOKEN",
            "NTFY_TOKEN",
            "AUTH_PASSWORD",
            "AUTH_PASSWORD_BCRYPT",
        ]
        for key in secrets:
            if key in os.environ:
                os.environ[key] = "********"
                os.environ.pop(key, None)


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
