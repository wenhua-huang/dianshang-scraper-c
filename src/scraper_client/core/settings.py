from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _resolve_env_file() -> str:
    explicit_env_file = os.getenv("SCRAPER_ENV_FILE", "").strip()
    if explicit_env_file:
        return explicit_env_file

    package_env_marker = Path(".package-env")
    if package_env_marker.exists():
        package_env = package_env_marker.read_text(encoding="utf-8").strip().lower()
        if package_env == "test":
            return ".env.test"
        if package_env == "prod":
            return ".env.prod"

    return ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_resolve_env_file(),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    log_level: str = Field(default="INFO")

    scraper_server_base_url: str = Field(default="http://127.0.0.1:8000/api/v1")
    scraper_internal_api_key: str = Field(default="change-me-scraper-key")
    scraper_client_id: str = Field(default="scraper-client-local")

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
    captcha_alert_cooldown_seconds: int = Field(default=600, ge=0)

    verification_poll_interval_seconds: float = Field(default=3.0, ge=0.5)
    verification_max_wait_seconds: int = Field(default=180, ge=10)

    def validate(self) -> None:
        if self.retry_backoff_max_seconds < self.empty_queue_backoff_seconds:
            raise ValueError("retry_backoff_max_seconds must be >= empty_queue_backoff_seconds")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
