from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from scraper_client.core.runtime import get_runtime_dir


def get_resolved_env_file() -> Path:
    runtime_dir = get_runtime_dir()

    explicit_env_file = os.getenv("SCRAPER_ENV_FILE", "").strip()
    if explicit_env_file:
        explicit_path = Path(explicit_env_file)
        if not explicit_path.is_absolute():
            explicit_path = runtime_dir / explicit_path
        return explicit_path

    package_env_marker = runtime_dir / ".package-env"
    if not package_env_marker.exists():
        package_env_marker = runtime_dir / "package-env"
    if package_env_marker.exists():
        package_env = package_env_marker.read_text(encoding="utf-8").strip().lower()
        if package_env == "test":
            for marker_name in (".env.test", "env.test"):
                marker_env = runtime_dir / marker_name
                if marker_env.exists():
                    return marker_env
        if package_env == "prod":
            for marker_name in (".env.prod", "env.prod"):
                marker_env = runtime_dir / marker_name
                if marker_env.exists():
                    return marker_env

    # Fallback for copied/moved package folders where .package-env may be missing.
    for candidate in (".env", "env", ".env.test", "env.test", ".env.prod", "env.prod"):
        candidate_path = runtime_dir / candidate
        if candidate_path.exists():
            return candidate_path

    return runtime_dir / ".env"


def _resolve_env_file() -> str:
    return str(get_resolved_env_file())


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_resolve_env_file(),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    log_level: str = Field(default="INFO")

    scraper_server_base_url: str = Field(default="http://127.0.0.1:8000/api/v1")
    scraper_frontend_base_url: str = Field(default="")
    scraper_internal_api_key: str = Field(default="change-me-scraper-key")
    scraper_client_id: str = Field(default="scraper-client-local")
    scraper_verify_ssl: bool = Field(default=True)

    playwright_cdp_url: str = Field(default="http://127.0.0.1:9222")
    playwright_timeout_ms: int = Field(default=20000)
    force_relogin_each_run: bool = Field(default=False)

    poll_interval_seconds: int = Field(default=30, ge=1)
    empty_queue_backoff_seconds: int = Field(default=30, ge=1)
    max_retry_attempts: int = Field(default=20, ge=1)
    retry_backoff_max_seconds: int = Field(default=300, ge=1)

    # SMTP email notification settings (optional, leave empty to disable)
    smtp_host: str = Field(default="")
    smtp_port: int = Field(default=465)
    smtp_username: str = Field(default="")
    smtp_password: str = Field(default="")
    smtp_from: str = Field(default="")
    smtp_to: str = Field(default="")
    captcha_alert_cooldown_seconds: int = Field(default=600, ge=0)

    verification_poll_interval_seconds: float = Field(default=3.0, ge=0.5)
    verification_max_wait_seconds: int = Field(default=180, ge=10)
    sms_flow_cooldown_seconds: int = Field(default=90, ge=0)
    sms_flow_max_duration_seconds: int = Field(default=600, ge=60)

    def validate(self) -> None:
        if self.retry_backoff_max_seconds < self.empty_queue_backoff_seconds:
            raise ValueError("retry_backoff_max_seconds must be >= empty_queue_backoff_seconds")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
