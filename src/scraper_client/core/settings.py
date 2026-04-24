from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    log_level: str = Field(default="INFO")

    scraper_server_base_url: str = Field(default="http://127.0.0.1:8000/api/v1")
    scraper_internal_api_key: str = Field(default="change-me-scraper-key")
    scraper_client_id: str = Field(default="scraper-client-local")

    playwright_cdp_url: str = Field(default="http://127.0.0.1:9222")
    playwright_timeout_ms: int = Field(default=20000)

    poll_interval_seconds: int = Field(default=30, ge=1)
    empty_queue_backoff_seconds: int = Field(default=30, ge=1)
    max_retry_attempts: int = Field(default=20, ge=1)
    retry_backoff_max_seconds: int = Field(default=300, ge=1)

    def validate(self) -> None:
        if self.retry_backoff_max_seconds < self.empty_queue_backoff_seconds:
            raise ValueError("retry_backoff_max_seconds must be >= empty_queue_backoff_seconds")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
