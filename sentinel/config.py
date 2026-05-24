"""Application configuration — loaded from environment variables.

Full implementation in ticket #2. This stub satisfies the __main__.py
import so `python -m sentinel --help` works from ticket #1 onward.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Printer
    printer_ip: str = "127.0.0.1"
    printer_access_code: str = "123456"
    printer_mqtt_port: int = 1883
    printer_mjpeg_port: int = 8080
    printer_mjpeg_path: str = "/mjpeg"

    # ML
    ml_api_url: str = "http://obico-ml:3333"
    ml_api_token_file: str = "/shared/token"
    ml_confirm_count: int = 3
    ml_poll_interval_seconds: int = 10
    ml_score_threshold: float = 0.4

    # Detection
    detection_warmup_seconds: int = 300
    detection_enabled_default: bool = True
    watcher_stall_seconds: int = 60

    # Telegram
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    telegram_user_ids: str | None = None

    # ntfy
    ntfy_url: str | None = None
    ntfy_token: str | None = None

    # Auth
    # Set either AUTH_PASSWORD_BCRYPT (a bcrypt hash starting with $2b$) or the
    # plain-text AUTH_PASSWORD — the latter is hashed at startup and then cleared
    # from memory so it never persists beyond process launch.
    auth_username: str | None = None
    auth_password_bcrypt: str | None = None
    auth_password: str | None = None

    # Web server
    bind_host: str = "0.0.0.0"
    bind_port: int = 8000
    external_bind_allowed: bool = False

    # Misc
    log_level: str = "INFO"
    db_path: str = "/data/sentinel.db"

    @property
    def telegram_enabled(self) -> bool:
        return self.telegram_bot_token is not None

    @property
    def ntfy_enabled(self) -> bool:
        return self.ntfy_url is not None

    @property
    def auth_enabled(self) -> bool:
        return bool(self.auth_username)

    @model_validator(mode="after")
    def _hash_plain_password(self) -> Settings:
        """Hash AUTH_PASSWORD into AUTH_PASSWORD_BCRYPT at startup, then clear it."""
        if self.auth_password and not self.auth_password_bcrypt:
            import bcrypt  # lazy import — only needed when plain password is provided

            self.auth_password_bcrypt = bcrypt.hashpw(
                self.auth_password.encode(), bcrypt.gensalt()
            ).decode()
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


@lru_cache
def get_settings() -> Settings:
    return Settings()
