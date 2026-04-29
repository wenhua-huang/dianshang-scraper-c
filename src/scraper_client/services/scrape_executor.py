from __future__ import annotations

import logging
from typing import Any, Callable

from scraper_client.core.settings import Settings
from scraper_client.infra.jd.jd_scraper import JDScraper

logger = logging.getLogger(__name__)


class UnsupportedPlatformError(RuntimeError):
    """Raised when an account platform is not implemented by scraper-c."""


class ScrapeExecutor:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._jd_scraper = JDScraper(settings)

    def execute_account(
        self,
        account: dict[str, Any],
        platform: str,
        scrape_config: dict[str, Any] | None = None,
        on_batch_ready: Callable[[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]], None]
        | None = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        if platform == "JINGDONG":
            return self._jd_scraper.scrape(account, scrape_config, on_batch_ready=on_batch_ready)

        raise UnsupportedPlatformError(f"unsupported platform: {platform}")

    def execute_aftersales(
        self,
        account: dict[str, Any],
        platform: str,
        scrape_config: dict[str, Any] | None = None,
        on_batch_ready: Callable[[list[dict[str, Any]]], None] | None = None,
    ) -> list[dict[str, Any]]:
        if platform == "JINGDONG":
            return self._jd_scraper.scrape_aftersales(
                account,
                scrape_config,
                on_batch_ready=on_batch_ready,
            )

        raise UnsupportedPlatformError(f"unsupported platform: {platform}")
