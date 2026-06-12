"""Application configuration — loaded from environment variables.

Full implementation in ticket #2. This stub satisfies the __main__.py
import so `python -m sentinel --help` works from ticket #1 onward.
"""

from __future__ import annotations

from functools import lru_cache
from typing import TYPE_CHECKING, Any

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

if TYPE_CHECKING:
    from pydantic.fields import FieldInfo

# ---------------------------------------------------------------------------
# Secret stash — populated by model_post_init on the first Settings()
# construction that exercises the env-scrub path (i.e. outside pytest).
#
# Why: model_post_init removes plaintext secrets from os.environ to keep
# them out of /proc/<pid>/environ.  Without the stash, any second Settings()
# construction (e.g. after get_settings.cache_clear()) would fail because
# the required env vars are gone.
#
# The stash maps env-var name (upper-case) → raw string value so that
# _StashSource can re-supply the values to pydantic-settings on subsequent
# constructions.
#
# Note: secrets loaded from the .env file are NOT scrubbed today (pydantic-
# settings reads .env independently of os.environ), so this asymmetry only
# affects values that were originally in os.environ.
# ---------------------------------------------------------------------------
_SECRET_STASH: dict[str, str] = {}

# Env-var names that are scrubbed and therefore need to be stashed.
_SCRUBBED_ENV_KEYS: tuple[str, ...] = (
    "PRINTER_ACCESS_CODE",
    "TELEGRAM_BOT_TOKEN",
    "NTFY_TOKEN",
    "AUTH_PASSWORD",
    "AUTH_PASSWORD_BCRYPT",
)


class _StashSource(PydanticBaseSettingsSource):
    """Settings source that reads from the module-level ``_SECRET_STASH``.

    This source is appended *after* all normal env / .env sources so that live
    env values still take priority.  Its sole job is to re-supply secrets that
    were previously scrubbed from os.environ by :meth:`Settings.model_post_init`.
    """

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:
        env_key = field_name.upper()
        value = _SECRET_STASH.get(env_key)
        return value, field_name, False

    def __call__(self) -> dict[str, Any]:
        data: dict[str, Any] = {}
        for field_name in self.settings_cls.model_fields:
            env_key = field_name.upper()
            if env_key in _SECRET_STASH:
                data[field_name] = _SECRET_STASH[env_key]
        return data


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Append _StashSource so previously-scrubbed secrets are still available."""
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            file_secret_settings,
            _StashSource(settings_cls),
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
    auto_stop_timeout_seconds: int = 0
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
    # Provide the bcrypt hash of the dashboard password one of two ways:
    #   - AUTH_PASSWORD_BCRYPT      — the hash inline. In a Docker Compose .env
    #                                 file you must escape every `$` as `$$`.
    #   - AUTH_PASSWORD_BCRYPT_FILE — path to a file containing the hash. Preferred:
    #                                 no escaping, and the hash never appears in
    #                                 `docker inspect` / `/proc/<pid>/environ`.
    #                                 Takes precedence over the inline value.
    # Generate a hash with:  python -m sentinel hash-password
    # WARNING: Plain-text AUTH_PASSWORD is not accepted (it would be visible via
    # `docker inspect`); the validator rejects it.
    auth_username: str | None = None
    auth_password_bcrypt: str | None = None
    auth_password_bcrypt_file: str | None = None
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

        # 2. Auth — resolve the bcrypt hash from a file if configured.  The file
        # path takes precedence over an inline AUTH_PASSWORD_BCRYPT.  Reading from
        # a file sidesteps Docker Compose's `$`-interpolation of `.env` values,
        # which silently corrupts an inline hash.
        if self.auth_password_bcrypt_file:
            self.auth_password_bcrypt = self._read_bcrypt_file(self.auth_password_bcrypt_file)

        if self.auth_username:
            if not self.auth_password_bcrypt and not self.auth_password:
                raise ValueError(
                    "AUTH_PASSWORD_BCRYPT or AUTH_PASSWORD_BCRYPT_FILE is required "
                    "when AUTH_USERNAME is set. Generate a hash with: "
                    "python -m sentinel hash-password"
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

    @staticmethod
    def _read_bcrypt_file(path_str: str) -> str:
        """Read and return the stripped bcrypt hash from ``path_str``.

        Raises ValueError with an actionable message if the file is missing,
        unreadable, or empty.  Hash-format validation (``$2a$``/``$2b$``/``$2y$``)
        is handled by the caller alongside the inline-hash path.
        """
        from pathlib import Path

        path = Path(path_str)
        try:
            content = path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            msg = f"AUTH_PASSWORD_BCRYPT_FILE points to {path_str!r}, which does not exist"
            raise ValueError(msg) from exc
        except OSError as exc:
            msg = f"AUTH_PASSWORD_BCRYPT_FILE {path_str!r} could not be read: {exc}"
            raise ValueError(msg) from exc
        hash_value = content.strip()
        if not hash_value:
            msg = f"AUTH_PASSWORD_BCRYPT_FILE {path_str!r} is empty"
            raise ValueError(msg)
        return hash_value

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

        for key in _SCRUBBED_ENV_KEYS:
            if key in os.environ:
                # Stash before scrubbing so that a second Settings() construction
                # can recover the value via _StashSource even after env is cleared.
                if key not in _SECRET_STASH:
                    _SECRET_STASH[key] = os.environ[key]
                os.environ[key] = "********"
                os.environ.pop(key, None)


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
